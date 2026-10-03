"""Helpers for scripts/replay_2x2.py: two prompts x two versions of a night's file, replayed through a
model with NO search and scored against Kalshi's official results.

Read only. Nothing here writes a table, sends a message or touches an order.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import prompt as prompt_mod, store

# Blocks the bot added to the file from Oct 1-2, 2026 on. The "old-style" file is the same night's file
# without them: date, words, WORD HISTORY and the per-word GOOGLE NEWS HEADLINES stay.
NEW_BLOCKS = ("PREVIOUS BROADCASTS (", "ABC NEWS FEEDS (", "OTHER NETWORKS AND WIRES (", "GOOGLE NEWS US TOP STORIES")
MARKS = ("WORD HISTORY (", "PREVIOUS BROADCASTS (", "ABC NEWS FEEDS (", "OTHER NETWORKS AND WIRES (",
         "GOOGLE NEWS US TOP STORIES", "GOOGLE NEWS HEADLINES (", "First do the MANDATORY RESEARCH PHASE")
SHORT = {"WORD HISTORY (": "history", "PREVIOUS BROADCASTS (": "broadcasts", "ABC NEWS FEEDS (": "ABC feeds",
         "OTHER NETWORKS AND WIRES (": "other networks", "GOOGLE NEWS US TOP STORIES": "top stories",
         "GOOGLE NEWS HEADLINES (": "Google headlines"}


def _positions(user: str) -> list[tuple[int, str]]:
    """(start, mark) of every block that begins a line, in file order."""
    out = []
    for mark in MARKS:
        m = re.search("^" + re.escape(mark), user, flags=re.M)
        if m:
            out.append((m.start(), mark))
    return sorted(out)


def blocks_in(user: str) -> list[str]:
    return [SHORT[mark] for _pos, mark in _positions(user) if mark in SHORT]


def old_style(user: str, drop: tuple = NEW_BLOCKS) -> str:
    """The same file without the blocks in `drop`. Everything else is kept byte for byte."""
    pos = _positions(user)
    if not pos:
        return user
    out = [user[:pos[0][0]]]
    for i, (start, mark) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(user)
        if mark not in drop:
            out.append(user[start:end])
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def prompt_text_for(version: str, days: int = 120) -> tuple[str | None, str]:
    """(system prompt text, where it was found) for a prompt label like gap-aba659f.
    Looks in the stored prompt versions first, then in the Grok file of the newest night that used it."""
    from sqlalchemy import text as sql
    try:
        with store.engine().connect() as conn:
            row = conn.execute(sql("select system_prompt from gap_prompt_versions where prompt_version = :v"),
                               {"v": version}).mappings().first()
        if row and (row["system_prompt"] or "").strip():
            return row["system_prompt"].strip(), "stored prompt versions"
    except Exception:  # noqa: BLE001  (an old database without the table)
        pass
    end = date.today() + timedelta(days=1)
    runs = store.runs_between((end - timedelta(days=days)).isoformat(), end.isoformat())
    for r in sorted(runs, key=lambda x: str(x["event_date"]), reverse=True):
        if r.get("prompt_version") == version and r.get("prompt_text"):
            sys_part, _user = prompt_mod.split_paste(r["prompt_text"])
            if sys_part.strip():
                return sys_part.strip(), f"the Grok file of {str(r['event_date'])[:10]}"
    return None, "not found"


def file_date(user: str) -> str | None:
    m = re.search(r"^Date: (\d{4}-\d{2}-\d{2})", user, flags=re.M)
    return m.group(1) if m else None


def brier(pairs: list[tuple[float, float]]) -> float | None:
    return (sum((p - y) ** 2 for p, y in pairs) / len(pairs)) if pairs else None


def cell_stats(forecast: dict[str, float], truth: dict[str, float], low: int = 30) -> dict:
    """forecast: word -> probability 1-99. truth: word -> 1.0 said / 0.0 not said."""
    words = [w for w in forecast if w in truth]
    pairs = [(forecast[w] / 100.0, truth[w]) for w in words]
    said = [w for w in words if truth[w] >= 0.5]
    not_said = [w for w in words if truth[w] < 0.5]
    lows = [w for w in words if forecast[w] <= low]
    return {
        "n": len(words), "said": len(said), "brier": brier(pairs),
        "avg_said": (sum(forecast[w] for w in said) / len(said)) if said else None,
        "avg_not_said": (sum(forecast[w] for w in not_said) / len(not_said)) if not_said else None,
        "low": len(lows), "low_said": sum(1 for w in lows if truth[w] >= 0.5),
    }


def mean_forecast(runs: list[dict[str, float]]) -> dict[str, float]:
    """Average several runs of one cell, word by word (words missing from a run are skipped for that run)."""
    out: dict[str, float] = {}
    for w in {w for r in runs for w in r}:
        vals = [r[w] for r in runs if w in r]
        out[w] = sum(vals) / len(vals)
    return out


def pooled(cells: list[tuple[dict[str, float], dict[str, float]]], low: int = 30) -> dict:
    """One line for a cell over several nights: every word of every night counts once."""
    f, t = {}, {}
    for i, (forecast, truth) in enumerate(cells):
        for w, p in forecast.items():
            f[f"{i}:{w}"] = p
        for w, y in truth.items():
            t[f"{i}:{w}"] = y
    return cell_stats(f, t, low)


def fmt(v, spec: str = ".3f") -> str:
    return "-" if v is None else format(v, spec)
