"""Env / Streamlit secrets. Frozen strategy knobs live here."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import streamlit as st
except Exception:  # local scripts, no streamlit
    st = None


def _secret(name: str, default: str = "") -> str:
    if st is not None:
        try:
            if name in st.secrets:
                val = st.secrets[name]
                if val is not None and str(val) != "":
                    return str(val)
        except Exception:
            pass
    val = os.environ.get(name)
    return default if val is None else str(val)


def _flag(name: str, default: bool = False) -> bool:
    raw = _secret(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    raw = _secret(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


PAPER = _flag("PAPER", True)
LIVE_TRADING = _flag("LIVE_TRADING", False)
DRY_RUN = _flag("DRY_RUN", True)
USE_DEMO = _flag("USE_DEMO", False)

VERSION = "wnt-gap-v1.5.1"
# ---------------------------------------------------------------------------
# The system prompt lives in a plain text file you edit on GitHub:
#     prompts/system_prompt.txt
# Replace the whole file to change the prompt. Git history = prompt history.
# It is read fresh every time it is needed, so a new prompt takes effect on the
# next file the bot builds. No secret to bump: the version label is made from
# the text itself ("gap-" + first 7 letters of its SHA-1).
# ---------------------------------------------------------------------------
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"
PROMPT_MIN_CHARS = 500  # anything shorter is almost surely an accident (empty file, half paste)


def prompt_text() -> str:
    """The exact system prompt, read from prompts/system_prompt.txt. Raises if unusable."""
    try:
        raw = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"prompt file missing: prompts/system_prompt.txt ({exc})") from exc
    text = raw.replace("\r\n", "\n").strip()
    if len(text) < PROMPT_MIN_CHARS:
        raise RuntimeError(
            f"prompt file too short ({len(text)} chars, need at least {PROMPT_MIN_CHARS}): "
            "prompts/system_prompt.txt"
        )
    return text


def prompt_version() -> str:
    try:
        text = prompt_text()
    except RuntimeError:
        return "gap-NO-PROMPT-FILE"
    return "gap-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:7]


def __getattr__(name: str):
    # Lets old code keep writing C.PROMPT_VERSION and always get the CURRENT label.
    if name == "PROMPT_VERSION":
        return prompt_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


HARNESS = _secret("HARNESS", "grok-web-expert")
MODEL_LABEL = _secret("MODEL_LABEL", "grok-web-expert")
ADDENDUM = "v1.4"

SERIES = _secret("SERIES", "KXWORLDNEWSMENTION")
GAP_THRESHOLD = int(_num("GAP_THRESHOLD", 15))
# How far we walk from the quoted mid toward the model. Not the filter.
# Filter 15 + take 15 on a 16¢ gap leaves ~0¢ vs the tape. Default 8.
LIMIT_OFFSET_CENTS = int(_num("LIMIT_OFFSET_CENTS", 8))
# v1.5.0: C/D (scalp) removed entirely. Scalp required trusting a LIVE
# Kalshi quote to decide when an early exit had "hit" -- that pattern caused
# four independent bugs in two days (tape.py x2, score.py x2), because a
# quote read after a market closes is not a live price, it is garbage, and
# every consumer of it eventually got that wrong in some way. Hold-to-
# settlement needs exactly one trustworthy signal -- Kalshi's final result --
# and nothing else. That is the only exit rule left in this repo.
ALL_VARIANTS = (
    {"id": "A", "notional": 1.0, "exit": "hold", "rule": "fade15", "cancel": "send60", "label": "A · $1 hold fade"},
    {"id": "B", "notional": 100.0, "exit": "hold", "rule": "fade15", "cancel": "send60", "label": "B · $100 hold fade"},
    {"id": "E", "notional": 1.0, "exit": "hold", "rule": "fade15_gate50", "cancel": "send60", "label": "E · $1 hold fade+Grok>50"},
    {"id": "F", "notional": 100.0, "exit": "hold", "rule": "fade15_gate50", "cancel": "send60", "label": "F · $100 hold fade+Grok>50"},
    {"id": "G", "notional": 1.0, "exit": "hold", "rule": "grok10", "cancel": "show529", "label": "G · $1 hold Grok−10"},
    {"id": "H", "notional": 100.0, "exit": "hold", "rule": "grok10", "cancel": "show529", "label": "H · $100 hold Grok−10"},
    # v1.5.1 NEW book (does not replace anything): edge measured against the price you
    # would really pay (the ask to buy YES, the bid to buy NO), not the mid. See
    # strategy.decide_exec. Threshold: EDGE_EXEC_THRESHOLD below.
    {"id": "I", "notional": 1.0, "exit": "hold", "rule": "edge_exec", "cancel": "send60", "label": "I · $1 hold edge vs ask/bid"},
)
# To retire books without touching code, set the Streamlit secret DISABLED_BOOKS, e.g. "G,H".
# Old rows stay in the database; the books just stop being booked and shown.
DISABLED_BOOKS = {x.strip().upper() for x in _secret("DISABLED_BOOKS", "").split(",") if x.strip()}
VARIANTS = tuple(v for v in ALL_VARIANTS if v["id"] not in DISABLED_BOOKS)
# Book I: executable edge (points) must be strictly greater than this.
EDGE_EXEC_THRESHOLD = int(_num("EDGE_EXEC_THRESHOLD", 10))
# Quotes: newest depth snapshot at/before the decision time, at most this old (seconds).
QUOTE_MAX_AGE_S = int(_num("QUOTE_MAX_AGE_S", 600))
# WORD HISTORY block sent to Grok: how many past nights (0 = off).
WORD_HISTORY_NIGHTS = int(_num("WORD_HISTORY_NIGHTS", 10))
# Paper fills are checked every poll tick (not only when someone opens the app).
BACKGROUND_FILLS = _flag("BACKGROUND_FILLS", True)
SHOW_CANCEL_CT = _secret("SHOW_CANCEL_CT", "17:29")
GROK10_OFFSET = int(_num("GROK10_OFFSET", 10))
# Addendum clocks
DECISION_LAG_MIN = int(_num("DECISION_LAG_MIN", 60))
CANCEL_AFTER_MIN = int(_num("CANCEL_AFTER_MIN", 60))
MARKET_OPEN_CT = _secret("MARKET_OPEN_CT", "12:30")  # modal WNT open; API open_time wins
STREAMLIT_APP_URL = _secret("STREAMLIT_APP_URL", "https://wnt-gap-bot.streamlit.app")
EXECUTION_MODEL = "capped_sweep"
# Each minute we may take this fraction of size sitting at our limit.
# Same 50% haircut the nofade backtest uses (BACKTEST_FILL_RATE).
FILL_TAKE_FRACTION = float(_num("FILL_TAKE_FRACTION", 1.00))
FILL_POLL_SECONDS = int(_num("FILL_POLL_SECONDS", 5))

CT = ZoneInfo("America/Chicago")
POLL_START_CT = _secret("POLL_START_CT", "11:00")  # matches no-fade's depth window start
JSON_DEADLINE_CT = _secret("JSON_DEADLINE_CT", "16:30")
QUOTE_AFTER_PARSE = _flag("QUOTE_AFTER_PARSE", True)

PROD_BASE = "https://external-api.kalshi.com"
DEMO_BASE = "https://external-api.demo.kalshi.co"
API_ROOT = "/trade-api/v2"
BASE_URL = DEMO_BASE if USE_DEMO else PROD_BASE

KALSHI_KEY_ID = _secret("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = _secret("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_PRIVATE_KEY_PEM = _secret("KALSHI_PRIVATE_KEY_PEM", "")
USER_AGENT = f"wnt-gap-bot/{VERSION}"

DATABASE_URL = _secret("DATABASE_URL", "")
SQLITE_PATH = _secret("SQLITE_PATH", "gap_bot.db")

TELEGRAM_TOKEN = _secret("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID", "")
TELEGRAM_COMMANDS = _flag("TELEGRAM_COMMANDS", True)

# Live placement is compiled off until Phase 2. Do not "just flip PAPER".
def may_place_live() -> bool:
    return (not PAPER) and LIVE_TRADING and (not DRY_RUN) and (not USE_DEMO)


def summary() -> str:
    mode = "PAPER" if PAPER else ("LIVE" if may_place_live() else "LIVE-BLOCKED")
    where = "DEMO" if USE_DEMO else "PRODUCTION"
    return (
        f"{VERSION} | {mode} | {where} | {SERIES}\n"
        f"|gap|>{GAP_THRESHOLD}¢ | take {LIMIT_OFFSET_CENTS}¢ from mid | "
        f"poll from {POLL_START_CT} every 60s | file first-seen+{DECISION_LAG_MIN}m | "
        f"cancel send+{CANCEL_AFTER_MIN}m\n"
        f"A/B fade hold (gap strictly > {GAP_THRESHOLD}) · E/F fade+Grok>50 hold · G/H Grok-10 hold cancel 5:29 CT · "
        f"I edge vs ask/bid > {EDGE_EXEC_THRESHOLD} · scalp removed v1.5.0\n"
        f"books on: {','.join(v['id'] for v in VARIANTS)} · quotes frozen at decision time · "
        f"invalid quote (bid<=1, ask<=1, bid>=99, bid>ask, spread>25) = no trade\n"
        f"NO bankroll / NO night cap / NO cluster cap\n"
        f"prompt {prompt_version()} | harness {HARNESS} | addendum {ADDENDUM}\n"
        f"poll {POLL_START_CT} CT | json deadline {JSON_DEADLINE_CT} CT"
    )
