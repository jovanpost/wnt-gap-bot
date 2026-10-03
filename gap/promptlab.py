"""Prompt lab (v1.12.0): prompts that improve from data. PAPER ONLY, never traded.

Nothing in this file can place, change or cancel an order, and no trading code reads its tables.

The loop, once per night after Kalshi settles:
  1. CHAMPION. One prompt is the champion. It forecasts every night on the frozen file, before the show.
  2. WRITER. A writer model (Gemini Pro if the key can use it, else Flash) sees the champion prompt,
     tonight's frozen file, tonight's results and the champion's own reasoning. It proposes
     LAB_VARIANTS_PER_NIGHT small edits. A program -- not the model -- applies and checks every edit:
       * one edit = ONE tagged line of the prompt replaced, inserted after, or deleted;
       * only lines in the editable sections can be touched (rules, schema and format are locked);
       * the new text may not name tonight's words, any name from tonight's file, any new proper
         noun, a date or a weekday, and may not mention searching, tools, prices or odds.
     A variant that breaks a rule is thrown away and the reason is kept.
  3. SCREEN. Each valid variant is replayed on TONIGHT's frozen file. Only the best one, and only if
     it beats the champion on tonight, goes on. This is a filter, not proof: the writer saw the answers.
  4. TEST. That variant is replayed on the OTHER frozen nights (which the writer did not see).
  5. CHAMPION RULE. A prompt becomes champion when, on nights other than the one it was written from,
     it has at least LAB_MIN_HELDOUT_WORDS words, beats the champion's Brier by LAB_MARGIN, and is
     better or equal on at least half of those nights. The best LAB_TOP_N others stay on the board
     and are replayed on each new night, so their evidence keeps growing.

v1.13.0: EVERY model with an API key takes part (Grok, Gemini, and each service in CHALLENGERS: DeepSeek,
Kimi, GLM, Nemotron, Mistral, OpenRouter ...). When tonight's file goes out the champion prompt runs on
all of them; at night every new variant is tried on all of them; every forecast is kept. One model (the
first in LAB_MODELS that has a key) is the JUDGE that decides which prompt becomes champion; the board
shows every model so the judge can be changed with data.
Only models without search take part: a search model cannot be replayed on a past night.
Money guard: paid lab runs stop at LAB_DAILY_BUDGET_USD per CT day; the rest wait for tomorrow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy import text

from . import clock, config as C, netlimit, parser, store, xai

log = logging.getLogger("gap.promptlab")

SEED_FILE = Path(__file__).resolve().parent.parent / "prompts" / "lab_seed_prompt.txt"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
EDITABLE = ("HOW TO READ THE FILE", "HOW THE SHOW WORKS", "RUNDOWN COMPETITION", "HOW TO FORECAST",
            "EXACT WORDS", "CALIBRATION DISCIPLINE")
LOCKED_MARKS = ("Blind:", "Proof in the output")     # the reasoning-proof rule stays as it is
PRIORITY = {"live": 0, "fill": 1, "test": 2, "screen": 3}    # tests finish before new screens start: nothing starves
NEWS_MARK = "ABC NEWS FEEDS"
DEFAULT_RUN_COST = 0.10            # used for the budget check until real costs are recorded

_running = threading.Lock()
_budget_lock = threading.Lock()
_reserved = {"usd": 0.0}
_cool: dict[str, tuple[float, int]] = {}      # model key -> (pause until, level): one busy answer pauses that service
COOL_S = (300, 900, 3600)


_gem = {"last": 0.0, "day": "", "calls": 0}
_slots: dict[str, dict] = {"gemini": _gem}
_slot_locks: dict[str, threading.Lock] = {}
_gem_lock = threading.Lock()


def _slot(key: str) -> bool:
    """One lab forecast on a FREE model may start now? Keeps a gap between calls and a daily count per
    model (in memory, and from today's finished runs after a restart). Free plans are small: Gemini allows
    20 calls a day per model, OpenRouter 50. Grok is paid and is limited by dollars instead."""
    if key == "xai":
        return True
    cap, gap = ((C.LAB_GEMINI_DAILY_CALLS, C.LAB_GEMINI_MIN_GAP_S) if key == "gemini"
                else (C.LAB_FREE_DAILY_CALLS, C.LAB_FREE_MIN_GAP_S))
    with _gem_lock:
        st = _slots.setdefault(key, {"last": 0.0, "day": "", "calls": 0})
        lock = _slot_locks.setdefault(key, threading.Lock())
    with lock:
        today = clock.today_ct()
        if st["day"] != today:
            done = _q("select count(*) as n from gap_lab_runs where model_key = :k and finished_at >= :t",
                      k=key, t=clock._at(today, "00:00"))[0]["n"]
            st.update(day=today, calls=int(done))
        if st["calls"] >= cap:
            return False
        wait = gap - (time.monotonic() - st["last"])
        if wait > 0:
            time.sleep(wait)
        st["last"] = time.monotonic()
        st["calls"] += 1
        return True


def _gemini_slot() -> bool:
    return _slot("gemini")


def _cooling(key: str) -> bool:
    until, _lvl = _cool.get(key, (0.0, 0))
    return time.monotonic() < until


def _cool_down(key: str) -> None:
    _until, lvl = _cool.get(key, (0.0, 0))
    _cool[key] = (time.monotonic() + COOL_S[min(lvl, len(COOL_S) - 1)], lvl + 1)


class Retry(Exception):
    """Try this run again later (busy service, timeout, broken answer)."""

    def __init__(self, msg: str, usage: dict | None = None, free: bool = False):
        super().__init__(msg)
        self.usage = usage
        self.free = free           # True = nothing was billed (rate limit, timeout before an answer)


class Fatal(Exception):
    """Retrying will not help (key refused, bad request)."""


# ------------------------------------------------------------------ small database helpers

def _q(sql: str, **p) -> list[dict]:
    with store.engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), p).mappings().all()]


def _x(sql: str, **p) -> int:
    with store.engine().begin() as conn:
        return conn.execute(text(sql), p).rowcount or 0


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _d(v) -> str:
    return str(v)[:10]


def enabled() -> bool:
    return bool(C.LAB_ON) and store.using_postgres()


def model_keys() -> list[str]:
    """Lab forecasters that have a key, in LAB_MODELS order. The first one is the judge.
    "xai" and "gemini" are themselves; "all" stands for every service listed in CHALLENGERS that has a
    key (the key is then "provider:model word", e.g. "nvidia:deepseek"); one such spec may also be listed."""
    from . import challengers as CH
    out: list[str] = []
    for k in C.LAB_MODELS:
        if k == "xai":
            got = ["xai"] if C.XAI_API_KEY else []
        elif k == "gemini":
            got = ["gemini"] if C.GEMINI_API_KEY else []
        elif k in ("all", "challengers"):
            got = [f"{prov}:{m}" for prov, m in CH.enabled_specs()]
        elif ":" in k and k.split(":", 1)[0] in CH.PROVIDERS and CH.key_for(k.split(":", 1)[0]):
            got = [k]
        else:
            got = []
        out += [g for g in got if g not in out]
    return out


def short(key: str) -> str:
    """Column name for a lab model key: xai -> grok, nvidia:deepseek -> deepseek, mistral:mistral-medium -> mistral."""
    if key == "xai":
        return "grok"
    if key == "gemini":
        return "gemini"
    prov, _, m = key.partition(":")
    if prov in ("openrouter", "mistral", "cerebras", "groq"):
        return prov
    head = re.split(r"[-_.:\d]", m.split("/")[-1])[0]
    return (head or prov)[:10]


def primary_key() -> str | None:
    keys = model_keys()
    return keys[0] if keys else None


# ------------------------------------------------------------------ prompts: ids, units, edits

def prompt_id(text_: str) -> str:
    return "lab-" + hashlib.sha1(text_.encode("utf-8")).hexdigest()[:7]


_HDR = re.compile(r"^##\s+(.*)$")
_PREFIX = re.compile(r"^(\s*(?:\d+[a-z]?\.|[a-z]\.|-|•)\s+)")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")


def section_name(header: str) -> str:
    return re.split(r"\s*\(", header.strip(), maxsplit=1)[0].strip().upper()


def units(prompt_text: str) -> list[dict]:
    """Every editable line of a prompt: [{"id": "U7", "line": index, "section": name, "text": line}].
    Only non-empty lines inside the EDITABLE sections get an id; everything else is locked."""
    out, sec, n = [], "HEADER", 0
    for i, ln in enumerate(prompt_text.split("\n")):
        m = _HDR.match(ln)
        if m:
            sec = section_name(m.group(1))
            continue
        if sec in EDITABLE and ln.strip() and not any(m in ln for m in LOCKED_MARKS):
            n += 1
            out.append({"id": f"U{n}", "line": i, "section": sec, "text": ln})
    return out


def tagged(prompt_text: str) -> str:
    """The prompt as the writer sees it: editable lines start with their [U#] tag."""
    lines = prompt_text.split("\n")
    for u in units(prompt_text):
        lines[u["line"]] = f"[{u['id']}] {lines[u['line']]}"
    return "\n".join(lines)


_BANNED = re.compile(
    r"\b(search(?:es|ed|ing)?|brows(?:e|es|ed|ing|er)|look(?:s|ed|ing)? up|web|internet|online|tools?|"
    r"prices?|priced|pricing|odds|kalshi|traders?|trading|trades?|bets?|betting|markets?|contracts?|cents?|"
    r"costs?|dollars?|money|profits?)\b", re.I)
# the output format, the 1-99 range and the 'Blind:' proof rule are not the writer's to change
_FORMAT = re.compile(
    r"\b(schema|json|outputs?|formats?|prose|blind|fields?|integers?|omit(?:s|ted|ting)?|leaves? out|"
    r"skip(?:s|ped|ping)? (?:a|any|the|some|that|those) words?)\b|\b(?:0|100)\s*(?:%|percent)|\bprobability of (?:0|100)\b", re.I)
_DATES = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)
_POSS = re.compile("(?:'|\u2019)s$")


def _bare(tok: str) -> str:
    return _POSS.sub("", tok)


_SEG = re.compile("[.!?:;|()\\[\\]\"\u201c\u201d\u2014\u2013\u2022]| - ")


def night_names(parent: str, night_text: str) -> set[str]:
    """Lower-cased NAMES in tonight's file: a word written with a capital in the MIDDLE of an ordinary
    sentence-case line, never seen in lower case, 4+ letters, and not a word the prompt already uses.
    Title Case headlines and the first word of a headline are left out, because there every ordinary
    word has a capital too ("Family", "Lawsuit") and banning those would throw away good general edits.
    (A name written with its capital is still caught by the checks in _leak wherever it stands. Known gap:
    a name that the file shows ONLY in Title Case or as a headline's first word, written by the writer in
    lower case, passes. The held-out test nights are the real guard against a prompt fitted to one night.)"""
    vocab = {_bare(m.group(0)).lower() for m in _TOKEN.finditer(parent)}
    lower = {_bare(m.group(0)).lower() for m in _TOKEN.finditer(night_text or "") if m.group(0)[0].islower()}
    names: set[str] = set()
    for line in (night_text or "").split("\n"):
        toks = [m.group(0) for m in _TOKEN.finditer(line)]
        longs = [t for t in toks[1:] if len(t) >= 4]
        if longs and sum(1 for t in longs if t[0].isupper()) / len(longs) >= 0.6:
            continue                                        # a Title Case line: capitals mean nothing here
        for seg in _SEG.split(line):
            seg_toks = [_bare(m.group(0)) for m in _TOKEN.finditer(seg)]
            names |= {t.lower() for t in seg_toks[1:] if t[0].isupper()}
    return {t for t in names - lower - vocab if len(t) >= 4}


def night_terms(words: list[dict]) -> list[str]:
    from . import headlines
    out = []
    for w in words:
        for t in headlines.search_terms(w["word"]):
            if t and t not in out:
                out.append(t)
    return out


def _leak(new_text: str, parent: str, words: list[dict], night_text: str) -> str | None:
    """Why this text is night-specific, or None. Strict on purpose: a false rejection costs one
    variant; a leaked name would let the writer fit tonight's answers."""
    from . import abcfeeds
    for t in night_terms(words):
        if abcfeeds.term_pattern(t).search(new_text):
            return f"names a word from tonight's list ({t})"
    if _DATES.search(new_text):
        return "contains a date or a weekday"
    vocab = {_bare(m.group(0)).lower() for m in _TOKEN.finditer(parent)}
    names = night_names(parent, night_text)
    for m in _TOKEN.finditer(new_text):
        tok = _bare(m.group(0))
        if tok.lower() in names:                            # any spelling: Powell, powell, POWELL, Powell's
            return f"uses a name from tonight's file ({tok})"
        if not tok[0].isupper() or tok.lower() in vocab:
            continue
        if len(tok) < 2 or (len(tok) < 3 and not tok.isupper()):
            continue
        before = new_text[:m.start()].rstrip(" \"'(\u201c\u2018")
        initial = (not before) or before[-1] in ".!?:"
        in_file = re.search(r"(?<![A-Za-z])" + re.escape(tok) + r"(?![A-Za-z])", night_text or "")
        plain = initial and re.search(r"(?<![A-Za-z])" + re.escape(tok.lower()) + r"(?![A-Za-z])", night_text or "")
        if in_file and not plain:                           # a capitalised word the file has and the prompt does not
            return f"uses a name from tonight's file ({tok})"
        if tok.isupper() and len(tok) >= 3:
            return f"introduces a new proper noun ({tok})"   # an acronym the prompt never used
        if not initial:
            return f"introduces a new proper noun ({tok})"
    return None


def apply_edit(parent: str, edit: dict, words: list[dict], night_text: str) -> tuple[str | None, str, dict]:
    """Apply ONE writer edit to the parent prompt. Returns (new prompt or None, reason, meta).
    The program does the editing; the model only says which tagged line and what the new text is."""
    meta = {"name": str(edit.get("name") or "")[:60], "why": str(edit.get("why") or "")[:400]}
    action = str(edit.get("action") or "").strip().lower()
    if action not in ("replace", "insert_after", "delete"):
        return None, f"unknown action {action!r}", meta
    us = {u["id"]: u for u in units(parent)}
    uid = str(edit.get("unit") or "").strip().upper().strip("[]")
    u = us.get(uid)
    if not u:
        return None, f"line tag {uid or '(none)'} is not an editable line", meta
    meta.update(section=u["section"], action=action, unit_before=u["text"])
    if _leak(meta["why"], parent, words, night_text):
        meta["why"] = "(removed: the writer's reason named tonight's news)"
    new = str(edit.get("text") or "")
    lines = parent.split("\n")
    if action == "delete":
        if new.strip():
            return None, "a delete must have empty text", meta
        if sum(1 for x in us.values() if x["section"] == u["section"]) <= 3:
            return None, "that section would be left too thin", meta
        del lines[u["line"]]
        meta["unit_after"] = ""
    else:
        new = new.strip()
        if "\n" in new or "\r" in new:
            return None, "the new text must be one line", meta
        if not (C.LAB_EDIT_MIN_TEXT <= len(new) <= C.LAB_EDIT_MAX_TEXT):
            return None, f"the new text must be {C.LAB_EDIT_MIN_TEXT}-{C.LAB_EDIT_MAX_TEXT} characters (got {len(new)})", meta
        if re.search(r"##|[{}`\[\]<>]", new):
            return None, "headings, braces, brackets and code marks are not allowed", meta
        bad = _BANNED.search(new)
        if bad:
            return None, f"mentions searching, tools, prices or trading ({bad.group(0)})", meta
        bad = _FORMAT.search(new)
        if bad:
            return None, f"touches the answer format, the 1-99 range or the proof rule ({bad.group(0)})", meta
        why_leak = _leak(new, parent, words, night_text)
        if why_leak:
            return None, why_leak, meta
        pm = _PREFIX.match(u["text"])
        prefix = pm.group(1) if pm else ""
        body = _PREFIX.sub("", new, count=1)              # the program keeps the line's own number / bullet
        if action == "replace":
            lines[u["line"]] = prefix + body
        else:
            indent = re.match(r"^\s*", u["text"]).group(0)
            marker = "- " if (pm and pm.group(1).strip() == "-") else ""
            lines.insert(u["line"] + 1, indent + marker + body)
        meta["unit_after"] = prefix + body if action == "replace" else body
    out = "\n".join(lines)
    if out == parent:
        return None, "the edit changes nothing", meta
    if len(out) > C.LAB_PROMPT_MAX_CHARS:
        return None, "the prompt would be too long", meta
    return out, "ok", meta


def get_prompt(pid: str) -> dict | None:
    rows = _q("select * from gap_lab_prompts where prompt_id = :p", p=pid)
    return rows[0] if rows else None


def champion() -> dict | None:
    rows = _q("select * from gap_lab_prompts where status = 'champion' order by created_at desc limit 1")
    return rows[0] if rows else None


def bootstrap() -> dict | None:
    """Make sure the seed prompt is stored and that there is a champion. Returns the champion."""
    champ = champion()
    if champ:
        return champ
    try:
        seed = SEED_FILE.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    except OSError:
        log.error("lab seed prompt missing: prompts/lab_seed_prompt.txt")
        return None
    if len(seed) < 2000 or not units(seed):
        log.error("lab seed prompt unusable")
        return None
    pid = prompt_id(seed)
    _x("""insert into gap_lab_prompts (prompt_id, system_prompt, name, written_by, status, champion_from)
          values (:p, :t, 'seed', 'seed', 'champion', cast(:d as date)) on conflict (prompt_id) do nothing""",
       p=pid, t=seed, d=clock.today_ct())
    _x("update gap_lab_prompts set status = 'champion' where prompt_id = :p and status <> 'champion'", p=pid)
    return champion()


# ------------------------------------------------------------------ nights: input and results

def night_input(d: str) -> dict | None:
    """The frozen input of a night: {"date", "words": [{word, market_ticker}], "user", "package_id"}."""
    pkg = store.get_package(d, "grok_file") or store.get_package(d, "manual")
    if pkg:
        words, user, pid = pkg["words"], pkg["user_message"], pkg["id"]
    else:
        run = store.get_run_for_date(d)
        if not run or not run.get("prompt_text"):
            return None
        from . import prompt as prompt_mod
        _sys, user = prompt_mod.split_paste(run["prompt_text"])
        words, pid = run.get("word_list") or [], None
    if isinstance(words, str):
        try:
            words = json.loads(words)
        except ValueError:
            return None
    words = [{"word": w["word"], "market_ticker": w.get("market_ticker")} for w in words if w.get("market_ticker")]
    if not words or not user:
        return None
    return {"date": d, "words": words, "user": user, "package_id": pid}


def outcomes(ni: dict, fetch: bool = False) -> dict[str, float]:
    """{ticker: 1.0 said / 0.0 not said} for the night's settled words."""
    tickers = [w["market_ticker"] for w in ni["words"]]
    if fetch:
        from . import scoring
        got = scoring.outcomes(tickers, fetch=True)
    else:
        got = store.official_results(tickers)
    return {t: (1.0 if r == "yes" else 0.0) for t, r in got.items() if r in ("yes", "no")}


def lab_nights(upto: str | None = None) -> list[str]:
    """Recent nights the lab can replay: a stored input (with the news block), every word settled,
    not marked void. Newest LAB_MAX_NIGHTS, oldest first."""
    end = upto or clock.today_ct()
    start = (date.fromisoformat(end) - timedelta(days=90)).isoformat()
    dates = {_d(p["event_date"]) for p in store.packages_between(start, end)}
    dates |= {_d(r["event_date"]) for r in store.runs_between(start, end) if r.get("prompt_text")}
    void = set((store.get_state("void_nights", {}) or {}).keys())
    out = []
    for d in sorted(dates - void, reverse=True):
        ni = night_input(d)
        if not ni or (C.LAB_REQUIRE_NEWS and NEWS_MARK not in ni["user"]):
            continue
        if len(outcomes(ni)) < len(ni["words"]):
            continue
        out.append(d)
        if len(out) >= C.LAB_MAX_NIGHTS:
            break
    return sorted(out)


# ------------------------------------------------------------------ model calls (no search, ever)

def _post(url: str, headers: dict, payload: str, timeout: float):
    with netlimit.ticket(url):
        return requests.post(url, headers=headers, data=payload, timeout=timeout)


def _get(url: str, headers: dict, timeout: float):
    with netlimit.ticket(url):
        return requests.get(url, headers=headers, timeout=timeout)


def _gemini_headers() -> dict:
    return {"x-goog-api-key": C.GEMINI_API_KEY, "Content-Type": "application/json"}


def _gemini_models() -> list[str]:
    try:
        r = _get(f"{GEMINI_BASE}/models?pageSize=200", _gemini_headers(), 20)
    except requests.RequestException as exc:
        raise Retry(f"Gemini model list: {type(exc).__name__}", free=True) from exc
    if r.status_code != 200:
        raise Retry(f"Gemini model list: HTTP {r.status_code}", free=True)
    out = []
    for m in r.json().get("models", []):
        name = (m.get("name") or "").replace("models/", "")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []) or not name.startswith("gemini"):
            continue
        if any(x in name for x in ("live", "image", "tts", "audio", "embed", "native", "robotics", "computer", "customtools")):
            continue
        out.append(name)
    return sorted(set(out))


_VER = re.compile(r"gemini-(\d+(?:\.\d+)?)")


def _rank(name: str) -> tuple:
    m = _VER.search(name)
    return (float(m.group(1)) if m else 0.0, not ("preview" in name or "exp" in name), "lite" not in name)


def _flash_models() -> list[str]:
    """Usable-looking Flash models, newest first, Lite ones last."""
    names = [n for n in _gemini_models() if "flash" in n]
    full = sorted([n for n in names if "lite" not in n], key=_rank, reverse=True)
    lite = sorted([n for n in names if "lite" in n], key=_rank, reverse=True)
    return full + lite


def gemini_lab_model() -> str:
    """The Flash model the lab FORECASTS with. Pinned in the database so every prompt is judged by the
    same model; re-picked only if Google retires it. v1.12.1: it is the SECOND newest Flash when there
    is one, because the free plan counts calls per model per day (20 on Oct 2, 2026) and the newest
    Flash is already used by the nightly Gemini challenger and by the writer."""
    if C.LAB_GEMINI_MODEL and C.LAB_GEMINI_MODEL.lower() != "auto":
        return C.LAB_GEMINI_MODEL.replace("models/", "")
    pinned = store.get_state("lab_gemini_model")
    if pinned:
        return str(pinned)
    flash = [n for n in _flash_models() if "lite" not in n]
    if not flash:
        raise Fatal("no Gemini Flash model available to this key")
    pick = flash[1] if len(flash) > 1 else flash[0]
    store.set_state("lab_gemini_model", pick)
    return pick


def _pro_blocked() -> bool:
    until = store.get_state("lab_writer_no_pro")
    return bool(until) and str(until) > _utc().isoformat()


def writer_candidates() -> list[str]:
    """Writer models to try, best first: the newest Gemini Pro (unless this key was refused it in the
    last 7 days), then EVERY Flash model newest first. A busy or used-up model falls through to the next."""
    names = _gemini_models()
    pro = sorted([n for n in names if "pro" in n and "flash" not in n], key=_rank, reverse=True)
    flash = _flash_models()[:5]
    want = C.LAB_WRITER_MODEL.replace("models/", "")
    if want.lower() in ("xai", "grok"):
        return []                                           # LAB_WRITER_MODEL = "grok": Grok writes, Gemini is not asked
    if want and want.lower() != "auto":
        return [want] + [n for n in flash if n != want]
    return ([] if _pro_blocked() else pro[:1]) + flash


def _note_writer_failure(model: str, exc: Exception) -> None:
    """A Pro model the plan does not include (429 quota 0, 403, 404) is skipped for 7 days."""
    if "pro" in model and "flash" not in model and getattr(exc, "http", None) in (403, 404, 429):
        try:
            store.set_state("lab_writer_no_pro", (_utc() + timedelta(days=7)).isoformat())
        except Exception:  # noqa: BLE001
            pass


def _short_err(model: str, exc: Exception) -> str:
    return str(exc).replace(f"{model}: ", "", 1)[:110]


def _gemini_call(model: str, system: str, user: str, temperature: float, timeout: float) -> tuple[str, dict]:
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}}
    try:
        r = _post(f"{GEMINI_BASE}/models/{model}:generateContent", _gemini_headers(), json.dumps(body), timeout)
    except requests.RequestException as exc:
        raise Retry(f"{model}: {type(exc).__name__}", free=True) from exc
    if r.status_code != 200:
        try:
            st = (r.json().get("error") or {}).get("status", "")
        except Exception:  # noqa: BLE001
            st = ""
        msg = f"{model}: HTTP {r.status_code} {st}".strip()
        if r.status_code in (400, 401):
            raise Fatal(msg)
        exc = Retry(msg, free=True)
        exc.http = r.status_code
        raise exc
    js = r.json()
    cands = js.get("candidates") or []
    if not cands:
        raise Retry(f"{model}: empty answer", free=True)
    parts = (cands[0].get("content") or {}).get("parts") or []
    um = js.get("usageMetadata") or {}
    usage = {"input_tokens": int(um.get("promptTokenCount") or 0),
             "output_tokens": int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0),
             "reasoning_tokens": int(um.get("thoughtsTokenCount") or 0), "cost_usd": None}
    return "".join(p.get("text", "") for p in parts if not p.get("thought")), usage


def ask(model_key: str, system: str, user: str, names: list[str], d: str) -> dict:
    """One forecast from one lab model: {"model", "forecasts", "usage", "seconds"}. One request, no tools."""
    t0 = time.monotonic()
    if model_key == "xai":
        body = {"model": C.XAI_MODEL, "reasoning": {"effort": C.LAB_XAI_EFFORT},
                "input": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = _post(xai.URL, xai._headers(), json.dumps(body), C.LAB_TIMEOUT_S)
        except requests.Timeout as exc:
            raise Retry("xai: no answer in time (it may still have been billed; an estimate is booked)",
                        usage={"cost_usd": _avg_cost(), "estimated": True}) from exc
        except requests.RequestException as exc:
            raise Retry(f"xai: {type(exc).__name__}", free=True) from exc
        if r.status_code != 200:
            msg = f"xai: HTTP {r.status_code} {xai._err(r)}".strip()
            if r.status_code in (408, 504, 524):            # the model may have run: counts as a paid try
                raise Retry(msg + " (may have been billed; an estimate is booked)",
                            usage={"cost_usd": _avg_cost(), "estimated": True})
            if r.status_code in xai.RETRYABLE:
                raise Retry(msg, free=True)
            raise Fatal(msg)
        try:
            js = r.json()
        except Exception:  # noqa: BLE001
            js = {}
        usage = xai.usage_of(js)
        try:
            data = parser.validate(xai.answer_text(js), names, d)
        except Exception as exc:  # noqa: BLE001
            raise Retry(f"xai: bad answer ({str(exc)[:120]})", usage=usage) from exc
        return {"model": f"xai:{C.XAI_MODEL}", "forecasts": data["forecasts"], "usage": usage,
                "seconds": round(time.monotonic() - t0, 1)}
    if model_key == "gemini":
        model = gemini_lab_model()
        try:
            raw, usage = _gemini_call(model, system, user, 0.2, C.LAB_TIMEOUT_S)
        except Retry as exc:
            if getattr(exc, "http", None) in (403, 404) and not (C.LAB_GEMINI_MODEL and C.LAB_GEMINI_MODEL.lower() != "auto"):
                store.set_state("lab_gemini_model", None)       # retired model: pick again next time
            raise
        try:
            data = parser.validate(raw, names, d)
        except Exception as exc:  # noqa: BLE001
            raise Retry(f"gemini: bad answer ({str(exc)[:120]})", usage=usage) from exc
        return {"model": f"gemini:{model}", "forecasts": data["forecasts"], "usage": usage,
                "seconds": round(time.monotonic() - t0, 1)}
    if ":" in model_key:
        return _ask_provider(model_key, system, user, names, d, t0)
    raise Fatal(f"unknown lab model {model_key!r}")


_stream_keys: set[str] = set()
_more_tokens: dict[str, int] = {}             # model key -> max_tokens after an answer was cut off ("length")


def _stream(url: str, headers: dict, body: dict, timeout: float):
    from . import challengers as CH
    return CH.stream_chat(url, headers, body, timeout, C.CHALLENGER_STREAM_MAX_S)


def _provider_model(key: str) -> tuple[str, str]:
    """(provider, exact model id) for a key like "nvidia:deepseek". The model id is pinned in the database
    so every prompt is tried on the same model; it is looked up again only if the service retires it."""
    from . import challengers as CH
    prov, _, wanted = key.partition(":")
    pinned = store.get_state(f"lab_model:{key}")
    if pinned:
        return prov, str(pinned)
    try:
        models = CH.resolve_list(prov, wanted)
    except CH.Fatal as exc:
        raise Fatal(str(exc)[:200]) from exc
    except Exception as exc:  # noqa: BLE001  a busy model list: try later
        raise Retry(f"{key}: model list: {type(exc).__name__}", free=True) from exc
    store.set_state(f"lab_model:{key}", models[0])
    return prov, models[0]


def _ask_provider(key: str, system: str, user: str, names: list[str], d: str, t0: float) -> dict:
    """One forecast from an OpenAI-style service (NVIDIA, Mistral, OpenRouter ...): the lab prompt as
    the system message, the frozen file as the user message, no tools."""
    from . import challengers as CH
    prov, model = _provider_model(key)
    base, _env = CH.PROVIDERS[prov]
    body = {"model": model, "temperature": 0.2, "max_tokens": _more_tokens.get(key, C.CHALLENGER_MAX_TOKENS),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    js = None
    try:
        if key in _stream_keys:                             # this host cut a silent answer before: ask piece by piece
            status, js, r = _stream(f"{base}/chat/completions", CH._headers(prov), body, C.LAB_FREE_TIMEOUT_S)
        else:
            r = _post(f"{base}/chat/completions", CH._headers(prov), json.dumps(body), C.LAB_FREE_TIMEOUT_S)
            status = r.status_code
    except requests.Timeout as exc:
        raise Retry(f"{key}: no answer in {C.LAB_FREE_TIMEOUT_S:.0f}s") from exc     # counts as a try: slow models must not loop
    except requests.RequestException as exc:
        raise Retry(f"{key}: {type(exc).__name__}", free=True) from exc
    if status != 200:
        msg = f"{key}: HTTP {status} {CH._err_msg(r)}".strip()[:200]
        if status in (401, 403):
            raise Fatal(msg)
        if status == 404:
            store.set_state(f"lab_model:{key}", None)       # retired model: look it up again next time
        if status in (504, 524):
            _stream_keys.add(key)                           # a gateway timeout: stream the answer from now on
        if status == 404 or status in CH.RETRYABLE:
            exc = Retry(msg, free=True)
            exc.http = status
            raise exc
        raise Fatal(msg)
    if js is None:
        try:
            js = r.json()
        except Exception:  # noqa: BLE001
            js = {}
    u = js.get("usage") or {}
    usage = {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0),
             "cost_usd": None}
    try:
        data = parser.validate(CH._answer_text(js), names, d)
    except Exception as exc:  # noqa: BLE001
        if CH.cut_off(js) and body["max_tokens"] < C.CHALLENGER_MAX_TOKENS_CAP:
            _more_tokens[key] = min(body["max_tokens"] * 2, C.CHALLENGER_MAX_TOKENS_CAP)   # cut off: more room next time
        raise Retry(f"{key}: bad answer ({str(exc)[:120]})", usage=usage) from exc
    return {"model": f"{prov}:{model}", "forecasts": data["forecasts"], "usage": usage,
            "seconds": round(time.monotonic() - t0, 1)}


# ------------------------------------------------------------------ the run queue

def enqueue(pid: str, key: str, d: str, purpose: str) -> int:
    return _x("""insert into gap_lab_runs (prompt_id, model_key, event_date, purpose)
                 values (:p, :k, cast(:d as date), :u) on conflict (prompt_id, model_key, event_date) do nothing""",
              p=pid, k=key, d=d, u=purpose)


def run_row(pid: str, key: str, d: str) -> dict | None:
    rows = _q("select * from gap_lab_runs where prompt_id = :p and model_key = :k and event_date = cast(:d as date)",
              p=pid, k=key, d=d)
    return rows[0] if rows else None


def spent_today() -> float:
    t0 = clock._at(clock.today_ct(), "00:00")
    rows = _q("select coalesce(sum(cost_usd), 0) as c from gap_llm_runs where model like :m and created_at >= :t",
              m="lab:%", t=t0)
    return float(rows[0]["c"] or 0)


def _avg_cost() -> float:
    rows = _q("""select avg(cost_usd) as c from (select cost_usd from gap_lab_runs
                 where model_key = 'xai' and status = 'done' and cost_usd is not null
                 order by finished_at desc limit 10) t""")
    v = rows[0]["c"] if rows else None
    return float(v) if v else DEFAULT_RUN_COST


def _reserve(key: str) -> float | None:
    """Book the expected cost of a paid run against today's budget. None = no budget left today."""
    if key != "xai":
        return 0.0
    est = _avg_cost()
    with _budget_lock:
        if spent_today() + _reserved["usd"] + est > C.LAB_DAILY_BUDGET_USD:
            return None
        _reserved["usd"] += est
    return est


def _release(est: float | None) -> None:
    if est:
        with _budget_lock:
            _reserved["usd"] = max(_reserved["usd"] - est, 0.0)


def _record(d: str, model: str, pid: str, usage: dict | None, ok: bool, seconds=None, detail=None, package_id=None) -> None:
    if not usage:
        return
    try:
        note = f"night {d}" + (" (estimated)" if usage.get("estimated") else "") + (f": {detail}" if detail else "")
        store.record_llm_run(clock.today_ct(), "lab:" + model, usage, ok=ok, seconds=seconds, prompt_version=pid,
                             package_id=package_id, detail=note)
    except Exception:  # noqa: BLE001  bookkeeping must never lose a forecast
        log.exception("lab usage row not saved")


def _run_one(row: dict) -> str:
    """Do one queued run. Returns 'done', 'retry', 'failed', 'budget' or 'skip'."""
    pid, key, d = row["prompt_id"], row["model_key"], _d(row["event_date"])
    if _cooling(key):
        return "skip"                                       # that service just said it is busy: wait
    est = _reserve(key)
    if est is None:
        return "budget"
    if not _slot(key):
        return "budget"                                     # today's free calls for this model are used: wait for tomorrow
    try:
        if not _x("""update gap_lab_runs set status = 'running', claimed_at = :t
                     where prompt_id = :p and model_key = :k and event_date = cast(:d as date) and status = 'pending'""",
                  t=_utc(), p=pid, k=key, d=d):
            return "skip"                                   # another thread took it

        def finish(status: str, **f):
            sets = ", ".join(f"{c} = :{c}" for c in f)
            _x(f"update gap_lab_runs set status = :s{', ' + sets if sets else ''} "
               "where prompt_id = :p and model_key = :k and event_date = cast(:d as date)", s=status, p=pid, k=key, d=d, **f)

        ni, pr = night_input(d), get_prompt(pid)
        if not ni or not pr:
            finish("failed", error="no stored input for that night" if not ni else "prompt not found", finished_at=_utc())
            return "failed"
        names = [w["word"] for w in ni["words"]]
        ticker = {w["word"]: w["market_ticker"] for w in ni["words"]}
        try:
            g = ask(key, pr["system_prompt"], ni["user"], names, d)
        except Retry as exc:
            _record(d, f"{key}", pid, exc.usage, False, detail=str(exc)[:300], package_id=ni["package_id"])
            paid = 0 if exc.free else 1
            tries = int(row.get("attempts") or 0) + paid
            cost = float(row.get("cost_usd") or 0) + float((exc.usage or {}).get("cost_usd") or 0)
            age_h = (_utc() - row["created_at"]).total_seconds() / 3600 if row.get("created_at") else 0
            cap = min(C.LAB_MAX_ATTEMPTS, 2) if row["purpose"] == "screen" else C.LAB_MAX_ATTEMPTS
            if tries >= cap or age_h > 72:
                finish("failed", attempts=tries, error=str(exc)[:300], finished_at=_utc(), cost_usd=cost or None)
                return "failed"
            if exc.free:
                _cool_down(key)                             # a busy service: pause every run for it, not just this one
                if key == "gemini" and getattr(exc, "http", None) == 429:
                    _gem["calls"] = max(_gem["calls"], C.LAB_GEMINI_DAILY_CALLS)   # the free plan's daily count is used up
            delay = 120 if exc.free else min(120 * (2 ** tries), 1800)
            finish("pending", attempts=tries, error=str(exc)[:300], cost_usd=cost or None,
                   not_before=_utc() + timedelta(seconds=delay))
            return "retry"
        except Exception as exc:  # noqa: BLE001  Fatal and anything unexpected
            finish("failed", error=str(exc)[:300], finished_at=_utc())
            log.warning("lab run failed: %s %s %s: %s", pid, key, d, exc)
            return "failed"
        _record(d, g["model"], pid, g["usage"], True, seconds=g["seconds"], package_id=ni["package_id"])   # booked first
        try:
            with store.engine().begin() as conn:
                for f in g["forecasts"]:
                    conn.execute(text(
                        """insert into gap_lab_forecasts (prompt_id, model_key, event_date, market_ticker, word, probability, reasoning)
                           values (:p, :k, cast(:d as date), :t, :w, :pr, :r)
                           on conflict (prompt_id, model_key, event_date, market_ticker) do nothing"""),
                        {"p": pid, "k": key, "d": d, "t": ticker[f["word"]], "w": f["word"],
                         "pr": int(f["probability"]), "r": str(f.get("reasoning") or "")[:2000]})
            now = _utc()
            live = row["purpose"] == "live" and now < clock._at(d, C.LAB_SHOW_START_CT)
            cost = float(row.get("cost_usd") or 0) + float((g["usage"] or {}).get("cost_usd") or 0)
            finish("done", model=g["model"], seconds=g["seconds"], finished_at=now, live=live, error=None,
                   cost_usd=cost if (g["usage"] or {}).get("cost_usd") is not None or cost else None,
                   attempts=int(row.get("attempts") or 0) + 1)
        except Exception as exc:  # noqa: BLE001  the answer is paid for: never send it again by accident
            log.exception("lab answer not stored: %s %s %s", pid, key, d)
            try:
                finish("failed", error=f"answer not stored: {type(exc).__name__}", finished_at=_utc(),
                       attempts=int(row.get("attempts") or 0) + 1)
            except Exception:  # noqa: BLE001  the stale-run reset counts the try instead
                pass
            return "failed"
        _cool.pop(key, None)
        return "done"
    finally:
        _release(est)


def process_queue(limit: int = 400) -> dict:
    """Run pending lab runs. Every model has its own lane: lanes run side by side, each lane does its
    own runs one after another, most urgent first. The judge model does up to LAB_JUDGE_ROWS_PER_PASS
    runs in one pass and every other model LAB_OTHER_ROWS_PER_PASS, so one slow free model cannot hold
    the night's result back for long. Paid runs stop at the daily budget; free ones at their daily count."""
    _x("""update gap_lab_runs set attempts = attempts + 1, error = 'stopped mid-run (restart); counted as a try',
              status = case when attempts + 1 >= :cap then 'failed' else 'pending' end
          where status = 'running' and claimed_at < :t""", t=_utc() - timedelta(minutes=30), cap=C.LAB_MAX_ATTEMPTS)
    keys = model_keys()
    if not keys:
        return {}
    rows = _q("select * from gap_lab_runs where status = 'pending' and (not_before is null or not_before <= :t)", t=_utc())
    rows = [r for r in rows if r["model_key"] in keys and not _cooling(r["model_key"])]
    rows.sort(key=lambda r: (PRIORITY.get(r["purpose"], 9), -date.fromisoformat(_d(r["event_date"])).toordinal()))
    rows = rows[:limit]
    out: dict = {}
    if not rows:
        return out
    judge = keys[0]
    lanes: dict[str, list[dict]] = {}
    for r in rows:
        cap = C.LAB_JUDGE_ROWS_PER_PASS if r["model_key"] == judge else C.LAB_OTHER_ROWS_PER_PASS
        lane = lanes.setdefault(r["model_key"], [])
        if len(lane) < cap:
            lane.append(r)

    def run_lane(lane_rows: list[dict]) -> list[str]:
        got = []
        for r in lane_rows:
            res = _safe_run(r)
            got.append(res)
            if res in ("budget", "skip"):                   # out of budget, or the service is pausing: stop this lane
                break
        return got

    with ThreadPoolExecutor(max_workers=max(1, min(C.LAB_LANES, len(lanes)))) as pool:
        for got in pool.map(run_lane, list(lanes.values())):
            for res in got:
                out[res] = out.get(res, 0) + 1
    return out


def _safe_run(row: dict) -> str:
    try:
        return _run_one(row)
    except Exception:  # noqa: BLE001
        log.exception("lab run crashed")
        return "failed"


# ------------------------------------------------------------------ scores

def _forecasts(pid: str, key: str) -> dict[tuple[str, str], float]:
    rows = _q("select event_date, market_ticker, probability from gap_lab_forecasts where prompt_id = :p and model_key = :k",
              p=pid, k=key)
    return {(_d(r["event_date"]), r["market_ticker"]): r["probability"] / 100.0 for r in rows}


def _truth(dates: list[str]) -> dict[tuple[str, str], float]:
    out = {}
    for d in dates:
        ni = night_input(d)
        if ni:
            for t, y in outcomes(ni).items():
                out[(d, t)] = y
    return out


def brier(pairs: list[tuple[float, float]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def compare(pid: str, champ_id: str, key: str, exclude: str | None = None, truth: dict | None = None) -> dict:
    """The prompt against the champion on the SAME words, on nights other than `exclude`
    (the night the prompt was written from). diff < 0 = the prompt is better."""
    a, c = _forecasts(pid, key), _forecasts(champ_id, key)
    dates = sorted({d for d, _t in a} | {d for d, _t in c})
    truth = truth if truth is not None else _truth(dates)
    own = [(p, truth[k]) for k, p in a.items() if k in truth and k[0] != exclude]
    both = [k for k in a if k in c and k in truth and k[0] != exclude]
    by_night: dict = {}
    for k in both:
        by_night.setdefault(k[0], []).append(k)
    wins = sum(1 for ks in by_night.values()
               if brier([(a[k], truth[k]) for k in ks]) <= brier([(c[k], truth[k]) for k in ks]) + 1e-12)
    ba, bc = brier([(a[k], truth[k]) for k in both]), brier([(c[k], truth[k]) for k in both])
    low = [(p, y) for p, y in own if p <= 0.30]
    return {"n": len(both), "nights": len(by_night), "wins": wins, "brier": ba, "champ": bc,
            "diff": None if ba is None or bc is None else ba - bc,
            "own_n": len(own), "own_brier": brier(own), "own_nights": len({k[0] for k in a if k in truth and k[0] != exclude}),
            "low_n": len(low), "low_said": (sum(y for _p, y in low) / len(low)) if low else None}


def qualifies(cmp_: dict) -> bool:
    return (cmp_["n"] >= C.LAB_MIN_HELDOUT_WORDS and cmp_["diff"] is not None
            and cmp_["diff"] <= -C.LAB_MARGIN and cmp_["wins"] * 2 >= cmp_["nights"])


def night_brier(pid: str, key: str, d: str, truth: dict[str, float]) -> float | None:
    rows = _q("""select market_ticker, probability from gap_lab_forecasts
                 where prompt_id = :p and model_key = :k and event_date = cast(:d as date)""", p=pid, k=key, d=d)
    return brier([(r["probability"] / 100.0, truth[r["market_ticker"]]) for r in rows if r["market_ticker"] in truth])


# ------------------------------------------------------------------ the writer

WRITER_SYSTEM = """You improve a forecasting prompt by ONE small edit at a time. You never forecast yourself.

You get three things:
1. PROMPT: the forecaster's instructions. Every line you are allowed to edit starts with a tag like [U12]. Lines without a tag are locked.
2. FILE: everything the forecaster saw for one night (word list, word history, news blocks). It had no other information and no tools.
3. RESULTS: for each word, whether it was said on the show, the forecaster's number (1-99) and the forecaster's own reasoning.

Your job: find the {n} biggest GENERAL weaknesses of the PROMPT that this night exposes, and propose exactly {n} variants. A weakness is a rule that pushed numbers the wrong way, a rule that is missing, or a rule the forecaster misapplied because it is unclear. Each variant is ONE edit to ONE tagged line.

HARD RULES. A program applies your edits and throws away any variant that breaks one of these; nobody reads an excuse:
1. One edit per variant: "replace" one tagged line, "insert_after" one tagged line, or "delete" one tagged line. Give only the tag; the program does the editing and keeps the line's own number or bullet.
2. The new text is ONE line of plain sentences, {lo}-{hi} characters. No headings, no lists, no braces or brackets, no line breaks.
3. GENERAL ONLY. The rule must be just as right on a night with a completely different word list and different news. Do not name any word from tonight's list. Do not name any person, place, company, storm, team, program or story from tonight's FILE. Do not introduce any proper noun that is not already in the PROMPT. No dates, no weekdays. Describe KINDS of stories ("a day-after follow of a scheduled event", "a legal brief with no new video"), never tonight's stories. The same applies to your "why".
4. Never tell the forecaster to search, browse, look anything up or use tools, and never mention prices, odds, trading or bets.
5. Do not change the output format, the 1-99 range or the 'Blind:' reasoning rule.
6. The {n} variants must edit {n} different tagged lines and address {n} different weaknesses.
7. Aim at being right over many nights, not at tonight's answers. The forecaster is scored with a Brier score, so honest, spread-out probabilities win. An edit that only pushes every number up or down is weak unless RESULTS show a clear general bias (for example: words with a same-day headline were rated far higher than the share of them that were said).

Output JSON only, exactly this shape:
{{"variants": [{{"name": "<2-4 lowercase words joined by hyphens>", "unit": "U<number>", "action": "replace" | "insert_after" | "delete", "text": "<the new line, empty string for delete>", "why": "<one sentence: the general weakness this fixes>"}}]}}"""


def writer_material(champ: dict, ni: dict, truth: dict[str, float], key: str) -> str:
    rows = _q("""select market_ticker, word, probability, reasoning from gap_lab_forecasts
                 where prompt_id = :p and model_key = :k and event_date = cast(:d as date)""",
              p=champ["prompt_id"], k=key, d=ni["date"])
    by = {r["market_ticker"]: r for r in rows}
    lines, pairs = [], []
    for w in ni["words"]:
        r, y = by.get(w["market_ticker"]), truth.get(w["market_ticker"])
        if not r or y is None:
            continue
        p = r["probability"]
        pairs.append((p / 100.0, y))
        lines.append(f"- {w['word']} | said: {'YES' if y else 'no'} | forecast: {p} | squared error: {(p / 100.0 - y) ** 2:.2f} | "
                     f"reasoning: {str(r.get('reasoning') or '')[:600]}")
    said = sum(y for _p, y in pairs)
    avg_yes = sum(p for p, y in pairs if y) / max(said, 1)
    avg_no = sum(p for p, y in pairs if not y) / max(len(pairs) - said, 1)
    summary = (f"{len(pairs)} words, {said:.0f} said. Brier {brier(pairs):.3f} (0.25 = always 50). "
               f"Average forecast on said words {100 * avg_yes:.0f}, on not-said words {100 * avg_no:.0f}.")
    return ("PROMPT (editable lines are tagged):\n" + tagged(champ["system_prompt"])
            + "\n\n=====\nFILE (what the forecaster saw that night):\n" + ni["user"]
            + "\n\n=====\nRESULTS:\n" + summary + "\n" + "\n".join(lines))


def write_variants(champ: dict, ni: dict, truth: dict[str, float], key: str) -> tuple[list[dict], str, list[str]]:
    """Ask the writer for edits. Returns (raw edits, writer model, notes about models that were skipped).
    Order: Gemini Pro (if the plan has it), every Gemini Flash, then Grok as the last resort."""
    system = WRITER_SYSTEM.format(n=C.LAB_VARIANTS_PER_NIGHT, lo=C.LAB_EDIT_MIN_TEXT, hi=C.LAB_EDIT_MAX_TEXT)
    material = writer_material(champ, ni, truth, key)
    notes: list[str] = []
    last: Exception | None = None
    try:
        cands = writer_candidates()
    except Exception as exc:  # noqa: BLE001  Google will not even list its models: go on to the fallback
        cands = []
        notes.append(f"gemini model list: {str(exc)[:80]}")
    for model in cands:
        try:
            raw, usage = _gemini_call(model, system, material, 0.7, C.LAB_TIMEOUT_S)
            _record(ni["date"], f"writer:gemini:{model}", champ["prompt_id"], usage, True, package_id=ni["package_id"])
            data = parser.load_json(raw)
            edits = data.get("variants")
            if not isinstance(edits, list) or not edits:
                raise ValueError("no variants in the answer")
            return [e for e in edits if isinstance(e, dict)][:C.LAB_VARIANTS_PER_NIGHT], f"gemini:{model}", notes
        except Exception as exc:  # noqa: BLE001  try the next writer model
            last = exc
            _note_writer_failure(model, exc)
            notes.append(f"{model}: {_short_err(model, exc)}")
    if C.LAB_WRITER_GROK_FALLBACK and C.XAI_API_KEY:        # v1.13.1: every Gemini model failed: Grok writes (a few cents)
        try:
            edits = _grok_writer(system, material, ni, champ)
            return edits, f"xai:{C.XAI_MODEL}", notes
        except Exception as exc:  # noqa: BLE001
            last = exc
            notes.append(f"grok: {str(exc)[:100]}")
    raise Retry("writer failed: " + ("; ".join(notes) or str(last)), free=True)


def _grok_writer(system: str, material: str, ni: dict, champ: dict) -> list[dict]:
    """Ask Grok (no tools) for the edits. Paid: it is booked and it respects the lab's daily budget."""
    est = _reserve("xai")
    if est is None:
        raise RuntimeError("no lab budget left today")
    try:
        body = {"model": C.XAI_MODEL, "reasoning": {"effort": C.LAB_WRITER_XAI_EFFORT},
                "input": [{"role": "system", "content": system}, {"role": "user", "content": material}]}
        r = _post(xai.URL, xai._headers(), json.dumps(body), C.LAB_TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {xai._err(r)}".strip())
        js = r.json()
        _record(ni["date"], f"writer:xai:{C.XAI_MODEL}", champ["prompt_id"], xai.usage_of(js), True,
                package_id=ni["package_id"])
        edits = parser.load_json(xai.answer_text(js)).get("variants")
        if not isinstance(edits, list) or not edits:
            raise ValueError("no variants in the answer")
        return [e for e in edits if isinstance(e, dict)][:C.LAB_VARIANTS_PER_NIGHT]
    finally:
        _release(est)


# ------------------------------------------------------------------ the nightly state machine

def _night(d: str, champ_id: str | None = None) -> dict:
    if champ_id:
        _x("""insert into gap_lab_nights (event_date, champion_id) values (cast(:d as date), :c)
              on conflict (event_date) do nothing""", d=d, c=champ_id)
    rows = _q("select * from gap_lab_nights where event_date = cast(:d as date)", d=d)
    return rows[0] if rows else {}


def _set_night(d: str, **f) -> None:
    if "detail" in f and not isinstance(f["detail"], str):
        f["detail"] = json.dumps(f["detail"], default=str)[:20000]
    sets = ", ".join(f"{c} = :{c}" for c in f)
    _x(f"update gap_lab_nights set {sets}, updated_at = :t where event_date = cast(:d as date)", t=_utc(), d=d, **f)


def _detail(row: dict) -> dict:
    try:
        return json.loads(row.get("detail") or "{}")
    except ValueError:
        return {}


def board_prompts() -> list[dict]:
    return _q("select * from gap_lab_prompts where status in ('testing', 'listed') order by created_at")


def live_start(d: str) -> int:
    """Called when tonight's file goes out: the champion forecasts tonight, before the show."""
    if not enabled():
        return 0
    champ = bootstrap()
    if not champ or not night_input(d):
        return 0
    n = sum(enqueue(champ["prompt_id"], k, d, "live") for k in model_keys())
    _night(d, champ["prompt_id"])
    return n


def night_step(d: str, send=None) -> int:
    """Move one night forward as far as it can go right now. Returns 1 if something changed."""
    ni = night_input(d)
    key = primary_key()
    if not ni or not key or (C.LAB_REQUIRE_NEWS and NEWS_MARK not in ni["user"]):
        return 0
    now = clock.now_ct()
    today = d == clock.today_ct()
    if today and now < clock._at(d, C.LAB_AFTER_CT):
        return 0
    row = _night(d)
    if row and row.get("stage") in ("decided", "skipped"):
        return 0
    if row and row.get("stage") == "writing":               # another thread is asking the writer right now
        if row["updated_at"] < _utc() - timedelta(minutes=20):
            _set_night(d, stage="new")                      # it died half way: start that step again
            return 1
        return 0
    truth = outcomes(ni, fetch=True)
    if len(truth) < len(ni["words"]) and (today and now < clock._at(d, C.LAB_LATEST_CT)):
        return 0
    if len(truth) < max(3, len(ni["words"]) // 2):
        return 0
    champ = bootstrap()
    if not champ:
        return 0
    if not row:
        row = _night(d, champ["prompt_id"])
    champ = get_prompt(row.get("champion_id") or champ["prompt_id"]) or champ
    cid = champ["prompt_id"]
    say = send or (lambda _t: None)

    if row["stage"] == "new":
        changed = 0
        for k in model_keys():
            changed += enqueue(cid, k, d, "fill")
            for p in board_prompts():
                changed += enqueue(p["prompt_id"], k, d, "fill")
        r = run_row(cid, key, d)
        det = _detail(row)
        if r and r["status"] == "failed" and not det.get("champion_retried"):
            det["champion_retried"] = True
            _set_night(d, detail=det)
            _x("""update gap_lab_runs set status = 'pending', attempts = 0, not_before = null, purpose = 'fill'
                  where prompt_id = :p and model_key = :k and event_date = cast(:d as date)""", p=cid, k=key, d=d)
            return 1
        if r and r["status"] == "failed":
            _set_night(d, stage="skipped", decision="champion_run_failed", detail={"error": r.get("error")})
            say(f"PROMPT LAB {d}: skipped. The champion's own run failed ({str(r.get('error'))[:160]}).")
            return 1
        if not r or r["status"] != "done":
            return 1 if changed else 0
        if det.get("writer_after") and _utc().isoformat() < det["writer_after"]:
            return 0
        if C.LAB_VARIANTS_PER_NIGHT <= 0 or not (C.GEMINI_API_KEY or (C.LAB_WRITER_GROK_FALLBACK and C.XAI_API_KEY)):
            _set_night(d, stage="decided", decision="no_writer", detail=det)
            say(night_message(d))
            return 1
        if not _x("""update gap_lab_nights set stage = 'writing', updated_at = :t
                     where event_date = cast(:d as date) and stage = 'new'""", t=_utc(), d=d):
            return 0                                        # claimed by another thread: the writer is asked once
        try:
            edits, writer, notes = write_variants(champ, ni, truth, key)
        except Exception as exc:  # noqa: BLE001
            _set_night(d, stage="new")
            det["writer_tries"] = int(det.get("writer_tries") or 0) + 1
            det["writer_error"] = str(exc)[:400]
            if det["writer_tries"] >= C.LAB_WRITER_TRIES:
                _set_night(d, stage="decided", decision="writer_failed", detail=det)
                say(night_message(d))
                return 1
            det["writer_after"] = (_utc() + timedelta(minutes=5 * det["writer_tries"])).isoformat()
            _set_night(d, detail=det)
            return 0
        det.update(writer_notes=notes, variants=[])
        seen_units = set()
        for e in edits:
            new, reason, meta = apply_edit(champ["system_prompt"], e, ni["words"], ni["user"])
            uid = str(e.get("unit") or "").upper()
            if new and uid in seen_units:
                new, reason = None, "edits the same line as another variant"
            pid = prompt_id(new) if new else None
            if new and get_prompt(pid):
                new, reason = None, "this exact prompt was already tried"
            item = {"name": meta.get("name"), "section": meta.get("section"), "action": meta.get("action"),
                    "why": meta.get("why"), "ok": bool(new), "reason": reason, "prompt_id": pid if new else None}
            det["variants"].append(item)
            if not new:
                continue
            seen_units.add(uid)
            _x("""insert into gap_lab_prompts (prompt_id, parent_id, system_prompt, name, section, action, unit_before,
                      unit_after, why, written_by, written_from, status)
                  values (:p, :par, :t, :n, :s, :a, :ub, :ua, :w, :by, cast(:d as date), 'candidate')
                  on conflict (prompt_id) do nothing""",
               p=pid, par=cid, t=new, n=meta.get("name"), s=meta.get("section"), a=meta.get("action"),
               ub=meta.get("unit_before"), ua=meta.get("unit_after"), w=meta.get("why"), by=writer, d=d)
            for k in model_keys():
                enqueue(pid, k, d, "screen")
        _set_night(d, stage="written", writer_model=writer, detail=det)
        return 1

    if row["stage"] == "written":
        det = _detail(row)
        cands = [v for v in det.get("variants", []) if v.get("ok")]
        runs = [run_row(v["prompt_id"], key, d) for v in cands]
        if any(r and r["status"] in ("pending", "running") for r in runs):
            return 0
        base = night_brier(cid, key, d, truth)
        best = None
        for v, r in zip(cands, runs):
            v["screen"] = night_brier(v["prompt_id"], key, d, truth) if r and r["status"] == "done" else None
            if v["screen"] is None:
                v["run_error"] = (r or {}).get("error") or "no run"
            elif best is None or v["screen"] < best["screen"]:
                best = v
        det["champion_screen"] = base
        winner = best if (best and base is not None and best["screen"] < base - 1e-12) else None
        testing_now = _q("select count(*) as n from gap_lab_prompts where status = 'testing'")[0]["n"]
        if winner and testing_now >= C.LAB_MAX_TESTING:
            det["queue_full"] = True                        # earlier winners are still being tested: do not pile up
            winner = None
        for v in cands:
            st = "testing" if winner is v else "screened_out"
            _x("update gap_lab_prompts set status = :s where prompt_id = :p and status = 'candidate'", s=st, p=v["prompt_id"])
        if winner:
            for n in lab_nights(d):
                if n == d:
                    continue
                enqueue(winner["prompt_id"], key, n, "test")   # the judge model only
                enqueue(cid, key, n, "fill")
            det["winner"] = winner["prompt_id"]
        decision = "winner_to_test" if winner else ("no_valid_variant" if not cands else (
            "test_queue_full" if det.get("queue_full") else "no_variant_beat_champion"))
        _set_night(d, stage="decided", decision=decision, detail=det)
        say(night_message(d))
        return 1
    return 0


def evaluate(send=None) -> int:
    """Apply the champion rule to every prompt on the board. Returns 1 if the champion changed."""
    champ, key = champion(), primary_key()
    if not champ or not key:
        return 0
    cid = champ["prompt_id"]
    scored = []
    for p in board_prompts():
        pid = p["prompt_id"]
        open_runs = _q("""select count(*) as n from gap_lab_runs where prompt_id = :p and model_key = :k
                          and status in ('pending', 'running')""", p=pid, k=key)[0]["n"]
        open_champ = _q("""select count(*) as n from gap_lab_runs c join gap_lab_runs a
                             on a.event_date = c.event_date and a.model_key = c.model_key
                           where a.prompt_id = :p and c.prompt_id = :c and c.model_key = :k
                             and c.status in ('pending', 'running')""", p=pid, c=cid, k=key)[0]["n"]
        cmp_ = compare(pid, cid, key, exclude=_d(p["written_from"]) if p.get("written_from") else None)
        if p["status"] == "testing" and not open_runs and not open_champ:
            _x("update gap_lab_prompts set status = 'listed' where prompt_id = :p and status = 'testing'", p=pid)
        scored.append((p, cmp_, bool(open_runs or open_champ)))
    winners = [(p, c) for p, c, busy in scored if not busy and qualifies(c)]
    changed = 0
    if winners:
        p, c = min(winners, key=lambda pc: pc[1]["diff"])
        nxt = date.fromisoformat(clock.today_ct())
        if clock.now_ct() >= clock._at(clock.today_ct(), C.POLL_START_CT):
            nxt += timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        _x("update gap_lab_prompts set status = 'listed' where prompt_id = :c and status = 'champion'", c=cid)
        _x("update gap_lab_prompts set status = 'champion', champion_from = cast(:d as date) where prompt_id = :p",
           p=p["prompt_id"], d=nxt.isoformat())
        logrows = list(store.get_state("lab_champion_log", []) or [])
        logrows.append({"at": clock.now_ct().isoformat(), "from": cid, "to": p["prompt_id"], "name": p.get("name"),
                        "diff": round(c["diff"], 4), "words": c["n"], "nights": c["nights"], "wins": c["wins"]})
        store.set_state("lab_champion_log", logrows[-50:])
        changed = 1
        if send:
            send(f"PROMPT LAB: NEW CHAMPION {p['prompt_id']} ({p.get('name') or '-'}), paper only.\n"
                 f"Edit: {p.get('section') or '-'} / {p.get('action') or '-'}\n"
                 f"On {c['nights']} other nights, {c['n']} words: Brier {c['brier']:.3f} vs old champion {c['champ']:.3f} "
                 f"({c['diff']:+.3f}); better or equal on {c['wins']} of {c['nights']} nights.\n"
                 f"It forecasts from {nxt.isoformat()}. Live trading is unchanged (manual Grok).\n"
                 f"/gap_lab_prompt {p['prompt_id']} shows the text; /gap_lab_champion {cid} puts the old one back.")
        cid = p["prompt_id"]
    listed = _q("select * from gap_lab_prompts where status = 'listed'")
    if len(listed) > C.LAB_TOP_N:
        ranked = []
        for p in listed:
            c = compare(p["prompt_id"], cid, key, exclude=_d(p["written_from"]) if p.get("written_from") else None)
            ranked.append((c["diff"] if c["diff"] is not None else 9.0, p["prompt_id"]))
        for _diff, pid in sorted(ranked)[C.LAB_TOP_N:]:
            _x("update gap_lab_prompts set status = 'retired' where prompt_id = :p and status = 'listed'", p=pid)
            _x("delete from gap_lab_runs where prompt_id = :p and status = 'pending'", p=pid)
    return changed


def set_champion(pid: str) -> str:
    """Manual override from Telegram (Jovan decides)."""
    p = get_prompt(pid)
    if not p:
        return f"no lab prompt {pid}"
    cur = champion()
    if cur and cur["prompt_id"] == pid:
        return f"{pid} is already the champion"
    if cur:
        _x("update gap_lab_prompts set status = 'listed' where prompt_id = :c", c=cur["prompt_id"])
    _x("update gap_lab_prompts set status = 'champion', champion_from = cast(:d as date) where prompt_id = :p",
       p=pid, d=clock.today_ct())
    logrows = list(store.get_state("lab_champion_log", []) or [])
    logrows.append({"at": clock.now_ct().isoformat(), "from": cur["prompt_id"] if cur else None, "to": pid, "manual": True})
    store.set_state("lab_champion_log", logrows[-50:])
    return f"champion is now {pid} ({p.get('name') or '-'}), set by hand. Paper only."


def writer_check() -> str:
    """Which writer model this key can really use: tiny calls, best first, stopping at the first that
    works (the free plan allows few calls a day). For /gap_lab_writer."""
    grok = bool(C.LAB_WRITER_GROK_FALLBACK and C.XAI_API_KEY)
    grok_line = ("If every Gemini model fails at night, Grok writes the edits instead (a few cents, inside the lab budget)."
                 if grok else "There is no Grok fallback (no xAI key, or LAB_WRITER_GROK_FALLBACK is off).")
    if not C.GEMINI_API_KEY:
        return "no Gemini key. " + (grok_line if grok else "The lab has no writer (the champion still forecasts every night).")
    lines = ["WRITER CHECK (tiny calls, best model first, stops at the first that works):"]
    try:
        cands = writer_candidates()
    except Exception as exc:  # noqa: BLE001
        return f"could not list Gemini models: {str(exc)[:160]}"
    if _pro_blocked():
        lines.append("- Gemini Pro: skipped (this key was refused it in the last 7 days; the free plan has no Pro)")
    first_ok = None
    for model in cands:
        try:
            _gemini_call(model, "Reply with JSON only.", 'Reply with exactly {"ok": true}', 0.0, 60)
            lines.append(f"- {model}: OK")
            first_ok = model
            break
        except Exception as exc:  # noqa: BLE001
            _note_writer_failure(model, exc)
            why = _short_err(model, exc)
            if "429" in why and "pro" in model and "flash" not in model:
                why += " = not on this plan"
            elif "503" in why:
                why += " = Google is busy right now"
            elif "429" in why:
                why += " = today's free calls for this model are used"
            lines.append(f"- {model}: not usable ({why})")
    lines.append(f"The writer would be: {first_ok}. Tonight it tries the same list in the same order." if first_ok
                 else "No Gemini model works right now. Tonight the lab tries again, several times.")
    lines.append(grok_line)
    return "\n".join(lines)


def start_night(d: str | None = None) -> str:
    """Run the lab on a PAST settled night now (rehearsal, or to seed the board). For /gap_lab start."""
    champ = bootstrap()
    if not champ or not primary_key():
        return "the lab cannot start: no champion or no model key"
    nights = lab_nights()
    if not nights:
        return "no settled night with a stored news file yet"
    d = d or nights[-1]
    if d not in nights:
        return f"{d} cannot be replayed (no stored news file, not fully settled, or void). Usable: {', '.join(nights[-5:])}"
    row = _night(d)
    if row and row.get("stage") in ("decided", "skipped"):
        return f"{d} was already done ({row.get('decision')}). /gap_lab_night {d} shows it."
    _night(d, champ["prompt_id"])
    _x("update gap_lab_nights set updated_at = :t where event_date = cast(:d as date)", t=_utc(), d=d)
    return f"lab started on {d}: champion run, writer, screen, test. The result arrives here when it is done."


def retry_failed() -> int:
    """Give failed runs of prompts still in play another go (from /gap_lab retry)."""
    return _x("""update gap_lab_runs set status = 'pending', attempts = 0, not_before = null, error = null
                 where status = 'failed' and prompt_id in
                   (select prompt_id from gap_lab_prompts where status in ('champion', 'testing', 'listed', 'candidate'))""")


# ------------------------------------------------------------------ the loop

def _open_nights() -> list[str]:
    today = clock.today_ct()
    rows = _q("""select event_date from gap_lab_nights where stage not in ('decided', 'skipped')
                 and updated_at >= :s order by event_date""", s=_utc() - timedelta(days=6))
    out = [_d(r["event_date"]) for r in rows]
    if clock.weekday_ct() and today not in out:
        out.append(today)
    return out


def _close_old_nights() -> None:
    old = _q("select event_date from gap_lab_nights where stage not in ('decided', 'skipped') and updated_at < :t",
             t=_utc() - timedelta(days=6))
    for r in old:
        d = _d(r["event_date"])
        _x("""update gap_lab_prompts set status = 'screened_out', note = 'night expired'
              where written_from = cast(:d as date) and status = 'candidate'""", d=d)
        _x("delete from gap_lab_runs where status = 'pending' and purpose = 'screen' and event_date = cast(:d as date)", d=d)
        _set_night(d, stage="skipped", decision="expired")


def cycle(send=None) -> int:
    """One pass: move open nights forward, run the queue, apply the champion rule. Returns work done."""
    if not bootstrap():
        return 0
    _close_old_nights()
    work = 0
    for d in _open_nights():
        try:
            work += night_step(d, send)
        except Exception:  # noqa: BLE001
            log.exception("lab night step failed for %s", d)
    res = process_queue()
    done = res.get("done", 0) + res.get("failed", 0)
    work += done
    if done or work:
        for d in _open_nights():
            try:
                work += night_step(d, send)
            except Exception:  # noqa: BLE001
                log.exception("lab night step failed for %s", d)
        try:
            work += evaluate(send)
        except Exception:  # noqa: BLE001
            log.exception("lab evaluate failed")
    return work


def tick(send=None) -> str:
    """Called from the bot's poll loop. Never blocks it: the work happens in one background thread."""
    if not enabled():
        return "off"
    if _running.locked():
        return "busy"

    def go():
        if not _running.acquire(blocking=False):
            return
        try:
            t0 = time.monotonic()
            for _ in range(40):
                if not cycle(send) or time.monotonic() - t0 > 1500:
                    break
        except Exception:  # noqa: BLE001
            log.exception("prompt lab tick failed")
        finally:
            _running.release()

    threading.Thread(target=go, name="promptlab", daemon=True).start()
    return "started"


# ------------------------------------------------------------------ text for Telegram and the weekly dump

def _fmt(v, spec=".3f") -> str:
    return "-" if v is None else format(v, spec)


def night_message(d: str) -> str:
    row = _night(d)
    det = _detail(row)
    key = primary_key() or "-"
    lines = [f"PROMPT LAB {d} (paper only, judge model: {key})",
             f"champion: {row.get('champion_id')}  |  writer: {row.get('writer_model') or '-'}"]
    for n in det.get("writer_notes") or []:
        lines.append(f"  writer model skipped: {n}")
    if row.get("decision") == "writer_failed":
        lines.append(f"The writer failed {C.LAB_WRITER_TRIES} times ({det.get('writer_error')}). No variants tonight.")
    if row.get("decision") == "no_writer":
        lines.append("No writer (no Gemini key, or LAB_VARIANTS_PER_NIGHT is 0). The champion still forecast tonight.")
    if det.get("variants"):
        lines += ["", f"champion's Brier tonight: {_fmt(det.get('champion_screen'))}",
                  "variant | edit | result"]
        for v in det["variants"]:
            edit = f"{v.get('section') or '-'} / {v.get('action') or '-'}"
            if not v.get("ok"):
                res = f"THROWN AWAY: {v.get('reason')}"
            elif v.get("screen") is None:
                res = f"no score ({v.get('run_error') or 'run failed'})"
            else:
                res = f"Brier tonight {v['screen']:.3f}"
                if det.get("winner") == v.get("prompt_id"):
                    res += "  <- best, now tested on the other nights"
            lines.append(f"{v.get('name') or '-'} ({v.get('prompt_id') or '-'}) | {edit} | {res}")
        for v in det["variants"]:
            if v.get("ok") and v.get("why"):
                lines.append(f"  why {v.get('name')}: {v['why']}")
        lines += [""] + night_models_lines(d, row.get("champion_id"), [v for v in det["variants"] if v.get("ok")])
    if row.get("decision") == "no_variant_beat_champion":
        lines.append("No variant beat the champion tonight. Nothing goes on to the test.")
    if row.get("decision") == "test_queue_full":
        lines.append(f"A variant beat the champion tonight, but {C.LAB_MAX_TESTING} earlier winners are still being tested. It was dropped.")
    if row.get("decision") == "no_valid_variant":
        lines.append("Every variant broke a rule and was thrown away. Nothing was run.")
    lines.append(f"lab spend today: ${spent_today():.2f} of ${C.LAB_DAILY_BUDGET_USD:.2f}")
    return "\n".join(lines)


def _all_forecasts(pids: list[str]) -> dict[tuple[str, str], dict[tuple[str, str], float]]:
    """{(prompt, model key): {(date, ticker): p}} for these prompts, in one query."""
    if not pids:
        return {}
    marks = ", ".join(f":p{i}" for i in range(len(pids)))
    rows = _q(f"select prompt_id, model_key, event_date, market_ticker, probability from gap_lab_forecasts "
              f"where prompt_id in ({marks})", **{f"p{i}": p for i, p in enumerate(pids)})
    out: dict = {}
    for r in rows:
        out.setdefault((r["prompt_id"], r["model_key"]), {})[(_d(r["event_date"]), r["market_ticker"])] = r["probability"] / 100.0
    return out


def night_models_lines(d: str, champ_id: str | None, variants: list[dict]) -> list[str]:
    """Tonight's Brier for the champion and each valid variant on EVERY model ('-' = that model has not answered yet)."""
    keys = model_keys()
    ni = night_input(d)
    if not keys or not ni or not champ_id:
        return []
    truth = outcomes(ni)
    lines = ["ALL MODELS tonight (Brier; '-' = not answered yet; /gap_lab_night shows it again later):",
             "prompt | " + " | ".join(short(k) for k in keys)]
    for name, pid in [("champion", champ_id)] + [(v.get("name") or "-", v.get("prompt_id")) for v in variants]:
        cells = [_fmt(night_brier(pid, k, d, truth)) if pid else "-" for k in keys]
        lines.append(f"{name} | " + " | ".join(cells))
    return lines


def model_board_lines() -> list[str]:
    """Every model on the champion prompt, on every settled night it has answered, plus the plain average
    of all models, plus the board prompt that does best on that model."""
    champ, keys = champion(), model_keys()
    if not champ or not keys:
        return ["ALL MODELS: the lab has not started yet."]
    cid = champ["prompt_id"]
    others = board_prompts()
    fc = _all_forecasts([cid] + [p["prompt_id"] for p in others])
    dates = sorted({d for m in fc.values() for d, _t in m})
    truth = _truth(dates)
    lines = [f"ALL MODELS on the champion prompt {cid} (settled nights only; lower Brier is better; judge = {short(keys[0])})",
             "model | nights | words | Brier | <=30: words (said%) | best other prompt on this model (diff, words)"]
    rows, pool = [], {}
    for k in keys:
        a = {kk: p for kk, p in fc.get((cid, k), {}).items() if kk in truth}
        for kk, p in a.items():
            pool.setdefault(kk, []).append(p)
        b = brier([(p, truth[kk]) for kk, p in a.items()])
        low = [(p, truth[kk]) for kk, p in a.items() if p <= 0.30]
        best = None
        for p in others:
            ex = _d(p["written_from"]) if p.get("written_from") else None
            o = fc.get((p["prompt_id"], k), {})
            both = [kk for kk in o if kk in a and kk[0] != ex]
            if len(both) < 8:
                continue
            diff = brier([(o[kk], truth[kk]) for kk in both]) - brier([(a[kk], truth[kk]) for kk in both])
            if best is None or diff < best[0]:
                best = (diff, p, len(both))
        best_txt = "-" if not best else f"{best[1]['prompt_id']} {best[1].get('name') or ''} ({best[0]:+.3f}, {best[2]})"
        low_txt = "-" if not low else f"{len(low)} ({100 * sum(y for _p, y in low) / len(low):.0f}%)"
        rows.append((9.0 if b is None else b,
                     f"{short(k)}{' (judge)' if k == keys[0] else ''} | {len({kk[0] for kk in a})} | {len(a)} | {_fmt(b)} | {low_txt} | {best_txt}"))
    lines += [ln for _b, ln in sorted(rows, key=lambda t: t[0])]
    avg = [(sum(v) / len(v), truth[kk]) for kk, v in pool.items() if len(v) >= 2]
    if avg:
        low = [(p, y) for p, y in avg if p <= 0.30]
        low_txt = "-" if not low else f"{len(low)} ({100 * sum(y for _p, y in low) / len(low):.0f}%)"
        lines.append(f"average of all models | {len({kk[0] for kk in pool})} | {len(avg)} | {_fmt(brier(avg))} | {low_txt} | -")
    lines.append("To make another model the judge, put it first in the LAB_MODELS setting.")
    return lines


def forecast_text(d: str | None = None) -> str:
    """Word by word: manual Grok next to the champion prompt on every model, for one night."""
    d = d or clock.today_ct()
    ni, keys = night_input(d), model_keys()
    row = _night(d)
    champ = champion()
    cid = (row or {}).get("champion_id") or (champ or {}).get("prompt_id")
    if not ni or not cid:
        return f"no lab forecasts for {d}"
    truth = outcomes(ni)
    fc = _all_forecasts([cid])
    manual = {f.get("market_ticker"): f.get("probability") for f in store.grok_forecasts_for_date(d)}
    lines = [f"LAB FORECASTS {d}: champion prompt {cid} on every model (paper only). 'manual' = your Grok paste.",
             "word | said? | manual | " + " | ".join(short(k) for k in keys)]
    for w in ni["words"]:
        t = w["market_ticker"]
        y = truth.get(t)
        cells = []
        for k in keys:
            p = fc.get((cid, k), {}).get((d, t))
            cells.append("-" if p is None else str(round(100 * p)))
        lines.append(f"{w['word']} | {'YES' if y == 1.0 else ('no' if y == 0.0 else '?')} | {manual.get(t, '-')} | " + " | ".join(cells))
    if truth:
        mp = [(manual[t] / 100.0, y) for t, y in truth.items() if manual.get(t) is not None]
        cells = [_fmt(night_brier(cid, k, d, truth)) for k in keys]
        lines.append(f"BRIER | {len(truth)} settled | {_fmt(brier(mp))} | " + " | ".join(cells))
    return "\n".join(lines)


def leaderboard_lines() -> list[str]:
    champ, key = champion(), primary_key()
    if not champ or not key:
        return ["PROMPT LAB: not started yet (no champion or no model key)."]
    cid = champ["prompt_id"]
    rows = [champ] + _q("select * from gap_lab_prompts where status in ('testing', 'listed') order by created_at")
    lines = [f"PROMPT LAB BOARD (paper only; judge model: {key}; lower Brier is better)",
             "Scores use only nights OTHER than the one a prompt was written from. diff = prompt minus champion on the same words.",
             "prompt | status | edit | from | nights | words | Brier | champion same words | diff | nights won | <=30: words (said%)"]
    out = []
    for p in rows:
        c = compare(p["prompt_id"], cid, key, exclude=_d(p["written_from"]) if p.get("written_from") else None)
        low = "-" if not c["low_n"] else f"{c['low_n']} ({100 * c['low_said']:.0f}%)"
        if p["prompt_id"] == cid:
            line = (f"{cid} {p.get('name') or ''} | CHAMPION | {p.get('section') or 'seed'} | {_d(p['written_from']) if p.get('written_from') else '-'} | "
                    f"{c['own_nights']} | {c['own_n']} | {_fmt(c['own_brier'])} | - | - | - | {low}")
            out.append((-9.0, line))
        else:
            line = (f"{p['prompt_id']} {p.get('name') or ''} | {p['status']} | {p.get('section') or '-'}/{p.get('action') or '-'} | "
                    f"{_d(p['written_from']) if p.get('written_from') else '-'} | {c['nights']} | {c['n']} | {_fmt(c['brier'])} | "
                    f"{_fmt(c['champ'])} | {_fmt(c['diff'], '+.3f')} | {c['wins']}/{c['nights']} | {low}")
            out.append((c["diff"] if c["diff"] is not None else 9.0, line))
    lines += [ln for _k, ln in sorted(out, key=lambda t: t[0])]
    lines.append(f"Champion rule: at least {C.LAB_MIN_HELDOUT_WORDS} words on other nights, beat the champion by "
                 f"{C.LAB_MARGIN:.3f} Brier, better or equal on half the nights.")
    return lines


def live_lines(start: str, end: str) -> list[str]:
    """The champion's LIVE forecasts (made before the show) against manual Grok on the same words."""
    key = primary_key()
    if not key:
        return []
    rows = _q("""select f.event_date, f.market_ticker, f.probability, f.prompt_id from gap_lab_forecasts f
                 join gap_lab_runs r on r.prompt_id = f.prompt_id and r.model_key = f.model_key and r.event_date = f.event_date
                 where r.live and f.model_key = :k and f.event_date between cast(:a as date) and cast(:b as date)""",
              k=key, a=start, b=end)
    if not rows:
        return [f"LIVE (before the show), {start} .. {end}: no live lab forecasts yet."]
    grok = {(_d(f["event_date"]), f["market_ticker"]): f["probability"] / 100.0
            for f in store.grok_forecasts_between(start, end)}
    res = store.official_results(sorted({r["market_ticker"] for r in rows}))
    mine, both_m, both_g = [], [], []
    for r in rows:
        y = res.get(r["market_ticker"])
        if y not in ("yes", "no"):
            continue
        yv, k = (1.0 if y == "yes" else 0.0), (_d(r["event_date"]), r["market_ticker"])
        mine.append((r["probability"] / 100.0, yv))
        if k in grok:
            both_m.append((r["probability"] / 100.0, yv))
            both_g.append((grok[k], yv))
    nights = len({_d(r["event_date"]) for r in rows})
    return [f"LIVE (before the show), {start} .. {end}: lab champion on {key}, {nights} night(s), {len(mine)} settled words",
            f"lab champion Brier {_fmt(brier(mine))} | on the words manual Grok also forecast ({len(both_m)}): "
            f"lab {_fmt(brier(both_m))} vs manual Grok {_fmt(brier(both_g))}"]


def spend_lines(start: str, end: str) -> list[str]:
    a, b = clock._at(start, "00:00"), clock._at(end, "00:00") + timedelta(days=1)
    rows = _q("""select model, count(*) as runs, coalesce(sum(cost_usd), 0) as cost from gap_llm_runs
                 where model like :m and created_at >= :a and created_at < :b group by model order by cost desc""",
              m="lab:%", a=a, b=b)
    total = sum(float(r["cost"] or 0) for r in rows)
    lines = [f"LAB SPEND {start} .. {end}: ${total:.2f} (daily limit ${C.LAB_DAILY_BUDGET_USD:.2f})"]
    lines += [f"- {r['model'][4:]}: {r['runs']} calls, ${float(r['cost'] or 0):.2f}" for r in rows]
    return lines


def status_text() -> str:
    if not enabled():
        return "prompt lab is off (LAB_ON is false, or no Postgres database)"
    champ = bootstrap()
    q = _q("select status, count(*) as n from gap_lab_runs group by status")
    counts = {r["status"]: r["n"] for r in q}
    today = clock.today_ct()
    row = _night(today)
    lines = [f"champion: {champ['prompt_id'] if champ else '-'} ({(champ or {}).get('name') or '-'}) since "
             f"{_d(champ['champion_from']) if champ and champ.get('champion_from') else '-'}",
             "models: " + (", ".join(short(k) + (" (judge)" if i == 0 else "") for i, k in enumerate(model_keys()))
                           or "none with a key") + f"  |  writer: {C.LAB_WRITER_MODEL}",
             f"tonight ({today}): {row.get('stage') or 'not started'}" + (f" / {row.get('decision')}" if row.get("decision") else ""),
             f"queue: {counts.get('pending', 0)} waiting, {counts.get('running', 0)} running, {counts.get('done', 0)} done, "
             f"{counts.get('failed', 0)} failed",
             f"lab spend today: ${spent_today():.2f} of ${C.LAB_DAILY_BUDGET_USD:.2f}", ""]
    lines += leaderboard_lines()
    start = (date.fromisoformat(today) - timedelta(days=27)).isoformat()
    lines += [""] + live_lines(start, today)
    lines += ["", "/gap_lab_models = every model on the champion prompt   /gap_lab_forecast = tonight word by word"]
    return "\n".join(lines)


def weekly_lines(start: str, end: str) -> list[str]:
    if not enabled():
        return ["Prompt lab is off."]
    lines = leaderboard_lines()
    lines += [""] + model_board_lines()
    lines += [""] + live_lines(start, end)
    lines += [""] + spend_lines(start, end)
    logrows = [r for r in (store.get_state("lab_champion_log", []) or []) if str(r.get("at", ""))[:10] >= start]
    lines += ["", "CHAMPION CHANGES THIS WEEK:"]
    lines += [f"- {str(r.get('at'))[:16]}: {r.get('from')} -> {r.get('to')}"
              + (" (set by hand)" if r.get("manual") else f" ({r.get('name')}, diff {r.get('diff'):+.3f} on {r.get('words')} words)")
              for r in logrows] or ["- none"]
    nights = _q("""select event_date, decision, writer_model, detail from gap_lab_nights
                   where event_date between cast(:a as date) and cast(:b as date) order by event_date""", a=start, b=end)
    lines += ["", "NIGHTS: date | writer | variants ok / thrown away | decision"]
    for n in nights:
        vs = _detail(n).get("variants") or []
        ok = sum(1 for v in vs if v.get("ok"))
        lines.append(f"{_d(n['event_date'])} | {n.get('writer_model') or '-'} | {ok} / {len(vs) - ok} | {n.get('decision') or 'in progress'}")
    return lines
