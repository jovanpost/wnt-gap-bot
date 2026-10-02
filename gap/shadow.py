"""Challenger forecasters: PAPER ONLY, never traded (v1.8.0).

Right after the Grok file goes out, the bot asks two challengers for the same words, with the
same news (ABC feeds + Google News + word history), and stores their numbers in
gap_shadow_forecasts. Every Saturday they are scored against the official results next to Grok.

  gemini:<model>  Google Gemini through the API (GEMINI_API_KEY). It reads the same Grok file,
                  with a short note in front: it has no web/X tools (unless GEMINI_SEARCH), so the
                  news blocks in the file ARE its research.
  baseline-v1     no AI at all: a fixed formula on word history + ABC title hit + Google headline
                  count. The yardstick: an AI that can't beat it adds nothing.

Nothing here can place, change or cancel an order. Every failure is caught and logged.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

import requests

from . import abcfeeds, config as C, headlines, netlimit, parser, store

log = logging.getLogger("gap.shadow")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
BASELINE = "baseline-v1"
_running = threading.Lock()

PREFACE = (
    "You are a CHALLENGER forecaster, scored against another model on the same words. "
    "{tools} Where the instructions below tell you to run a search you cannot run, skip that step and use "
    "the ABC NEWS FEEDS, GOOGLE NEWS HEADLINES and WORD HISTORY blocks in the message as your research. "
    "Start every reasoning with 'Blind: ' and name the items you used. Everything else (rules, calibration, "
    "schema) applies unchanged. Output the JSON object only."
)


# ---------------------------------------------------------------- Gemini

def _gemini_headers() -> dict:
    return {"x-goog-api-key": C.GEMINI_API_KEY, "Content-Type": "application/json"}


_VER_RE = re.compile(r"gemini-(\d+(?:\.\d+)?)")


def _flash_rank(name: str) -> tuple:
    m = _VER_RE.search(name)
    ver = float(m.group(1)) if m else 0.0
    lite = "lite" in name
    preview = "preview" in name or "exp" in name
    return (not lite, ver, not preview)


def gemini_candidates(timeout: float = 20) -> list[str]:
    """Model ids to try, best first. GEMINI_MODEL wins if set; 'auto' lists the key's models and
    keeps text Flash models (newest first, Lite last)."""
    if C.GEMINI_MODEL and C.GEMINI_MODEL.lower() != "auto":
        return [C.GEMINI_MODEL.replace("models/", "")]
    url = f"{GEMINI_BASE}/models?pageSize=200"
    with netlimit.ticket(url):
        r = requests.get(url, headers=_gemini_headers(), timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini model list: HTTP {r.status_code}")
    names = []
    for m in r.json().get("models", []):
        name = (m.get("name") or "").replace("models/", "")
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods or "flash" not in name:
            continue
        if any(x in name for x in ("live", "image", "tts", "audio", "embed", "native", "thinking-exp", "robotics")):
            continue
        names.append(name)
    return sorted(set(names), key=_flash_rank, reverse=True)


def _gemini_text(resp: dict) -> str:
    cands = resp.get("candidates") or []
    if not cands:
        why = (resp.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise RuntimeError(f"Gemini returned nothing ({why})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


RETRYABLE = {429, 500, 502, 503, 504}
BACKOFF_S = (30, 60, 120, 180, 300)          # wait after try 1, 2, 3, 4, then 300s each time


def _err_status(r) -> str:
    try:
        return (r.json().get("error") or {}).get("status", "")
    except Exception:  # noqa: BLE001
        return ""


def gemini_forecast(paste: str, words: list[str], event_date: str, *, budget_s: float | None = None,
                    sleep=None, now=None, on_attempt=None) -> dict:
    """{"model", "forecasts", "seconds", "attempts", "waited_s"}.

    v1.8.1 retry rule (Jovan, Oct 1): the SAME request (same file text, no new news fetch) is sent
    again and again while Google says it is busy (429/5xx), timeouts or bad JSON -- waiting 30s,
    60s, 120s, 180s, then 300s between tries -- for up to GEMINI_RETRY_BUDGET_S (30 min).
    It stays on the best model; only after GEMINI_DOWNGRADE_AFTER_S (20 min) of failures does it
    step down one model per failed try. A model the key cannot use at all (403/404) is skipped at once."""
    if not C.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    sleep = sleep or time.sleep
    now = now or time.monotonic
    budget = C.GEMINI_RETRY_BUDGET_S if budget_s is None else budget_s
    say = on_attempt or (lambda msg: log.info("gemini: %s", msg))
    tools = ("You may use Google Search." if C.GEMINI_SEARCH
             else "You have NO web, X or browsing tools in this run.")
    body = {
        "system_instruction": {"parts": [{"text": PREFACE.format(tools=tools)}]},
        "contents": [{"role": "user", "parts": [{"text": paste}]}],
        "generationConfig": {"temperature": 0.3},
    }
    if C.GEMINI_SEARCH:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"
    payload = json.dumps(body)                  # built ONCE: every retry sends exactly this

    start = now()
    cands: list[str] = []
    level = 0
    attempt = 0
    waited = 0.0
    last = ""
    while True:
        attempt += 1
        err = None
        model = None
        t0 = now()
        try:
            if not cands:
                cands = gemini_candidates()
                if not cands:
                    raise RuntimeError("no Gemini Flash model available to this key")
            model = cands[min(level, len(cands) - 1)]
            url = f"{GEMINI_BASE}/models/{model}:generateContent"
            with netlimit.ticket(url):
                r = requests.post(url, headers=_gemini_headers(), data=payload, timeout=C.GEMINI_TIMEOUT_S)
            if r.status_code == 200:
                try:
                    data = parser.validate(_gemini_text(r.json()), words, event_date)
                    return {"model": f"gemini:{model}", "forecasts": data["forecasts"],
                            "seconds": round(now() - t0, 1), "attempts": attempt, "waited_s": round(waited)}
                except Exception as exc:  # noqa: BLE001
                    err = f"bad answer ({str(exc)[:120]})"
            elif r.status_code in (403, 404):
                cands.remove(model)             # this key cannot use it: skip, no waiting
                level = min(level, max(len(cands) - 1, 0))
                last = f"{model}: HTTP {r.status_code} {_err_status(r)}".strip()
                say(f"try {attempt}: {last} -> skipping that model")
                if not cands:
                    raise RuntimeError(f"Gemini failed: no usable model ({last})")
                continue
            elif r.status_code in RETRYABLE:
                err = f"HTTP {r.status_code} {_err_status(r)}".strip()
            else:
                raise RuntimeError(f"Gemini failed: {model}: HTTP {r.status_code} {_err_status(r)}".strip())
        except requests.Timeout:
            err = "timeout"
        except requests.RequestException as exc:
            err = type(exc).__name__
        except RuntimeError as exc:
            if str(exc).startswith("Gemini failed"):
                raise
            m = re.search(r"model list: HTTP (\d+)", str(exc))
            if not m or int(m.group(1)) not in RETRYABLE:
                raise RuntimeError(f"Gemini failed: {exc}") from exc   # e.g. a wrong key: no point waiting
            err = str(exc)                      # model list busy: retry it like any busy answer

        last = f"{model or 'model list'}: {err}"
        elapsed = now() - start
        delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        if elapsed + delay > budget:
            raise RuntimeError(f"Gemini failed: gave up after {attempt} tries in {elapsed / 60:.0f} min "
                               f"(last: {last})")
        if cands and elapsed >= C.GEMINI_DOWNGRADE_AFTER_S and level < len(cands) - 1:
            level += 1
            say(f"try {attempt}: {last}; over {C.GEMINI_DOWNGRADE_AFTER_S // 60:.0f} min of failures -> "
                f"next try uses {cands[level]}")
        say(f"try {attempt}: {last}; same request again in {delay}s")
        sleep(delay)
        waited += delay


# ---------------------------------------------------------------- no-AI baseline

_HIST_RE = re.compile(r"^- (?P<word>.+?): .*\(said (?P<y>\d+) of (?P<n>\d+) listed nights\)\s*$")
_COUNT_RE = re.compile(r"\(\s*\d+\s*\+")


def history_counts(history_block: str) -> dict[str, tuple[int, int]]:
    """{word: (said, listed)} parsed from the WORD HISTORY block the Grok file carries."""
    out = {}
    for line in (history_block or "").splitlines():
        m = _HIST_RE.match(line.strip())
        if m:
            out[m.group("word")] = (int(m.group("y")), int(m.group("n")))
    return out


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def baseline_prob(word: str, hist: tuple[int, int] | None, abc_tag: str | None, google_hits: int) -> int:
    """Hand-set v1 formula (no fitting yet -- Saturday data will set real weights):
    start = word's said-rate over recent nights (smoothed), else 40%;
    ABC headline has the word +1.0 logit, ABC summary only +0.3, not at ABC -0.4;
    Google headlines with the word in the title: +0.15 per headline above 3 (0..6);
    count words like 'Trump (5+ times)' -0.5."""
    if hist and hist[1] >= 3:
        p = (hist[0] + 1) / (hist[1] + 2)
    else:
        p = 0.40
    x = _logit(p)
    x += {"title": 1.0, "summary": 0.3}.get(abc_tag or "", -0.4)
    x += 0.15 * (min(google_hits, 6) - 3)
    if _COUNT_RE.search(word):
        x -= 0.5
    prob = 1 / (1 + math.exp(-x))
    return int(round(min(max(prob * 100, 2), 97)))


def baseline_forecast(words: list[dict], news: dict, history_block: str) -> list[dict]:
    hist = history_counts(history_block)
    g_out, a_out = news.get("google"), news.get("abc")
    g_results = g_out[1] if g_out else {}
    feeds, got, _err = a_out if a_out else ([], {}, {})
    labels = {key: label for key, label, *_ in feeds}
    gabc = abcfeeds.google_abc_items(g_results)
    out = []
    for w in words:
        word = w["word"]
        terms = headlines.search_terms(word)
        pats = [abcfeeds.term_pattern(t) for t in terms]
        hits = abcfeeds.word_matches(word, got, labels, gabc)
        tag = "title" if any(h[2] == "title" for h in hits) else ("summary" if hits else None)
        g = 0
        for t in terms:
            items = (g_results.get(t) or ([], None))[0] or []
            g = max(g, sum(1 for it in items if any(p.search(it.get("title", "")) for p in pats)))
        h = hist.get(word)
        prob = baseline_prob(word, h, tag, g)
        why = (f"history {h[0]}/{h[1]}" if h else "no history") + f"; ABC {tag or 'none'}; Google title hits {g}"
        out.append({"word": word, "probability": prob, "reasoning": why})
    return out


# ---------------------------------------------------------------- run + store

def run(event_date: str, event_ticker: str, words: list[dict], paste: str, history_block: str = "",
        save: bool = True, force: bool = False, on_attempt=None, package_id: int | None = None) -> dict:
    """Run every challenger once for the night. Returns {model: {"ok", "n"|"error", "forecasts"}}.
    With save=False nothing is written (used by the Mac test script without a database)."""
    report: dict = {}
    if not C.SHADOW_ON:
        return report
    by_word = {w["word"]: w for w in words}
    done = store.shadow_models_for(event_date) if (save and not force) else set()

    def keep(model: str, rows: list[dict], seconds=None, raw=None):
        stored = [{
            "event_date": event_date, "event_ticker": event_ticker,
            "market_ticker": by_word.get(r["word"], {}).get("market_ticker"),
            "word": r["word"], "model": model, "probability": r["probability"],
            "reasoning": str(r.get("reasoning") or "")[:2000],
            "raw": json.dumps(r, default=str)[:8000] if raw is None else raw, "seconds": seconds,
            "prompt_version": C.PROMPT_VERSION, "package_id": package_id,
        } for r in rows]
        n = store.insert_shadow_forecasts(stored) if save else len(stored)
        report[model] = {"ok": True, "n": n, "forecasts": {r["word"]: r["probability"] for r in rows}, "seconds": seconds}

    if C.BASELINE_ON and (force or BASELINE not in done):
        try:
            news = abcfeeds.news_data(words)   # reuses the Grok file's news if fresh; Gemini never fetches news
            keep(BASELINE, baseline_forecast(words, news, history_block or paste))  # paste carries WORD HISTORY
        except Exception as exc:  # noqa: BLE001
            log.exception("baseline failed")
            report[BASELINE] = {"ok": False, "error": type(exc).__name__}

    # Gemini and every other challenger run SIDE BY SIDE (different services), each with its own
    # 30-minute patience. All of them send the same file text; none fetches news.
    jobs = []
    if C.GEMINI_API_KEY and (force or not any(m.startswith("gemini:") for m in done)):
        jobs.append(("gemini", None))
    elif not C.GEMINI_API_KEY:
        report["gemini"] = {"ok": False, "error": "GEMINI_API_KEY not set"}
    from . import challengers as CH, xai
    for prov, wanted in CH.enabled_specs():
        jobs.append((prov, wanted))
    for mode in xai.enabled_modes():                      # v1.11.0: Grok through the API, plain + search
        if force or xai.label(mode) not in done:
            jobs.append(("xai", mode))
    word_list = [w["word"] for w in words]

    def usage_row(model, usage, ok, seconds=None, attempts=None, detail=None):
        if save and usage:
            try:
                store.record_llm_run(event_date, model, usage, ok=ok, seconds=seconds, attempts=attempts,
                                     prompt_version=C.PROMPT_VERSION, package_id=package_id, detail=detail)
            except Exception:  # noqa: BLE001  cost bookkeeping must never lose a forecast
                log.exception("usage row not saved")

    def one(job):
        prov, wanted = job
        tag = "gemini" if prov == "gemini" else f"{prov}:{wanted}"
        try:
            if prov == "gemini":
                g = gemini_forecast(paste, word_list, event_date, on_attempt=on_attempt)
            elif prov == "xai":
                tag = xai.label(wanted)
                if wanted == "expert" and save:
                    spent = store.llm_spend(event_date, "xai:")
                    if spent >= C.XAI_NIGHTLY_BUDGET_USD:
                        raise RuntimeError(f"not started: tonight's xAI spend is already ${spent:.2f} "
                                           f"(limit ${C.XAI_NIGHTLY_BUDGET_USD:.2f}, XAI_NIGHTLY_BUDGET_USD)")
                g = xai.forecast(wanted, paste, word_list, event_date, PREFACE, on_attempt=on_attempt)
            else:
                t_start = time.monotonic()
                models = CH.resolve_with_retry(prov, wanted, budget_s=C.CHALLENGER_RETRY_BUDGET_S, on_attempt=on_attempt)
                model = models[0]
                if not force and any(f"{prov}:{m}" in done for m in models):
                    return
                left = max(C.CHALLENGER_RETRY_BUDGET_S - (time.monotonic() - t_start), 0)
                g = CH.forecast(prov, model, paste, word_list, event_date, PREFACE, budget_s=left, on_attempt=on_attempt,
                                fallbacks=models[1:] if prov == "openrouter" else None)
            with _report_lock:
                keep(g["model"], g["forecasts"], g["seconds"])
                report[g["model"]].update(attempts=g["attempts"], waited_s=g["waited_s"], usage=g.get("usage"))
            usage_row(g["model"], g.get("usage"), True, g["seconds"], g["attempts"])
            if on_attempt:
                cost = xai.cost_line(g.get("usage"))
                on_attempt(f"done: {g['model']}, {len(g['forecasts'])} words in {g['seconds']}s "
                           f"({g['attempts']} tr{'y' if g['attempts'] == 1 else 'ies'})"
                           + (f" | {cost}" if cost else "") + " -- saved")
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", tag, exc)
            paid = getattr(exc, "usage", None)            # a failed xAI run may still have cost money
            usage_row(tag, paid, False, detail=str(exc)[:300])
            with _report_lock:
                report[tag] = {"ok": False, "error": str(exc)[:300], "usage": paid}
            if on_attempt:
                on_attempt(f"stopped: {tag}: {str(exc)[:200]}"
                           + (f" | spent {xai.cost_line(paid)}" if paid and xai.cost_line(paid) else ""))

    if jobs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(jobs), 12)) as pool:
            list(pool.map(one, jobs))
    return report


_report_lock = threading.Lock()


def short_name(model: str) -> str:
    """Column name for a stored model id: 'gemini:gemini-3.8-flash' -> 'Gemini',
    'nvidia:moonshotai/kimi-k3' -> 'kimi', 'cerebras:gpt-oss-120b' -> 'gpt-oss', baseline -> 'base'."""
    if model == BASELINE:
        return "base"
    if model.startswith("gemini:"):
        return "Gemini"
    if model.startswith("xai:"):
        return "grokWeb" if model.endswith("+search") else "grokAPI"
    prov, _, mid = model.partition(":")
    name = mid.split("/")[-1].split(":")[0].lower()
    if name.startswith("gpt-oss"):
        return "gpt-oss"
    head = re.split(r"[-_.]", name)[0]
    head = re.sub(r"\d+$", "", head) or head
    return head[:10]


def summary_text(event_date: str, words: list[dict], report: dict, grok: dict[str, int] | None = None) -> str:
    """Plain-text table for Telegram / the terminal: word | Grok | each challenger."""
    models = [m for m, r in report.items() if r.get("ok")]
    head = ["word", "Grok"] + [short_name(m) for m in models]
    lines = [f"CHALLENGERS {event_date} (paper only, never traded)"]
    for m, r in report.items():
        if r.get("ok"):
            extra = f", {r['seconds']}s" if r.get("seconds") else ""
            if r.get("attempts", 1) > 1:
                extra += f", {r['attempts']} tries, waited {r['waited_s'] // 60:.0f} min {r['waited_s'] % 60:.0f}s"
            cost = _cost(r.get("usage"))
            lines.append(f"- {m}: {r['n']} words{extra}" + (f" | {cost}" if cost else ""))
        else:
            cost = _cost(r.get("usage"))
            lines.append(f"- {m}: FAILED ({r.get('error')})" + (f" | spent {cost}" if cost else ""))
    lines.append("")
    lines.append(" | ".join(head))
    for w in words:
        word = w["word"]
        cells = [word, str((grok or {}).get(word, "-"))]
        cells += [str(report[m]["forecasts"].get(word, "-")) for m in models]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _cost(usage) -> str:
    from . import xai
    return xai.cost_line(usage)


def is_running() -> bool:
    return _running.locked()


def run_async(event_date: str, event_ticker: str, words: list[dict], paste: str, history_block: str = "",
              notify_fn=None, force: bool = False, package_id: int | None = None) -> bool:
    """Start run() in a background thread so it never delays the bot. One at a time."""
    if not C.SHADOW_ON:
        return False

    def go():
        if not _running.acquire(blocking=False):
            return
        try:
            rep = run(event_date, event_ticker, words, paste, history_block, save=True, force=force,
                      package_id=package_id)
            if notify_fn:
                if any(r.get("ok") for r in rep.values()):
                    grok = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(event_date)}
                    txt = summary_text(event_date, words, rep, grok)
                    full = stored_summary(event_date, words)      # every model stored tonight, incl. earlier runs
                    notify_fn(full if full else txt)
                    fails = [f"- {m}: FAILED ({r.get('error')})" for m, r in rep.items() if not r.get("ok")]
                    if fails:
                        notify_fn("Challengers that failed tonight:\n" + "\n".join(fails))
                else:
                    notify_fn(stored_summary(event_date, words) or
                              "Challengers: nothing new tonight.\n" + "\n".join(
                                  f"- {m}: FAILED ({r.get('error')})" for m, r in rep.items()))
            store.log_activity("shadow", json.dumps({m: {k: v for k, v in r.items() if k != "forecasts"}
                                                     for m, r in rep.items()})[:900])
        except Exception:
            log.exception("shadow run failed")
        finally:
            _running.release()

    threading.Thread(target=go, name="shadow", daemon=True).start()
    return True


# ---------------------------------------------------------------- Saturday scoring

def weekly_block(start: str, end: str) -> list[str]:
    """Brier score per model on words that have an official result, side by side with Grok on the
    SAME words (lower = better; always guessing 50% scores 0.25)."""
    rows = store.shadow_forecasts(start, end)
    if not rows:
        return ["No challenger forecasts this week."]
    results = store.official_results(list({r["market_ticker"] for r in rows if r.get("market_ticker")}))
    grok_by: dict = {}
    for d in sorted({str(r["event_date"])[:10] for r in rows}):
        for f in store.grok_forecasts_for_date(d):
            grok_by[(d, f.get("market_ticker"))] = f.get("probability")

    # v1.9.0: the plain average of all AI challengers (not the baseline) as one more "model".
    avg: dict = {}
    for r in rows:
        if r["model"] != BASELINE and r.get("market_ticker"):
            avg.setdefault((str(r["event_date"])[:10], r["market_ticker"]), []).append(r["probability"])
    rows = list(rows) + [{"event_date": d, "market_ticker": t, "model": "avg-of-AI-challengers",
                          "probability": round(sum(v) / len(v))} for (d, t), v in avg.items() if len(v) >= 2]

    by_model: dict = {}
    for r in rows:
        res = results.get(r.get("market_ticker"))
        if res not in ("yes", "no"):
            continue
        y = 1.0 if res == "yes" else 0.0
        g = grok_by.get((str(r["event_date"])[:10], r.get("market_ticker")))
        by_model.setdefault(r["model"], []).append((r["probability"] / 100.0, y, None if g is None else g / 100.0))

    def brier(pairs):
        return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None

    lines = ["Brier score: lower is better; 0.25 = always saying 50%. Grok is scored on the SAME words as each challenger.",
             "model | words scored | model Brier | Grok Brier same words | winner"]
    for model, trip in sorted(by_model.items()):
        mb = brier([(p, y) for p, y, _g in trip])
        both = [(p, y, g) for p, y, g in trip if g is not None]
        gb = brier([(g, y) for _p, y, g in both])
        mb2 = brier([(p, y) for p, y, _g in both])
        win = "-" if gb is None else ("challenger" if mb2 < gb else ("Grok" if gb < mb2 else "tie"))
        lines.append(f"{model} | {len(trip)} | {mb:.3f} | {'-' if gb is None else f'{gb:.3f}'} | {win}")
    lines.append(f"Small samples: about 14 words a night. One week is not enough to pick a winner.")
    return lines


def stored_summary(event_date: str, words: list[dict]) -> str | None:
    """Summary from the database (no new model calls). None if nothing stored yet."""
    rows = store.shadow_forecasts(event_date)
    if not rows:
        return None
    rep: dict = {}
    for r in rows:
        m = rep.setdefault(r["model"], {"ok": True, "n": 0, "forecasts": {}, "seconds": r.get("seconds")})
        m["n"] += 1
        m["forecasts"][r["word"]] = r["probability"]
    try:                                               # v1.11.0: what each model cost tonight
        for run in store.llm_runs(event_date):
            if str(run["model"]).startswith("lab:"):       # v1.12.0: prompt-lab replays are not tonight's challengers
                continue
            target = rep.get(run["model"])
            if target is None:
                if run.get("ok"):
                    continue
                target = rep.setdefault(run["model"], {"ok": False, "error": (run.get("detail") or "failed")[:120]})
            u = target.setdefault("usage", {})
            for k in ("input_tokens", "output_tokens", "reasoning_tokens", "tool_calls"):
                u[k] = (u.get(k) or 0) + int(run.get(k) or 0)
            if run.get("cost_usd") is not None:
                u["cost_usd"] = (u.get("cost_usd") or 0.0) + float(run["cost_usd"])
    except Exception:  # noqa: BLE001
        log.exception("cost lines skipped")
    grok = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(event_date)}
    return summary_text(event_date, words, rep, grok)


def cost_lines(start: str, end: str, title: str = "MODEL COST") -> list[str]:
    """What the paid models cost in a date range (free models show $0.00 and their token counts if known)."""
    rows = store.llm_costs(start, end)
    if not rows:
        return [f"{title}: no model usage recorded for {start}" + ("" if start == end else f" .. {end}")]
    total = sum(float(r["cost_usd"] or 0) for r in rows)
    lines = [f"{title} ({start}" + ("" if start == end else f" .. {end}") + f"): ${total:.2f} in total",
             "model | runs (ok) | cost | per ok run | tokens in / out | tool calls | avg seconds"]
    for r in rows:
        ok = int(r["ok_runs"] or 0)
        cost = float(r["cost_usd"] or 0)
        per = f"${cost / ok:.2f}" if ok else "-"
        secs = f"{float(r['avg_seconds']):.0f}" if r.get("avg_seconds") is not None else "-"
        lines.append(f"{r['model']} | {r['runs']} ({ok}) | ${cost:.2f} | {per} | "
                     f"{int(r['input_tokens']):,} / {int(r['output_tokens']):,} | {int(r['tool_calls'])} | {secs}")
    return lines
