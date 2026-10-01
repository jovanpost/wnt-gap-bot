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

VERSION = "wnt-gap-v1.9.0"  # more free challengers: NVIDIA (DeepSeek, Kimi, GLM, Qwen), Cerebras, Mistral, OpenRouter
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
    {"id": "A", "notional": 1.0, "exit": "hold", "rule": "fade15", "cancel": "show529", "label": "A · $1 hold fade"},
    {"id": "B", "notional": 100.0, "exit": "hold", "rule": "fade15", "cancel": "show529", "label": "B · $100 hold fade"},
    {"id": "E", "notional": 1.0, "exit": "hold", "rule": "fade15_gate50", "cancel": "show529", "label": "E · $1 hold fade+Grok>50"},
    {"id": "F", "notional": 100.0, "exit": "hold", "rule": "fade15_gate50", "cancel": "show529", "label": "F · $100 hold fade+Grok>50"},
    {"id": "G", "notional": 1.0, "exit": "hold", "rule": "grok10", "cancel": "show529", "label": "G · $1 hold Grok−10"},
    {"id": "H", "notional": 100.0, "exit": "hold", "rule": "grok10", "cancel": "show529", "label": "H · $100 hold Grok−10"},
    # v1.5.1 NEW book (does not replace anything): edge measured against the price you
    # would really pay (the ask to buy YES, the bid to buy NO), not the mid. See
    # strategy.decide_exec. Threshold: EDGE_EXEC_THRESHOLD below.
    {"id": "I", "notional": 1.0, "exit": "hold", "rule": "edge_exec", "cancel": "show529", "label": "I · $1 hold edge vs ask/bid"},
    # v1.7.0: paper twin of live Book L. Same rule, same $10 size, same 5:29 cancel, so paper
    # vs live compares one rule. Ignores the market like G/H: every word with Grok <= L_MAX_GROK
    # gets SELL YES at Grok + L_OFFSET_CENTS (= BUY NO at 100 - Grok - offset).
    {"id": "N", "notional": 10.0, "exit": "hold", "rule": "grok15_no", "cancel": "show529", "label": "N · $10 paper twin of L"},
)
# To retire books without touching code, set the Streamlit secret DISABLED_BOOKS, e.g. "G,H".
# Old rows stay in the database; the books just stop being booked and shown.
DISABLED_BOOKS = {x.strip().upper() for x in _secret("DISABLED_BOOKS", "").split(",") if x.strip()}
VARIANTS = tuple(v for v in ALL_VARIANTS if v["id"] not in DISABLED_BOOKS)
# Book I: executable edge (points) must be strictly greater than this.
EDGE_EXEC_THRESHOLD = int(_num("EDGE_EXEC_THRESHOLD", 10))
# Quotes: newest depth snapshot at/before the decision time, at most this old (seconds).
QUOTE_MAX_AGE_S = int(_num("QUOTE_MAX_AGE_S", 600))
# v1.7.5: the bot fetches Google News search RSS for every word (each side of a slash word)
# and prints the top titles into the Grok file. Off switch + size + timeout per request.
HEADLINES_ON = _flag("HEADLINES_ON", True)
HEADLINES_PER_TERM = int(_num("HEADLINES_PER_TERM", 6))
HEADLINES_TIMEOUT_S = float(_num("HEADLINES_TIMEOUT_S", 6))
HEADLINES_BUDGET_S = float(_num("HEADLINES_BUDGET_S", 25))  # total time for all words; never delays the file more
HEADLINES_WORKERS = int(_num("HEADLINES_WORKERS", 4))       # searches running at the same time
HEADLINES_STAGGER_S = float(_num("HEADLINES_STAGGER_S", 0.2))  # small gap between starts, gentle on Google
# v1.7.6: ABC News' own RSS feeds, read ONCE per Grok file (not per word): Top 25, then US,
# Politics, International 15 each (v1.7.7: World and GMA feeds are empty at ABC); Health 15 only when tonight's list has a health-type word.
ABC_FEEDS_ON = _flag("ABC_FEEDS_ON", True)
ABC_TOP_N = int(_num("ABC_TOP_N", 25))
ABC_SECTION_N = int(_num("ABC_SECTION_N", 15))
ABC_MAX_AGE_H = int(_num("ABC_MAX_AGE_H", 36))           # older items are dropped
ABC_TIMEOUT_S = float(_num("ABC_TIMEOUT_S", 8))
ABC_BUDGET_S = float(_num("ABC_BUDGET_S", 20))           # total time for all ABC feeds
ABC_CACHE_S = int(_num("ABC_CACHE_S", 300))              # /gap_resend within 5 min reuses the feeds
ABC_MATCHES_PER_WORD = int(_num("ABC_MATCHES_PER_WORD", 4))
ABC_SKIP_FEEDS = {x.strip().lower() for x in _secret("ABC_SKIP_FEEDS", "").split(",") if x.strip()}
# v1.7.6: shared speed limit for every outside news fetch, per website: requests start at least
# NET_MIN_GAP_S apart, and at most NET_MAX_PARALLEL run at once. Google and ABC don't wait for each other.
NET_MIN_GAP_S = float(_num("NET_MIN_GAP_S", 0.25))
NET_MAX_PARALLEL = int(_num("NET_MAX_PARALLEL", 3))
# v1.8.0: challenger forecasters (PAPER ONLY, never traded), run right after the Grok file is
# sent, with the same news. Scored against Grok every Saturday. SHADOW_ON=false turns all off.
SHADOW_ON = _flag("SHADOW_ON", True)
NEWS_REUSE_S = int(_num("NEWS_REUSE_S", 900))            # challengers reuse the Grok file's news if this fresh
GEMINI_API_KEY = _secret("GEMINI_API_KEY", "").strip()   # Google AI Studio key; empty = Gemini off
GEMINI_MODEL = _secret("GEMINI_MODEL", "auto").strip()   # "auto" = newest Flash the key can use
GEMINI_SEARCH = _flag("GEMINI_SEARCH", False)            # Google Search grounding (needs billing on)
GEMINI_TIMEOUT_S = float(_num("GEMINI_TIMEOUT_S", 300))
GEMINI_RETRY_BUDGET_S = float(_num("GEMINI_RETRY_BUDGET_S", 1800))      # keep retrying the same request up to 30 min
GEMINI_DOWNGRADE_AFTER_S = float(_num("GEMINI_DOWNGRADE_AFTER_S", 1200))  # step down a model only after 20 min of failures
# v1.9.0: more free challengers through OpenAI-compatible APIs (paper only, never traded).
# Each runs only if its key is in the secrets. CHALLENGERS = "provider:model" list; the model can be
# an exact id or a word to search for in that provider's model list (newest match wins).
NVIDIA_API_KEY = _secret("NVIDIA_API_KEY", "").strip()
CEREBRAS_API_KEY = _secret("CEREBRAS_API_KEY", "").strip()
MISTRAL_API_KEY = _secret("MISTRAL_API_KEY", "").strip()
OPENROUTER_API_KEY = _secret("OPENROUTER_API_KEY", "").strip()
GROQ_API_KEY = _secret("GROQ_API_KEY", "").strip()
CHALLENGERS = [x.strip() for x in _secret(
    "CHALLENGERS",
    "nvidia:deepseek, nvidia:kimi, nvidia:glm, nvidia:qwen, cerebras:gpt-oss-120b, mistral:mistral-medium, openrouter:free",
).split(",") if x.strip()]
CHALLENGER_MAX_TOKENS = int(_num("CHALLENGER_MAX_TOKENS", 12000))
CHALLENGER_RETRY_BUDGET_S = float(_num("CHALLENGER_RETRY_BUDGET_S", 1800))   # same 30-minute patience as Gemini
BASELINE_ON = _flag("BASELINE_ON", True)
# v1.7.0: paper quotes are never frozen before Kalshi's real open + this many seconds (or
# DECISION_LAG_MIN, whichever is later) -- no-fade needs a moment to save the first books.
PAPER_MIN_AFTER_OPEN_S = int(_num("PAPER_MIN_AFTER_OPEN_S", 120))
# ...and if no-fade has not saved ANY book for tonight yet, keep waiting up to this long.
NO_DEPTH_GRACE_S = int(_num("NO_DEPTH_GRACE_S", 900))
# v1.5.9: wide spreads are TRADED. 0 = no spread rule. (It was 25 in v1.5.1-v1.5.8.) Only broken quotes
# (bid<=1, ask<=1, bid>=99, bid>ask, a missing side) are skipped. Set the secret QUOTE_MAX_SPREAD to bring a limit back.
QUOTE_MAX_SPREAD = int(_num("QUOTE_MAX_SPREAD", 0))

# v1.5.10 A: SCALP book (pre-registered, backed by the Aug17-Sep23 backtest). FROZEN --
# do not tune any of these before 30 filled trades or 6 weeks, whichever comes first.
SCALP_ON = _flag("SCALP_ON", True)
SCALP_QUALIFY_PROB = int(_num("SCALP_QUALIFY_PROB", 70))     # Grok >= this to qualify a word
SCALP_BUY_MAX_CENTS = int(_num("SCALP_BUY_MAX_CENTS", 70))   # buy YES any time the ask is <= this
SCALP_SELL_CENTS = int(_num("SCALP_SELL_CENTS", 85))         # every batch rests a sell at this price
SCALP_BUDGET_DOLLARS = float(_num("SCALP_BUDGET_DOLLARS", 100.0))  # target per word, not a guarantee
SCALP_BUY_CUTOFF_HHMM = _secret("SCALP_BUY_CUTOFF_HHMM", "16:30")   # CT, last new buy (v1.7.2: was 17:25)
SCALP_FALLBACK_HHMM = _secret("SCALP_FALLBACK_HHMM", "17:00")       # CT, sell everything unsold at market (v1.7.2: was 17:29)
# WORD HISTORY block sent to Grok: how many past nights (0 = off).
WORD_HISTORY_NIGHTS = int(_num("WORD_HISTORY_NIGHTS", 10))
# Paper fills are checked every poll tick (not only when someone opens the app).
BACKGROUND_FILLS = _flag("BACKGROUND_FILLS", True)
SHOW_CANCEL_CT = _secret("SHOW_CANCEL_CT", "17:29")
GROK10_OFFSET = int(_num("GROK10_OFFSET", 10))
# Addendum clocks
# v1.5.10 B: was 60. A 5-minute buffer, not zero -- if the word list gets corrected right
# after first-seen (it has happened: e.g. Sep 16 was "upgraded" ~3h10m after first-seen, a
# late correction the buffer would not have caught anyway), the existing "upgraded" path
# already pushes decision_at later and re-freezes quotes, so a short buffer here is just
# insurance against a same-minute typo, not the only protection.
DECISION_LAG_MIN = int(_num("DECISION_LAG_MIN", 5))
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
# Same shape as wnt-nofade-bot's order path (gap/kalshi.py's create_no_order/cancel_order
# were ported from there). Use the SAME real key -- this is not a second Kalshi account,
# it is the same live-trading credential nofade already uses, added to gap-bot's own
# Streamlit secrets so gap-bot can place its own (much smaller) real orders.
ORDER_API = _secret("ORDER_API", "v2")
POST_ONLY = _flag("POST_ONLY", True)
USE_SERVER_SIDE_EXPIRY = _flag("USE_SERVER_SIDE_EXPIRY", True)
USER_AGENT = f"wnt-gap-bot/{VERSION}"

# ---------------------------------------------------------------------------
# Book L: LIVE real-money trading. v1.7.0 rule "grok15_no" (see strategy.order_for_rule):
# Grok <= L_MAX_GROK -> SELL YES at Grok + L_OFFSET_CENTS. Paper book N is its twin.
#
# L_LIVE_ON is a hard kill switch, default OFF. Nothing in gap/live.py places a
# real order unless this is explicitly set to true in Streamlit secrets, in
# addition to KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM being the real production key.
#
# FROZEN like K: do not tune L_NOTIONAL_DOLLARS, the rule, or the cancel window
# before L_FREEZE_MIN_FILLED filled trades or L_FREEZE_WEEKS weeks, whichever
# comes first -- an early live read is only honest if nothing moves under it.
# ---------------------------------------------------------------------------
L_LIVE_ON = _flag("L_LIVE_ON", False)
# v1.7.0 rule (replaces the Book K rule): every word with Grok <= L_MAX_GROK gets a LIMIT
# order to SELL YES at Grok + L_OFFSET_CENTS (= BUY NO at 100 - Grok - offset). No market
# quote is needed. The order may take right away (at the buyers' better price, paying the
# taker fee); whatever is left rests until the 5:29 CT cancel. Lowest Grok goes first when
# the nightly cap cannot fit every word.
L_MAX_GROK = int(_num("L_MAX_GROK", 30))
L_OFFSET_CENTS = int(_num("L_OFFSET_CENTS", 15))
# v1.7.3: seconds AFTER Kalshi's real open before L sends. At the first second the book is
# nearly empty, so a crossing limit fills at its worst allowed price (Pentagon, Sep 29).
L_FIRE_DELAY_S = int(_num("L_FIRE_DELAY_S", 300))
L_NOTIONAL_DOLLARS = float(_num("L_NOTIONAL_DOLLARS", 10.0))  # $ per word. Fixed, not a %, not scaled.
L_NIGHTLY_CAP_DOLLARS = float(_num("L_NIGHTLY_CAP_DOLLARS", 50.0))
L_MAX_WORDS_PER_NIGHT = int(_num("L_MAX_WORDS_PER_NIGHT", 5))  # first N qualifying words; never scaled down
L_CIRCUIT_BREAKER_WEEKLY_LOSS = float(_num("L_CIRCUIT_BREAKER_WEEKLY_LOSS", 30.0))
L_FREEZE_MIN_FILLED = int(_num("L_FREEZE_MIN_FILLED", 30))
L_FREEZE_WEEKS = int(_num("L_FREEZE_WEEKS", 6))
# Fast-arm watch: same technique wnt-nofade-bot's _fast_arm/_fast_wait_and_watch
# use (see its wnt/config.py FAST_* knobs) -- these are the same values,
# just under an L_ prefix since only Book L uses them here. Starting this many
# seconds before Kalshi's own published open_time, gap/live.py's dedicated
# watch thread polls the real market status every L_FAST_WATCH_SECONDS and
# fires the instant it reports active, instead of waiting on the regular 30s
# poll loop. Gives up after L_FAST_GIVE_UP_SECONDS past open_time and falls
# back to arm_tonight() on the normal loop (already gated on the same real
# open time), exactly like nofade's own fallback to its "normal path".
L_FAST_LEAD_SECONDS = float(_num("L_FAST_LEAD_SECONDS", 3.0))
L_FAST_WATCH_SECONDS = float(_num("L_FAST_WATCH_SECONDS", 0.3))
L_FAST_GIVE_UP_SECONDS = float(_num("L_FAST_GIVE_UP_SECONDS", 180.0))
L_FAST_MAX_WORKERS = int(_num("L_FAST_MAX_WORKERS", 5))
# v1.7.0: L orders are sent WITHOUT post_only on purpose: a limit that crosses fills at once
# at the buyers' (better) price. L_POST_ONLY=true would make Kalshi refuse those instead.
L_POST_ONLY = _flag("L_POST_ONLY", False)
# L cancels at 5:29 CT (SHOW_CANCEL_CT below), the SAME cancel every paper book
# (A/B/E/F/G/H/I) now uses -- one cancel time for every strategy, paper and live.
# See gap/live.py's arm_tonight(), which computes this deadline the same way
# gap/fills.py does for the paper books (_show_cancel_utc).

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
        f"broken quote (bid<=1, ask<=1, bid>=99, bid>ask{f', spread>{QUOTE_MAX_SPREAD}' if QUOTE_MAX_SPREAD else ''}) = no trade · wide spreads "
        f"{'skipped' if QUOTE_MAX_SPREAD else 'are traded'}\n"
        f"NO bankroll / NO night cap / NO cluster cap\n"
        f"L (LIVE $ real): {'ON' if L_LIVE_ON else 'off'} · ${L_NOTIONAL_DOLLARS:g}/word · "
        f"cap ${L_NIGHTLY_CAP_DOLLARS:g}/night (first {L_MAX_WORDS_PER_NIGHT} words) · "
        f"cancel {SHOW_CANCEL_CT} CT (same as every paper book) · circuit breaker -${L_CIRCUIT_BREAKER_WEEKLY_LOSS:g}/wk\n"
        f"prompt {prompt_version()} | harness {HARNESS} | addendum {ADDENDUM}\n"
        f"poll {POLL_START_CT} CT | json deadline {JSON_DEADLINE_CT} CT"
    )
