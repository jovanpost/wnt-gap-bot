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


def gemini_forecast(paste: str, words: list[str], event_date: str) -> dict:
    """{"model", "forecasts": [...], "seconds"}. Tries candidate models in order; a model the free
    tier refuses (429/403/404) just moves on to the next one."""
    if not C.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
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

    errors = []
    for model in gemini_candidates()[:4]:
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        t0 = time.monotonic()
        try:
            with netlimit.ticket(url):
                r = requests.post(url, headers=_gemini_headers(), data=json.dumps(body), timeout=C.GEMINI_TIMEOUT_S)
        except requests.Timeout:
            errors.append(f"{model}: timeout")
            continue
        if r.status_code in (403, 404, 429, 500, 503):
            msg = ""
            try:
                msg = (r.json().get("error") or {}).get("status", "")
            except Exception:  # noqa: BLE001
                pass
            errors.append(f"{model}: HTTP {r.status_code} {msg}".strip())
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{model}: HTTP {r.status_code}")
        txt = _gemini_text(r.json())
        try:
            data = parser.validate(txt, words, event_date)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}: bad JSON ({exc})")
            continue
        return {"model": f"gemini:{model}", "forecasts": data["forecasts"], "seconds": round(time.monotonic() - t0, 1)}
    raise RuntimeError("Gemini failed: " + "; ".join(errors or ["no models available"]))


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
        save: bool = True, force: bool = False) -> dict:
    """Run every challenger once for the night. Returns {model: {"ok", "n"|"error", "forecasts"}}.
    With save=False nothing is written (used by the Mac test script without a database)."""
    report: dict = {}
    if not C.SHADOW_ON:
        return report
    by_word = {w["word"]: w for w in words}
    done = store.shadow_models_for(event_date) if (save and not force) else set()
    news = abcfeeds.news_data(words)          # reuses the Grok file's news if fresh

    def keep(model: str, rows: list[dict], seconds=None, raw=None):
        stored = [{
            "event_date": event_date, "event_ticker": event_ticker,
            "market_ticker": by_word.get(r["word"], {}).get("market_ticker"),
            "word": r["word"], "model": model, "probability": r["probability"],
            "reasoning": str(r.get("reasoning") or "")[:2000],
            "raw": json.dumps(r, default=str)[:8000] if raw is None else raw, "seconds": seconds,
        } for r in rows]
        n = store.insert_shadow_forecasts(stored) if save else len(stored)
        report[model] = {"ok": True, "n": n, "forecasts": {r["word"]: r["probability"] for r in rows}, "seconds": seconds}

    if C.BASELINE_ON and (force or BASELINE not in done):
        try:
            keep(BASELINE, baseline_forecast(words, news, history_block or paste))  # paste carries WORD HISTORY
        except Exception as exc:  # noqa: BLE001
            log.exception("baseline failed")
            report[BASELINE] = {"ok": False, "error": type(exc).__name__}

    if C.GEMINI_API_KEY and (force or not any(m.startswith("gemini:") for m in done)):
        try:
            g = gemini_forecast(paste, [w["word"] for w in words], event_date)
            keep(g["model"], g["forecasts"], g["seconds"])
        except Exception as exc:  # noqa: BLE001
            log.warning("gemini failed: %s", exc)
            report["gemini"] = {"ok": False, "error": str(exc)[:300]}
    elif not C.GEMINI_API_KEY:
        report["gemini"] = {"ok": False, "error": "GEMINI_API_KEY not set"}
    return report


def summary_text(event_date: str, words: list[dict], report: dict, grok: dict[str, int] | None = None) -> str:
    """Plain-text table for Telegram / the terminal: word | Grok | each challenger."""
    models = [m for m, r in report.items() if r.get("ok")]
    head = ["word", "Grok"] + [("Gemini" if m.startswith("gemini:") else "base") for m in models]
    lines = [f"CHALLENGERS {event_date} (paper only, never traded)"]
    for m, r in report.items():
        if r.get("ok"):
            lines.append(f"- {m}: {r['n']} words" + (f", {r['seconds']}s" if r.get("seconds") else ""))
        else:
            lines.append(f"- {m}: FAILED ({r.get('error')})")
    lines.append("")
    lines.append(" | ".join(head))
    for w in words:
        word = w["word"]
        cells = [word, str((grok or {}).get(word, "-"))]
        cells += [str(report[m]["forecasts"].get(word, "-")) for m in models]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def run_async(event_date: str, event_ticker: str, words: list[dict], paste: str, history_block: str = "",
              notify_fn=None, force: bool = False) -> bool:
    """Start run() in a background thread so it never delays the bot. One at a time."""
    if not C.SHADOW_ON:
        return False

    def go():
        if not _running.acquire(blocking=False):
            return
        try:
            rep = run(event_date, event_ticker, words, paste, history_block, save=True, force=force)
            if notify_fn and rep:
                grok = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(event_date)}
                notify_fn(summary_text(event_date, words, rep, grok))
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
    grok = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(event_date)}
    return summary_text(event_date, words, rep, grok)
