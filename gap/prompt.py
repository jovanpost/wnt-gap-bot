"""System prompt loader.

The prompt text is NOT in this file any more. It lives in prompts/system_prompt.txt
so you can replace it on GitHub (pencil -> select all -> paste -> commit) and see
every past version in the file's History.

The file is read fresh each time a Grok file is built, so nothing needs restarting.
If the file is missing or nearly empty the bot refuses to build a file (it will NOT
quietly fall back to some older prompt) and sends you a Telegram alert.
"""
from __future__ import annotations

import logging
import time

from . import config as C, history, notify

log = logging.getLogger("gap.prompt")

_ALERT = {"at": 0.0}


def get_system_prompt() -> str:
    try:
        return C.prompt_text()
    except RuntimeError as exc:
        log.error("prompt problem: %s", exc)
        now = time.time()
        if now - _ALERT["at"] > 1800:  # at most one alert per 30 min
            _ALERT["at"] = now
            try:
                notify.send(
                    f"PROMPT FILE PROBLEM: {exc}\n"
                    "No Grok file will be built until prompts/system_prompt.txt is fixed on GitHub."
                )
            except Exception:
                pass
        raise


def __getattr__(name: str):
    # Old code that reads prompt.SYSTEM_PROMPT / prompt.PROMPT_VERSION keeps working, always current.
    if name == "SYSTEM_PROMPT":
        return get_system_prompt()
    if name == "PROMPT_VERSION":
        return C.PROMPT_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_user_message(event_date: str, event_ticker: str, words: list[dict],
                       history_block: str = "") -> str:
    lines = [
        f"Date: {event_date}",
        f"Event: {event_ticker}",
        "",
        "Words (exact strings — emit one forecast object for each, using this spelling):",
    ]
    for i, w in enumerate(words, start=1):
        lines.append(f"{i}. {w['word']}")
    if history_block:
        lines.append(history_block)
    lines += [
        "",
        "Output valid JSON only, matching the schema. No preamble, no markdown fences.",
    ]
    return "\n".join(lines)


def build_paste_file(event_date: str, event_ticker: str, words: list[dict]) -> str:
    """One blob for a new Expert chat. Consumer Grok has no system-role box."""
    hist = ""
    if C.WORD_HISTORY_NIGHTS > 0:
        try:
            hist = history.word_history_block(event_date, words, C.WORD_HISTORY_NIGHTS)
        except Exception:
            # Never let a history problem stop tonight's file. Grok works without it.
            log.exception("word history skipped")
    user = build_user_message(event_date, event_ticker, words, hist)
    return (
        get_system_prompt().rstrip()
        + "\n\n---\n\n"
        + user
        + "\n"
    )


def build_telegram_caption(event_date: str, event_ticker: str, n: int) -> str:
    return (
        f"WNT gap {event_date}\n"
        f"event: {event_ticker}\n"
        f"markets: {n}\n"
        f"harness: grok-web-expert\n"
        f"prompt: {C.PROMPT_VERSION}\n"
        f"status: awaiting_json\n\n"
        f"1. New chat on grok.com / Grok app\n"
        f"2. Expert (not Auto)\n"
        f"3. Paste the file. Do not add prices.\n"
        f"4. Reply to this message with the JSON only."
    )
