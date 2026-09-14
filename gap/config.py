"""Env / Streamlit secrets. Frozen strategy knobs live here."""
from __future__ import annotations

import os
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

VERSION = "wnt-gap-v1.1"
PROMPT_VERSION = _secret("PROMPT_VERSION", "gap-v1.0")
HARNESS = _secret("HARNESS", "grok-web-expert")
MODEL_LABEL = _secret("MODEL_LABEL", "grok-web-expert")
ADDENDUM = "v1.1"

SERIES = _secret("SERIES", "KXWORLDNEWSMENTION")
GAP_THRESHOLD = int(_num("GAP_THRESHOLD", 15))
# Paper/live notional stays parked until the four-way pick is made.
NOTIONAL_DOLLARS = _num("NOTIONAL_DOLLARS", 5.00)
CLUSTER_CAP = int(_num("CLUSTER_CAP", 2))
NIGHT_CAP_FRACTION = _num("NIGHT_CAP_FRACTION", 0.20)
BANKROLL_DOLLARS = _num("BANKROLL_DOLLARS", 150.00)

# Addendum clocks
DECISION_LAG_MIN = int(_num("DECISION_LAG_MIN", 60))
CANCEL_AFTER_MIN = int(_num("CANCEL_AFTER_MIN", 60))
MARKET_OPEN_CT = _secret("MARKET_OPEN_CT", "10:00")  # fallback if API has no open_time
EXECUTION_MODEL = "capped_sweep"

CT = ZoneInfo("America/Chicago")
POLL_START_CT = _secret("POLL_START_CT", "07:00")
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
        f"capped sweep | limit = model − {GAP_THRESHOLD}¢ | "
        f"decide open+{DECISION_LAG_MIN}m | cancel send+{CANCEL_AFTER_MIN}m\n"
        f"parked notional ${NOTIONAL_DOLLARS:.2f}/word | "
        f"cluster cap {CLUSTER_CAP} | night cap {100 * NIGHT_CAP_FRACTION:.0f}%\n"
        f"prompt {PROMPT_VERSION} | harness {HARNESS} | addendum {ADDENDUM}\n"
        f"poll {POLL_START_CT} CT | json deadline {JSON_DEADLINE_CT} CT"
    )
