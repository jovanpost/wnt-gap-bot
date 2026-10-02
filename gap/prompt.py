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

from . import config as C, history, notify, store

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
                       history_block: str = "", headlines_block: str = "", abc_block: str = "",
                       more_block: str = "") -> str:
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
    if abc_block:
        lines.append(abc_block)
    if more_block:
        lines.append(more_block)
    if headlines_block:
        lines.append(headlines_block)
    lines += [
        "",
        "First do the MANDATORY RESEARCH PHASE for every word above (each side of a slash word separately, never two",
        "list words in one query): read its ABC NEWS FEEDS matches, its OTHER NETWORKS AND WIRES headline hits and its",
        "GOOGLE NEWS HEADLINES above, fetch its",
        "Google News RSS, run its blind web",
        "search, its X search sorted Top, and its from:ABC X search (Top). A same-day US story using the word in a news",
        "sense is a hit, even an interview or a 'would consider'. Only after every word has been searched, write your",
        "final answer: valid JSON only, matching the schema, every reasoning starting with 'Blind: ...'.",
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
    heads, abc, more = _news_blocks(words)
    user = build_user_message(event_date, event_ticker, words, hist, heads, abc, more)
    return (
        get_system_prompt().rstrip()
        + "\n\n---\n\n"
        + user
        + "\n"
    )


def _news_blocks(words: list[dict]) -> tuple[str, str, str]:
    """(google_block, abc_block, other_networks_block). Fetched together; any failure gives "" for that block only."""
    try:
        from . import abcfeeds
        return abcfeeds.all_blocks(words)
    except Exception:
        # Never let a news problem stop tonight's file. Grok still searches itself.
        log.exception("news blocks skipped")
        return "", "", ""


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


SEP = "\n\n---\n\n"


def split_paste(paste: str) -> tuple[str, str]:
    """(system prompt, user message) from a built Grok file. v1.10.0: the user message is the frozen
    nightly package (date, words, word history, ABC, other networks, Google News)."""
    if SEP in paste:
        sys_part, user = paste.split(SEP, 1)
        return sys_part, user
    return "", paste


def freeze(event_date: str, event_ticker: str, words: list[dict], paste: str, kind: str = "grok_file"):
    """Store tonight's package once (and the system prompt by version). Returns the package row or None.
    Never raises: freezing must never stop the Grok file."""
    try:
        sys_part, user = split_paste(paste)
        version = C.PROMPT_VERSION
        if sys_part:
            store.save_prompt_version(version, sys_part)
        return store.freeze_package(event_date, event_ticker, kind, version, words, user)
    except Exception:
        log.exception("package freeze failed")
        return None
