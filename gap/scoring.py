"""The general scorer (v1.10.0): every forecast vs what actually happened. Strategy-free.

For every night, every forecaster (Grok + each challenger model), and every prompt version, it asks
one question: "you said X% -- was the word said?" and scores the answers. It does not know or care
about Book L, no-fade or nolive. Any strategy can use the same numbers.

Scores (all on the 1-99 scale the forecasters use, turned into 0.01-0.99):
  Brier      average of (p - outcome)^2. 0 = perfect, 0.25 = always saying 50%. Lower is better.
  log loss   average of -ln(p) when said, -ln(1-p) when not. Punishes confident misses much harder
             than Brier. A forecaster that says 2% on a word that gets said pays a lot.
  calib      average forecast minus how often words were actually said. +10 = 10 points too high.
  extreme    share of forecasts at 5 or below / 95 or above. Watched because a binary outcome can
             tempt prompt selection toward all-or-nothing numbers; we want numbers spread over 1-99.
  sharpness  average distance from 50. 0 = always 50 (useless), higher = more decisive.

Both Brier and log loss are "proper" scores: a forecaster gets its best expected score only by
saying what it really believes, so pushing numbers to 1/99 does NOT pay on average. It can look good
by luck on a few nights, which is why "extreme" is reported and why small samples are flagged.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import clock, config as C, store

GROK = "grok"


def _clip(p: float) -> float:
    return min(max(p, 0.01), 0.99)


def collect(start: str, end: str) -> list[dict]:
    """All forecasts in the date range: [{date, ticker, word, model, prompt, p}] with p in 0..1."""
    rows = []
    for f in store.grok_forecasts_between(start, end):
        rows.append({"date": str(f["event_date"])[:10], "ticker": f["market_ticker"], "word": f["word"],
                     "model": GROK, "prompt": f.get("prompt_version") or "?", "p": f["probability"] / 100.0})
    for r in store.shadow_forecasts(start, end):
        if not r.get("market_ticker"):
            continue
        rows.append({"date": str(r["event_date"])[:10], "ticker": r["market_ticker"], "word": r["word"],
                     "model": r["model"], "prompt": r.get("prompt_version") or "?", "p": r["probability"] / 100.0})
    return rows


def outcomes(tickers: list[str], fetch: bool = True) -> dict[str, str]:
    """{ticker: 'yes'/'no'} for settled words. Uses the permanent result cache; with fetch=True the
    missing ones are asked from Kalshi (public data) and cached."""
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    if fetch:
        from . import results
        got, _err = results.results_for(tickers, budget_s=60)
    else:
        got = store.official_results(tickers)
    return {t: r for t, r in got.items() if r in ("yes", "no")}


def stats(pairs: list[tuple[float, float]]) -> dict:
    """pairs = [(p 0..1, outcome 0/1)]."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    logl = -sum(math.log(_clip(p)) if y else math.log(1 - _clip(p)) for p, y in pairs) / n
    said = sum(y for _p, y in pairs) / n
    mean_p = sum(p for p, _y in pairs) / n
    extreme = sum(1 for p, _y in pairs if p <= 0.05 or p >= 0.95) / n
    sharp = sum(abs(p - 0.5) for p, _y in pairs) / n
    return {"n": n, "brier": brier, "logloss": logl, "said": said, "mean_p": mean_p,
            "calib": mean_p - said, "extreme": extreme, "sharp": sharp}


def board(rows: list[dict], result: dict[str, str]) -> list[dict]:
    """One line per (model, prompt): its stats on the words it answered, plus Grok's Brier on the
    SAME words so every comparison is like for like. Sorted best Brier first."""
    grok_p = {(r["date"], r["ticker"]): r["p"] for r in rows if r["model"] == GROK}
    groups: dict = {}
    for r in rows:
        y = result.get(r["ticker"])
        if y is None:
            continue
        groups.setdefault((r["model"], r["prompt"]), []).append((r["p"], 1.0 if y == "yes" else 0.0,
                                                                  grok_p.get((r["date"], r["ticker"]))))
    out = []
    for (model, prompt), trip in groups.items():
        st = stats([(p, y) for p, y, _g in trip])
        both = [(g, y) for _p, y, g in trip if g is not None]
        st.update(model=model, prompt=prompt,
                  grok_same=stats(both).get("brier") if model != GROK else None)
        out.append(st)
    nights = {}
    for r in rows:
        if result.get(r["ticker"]) is not None:
            nights.setdefault((r["model"], r["prompt"]), set()).add(r["date"])
    for st in out:
        st["nights"] = len(nights.get((st["model"], st["prompt"]), ()))
    return sorted(out, key=lambda s: s["brier"])


def _name(model: str) -> str:
    if model == GROK:
        return "Grok"
    from .shadow import short_name
    return short_name(model)


def report_lines(start: str, end: str, title: str, fetch: bool = True, per_word: bool = False) -> list[str]:
    rows = collect(start, end)
    if not rows:
        return [f"{title}: no forecasts stored for {start}" + ("" if start == end else f" .. {end}")]
    result = outcomes([r["ticker"] for r in rows], fetch=fetch)
    words_total = len({(r["date"], r["ticker"]) for r in rows})
    settled = len({(r["date"], r["ticker"]) for r in rows if r["ticker"] in result})
    lines = [f"{title} ({start}" + ("" if start == end else f" .. {end}") + ")",
             f"settled words: {settled} of {words_total}"
             + ("" if settled == words_total else "  (the rest are not settled yet)")]
    if not settled:
        return lines
    said = sum(1 for (d, t) in {(r["date"], r["ticker"]) for r in rows} if result.get(t) == "yes")
    lines.append(f"said: {said} of {settled} ({100 * said / settled:.0f}%)")
    lines.append("")
    lines.append("Brier: lower is better, 0.25 = always 50%. Grok-same = Grok's Brier on the same words.")
    lines.append("calib: + means too high. extreme: share of forecasts at <=5 or >=95.")
    lines.append("model | prompt | words | nights | Brier | Grok-same | log loss | calib | extreme")
    for s in board(rows, result):
        gs = "-" if s.get("grok_same") is None else f"{s['grok_same']:.3f}"
        lines.append(f"{_name(s['model'])} | {s['prompt']} | {s['n']} | {s['nights']} | {s['brier']:.3f} | {gs} | "
                     f"{s['logloss']:.2f} | {100 * s['calib']:+.0f} | {100 * s['extreme']:.0f}%")
    small = min((s["n"] for s in board(rows, result)), default=0)
    if small < 50:
        lines.append(f"Small sample: the smallest line has {small} words. Treat differences under ~0.03 Brier as noise.")
    if per_word:
        lines += ["", "word | said? | " + " | ".join(_name(m) for m in sorted({r["model"] for r in rows}, key=lambda m: (m != GROK, m)))]
        models = sorted({r["model"] for r in rows}, key=lambda m: (m != GROK, m))
        cell = {(r["date"], r["ticker"], r["model"]): round(100 * r["p"]) for r in rows}
        for d, t, w in sorted({(r["date"], r["ticker"], r["word"]) for r in rows}):
            y = result.get(t)
            lines.append(f"{w} | {'YES' if y == 'yes' else ('no' if y == 'no' else '?')} | "
                         + " | ".join(str(cell.get((d, t, m), "-")) for m in models))
    return lines


# ---------- nightly scorecard (sent once per night after settlement) ----------

def _key(date_str: str) -> str:
    return f"scorecard_sent:{date_str}"


def scorecard_if_due(send) -> str:
    """Called from the poll loop. After SCORECARD_AFTER_CT, once every word of tonight has an
    official result (or at SCORECARD_LATEST_CT with whatever is settled), send tonight's scorecard
    once. Never touches orders."""
    if not C.SCORECARD_ON or not clock.weekday_ct():
        return "off"
    d = clock.today_ct()
    now = clock.now_ct()
    if now < clock._at(d, C.SCORECARD_AFTER_CT) or store.get_state(_key(d)):
        return "not_due"
    rows = collect(d, d)
    if not rows:
        return "no_forecasts"
    tickers = sorted({r["ticker"] for r in rows})
    result = outcomes(tickers, fetch=True)
    if len(result) < len(tickers) and now < clock._at(d, C.SCORECARD_LATEST_CT):
        return "waiting_results"
    lines = report_lines(d, d, "SCORECARD tonight", fetch=False, per_word=True)
    start = (date.fromisoformat(d) - timedelta(days=27)).isoformat()
    lines += ["", ""] + report_lines(start, d, "LAST 4 WEEKS", fetch=False)
    send("\n".join(lines))
    store.set_state(_key(d), {"sent_at": now.isoformat(), "settled": len(result), "words": len(tickers)})
    return "sent"
