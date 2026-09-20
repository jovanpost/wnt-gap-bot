"""WORD HISTORY block for Grok's user message.

For tonight's words: was it said (Y), not said (N), or not on the list (-) on each of the
last N nights. Built ONLY from official Kalshi results. No prices, no forecasts, and
nothing from tonight (strictly earlier dates), so it cannot leak the answer or the market.
"""
from __future__ import annotations

import logging

from . import results, store

log = logging.getLogger("gap.history")


def _norm(word: str) -> str:
    return " ".join(str(word or "").lower().split())


def word_history_block(event_date: str, words: list[dict], nights: int = 10,
                       client=None, budget_s: int = 45) -> str:
    if nights <= 0 or not words:
        return ""
    rows = store.markets_history(event_date, n_dates=nights)
    if not rows:
        return ""
    dates = sorted({r["event_date"] for r in rows}, reverse=True)[:nights]  # newest first
    tickers = [r["market_ticker"] for r in rows if r["event_date"] in dates]
    res, _errors = results.results_for(tickers, client=client, budget_s=budget_s)
    ticker_by = {(r["event_date"], _norm(r["word"])): r["market_ticker"] for r in rows}

    def sym(date, word):
        t = ticker_by.get((date, _norm(word)))
        if t is None:
            return "-"
        r = res.get(t)
        return "Y" if r == "yes" else ("N" if r == "no" else "?")

    lines = [
        "",
        f"WORD HISTORY (official Kalshi results, newest night first, last {len(dates)} nights)",
        "Y = said, N = not said, - = word was not on that night's list, ? = result not available.",
        "Nights: " + " ".join(d[5:] for d in dates),
    ]
    fresh = []
    n_listed = 0
    for w in words:
        syms = [sym(d, w["word"]) for d in dates]
        y, n = syms.count("Y"), syms.count("N")
        if not (y + n):
            fresh.append(w["word"])
            continue
        n_listed += 1
        lines.append(f"- {w['word']}: {' '.join(syms)}   (said {y} of {y + n} listed nights)")
    if not n_listed:
        return ""
    if fresh:
        lines.append("No history yet for: " + ", ".join(fresh))
    return "\n".join(lines)
