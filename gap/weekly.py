"""Saturday weekly dump (v1.5.1).

Sends TWO files to Telegram every Saturday at 7:00 AM Central:
  1. gap-week-<week>.txt        -- the big report (paste this into Claude)
  2. gap-week-<week>-words.csv  -- one row per word per night (for your notebook)

Built from the database plus Kalshi's official results. Nothing here places or
changes a trade.

What is new in v1.5.1 (from the W38 review)
  * RULE AUDIT at the top: every order re-checked against its own book's rule.
  * Night status: TRADED / FORECAST-ONLY / VOID, plus the fill-model go-live time.
  * Market comparisons use VALID quotes only (bid<=1, ask<=1, bid>=99, bid>ask,
    spread>25 are INVALID and show "INVALID QUOTE" instead of a fake mid).
  * Gross / fees / net everywhere. Fees now really are 0.07 x ct x P x (1-P), rounded up.
  * Partial fills flagged; dollar-weighted and full-fill-only hit rates.
  * Headline Grok-vs-market scoreboard (all / count / plain) + week-over-week table
    with the prompt version on every row.
  * Wider calibration buckets, prompt sha1 + changelog vs last week + diff.
  * Fill-selection check (do fills happen mostly on adverse moves?).
  * Per-night timeline from the activity log, and the exact user message Grok saw.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import clock, config as C, lab, notify, pricing, quotes as Q, results, settle, store, strategy

log = logging.getLogger("gap.weekly")

# When the Saturday file goes out (Central time, 24h "HH:MM"). Override with the
# WEEKLY_SEND_CT secret if you ever want a different time.
SEND_AFTER_CT = C._secret("WEEKLY_SEND_CT", "07:00")

_LOCK = threading.Lock()
_AUTO = {"week": None, "fails": 0, "next_try": 0.0}
MAX_AUTO_FAILS = 3
RETRY_SECONDS = 20 * 60

THRESHOLDS = (10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90)
GAP_BINS = ((-101, -40), (-40, -25), (-25, -15), (-15, -5), (-5, 5),
            (5, 15), (15, 25), (25, 40), (40, 101))
ABS_GAP_BINS = ((15, 20), (20, 25), (25, 35), (35, 101))
P_BINS = tuple((lo, lo + 10) for lo in range(0, 100, 10))
WIDE_BINS = ((0, 20, "0-19"), (20, 40, "20-39"), (40, 60, "40-59"), (60, 80, "60-79"), (80, 101, "80+"))
MARKET_RULES = ("fade15", "fade15_gate50", "edge_exec")

# ---- Slice K (pre-registered W38 review). DO NOT CHANGE THESE NUMBERS. ----
# K = Book A orders where side = NO, Grok <= 30, quote valid at booking, |Grok - mid| strictly > 15.
# Frozen until 30 FILLED trades or 6 weeks, whichever comes first.
K_MAX_GROK = 30
K_MIN_GAP = 15          # fixed at 15 on purpose: does not follow GAP_THRESHOLD if that ever changes
K_MIN_FILLED = 30
K_SPLIT_MID = lab.SPLIT_MID   # 55: K_HIGH = booked mid >= 55, K_LOW = mid < 55 (frozen 6 weeks / 30 filled K_HIGH)
K_WINDOW_WEEKS = 6
K_PASS_MARGIN = 5.0     # points

_COUNT_RES = (
    re.compile(r"\d+\s*\+"),                          # "5+", "Trump (5+ times)"
    re.compile(r"\b\d+\s*(?:times|mentions)\b", re.I),  # "3 times"
    re.compile(r"\bat least\s+\d+\b", re.I),
)


def is_count_word(word: str) -> bool:
    """Count markets look like 'Trump (5+ times)' or 'Iran 3+'. The W38 dump missed the
    parenthesised form and filed them under plain words."""
    w = str(word or "")
    return any(r.search(w) for r in _COUNT_RES)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _f(x):
    """Any number-ish thing (Decimal, str, int) -> float, or None."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _dumps(obj, **kw) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False, **kw)


def _obj(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


def _ts(v) -> str:
    dt = clock.parse_dt(v)
    if dt is None:
        return "n/a"
    return dt.astimezone(C.CT).strftime("%a %m-%d %H:%M:%S CT")


def _hm(v) -> str:
    dt = clock.parse_dt(v)
    if dt is None:
        return "n/a"
    return dt.astimezone(C.CT).strftime("%H:%M:%S")


def _money(cents) -> str:
    c = _f(cents)
    if c is None:
        return "n/a"
    return f"{c / 100.0:+.2f}"


def _usd(cents) -> str:
    c = _f(cents)
    return "n/a" if c is None else f"{c / 100.0:.2f}"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fx(x, nd=1, suffix="") -> str:
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


def _rate(hits, n) -> str:
    if not n:
        return "n/a"
    return f"{100.0 * hits / n:.0f}% ({hits}/{n})"


def _brier(pairs):
    pairs = [(p, y) for p, y in pairs if p is not None and y is not None]
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _trunc(s, n) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _is_none_text(s) -> bool:
    t = str(s or "").strip().lower()
    return t in ("", "n/a", "na") or t.startswith("none")


def _hdr(lines, title):
    lines.append("")
    lines.append("=" * 72)
    lines.append(title)
    lines.append("=" * 72)


def _week_id(start: str) -> str:
    d = datetime.strptime(start[:10], "%Y-%m-%d").date()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _prompt_head(prompt_text: str) -> str:
    """The system-prompt part of a stored paste file (everything before the --- line)."""
    return str(prompt_text or "").split("\n\n---\n\n")[0].strip()


def _prompt_user_part(prompt_text: str) -> str:
    parts = str(prompt_text or "").split("\n\n---\n\n", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _sha7(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:7]


# --------------------------------------------------------------------------
# collecting data
# --------------------------------------------------------------------------
def _booking_row(rows, booked_at):
    """Old-style quotes: pick the row written when the night was BOOKED (the row nearest the
    parse time), not the first or last row. Older code also logged quotes in sweeps before
    booking (e.g. 14:03 for a 14:21 booking) and all evening after it (up to 11:59 PM)."""
    if booked_at is None:
        return rows[0]

    def when(q):
        return clock.parse_dt(q.get("quoted_at"))

    inside = [q for q in rows if when(q) and booked_at - timedelta(seconds=30) <= when(q) <= booked_at + timedelta(minutes=5)]
    if inside:
        return inside[0]
    before = [q for q in rows if when(q) and when(q) < booked_at]
    if before:
        return before[-1]
    return rows[0]


def _quote_for(ticker, frozen_by_t, legacy_by_t, booked_at=None):
    """Best quote we have for a ticker: the frozen decision-time one, else (old rows)
    the last row on file. Returns a dict with a real validity verdict."""
    fq = frozen_by_t.get(ticker)
    if fq:
        bid, ask = _f(fq.get("yes_bid_cents")), _f(fq.get("yes_ask_cents"))
        valid = bool(fq.get("valid"))
        return {
            "bid": bid, "ask": ask, "valid": valid,
            "reason": fq.get("invalid_reason"),
            "time": fq.get("captured_at"), "age_s": fq.get("age_s"),
            "source": "frozen", "n_rows": 1,
        }
    rows = legacy_by_t.get(ticker) or []
    if rows:
        # Older code logged quotes in several sweeps: before booking, AT booking, and all evening
        # after it (5:40 PM, 6:25 PM, even 11:59 PM, after the book had collapsed). Use the
        # sweep written when booking ran.
        q = _booking_row(rows, booked_at)
        bid, ask = _f(q.get("yes_bid_cents")), _f(q.get("yes_ask_cents"))
        ok, why = Q.validate(bid, ask)
        return {
            "bid": bid, "ask": ask, "valid": ok, "reason": why,
            "time": q.get("quoted_at"), "age_s": None,
            "source": "legacy", "n_rows": len(rows),  # n_rows > 1 = extra later rows, ignored
        }
    return {"bid": None, "ask": None, "valid": False, "reason": "no quote row",
            "time": None, "age_s": None, "source": "none", "n_rows": 0}


def _build_rows(run, forecasts, quote_rows, outcome_by_ticker):
    parsed = _obj(run.get("parsed")) or {}
    cycle_temp = parsed.get("cycle_temp") if isinstance(parsed, dict) else None
    frozen_by_t = {q["market_ticker"]: q for q in quote_rows if q.get("frozen")}
    legacy_by_t: dict = {}
    for q in quote_rows:
        if not q.get("frozen"):
            legacy_by_t.setdefault(q["market_ticker"], []).append(q)
    rows = []
    for f in forecasts:
        t = f["market_ticker"]
        q = _quote_for(t, frozen_by_t, legacy_by_t, clock.parse_dt(run.get("parsed_at")))
        grok = _f(f.get("probability"))
        mid = ((q["bid"] + q["ask"]) / 2.0) if q["valid"] else None  # NO fake mid for invalid quotes
        outcome = outcome_by_ticker.get(t)
        y = 1 if outcome == "yes" else (0 if outcome == "no" else None)
        rows.append({
            "run_id": run.get("id"),
            "date": str(run.get("event_date"))[:10],
            "week": _week_id(str(run.get("event_date"))[:10]),
            "event_ticker": run.get("event_ticker"),
            "ticker": t,
            "word": f["word"],
            "is_count": is_count_word(f["word"]),
            "grok": grok,
            "p_block": _f(f.get("p_block_airs")),
            "p_said": _f(f.get("p_said_given_airs")),
            "bid": q["bid"], "ask": q["ask"], "mid": mid,
            "quote_valid": q["valid"], "quote_reason": q["reason"],
            "quote_time": q["time"], "quote_age_s": q["age_s"],
            "quote_source": q["source"], "quote_rows": q["n_rows"],
            "gap": (grok - mid) if (grok is not None and mid is not None) else None,
            "outcome": outcome, "y": y,
            "cycle_temp": cycle_temp,
            "has_sub": not _is_none_text(f.get("substitute_risk")),
            "story": f.get("carrying_story"),
            "substitute_risk": f.get("substitute_risk"),
            "other_routes": f.get("other_routes"),
            "reasoning": f.get("reasoning"),
            "trades": [],
        })
    return rows, cycle_temp


def _night_status(run, n_forecasts, n_orders, go_live, voids):
    d = str(run.get("event_date"))[:10]
    if d in voids:
        return "VOID", f"marked void by you: {voids[d]}"
    if not n_forecasts:
        return "VOID", f"no forecasts stored (run status = {run.get('status')})"
    parsed_at = clock.parse_dt(run.get("parsed_at"))
    if go_live is not None and parsed_at is not None and parsed_at < go_live:
        return "FORECAST-ONLY", (
            f"booked {_ts(parsed_at)}, before the current fill model went live ({_ts(go_live)}); "
            "any orders on this night are old and left out of the books"
        )
    if go_live is None and not n_orders:
        return "FORECAST-ONLY", "no fill-model go-live time on record and no orders"
    if not n_orders:
        return "FORECAST-ONLY", ("no orders on file for this night (wiped, or never booked): "
                                 "the forecast is good data, there is no trade record")
    return "TRADED", "paper books were live when this night was booked"


def _order_row(o, s, run_date, row_by_ticker, status_label):
    filled = _f(o.get("filled_contracts")) or 0.0
    intended = _f(o.get("contracts")) or 0.0
    side = str(o.get("side") or "").upper()
    our_px = pricing.entry_price_cents(o)
    status = str(o.get("status") or "")
    trow = row_by_ticker.get(o["market_ticker"], {})
    outcome = (s.get("outcome") if s else None) or trow.get("outcome")
    settled = bool(s)
    if status == "void":
        state = "void"
    elif filled <= 0:
        state = "unfilled"
    elif not settled:
        state = "pending"
    else:
        won = (side == "YES" and outcome == "yes") or (side == "NO" and outcome == "no")
        state = "won" if won else "lost"
    rule = o.get("book_rule")
    if not rule:
        for spec in C.ALL_VARIANTS:
            if spec["id"] == o.get("variant_id"):
                rule = spec["rule"]
                break
    # what the order was booked against
    qb, qa = _f(o.get("quote_bid_cents")), _f(o.get("quote_ask_cents"))
    own_quote = qb is not None or qa is not None
    if not own_quote:
        qb, qa = trow.get("bid"), trow.get("ask")
    excluded = None
    pre_rule = False
    if status_label != "TRADED":
        excluded = f"night is {status_label}"
    elif rule in MARKET_RULES and not trow.get("quote_valid"):
        if own_quote:
            excluded = "INVALID QUOTE"   # booked under the new rule and still traded a bad quote
        else:
            pre_rule = True              # booked before the rule existed: a real trade, keep it, flag it
    gross = _f(s.get("gross_cents")) if s else None
    fees = _f(s.get("fees_cents")) if s else None
    net = _f(s.get("net_cents")) if s else None
    won_bool = None
    if outcome in ("yes", "no"):
        won_bool = (side == "YES" and outcome == "yes") or (side == "NO" and outcome == "no")
    hyp = None  # what an UNFILLED order would have made at its limit (gross, no fees)
    if filled <= 0 and won_bool is not None:
        per = (100 - our_px) if won_bool else -our_px
        hyp = intended * per
    return {
        "date": run_date,
        "variant": o.get("variant_id") or "?",
        "rule": rule,
        "notional": _f(o.get("notional_dollars")),
        "word": o["word"],
        "ticker": o["market_ticker"],
        "side": side,
        "intended": intended,
        "filled": filled,
        "fill_pct": (100.0 * filled / intended) if intended else 0.0,
        "partial": bool(filled > 0 and intended and filled / intended < 0.98),
        "our_px": our_px,
        "yes_ticket": _f(o.get("limit_price_cents")),
        "gap_booked": _f(o.get("gap_points")),
        "threshold_booked": _f(o.get("threshold")),
        "status": status,
        "state": state,
        "outcome": outcome,
        "gross": gross, "fees": fees, "net": net,
        "cost": float(pricing.cost_cents(o)),
        "grok": trow.get("grok"),
        "mid": trow.get("mid"),
        "gap_real": trow.get("gap"),
        "q_bid": qb, "q_ask": qa, "own_quote": own_quote,
        "q_time": o.get("quote_captured_at"),
        "excluded": excluded,
        "pre_rule_invalid": pre_rule,
        "hyp": hyp,
        "legacy_px": o.get("our_price_cents") in (None, ""),
        "placed_at": o.get("placed_at"),
        "q_mid": ((qb + qa) / 2.0) if (qb is not None and qa is not None) else None,
        "quote_valid": bool(Q.validate(qb, qa)[0]),
        "is_count": bool(trow.get("is_count")),
        "gap_exact": (strategy.gap_points_exact(int(round(trow["grok"])), int(round(qb)), int(round(qa)))
                      if (trow.get("grok") is not None and qb is not None and qa is not None) else None),
    }


def collect(start: str, end: str, light: bool = False) -> dict:
    """Pull everything into plain-Python structures (no Decimals anywhere).
    light=True is for the Streamlit page: read the database only (no settle sweep, no Kalshi
    calls, no activity log). Results come from the permanent gap_results cache."""
    if not light:
        for run in store.runs_between(start, end):    # H mirrors G's fills, exactly as the board does
            try:
                settle.sync_gh_fills(str(run["event_date"])[:10])
            except Exception:
                log.exception("sync_gh_fills")
        settle_totals = settle.settle_range(start, end)   # also recomputes fees for the week
    else:
        settle_totals = {"light": True}
    all_runs = store.runs_between("2000-01-01", end)
    go_live_val, go_live = store.state_meta("fill_model")
    voids = store.get_state("void_nights", {}) or {}
    if not isinstance(voids, dict):
        voids = {}

    packs = []
    tickers: set = set()
    for run in all_runs:
        rid = run["id"]
        forecasts = store.forecasts_for_run(rid)
        markets = store.markets_for_run(rid)
        qrows = store.quotes_for_run(rid)
        packs.append((run, forecasts, markets, qrows))
        tickers.update(f["market_ticker"] for f in forecasts)
        tickers.update(m["market_ticker"] for m in markets)
    if light:
        cached = store.results_cached(sorted(tickers))
        outcome_by_ticker = {t: cached.get(t) for t in tickers}
        fetch_errors = 0
    else:
        outcome_by_ticker, fetch_errors = results.results_for(tickers)

    nights = []
    run_info: dict = {}         # per run: decision time, week, prompt (the strategy lab joins on this)
    slice_orders: list = []     # every order of every week (slice K needs the history)
    history_rows = []           # every scored forecast row from every week (for week-over-week)
    run_meta_by_week: dict = {}
    for run, forecasts, markets, qrows in packs:
        rows, cycle_temp = _build_rows(run, forecasts, qrows, outcome_by_ticker)
        d = str(run.get("event_date"))[:10]
        wk = _week_id(d)
        pt = str(run.get("prompt_text") or "")
        run_meta_by_week.setdefault(wk, []).append({
            "date": d, "version": run.get("prompt_version"),
            "sha": _sha7(_prompt_head(pt)) if pt else None,
            "usable": bool(forecasts) and d not in voids,
        })
        if rows and d not in voids:
            history_rows.extend(rows)
        orders = store.orders_for_run(run["id"])
        settles = store.settlements_for_order_ids([o["id"] for o in orders]) if orders else {}
        status, why = _night_status(run, len(forecasts), len(orders), go_live, voids)
        row_by_ticker = {r["ticker"]: r for r in rows}
        orows = []
        plabel = str(run.get("prompt_version") or "?")
        if pt and _sha7(_prompt_head(pt)) not in plabel:
            plabel += f" ({_sha7(_prompt_head(pt))})"
        for o in orders:
            s = settles.get(int(o["id"]), {})
            orow = _order_row(o, s, d, row_by_ticker, status)
            orow["week"] = wk
            orow["prompt"] = plabel
            orows.append(orow)
            if orow["ticker"] in row_by_ticker:
                row_by_ticker[orow["ticker"]]["trades"].append(orow)
        slice_orders.extend(orows)
        run_info[run["id"]] = {"date": d, "week": wk, "prompt": plabel, "decision_at": run.get("decision_at"), "status": status}
        if not (start <= d <= end):
            continue
        for r in rows:
            r["night_status"] = status
        wl = _obj(run.get("word_list")) or []
        nights.append({
            "run": run, "date": d, "status": status, "status_why": why,
            "cycle_temp": cycle_temp, "rows": rows, "orders": orows,
            "markets": markets, "word_list": wl,
            "raw": run.get("raw_response") or "",
            "missing_forecast": sorted({m["word"] for m in markets} - {r["word"] for r in rows}),
        })

    # activity log -> per-night timeline
    activity: dict = {}
    try:
        if light:
            raise StopIteration
        lo = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=C.CT).astimezone(timezone.utc)
        hi = (datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=C.CT) + timedelta(days=1, hours=12)).astimezone(timezone.utc)
        for a in store.activity_between(lo, hi):
            d = clock.parse_dt(a["at"]).astimezone(C.CT).strftime("%Y-%m-%d")
            activity.setdefault(d, []).append(a)
    except StopIteration:
        pass
    except Exception:
        log.exception("activity timeline skipped")

    return {
        "start": start, "end": end, "week_id": _week_id(start),
        "settle_totals": settle_totals,
        "nights": nights,
        "history_rows": history_rows,
        "slice_orders": slice_orders,
        "run_info": run_info,
        "run_meta_by_week": run_meta_by_week,
        "all_runs": all_runs,
        "go_live": go_live, "go_live_model": go_live_val,
        "voids": voids,
        "activity": activity,
        "fetch_errors": fetch_errors,
        "n_tickers": len(tickers),
    }


def _all_rows(data):
    return [r for n in data["nights"] for r in n["rows"]]


def _all_orders(data):
    return [o for n in data["nights"] for o in n["orders"]]


def _week_rows(data):
    """Forecast rows of this week that count for forecast-quality stats (not VOID nights)."""
    return [r for n in data["nights"] if n["status"] != "VOID" for r in n["rows"]]


def _scored(rows):
    return [r for r in rows if r["y"] is not None and r["grok"] is not None]


def _valid_scored(rows):
    return [r for r in _scored(rows) if r["quote_valid"] and r["mid"] is not None]


def _included_orders(data):
    return [o for o in _all_orders(data) if not o["excluded"]]


# --------------------------------------------------------------------------
# RULE AUDIT
# --------------------------------------------------------------------------
def _why_no_order(rule, p, bid, ask, valid, reason) -> str:
    if rule == "grok10":
        return f"Grok says {p}: no side (50 has no side)"
    if not valid:
        return f"INVALID QUOTE ({reason or 'bad quote'}) - market-based books must skip"
    if rule in ("fade15", "fade15_gate50"):
        g = strategy.gap_points_exact(p, bid, ask)
        if g is not None and not abs(g) > C.GAP_THRESHOLD:
            return f"gap {g:+.1f} is not > {C.GAP_THRESHOLD}"
        if rule == "fade15_gate50":
            return "gap ok but Grok's own side is not >= 50.01 on the side traded"
    if rule == "edge_exec":
        return f"edge vs ask/bid is not > {C.EDGE_EXEC_THRESHOLD}"
    return "the rule books nothing here"


def _audit(data) -> dict:
    """Recompute every order on TRADED nights with the SAME function the booking code
    uses (strategy.order_for_rule) and list every disagreement. Should be empty."""
    viol: list = []
    checked = expected_n = unaudited = 0
    spec_by_id = {s["id"]: s for s in C.VARIANTS}
    first_night: dict = {}      # a book that has never booked anything (or started later) cannot be "missing" earlier orders
    for n_ in data["nights"]:
        for o_ in n_["orders"]:
            if o_["variant"] not in first_night or n_["date"] < first_night[o_["variant"]]:
                first_night[o_["variant"]] = n_["date"]
    for n in data["nights"]:
        if n["status"] != "TRADED":
            unaudited += len(n["orders"])
            continue
        rows = {r["ticker"]: r for r in n["rows"]}
        actual: dict = {}
        for o in n["orders"]:
            actual.setdefault((o["variant"], o["ticker"]), []).append(o)

        for o in n["orders"]:
            spec = spec_by_id.get(o["variant"])
            r = rows.get(o["ticker"])
            if spec is None:
                unaudited += 1
                continue
            if r is None or r["grok"] is None:
                viol.append({"night": n["date"], "book": o["variant"], "word": o["word"],
                             "kind": "NO FORECAST", "detail": "order has no forecast row"})
                continue
            checked += 1
            rule = o["rule"] or spec["rule"]
            p = int(r["grok"])
            if o["own_quote"]:
                qb, qa = o["q_bid"], o["q_ask"]
                ok, why = Q.validate(qb, qa)
                if r["quote_source"] == "frozen" and (r["bid"], r["ask"]) != (qb, qa):
                    viol.append({"night": n["date"], "book": o["variant"], "word": o["word"],
                                 "kind": "QUOTE CHANGED",
                                 "detail": f"order was booked on bid {qb:g}/ask {qa:g} but the frozen quote is "
                                           f"{r['bid']}/{r['ask']}" if qb is not None and qa is not None else
                                           "order quote differs from frozen quote"})
            else:
                qb, qa, ok, why = r["bid"], r["ask"], r["quote_valid"], r["quote_reason"]
            qbi = int(qb) if qb is not None else None
            qai = int(qa) if qa is not None else None
            exp = strategy.order_for_rule(rule, p, qbi, qai, ok, spec["notional"])
            tag = f"{o['side']} @our {o['our_px']}¢"
            if exp is None:
                kind = "SHOULD NOT EXIST"
                if rule in MARKET_RULES and not ok:
                    kind = "INVALID QUOTE" if o["own_quote"] else "PRE-RULE INVALID QUOTE"
                viol.append({"night": n["date"], "book": o["variant"], "word": o["word"], "kind": kind,
                             "detail": f"{tag}; " + _why_no_order(rule, p, qbi, qai, ok, why)
                                       + (f" [order's own booked gap {o['gap_booked']:+.1f}]" if o["gap_booked"] is not None and rule in MARKET_RULES else "")})
                continue
            if exp["side"] != o["side"]:
                viol.append({"night": n["date"], "book": o["variant"], "word": o["word"], "kind": "WRONG SIDE",
                             "detail": f"booked {o['side']}, rule says {exp['side']}"})
            if abs(int(exp["our_price_cents"]) - int(o["our_px"])) > 1:
                viol.append({"night": n["date"], "book": o["variant"], "word": o["word"], "kind": "WRONG PRICE",
                             "detail": f"booked our price {o['our_px']}¢, rule says {exp['our_price_cents']}¢"})
            if exp["contracts"] and abs(exp["contracts"] - o["intended"]) / exp["contracts"] > 0.02:
                viol.append({"night": n["date"], "book": o["variant"], "word": o["word"], "kind": "WRONG SIZE",
                             "detail": f"booked {o['intended']:.2f} ct, rule says {exp['contracts']:.2f} ct"})
            if rule in ("fade15", "fade15_gate50"):
                if o["threshold_booked"] is not None and o["threshold_booked"] != C.GAP_THRESHOLD:
                    viol.append({"night": n["date"], "book": o["variant"], "word": o["word"],
                                 "kind": "OTHER THRESHOLD",
                                 "detail": f"booked under threshold {o['threshold_booked']:g}, config is {C.GAP_THRESHOLD}"})
                g = strategy.gap_points_exact(p, qbi, qai)
                if g is not None and o["gap_booked"] is not None and abs(g - o["gap_booked"]) > 0.51:
                    viol.append({"night": n["date"], "book": o["variant"], "word": o["word"],
                                 "kind": "GAP DIFFERS FROM QUOTE ON FILE",
                                 "detail": f"order stored gap {o['gap_booked']:+.1f}; Grok {p} vs quote on file "
                                           f"{qbi}/{qai} gives {g:+.1f} (quote on file is not the one used at booking)"})
            expected_n += 1

        # orders that SHOULD exist but do not, and duplicates
        for spec in C.VARIANTS:
            if n["date"] < first_night.get(spec["id"], "9999-12-31"):
                continue            # this book did not exist yet on this night
            for r in n["rows"]:
                if r["grok"] is None:
                    continue
                acts = actual.get((spec["id"], r["ticker"]), [])
                if len(acts) > 1:
                    viol.append({"night": n["date"], "book": spec["id"], "word": r["word"], "kind": "DUPLICATE",
                                 "detail": f"{len(acts)} orders for one word in one book"})
                if acts:
                    continue
                bid = int(r["bid"]) if r["bid"] is not None else None
                ask = int(r["ask"]) if r["ask"] is not None else None
                exp = strategy.order_for_rule(spec["rule"], int(r["grok"]), bid, ask,
                                              r["quote_valid"], spec["notional"])
                if exp is not None:
                    expected_n += 1
                    viol.append({"night": n["date"], "book": spec["id"], "word": r["word"], "kind": "MISSING ORDER",
                                 "detail": f"rule says {exp['side']} @our {exp['our_price_cents']}¢ but no order exists"})
    real = [x for x in viol if x["kind"] != "PRE-RULE INVALID QUOTE"]
    return {"checked": checked, "expected": expected_n, "violations": viol, "real": real, "unaudited": unaudited}


def _audit_block(a) -> list[str]:
    v = a["violations"]
    real = a.get("real", v)
    pre = [x for x in v if x["kind"] == "PRE-RULE INVALID QUOTE"]
    lines = [
        f"orders checked: {a['checked']} (on TRADED nights, every book) | orders the rules call for: {a['expected']} | "
        f"not audited (old-night orders or retired books): {a['unaudited']}",
        f"rules used: A/B fade15 = valid quote AND |Grok - mid| strictly > {C.GAP_THRESHOLD}; "
        "E/F = same + Grok side >= 50.01; G/H = Grok-10 (no market); "
        f"I = edge vs ask/bid strictly > {C.EDGE_EXEC_THRESHOLD}.",
    ]
    if not v:
        lines.append("VIOLATIONS: 0   <- this is what it should say")
        return lines
    if pre:
        lines.append(f"PRE-RULE: {len(pre)} old orders traded a quote that today's rule calls INVALID (usually spread > 25). "
                     "They were booked BEFORE that rule existed, so they are expected, not bugs. They stay in the books.")
    if not real:
        lines.append("REAL VIOLATIONS: 0   <- this is what it should say")
    else:
        lines.append(f"REAL VIOLATIONS: {len(real)}   <- should be 0")
    by_kind: dict = {}
    for x in v:
        by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + 1
    lines.append("all kinds: " + ", ".join(f"{k} x{c}" for k, c in sorted(by_kind.items())))
    lines.append("kinds: INVALID QUOTE = market-based book traded a bad quote | SHOULD NOT EXIST = rule books nothing | "
                 "GAP DIFFERS FROM QUOTE ON FILE = quote on file is not the quote the order was booked on | "
                 "OTHER THRESHOLD = booked under a different threshold | MISSING ORDER = rule wanted a trade that is not there")
    for x in v[:80]:
        lines.append(f"  - {x['night']} [{x['book']}] {x['word']}: {x['kind']} - {x['detail']}")
    if len(v) > 80:
        lines.append(f"  ... and {len(v) - 80} more")
    return lines


# --------------------------------------------------------------------------
# NIGHT STATUS + SCOREBOARDS
# --------------------------------------------------------------------------
def _nights_status_block(data) -> list[str]:
    lines = []
    if data["go_live"] is not None:
        lines.append(f"FILL MODEL '{data['go_live_model']}' went live: {_ts(data['go_live'])}  "
                     "(nights booked before this are FORECAST-ONLY; their orders are left out of the books)")
    else:
        lines.append("FILL MODEL go-live time: not recorded")
    lines.append("TRADED = paper books were live | FORECAST-ONLY = Grok forecast is good data but no trusted trades | "
                 "VOID = no usable forecast, or you marked it with /gap_void")
    lines.append("")
    lines.append(f"{'night':<12}{'status':<15}{'words':>6}{'orders':>7}{'kept':>6}  why")
    for n in data["nights"]:
        kept = sum(1 for o in n["orders"] if not o["excluded"])
        lines.append(f"{n['date']:<12}{n['status']:<15}{len(n['rows']):>6}{len(n['orders']):>7}{kept:>6}  {n['status_why']}")
    counts = {}
    for n in data["nights"]:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    lines.append("")
    lines.append("nights: " + ", ".join(f"{c} {k}" for k, c in sorted(counts.items())) if counts else "no nights this week")
    return lines


def blend(r):
    """Simple average of Grok and the market mid (points). Needs a valid quote."""
    if r.get("grok") is None or r.get("mid") is None:
        return None
    return (r["grok"] + r["mid"]) / 2.0


def _group_stats(rows):
    n = len(rows)
    if not n:
        return None
    yes = sum(r["y"] for r in rows)
    base = yes / n
    return {
        "n": n, "yes": yes, "base": base,
        "grok_mean": _mean([r["grok"] for r in rows]),
        "b_g": _brier([(r["grok"] / 100.0, r["y"]) for r in rows]),
        "b_m": _brier([(r["mid"] / 100.0, r["y"]) for r in rows]),
        "b_bl": _brier([(blend(r) / 100.0, r["y"]) for r in rows]),
        "b_base": _brier([(base, r["y"]) for r in rows]),
    }


def _sb_line(label, st) -> str:
    if not st:
        return f"{label:<14}{'0':>5}   (no valid-quote words with a result)"
    diff = st["b_g"] - st["b_m"]
    return (f"{label:<14}{st['n']:>5}{100 * st['base']:>7.0f}%{_fx(st['grok_mean'], 1):>10}"
            f"{st['b_g']:>10.4f}{st['b_m']:>10.4f}{st['b_bl']:>10.4f}{st['b_base']:>12.4f}"
            f"{diff:>+11.4f}{st['b_bl'] - st['b_m']:>+11.4f}")


def _scoreboard_block(data) -> list[str]:
    wr = _week_rows(data)
    sc = _scored(wr)
    vs = _valid_scored(wr)
    lines = [
        f"VALID-quote words only. Excluded: {sum(1 for r in sc if not r['quote_valid'])} scored words with an "
        f"INVALID/missing quote, {sum(1 for r in wr if r['y'] is None)} words with no result yet.",
        "The market mid is the yardstick. Brier: lower is better. G - M < 0 means Grok BEAT the market. "
        "BL = blend = (Grok + market mid) / 2; BL - M < 0 means the blend beat the market.",
        "",
        f"{'group':<14}{'n':>5}{'YES%':>8}{'avg Grok':>10}{'Brier G':>10}{'Brier M':>10}{'Brier BL':>10}{'Brier base':>12}{'G - M':>11}{'BL - M':>11}",
        _sb_line("all", _group_stats(vs)),
        _sb_line("count words", _group_stats([r for r in vs if r["is_count"]])),
        _sb_line("plain words", _group_stats([r for r in vs if not r["is_count"]])),
        "(base = always predict this group's own YES rate. Small samples: a hint, not a verdict.)",
    ]
    return lines


def _wow_block(data) -> list[str]:
    by_week: dict = {}
    for r in data["history_rows"]:
        if r["y"] is not None and r["grok"] is not None and r["quote_valid"] and r["mid"] is not None:
            by_week.setdefault(r["week"], []).append(r)
    weeks = sorted(set(by_week) | set(data["run_meta_by_week"]))
    lines = [f"{'week':<10}{'prompt (version / sha)':<30}{'nights':>7}{'words':>7}{'Brier G':>9}{'Brier M':>9}{'Brier BL':>9}{'base':>8}{'G - M':>9}"]
    for wk in weeks:
        meta = [m for m in data["run_meta_by_week"].get(wk, []) if m["usable"]]
        labels = []
        for m in meta:
            lab = f"{m['version']}" + (f" ({m['sha']})" if m["sha"] and m["sha"] not in str(m["version"]) else "")
            if lab not in labels:
                labels.append(lab)
        rows = by_week.get(wk, [])
        st = _group_stats(rows)
        mark = "  <- this week" if wk == data["week_id"] else ""
        if st:
            lines.append(f"{wk:<10}{_trunc(' / '.join(labels), 28):<30}{len(meta):>7}{st['n']:>7}{st['b_g']:>9.4f}"
                         f"{st['b_m']:>9.4f}{st['b_bl']:>9.4f}{st['b_base']:>8.4f}{st['b_g'] - st['b_m']:>+9.4f}{mark}")
        else:
            lines.append(f"{wk:<10}{_trunc(' / '.join(labels) or '-', 28):<30}{len(meta):>7}{0:>7}{'n/a':>9}{'n/a':>9}{'n/a':>9}{'n/a':>8}{'n/a':>9}{mark}")
    lines.append("One prompt per week, so each row is one prompt's score against the market. "
                 "'version' is what was recorded when the night was detected; sha = hash of the prompt text actually sent.")
    return lines


# --------------------------------------------------------------------------
# BOOKS: counts, money, partial fills, fill selection
# --------------------------------------------------------------------------
def _book_stats(os_):
    settled = [o for o in os_ if o["state"] in ("won", "lost")]
    w = [o for o in settled if o["state"] == "won"]
    full = [o for o in settled if not o["partial"]]
    risk = sum(o["cost"] for o in settled)
    win_risk = sum(o["cost"] for o in w)
    gross = sum(o["gross"] or 0 for o in settled)
    fees = sum(o["fees"] or 0 for o in settled)
    net = sum(o["net"] or 0 for o in settled)
    return {
        "booked": len(os_),
        "filled": sum(1 for o in os_ if o["filled"] > 0),
        "partial": sum(1 for o in os_ if o["partial"]),
        "unfilled": sum(1 for o in os_ if o["state"] == "unfilled"),
        "pending": sum(1 for o in os_ if o["state"] == "pending"),
        "w": len(w), "l": len(settled) - len(w),
        "full_n": len(full), "full_w": sum(1 for o in full if o["state"] == "won"),
        "risk": risk, "win_risk": win_risk,
        "gross": gross, "fees": fees, "net": net,
        "roi_net": (100.0 * net / risk) if risk else None,
        "roi_gross": (100.0 * gross / risk) if risk else None,
    }


def _books_block(data) -> list[str]:
    lines = []
    inc = _included_orders(data)
    exc = [o for o in _all_orders(data) if o["excluded"]]
    lines.append("Only TRADED nights and VALID-quote orders are counted. Win = the side we held was right.")
    lines.append("Unfilled = paper order never filled (no P&L). Pending = filled, no official result yet. "
                 "Partial = filled less than 98% of the intended size.")
    pre = [o for o in inc if o.get("pre_rule_invalid")]
    if pre:
        lines.append(f"included, but booked on a quote today's rule would skip (booked before the rule existed): "
                     f"{len(pre)} orders, net ${sum(o['net'] or 0 for o in pre) / 100:+.2f} across all books")
    if exc:
        by: dict = {}
        for o in exc:
            by[o["excluded"]] = by.get(o["excluded"], 0) + 1
        lines.append("left out: " + ", ".join(f"{c} orders ({k})" for k, c in sorted(by.items())))
    lines.append("")
    lines.append("COUNTS")
    lines.append(f"{'book':<28}{'booked':>7}{'filled':>7}{'part':>5}{'unfil':>6}{'pend':>5}{'W':>4}{'L':>4}"
                 f"{'hit%':>6}{'$-wtd':>7}{'full-fill hit':>15}")
    tot = {"gross": 0.0, "fees": 0.0, "net": 0.0}
    stats = {}
    for spec in C.VARIANTS:
        os_ = [o for o in inc if o["variant"] == spec["id"]]
        st = _book_stats(os_)
        stats[spec["id"]] = st
        n = st["w"] + st["l"]
        hit = (100.0 * st["w"] / n) if n else None
        wtd = (100.0 * st["win_risk"] / st["risk"]) if st["risk"] else None
        full = f"{_rate(st['full_w'], st['full_n'])}"
        lines.append(f"{spec['label'][:27]:<28}{st['booked']:>7}{st['filled']:>7}{st['partial']:>5}{st['unfilled']:>6}"
                     f"{st['pending']:>5}{st['w']:>4}{st['l']:>4}{_fx(hit, 0):>6}{_fx(wtd, 0):>7}{full:>15}")
        tot["gross"] += st["gross"]; tot["fees"] += st["fees"]; tot["net"] += st["net"]
    lines.append("hit% counts every filled trade equally (a 10-of-117 fill counts as a full win). "
                 "$-wtd weights each trade by dollars at risk. full-fill hit = only trades filled >= 98%.")
    lines.append("")
    lines.append("MONEY (settled, filled orders). gross - fees = net. Fee = 0.07 x contracts x P x (1-P), rounded up.")
    lines.append(f"{'book':<28}{'at-risk$':>10}{'gross$':>10}{'fees$':>9}{'net$':>10}{'ROI net%':>10}{'ROI gross%':>12}")
    for spec in C.VARIANTS:
        st = stats[spec["id"]]
        lines.append(f"{spec['label'][:27]:<28}{st['risk'] / 100:>10.2f}{st['gross'] / 100:>+10.2f}{st['fees'] / 100:>9.2f}"
                     f"{st['net'] / 100:>+10.2f}{_fx(st['roi_net'], 1):>10}{_fx(st['roi_gross'], 1):>12}")
    lines.append(f"ALL BOOKS: gross ${tot['gross'] / 100:+.2f}  fees ${tot['fees'] / 100:.2f}  net ${tot['net'] / 100:+.2f} "
                 "(books are independent paper copies; adding them is only a sanity check)")

    lines.append("")
    lines.append("BY SIDE (settled, filled)")
    lines.append(f"{'book':<6}{'side':<5}{'n':>4}{'W':>4}{'L':>4}{'hit%':>6}{'gross$':>10}{'fees$':>8}{'net$':>10}{'avg our px':>12}")
    for spec in C.VARIANTS:
        for side in ("YES", "NO"):
            os_ = [o for o in inc if o["variant"] == spec["id"] and o["side"] == side and o["state"] in ("won", "lost")]
            if not os_:
                continue
            w = sum(1 for o in os_ if o["state"] == "won")
            lines.append(f"{spec['id']:<6}{side:<5}{len(os_):>4}{w:>4}{len(os_) - w:>4}{_fx(100.0 * w / len(os_), 0):>6}"
                         f"{sum(o['gross'] or 0 for o in os_) / 100:>+10.2f}{sum(o['fees'] or 0 for o in os_) / 100:>8.2f}"
                         f"{sum(o['net'] or 0 for o in os_) / 100:>+10.2f}{_fx(_mean([o['our_px'] for o in os_]), 1, '¢'):>12}")

    lines.append("")
    lines.append("NET $ BY NIGHT AND BOOK (settled, after fees)")
    lines.append(f"{'night':<12}{'status':<15}" + "".join(f"{s['id']:>9}" for s in C.VARIANTS))
    for n in data["nights"]:
        cells = []
        for spec in C.VARIANTS:
            nets = [o["net"] for o in n["orders"] if o["variant"] == spec["id"] and not o["excluded"] and o["net"] is not None and o["filled"] > 0]
            cells.append(f"{sum(nets) / 100:>+9.2f}" if nets else f"{'-':>9}")
        lines.append(f"{n['date']:<12}{n['status']:<15}" + "".join(cells))

    lines.append("")
    lines.append("FILL QUALITY (filled contracts / intended contracts)")
    for spec in C.VARIANTS:
        os_ = [o for o in inc if o["variant"] == spec["id"]]
        intended = sum(o["intended"] for o in os_)
        filled = sum(o["filled"] for o in os_)
        lines.append(f"  {spec['id']}: {filled:.1f} / {intended:.1f} = "
                     f"{_fx(100.0 * filled / intended if intended else None, 0, '%')}")

    part = [o for o in inc if o["partial"]]
    lines.append("")
    lines.append(f"PARTIAL FILLS ({len(part)}): a partial fill is a small bet, not a full one")
    for o in sorted(part, key=lambda x: (x["date"], x["variant"]))[:40]:
        lines.append(f"  {o['date']} [{o['variant']}] {o['word']} {o['side']} filled {o['filled']:.2f} of {o['intended']:.2f} "
                     f"({o['fill_pct']:.0f}%) -> {o['state']}" + (f" net {_money(o['net'])}" if o["net"] is not None else ""))
    return lines


def _k_member(o, book="A", sub=None) -> bool:
    """Is this order in the pre-registered slice? (Books A and B: NO side, Grok <= 30, valid quote,
    |gap| strictly > 15. Book I: same but I's own edge rule already applied at booking.
    sub = "HIGH" (booked mid >= 55) or "LOW" (mid < 55) or None for all of K.)"""
    if o["variant"] != book or o["excluded"] or o["side"] != "NO":
        return False
    if o["grok"] is None or o["grok"] > K_MAX_GROK or not o["quote_valid"]:
        return False
    if sub:
        m = o.get("q_mid")
        if m is None or (sub == "HIGH" and m < K_SPLIT_MID) or (sub == "LOW" and m >= K_SPLIT_MID):
            return False
    if book in ("A", "B"):
        g = o["gap_exact"]
        return g is not None and abs(g) > K_MIN_GAP
    return True


def _k_stats(orders):
    filled = [o for o in orders if o["filled"] > 0]
    settled = [o for o in filled if o["state"] in ("won", "lost")]
    won = [o for o in settled if o["state"] == "won"]
    unf = [o for o in orders if o["state"] == "unfilled" and o["hyp"] is not None]
    risk = sum(o["cost"] for o in settled)
    net = sum(o["net"] or 0 for o in settled)
    px = _mean([o["our_px"] for o in settled])
    fee_pc = _mean([(o["fees"] or 0) / o["filled"] for o in settled if o["filled"]])
    hit = (100.0 * len(won) / len(settled)) if settled else None
    be = (px + fee_pc) if (px is not None and fee_pc is not None) else None
    intended_ct = sum(o["intended"] for o in orders)
    return {
        "fill_ct": (100.0 * sum(o["filled"] for o in orders) / intended_ct) if intended_ct else None,
        "booked": len(orders), "filled": len(filled), "unfilled": sum(1 for o in orders if o["state"] == "unfilled"),
        "w": len(won), "l": len(settled) - len(won),
        "hit": hit,
        "gross": sum(o["gross"] or 0 for o in settled), "fees": sum(o["fees"] or 0 for o in settled), "net": net,
        "roi": (100.0 * net / risk) if risk else None,
        "px": px, "be": be,
        "margin": (hit - be) if (hit is not None and be is not None) else None,
        "unf_hit": (100.0 * sum(1 for o in unf if o["hyp"] > 0) / len(unf)) if unf else None,
        "unf_n": len(unf),
    }


def _k_status(st, weeks_in) -> str:
    if st["filled"] < K_MIN_FILLED:
        tag = f"TOO EARLY (fewer than {K_MIN_FILLED} filled: {st['filled']})"
    elif st["margin"] is None:
        tag = "UNCLEAR"
    elif st["margin"] >= K_PASS_MARGIN:
        tag = f"PASSING (margin {st['margin']:+.1f} points, needs >= +{K_PASS_MARGIN:.0f})"
    elif st["margin"] < 0:
        tag = f"FAILING (margin {st['margin']:+.1f} points)"
    else:
        tag = f"UNCLEAR (margin {st['margin']:+.1f} points, between 0 and +{K_PASS_MARGIN:.0f})"
    if weeks_in >= K_WINDOW_WEEKS or st["filled"] >= K_MIN_FILLED:
        tag += "  [FROZEN WINDOW OVER: this is the final read]"
    else:
        tag += f"  [week {weeks_in} of {K_WINDOW_WEEKS}; frozen until {K_MIN_FILLED} filled or {K_WINDOW_WEEKS} weeks]"
    return tag


def _k_panel(data, book, sub=None) -> dict:
    allo = [o for o in data["slice_orders"] if not o["excluded"]]
    members = [o for o in allo if _k_member(o, book, sub)]
    weeks = sorted({o["week"] for o in allo})
    rows = []
    for wk in weeks:
        wm = [o for o in members if o["week"] == wk]
        plabels = sorted({o["prompt"] for o in allo if o["week"] == wk})
        rows.append({"label": wk, "prompt": " / ".join(plabels), **_k_stats(wm)})
    cum = _k_stats(members)
    rows.append({"label": "CUMULATIVE", "prompt": "all weeks", **cum})
    first = min((o["date"] for o in allo), default=None)
    if first:
        d0 = datetime.strptime(first, "%Y-%m-%d").date()
        mon0 = d0 - timedelta(days=d0.weekday())
        cur = datetime.strptime(data["end"] if data["start"] < "2001" else data["start"], "%Y-%m-%d").date()
        weeks_in = max(1, (cur - mon0).days // 7 + 1)
    else:
        weeks_in = 1
    detail = [{
        "date": o["date"], "book": o["variant"], "word": o["word"], "grok": o["grok"],
        "gap": o["gap_exact"], "our_px": o["our_px"], "fill_pct": round(o["fill_pct"], 0),
        "state": o["state"], "net_dollars": None if o["net"] is None else round(o["net"] / 100.0, 2),
        "is_count": o["is_count"],
    } for o in sorted(members, key=lambda x: (x["date"], x["word"]))]
    return {"rows": rows, "cum": cum, "weeks_in": weeks_in, "status": _k_status(cum, weeks_in), "orders": detail}


def score_panel(data) -> dict:
    """Grok vs market vs blend on valid-quote words, all weeks together and week by week."""
    wr = _week_rows(data)
    vs = _valid_scored(wr)

    def line(label, rows):
        st = _group_stats(rows)
        if not st:
            return None
        return {"group": label, "n": st["n"], "yes_pct": 100 * st["base"], "avg_grok": st["grok_mean"],
                "brier_grok": st["b_g"], "brier_market": st["b_m"], "brier_blend": st["b_bl"], "brier_base": st["b_base"],
                "grok_minus_market": st["b_g"] - st["b_m"], "blend_minus_market": st["b_bl"] - st["b_m"]}

    out = [x for x in (line("all", vs), line("count words", [r for r in vs if r["is_count"]]),
                       line("plain words", [r for r in vs if not r["is_count"]])) if x]
    weekly_rows = []
    for wk in sorted({r["week"] for r in vs}):
        x = line(wk, [r for r in vs if r["week"] == wk])
        if x:
            weekly_rows.append(x)
    return {"cumulative": out, "by_week": weekly_rows}


def slice_panel() -> dict:
    """Everything the Streamlit 'Book K' and 'Strategy lab' tabs show. Database only, no Kalshi calls."""
    end = clock.today_ct()
    data = collect("2000-01-01", end, light=True)
    L = lab.panel(data)
    data["lab"] = L
    weeks = sorted({o["week"] for o in data["slice_orders"]} | {w["week"] for w in L["words"]})
    return {
        "k": _k_panel(data, "A"), "k_high": _k_panel(data, "A", "HIGH"), "k_low": _k_panel(data, "A", "LOW"),
        "kb": _k_panel(data, "B"), "kb_high": _k_panel(data, "B", "HIGH"), "kb_low": _k_panel(data, "B", "LOW"),
        "i": _k_panel(data, "I"),
        "score": score_panel(data),
        "lab": L,
        "recompute": recompute_table(data),
        "weeks": weeks,
        "asof": clock.fmt(clock.now_ct()),
    }


def _k_table(data, book, title, sub=None):
    panel = _k_panel(data, book, sub)
    hdr = (f"{'week':<12}{'prompt':<24}{'booked':>7}{'filled':>7}{'unfil':>6}{'W':>4}{'L':>4}{'hit%':>6}"
           f"{'gross$':>9}{'fees$':>7}{'net$':>9}{'ROI%':>7}{'avg px':>8}{'break-even':>11}{'margin':>8}{'unf would-hit':>15}")
    lines = [title, hdr]
    for r in panel["rows"]:
        uh = f"{_fx(r['unf_hit'], 0)}% ({r['unf_n']})" if r["unf_n"] else "n/a"
        lines.append(f"{r['label']:<12}{_trunc(r['prompt'], 22):<24}{r['booked']:>7}{r['filled']:>7}{r['unfilled']:>6}{r['w']:>4}{r['l']:>4}"
                     f"{_fx(r['hit'], 0):>6}{r['gross'] / 100:>+9.2f}{r['fees'] / 100:>7.2f}{r['net'] / 100:>+9.2f}"
                     f"{_fx(r['roi'], 0):>7}{_fx(r['px'], 1):>8}{_fx(r['be'], 1):>11}{_fx(r['margin'], 1):>8}{uh:>15}")
    return lines, panel


def _slice_name(sub):
    return {None: "K", "HIGH": "K_HIGH (booked mid >= 55)", "LOW": "K_LOW (booked mid < 55)"}[sub]


def _book_k_block(data) -> list[str]:
    lines = [
        "PRE-REGISTERED SLICE. Not new orders: a filter on Book A's (and Book B's) existing orders. The numbers below are FROZEN.",
        f"K = an order where ALL are true: side = NO | Grok <= {K_MAX_GROK} (inclusive) | quote VALID at booking | "
        f"|Grok - mid| strictly > {K_MIN_GAP}. Count words (Trump 5+, Iran 3+) stay in.",
        f"K_HIGH = K orders with booked mid >= {K_SPLIT_MID}; K_LOW = K orders with booked mid < {K_SPLIT_MID}. Kept apart on purpose: "
        "cheap NO tickets (K_HIGH, break-even under ~47%) and dear ones (K_LOW, NO costs 60c+) are different bets.",
        f"Frozen for {K_MIN_FILLED} filled trades or {K_WINDOW_WEEKS} weeks, whichever comes first. Nothing here changes the rules.",
        "break-even hit% = average NO price paid + average fee per contract (points). margin = hit% - break-even.",
        "unf would-hit = of the slice's orders that never filled, the share that would have won had they filled (count in brackets).",
        "",
    ]
    for book, name in (("A", "BOOK A ($1)"), ("B", "BOOK B ($100 copy of A)")):
        for sub in (None, "HIGH", "LOW"):
            tab, panel = _k_table(data, book, f"{name} · {_slice_name(sub)}", sub)
            lines += tab
            lines.append("STATUS: " + panel["status"])
            lines.append("")
    lines += _ab_compare(data)
    lines.append("")
    tab_i, _ = _k_table(data, "I", "BOOK I, same slice as K (side = NO, Grok <= 30, valid quote; I's own edge rule applies)")
    lines += tab_i
    return lines


def k_orders_ab(data) -> list[dict]:
    return [o for o in data["slice_orders"] if not o["excluded"] and (_k_member(o, "A") or _k_member(o, "B"))]


def recompute_table(data) -> list[dict]:
    """K orders of Books A and B replayed with the no-double-counting fill rule (depth history permitting)."""
    return lab.recompute_fills(k_orders_ab(data))


def ab_verdict(a, b) -> str:
    """Does the edge survive at size? B's margin within ~5 pts of A's AND B's contract fill % not >10 pts lower."""
    if a["filled"] >= 1 and b["filled"] >= 1 and a["margin"] is not None and b["margin"] is not None:
        close = abs(b["margin"] - a["margin"]) <= 5.0
        fill_ok = (b["fill_ct"] or 0) >= (a["fill_ct"] or 0) - 10.0
        v = "EDGE SURVIVES AT SIZE" if (close and fill_ok) else "DOES NOT SURVIVE AT SIZE (margin moved > 5 pts or fill% much lower)"
        if min(a["filled"], b["filled"]) < 10:
            v += "  [TOO FEW TRADES to trust]"
        return v
    return "TOO EARLY"


def _ab_compare(data) -> list[str]:
    allo = [o for o in data["slice_orders"] if not o["excluded"]]
    lines = ["SIDE BY SIDE: does the edge survive at size? (Book A = $1, Book B = $100 copy; cumulative, same orders)",
             f"{'slice':<26}{'book':<6}{'booked':>7}{'filled':>7}{'fill% ct':>9}{'avg px':>8}{'hit%':>6}{'margin':>8}{'net$':>10}   verdict"]
    for sub in (None, "HIGH", "LOW"):
        st = {}
        for book in ("A", "B"):
            st[book] = _k_stats([o for o in allo if _k_member(o, book, sub)])
        verdict = ab_verdict(st["A"], st["B"])
        for book in ("A", "B"):
            r = st[book]
            lines.append(f"{_slice_name(sub)[:25]:<26}{book:<6}{r['booked']:>7}{r['filled']:>7}{_fx(r['fill_ct'], 0):>9}"
                         f"{_fx(r['px'], 1):>8}{_fx(r['hit'], 0):>6}{_fx(r['margin'], 1):>8}{r['net'] / 100:>+10.2f}   "
                         + (verdict if book == "A" else ""))
    lines.append("'Survives' = B's margin within about 5 points of A's AND B's contract fill % not more than 10 points lower.")
    lines.append("WARNING: W38's Book B fills came from the OLD paper model, which counted the same displayed liquidity on every poll. "
                 "That is why B looks like A x 100. B is NOT evidence that $100 fills. From v1.5.6 an order can only take the size it sees. "
                 "Also: every paper order is priced at its own limit price whatever its size, so dollars just scale; a real $100 order pays worse prices as it walks the book.")
    lines.append("REAL SIZE evidence is the 'take at market' size sweep in section 4D (walks the actual order book).")
    rc = recompute_table(data)
    if rc:
        lines += ["", "RECOMPUTED: the same K orders replayed against the stored depth history with the no-double-counting rule (booking time + cancel window)",
                  f"{'night':<12}{'book':<5}{'word':<26}{'want ct':>8}{'paper ct':>9}{'real ct':>9}{'paper net$':>11}{'real net$':>10}"]
        for r in rc:
            if r.get("note"):
                lines.append(f"{r['date']:<12}{r['book']:<5}{_trunc(r['word'], 25):<26}{r['intended']:>8.1f}{r['paper_filled']:>9.1f}   {r['note']}")
            else:
                lines.append(f"{r['date']:<12}{r['book']:<5}{_trunc(r['word'], 25):<26}{r['intended']:>8.1f}{r['paper_filled']:>9.1f}{r['real_filled']:>9.1f}"
                             f"{_fx(r['paper_net'], 2):>11}{_fx(r['real_net'], 2):>10}")
    return lines


def _buckets_block(data) -> list[str]:
    rows = lab.fills_by_bucket(data["slice_orders"])
    lines = ["Every FILLED, settled order, after fees, split by Grok's probability. Books A, B and I. Weekly and cumulative.",
             "(In the live no-fade bot the same table showed 8 of 12 wins for Grok <= 30 against 10 of 37 above 30.)",
             f"{'book':<5}{'scope':<12}{'bucket':<13}{'fills':>6}{'wins':>5}{'hit%':>6}{'avg px':>8}{'net$':>10}"]
    for r in rows:
        if r["scope"] == "CUMULATIVE" or r["fills"]:
            lines.append(f"{r['book']:<5}{r['scope']:<12}{r['bucket']:<13}{r['fills']:>6}{r['wins']:>5}{_fx(r['hit'], 0):>6}"
                         f"{_fx(r['avg_px'], 1):>8}{r['net']:>+10.2f}")
    return lines


def _capacity_block(data) -> list[str]:
    L = data["lab"]
    cov = L["coverage"]
    lines = [
        "All of this is REPORT-ONLY (no orders). It uses the ORDER BOOK AT THE DECISION TIME (nearest depth snapshot within +/- 15 min of the",
        "scheduled send time; saved in gap_decision_books, backfilled from the shared depth table for older nights).",
        f"coverage: {cov['words']} scored words, {cov['with_book']} have a decision-time book, {cov['valid']} of those have a VALID quote.",
        "",
        "CAPACITY: dollars of NO you could buy from YES bids at or above the floor (yes_book top 10 levels; sum of contracts x (100 - price)/100)",
        f"{'group':<20}{'YES bid >=':>11}{'n':>5}{'median$':>10}{'p75$':>10}{'p90$':>10}",
    ]
    for r in L["capacity"]:
        lines.append(f"{r['group']:<20}{r['floor']:>11}{r['n']:>5}{_fx(r['median'], 2):>10}{_fx(r['p75'], 2):>10}{_fx(r['p90'], 2):>10}")
    lines += ["", "SEGMENT FREQUENCY: K_HIGH candidates = Grok <= 30 with a valid decision-time mid >= 55",
              f"{'night':<12}{'candidates':>11}{'booked A':>10}{'filled':>7}{'no book':>9}   words"]
    for r in L["segments"]:
        lines.append(f"{r['date']:<12}{r['candidates']:>11}{r['booked_by_A']:>10}{r['filled']:>7}{r['no_book']:>9}   {r['words']}")
    lines += ["", "K_HIGH SIZE SWEEP (report only): TAKE NO at the decision-time book on every K_HIGH candidate at each dollar size.",
              "Walk the yes_book from the best bid down, pay (100 - bid) per contract until the dollars or the top 10 levels run out.",
              f"{'scope':<12}{'size$':>6}{'signals':>8}{'trades':>7}{'fill%':>7}{'avg NO px':>10}{'fee/ct':>7}{'break-even':>11}{'hit%':>6}{'90% range':>12}{'margin':>8}{'net$':>9}"]
    for r in L["sweep"]:
        rng = f"{_fx(r['lo'], 0)}-{_fx(r['hi'], 0)}" if r["lo"] is not None else "n/a"
        lines.append(f"{r['scope']:<12}{r['size']:>6}{r['signals']:>8}{r['trades']:>7}{_fx(r['filled_pct'], 0):>7}{_fx(r['avg_px'], 1):>10}"
                     f"{_fx(r['fee_pc'], 1):>7}{_fx(r['be'], 1):>11}{_fx(r['hit'], 0):>6}{rng:>12}{_fx(r['margin'], 1):>8}{r['net']:>+9.2f}")
    lines.append("PRE-REGISTERED PASS: " + L["sweep_pass"]["status"])
    for vname in ("K", "K_LOW"):
        lines += ["", f"{vname} SIZE SWEEP (cumulative, take at market, same method)",
                  f"{'size$':>6}{'signals':>8}{'trades':>7}{'fill%':>7}{'avg NO px':>10}{'break-even':>11}{'hit%':>6}{'margin':>8}{'net$':>9}"]
        for r in [x for x in L["sweeps"][vname] if x["scope"] == "CUMULATIVE"]:
            lines.append(f"{r['size']:>6}{r['signals']:>8}{r['trades']:>7}{_fx(r['filled_pct'], 0):>7}{_fx(r['avg_px'], 1):>10}"
                         f"{_fx(r['be'], 1):>11}{_fx(r['hit'], 0):>6}{_fx(r['margin'], 1):>8}{r['net']:>+9.2f}")
    # long-rest shadow
    kh = [o for o in data["slice_orders"] if not o["excluded"] and (_k_member(o, "A", "HIGH") or _k_member(o, "B", "HIGH"))]
    lines += ["", "LONG-REST SHADOW for K_HIGH orders (report only): what if each order kept resting until 17:28 CT instead of 60 min after booking?",
              "(replays the depth history with the same no-double-counting rule as the fill model; blank if the depth history was pruned)"]
    if not kh:
        lines.append("no K_HIGH orders yet")
    else:
        lines.append(f"{'night':<12}{'book':<5}{'word':<26}{'want ct':>8}{'actual ct':>10}{'long ct':>9}{'actual net$':>12}{'long net$':>10}")
        for r in lab.long_rest_shadow(kh):
            if r.get("note"):
                lines.append(f"{r['date']:<12}{r['book']:<5}{_trunc(r['word'], 25):<26}{r['intended']:>8.1f}   {r['note']}")
            else:
                lines.append(f"{r['date']:<12}{r['book']:<5}{_trunc(r['word'], 25):<26}{r['intended']:>8.1f}{r['actual_filled']:>10.1f}"
                             f"{r['long_filled']:>9.1f}{_fx(r['actual_net'], 2):>12}{_fx(r['long_net'], 2):>10}")
    return lines


def _m_block(data) -> list[str]:
    L = data["lab"]
    lines = [
        f"BOOK M (pre-registered, no rules to tune): buy NO at market on EVERY word with a valid frozen YES bid >= {lab.M_MIN_YES_BID}, regardless of Grok.",
        "Fill = walk the YES-bid book from the best bid down, pay (100 - bid), fees included. $1 per trade.",
        "M_GROKLOW = M words where Grok is also low (Grok <= 30, market > 15 above). M_REST = M words where Grok is NOT low.",
        "If M is at break-even and K_HIGH is above it, Grok is what makes taking at market work. If M also clears break-even, there is a bigger, deeper edge.",
        f"{'scope':<12}{'variant':<11}{'trades':>7}{'avg NO px':>10}{'fee/ct':>7}{'break-even':>11}{'hit%':>6}{'90% range':>11}{'margin':>8}{'net$':>8}{'ROI%':>6}",
    ]
    for r in L["m"]:
        rng = f"{_fx(r['lo'], 0)}-{_fx(r['hi'], 0)}" if r["lo"] is not None else "n/a"
        lines.append(f"{r['scope']:<12}{r['variant']:<11}{r['trades']:>7}{_fx(r['avg_px'], 1):>10}{_fx(r['fee_pc'], 1):>7}{_fx(r['be'], 1):>11}"
                     f"{_fx(r['hit'], 0):>6}{rng:>11}{_fx(r['margin'], 1):>8}{r['net']:>+8.2f}{_fx(r['roi'], 0):>6}")
    lines += ["READING: " + L["m_reading"], "",
              "REAL SIZE for M (book walk, cumulative): price gets worse and fill % falls as size grows",
              f"{'size$':>6}{'signals':>8}{'trades':>7}{'fill%':>7}{'avg NO px':>10}{'break-even':>11}{'hit%':>6}{'margin':>8}{'net$':>9}"]
    for r in L["sweeps"]["M"]:
        if r["scope"] == "CUMULATIVE":
            lines.append(f"{r['size']:>6}{r['signals']:>8}{r['trades']:>7}{_fx(r['filled_pct'], 0):>7}{_fx(r['avg_px'], 1):>10}{_fx(r['be'], 1):>11}"
                         f"{_fx(r['hit'], 0):>6}{_fx(r['margin'], 1):>8}{r['net']:>+9.2f}")
    return lines


def _lab_block(data) -> list[str]:
    L = data["lab"]
    words = L["words"]
    lines = [
        "STRATEGY LAB (report only). Question: which way of trading Grok's forecasts could grow an account fastest and highest, and how sure are we?",
        "Every variant is TAKEN AT MARKET on the decision-time book at $25 per trade (so fill timing does not matter), fees included.",
        f"Weeks before {lab.FROZEN_FROM} are IN-SAMPLE (we designed these while looking at them). Only {lab.FROZEN_FROM} onward is out-of-sample.",
        "verdict: CLEAR = even the low end of the 90% range beats break-even | POSSIBLE = beats break-even but the range crosses it | TOO FEW = under 10 trades.",
        "",
        f"{'variant':<16}{'family':<15}{'period':<14}{'trades':>7}{'hit%':>6}{'90% range':>11}{'break-even':>11}{'margin':>8}{'net$':>9}{'ROI%':>6}  verdict",
    ]
    for r in lab.leaderboard(words, 25.0):
        for label, key in (("all weeks", "all"), ("in-sample", "in_sample"), ("out-of-sample", "out_of_sample")):
            st = r[key]
            if key != "all" and st["trades"] == 0:
                if key == "out_of_sample":
                    lines.append(f"{r['id']:<16}{r['family']:<15}{label:<14}{'-':>7}   (waiting for {lab.FROZEN_FROM})")
                continue
            rng = f"{_fx(st['lo'], 0)}-{_fx(st['hi'], 0)}" if st["lo"] is not None else "n/a"
            lines.append(f"{r['id']:<16}{r['family']:<15}{label:<14}{st['trades']:>7}{_fx(st['hit'], 0):>6}{rng:>11}{_fx(st['be'], 1):>11}"
                         f"{_fx(st['margin'], 1):>8}{st['net']:>+9.2f}{_fx(st['roi'], 0):>6}  "
                         f"{(st['verdict'] + ('  [' + r['note'] + ']' if r['note'] else '')) if key == 'all' else ''}")
    lines += ["", "EXPLORATORY GRID (NO when Grok <= g, market mid >= m, market more than 15 above Grok). $25 per trade. These are HYPOTHESES for next week, not results:",
              "trying 20 cells guarantees some look good by luck. Only a cell that keeps winning on NEW weeks means anything.",
              f"{'Grok <=':>8}{'mid >=':>8}{'trades':>7}{'hit%':>6}{'break-even':>11}{'margin':>8}{'net$':>9}"]
    for r in lab.grid_no(words):
        if r["trades"]:
            lines.append(f"{r['grok_max']:>8}{r['mid_min']:>8}{r['trades']:>7}{_fx(r['hit'], 0):>6}{_fx(r['be'], 1):>11}{_fx(r['margin'], 1):>8}{r['net']:>+9.2f}")
    # growth summary for the pre-registered segments
    lines += ["", "GROWTH CHECK (report only): what the data would need to justify sizing up. Kelly = the bet size (share of bankroll) that grows an account fastest IF the hit rate is real.",
              "Full Kelly is far too aggressive on a few trades. It is shown using the LOW end of the 90% range, then divided by 4.",
              f"{'variant':<12}{'trades':>7}{'trades/night':>13}{'hit%':>6}{'low end%':>9}{'avg px':>8}{'Kelly@low':>10}{'1/4 Kelly':>10}"]
    for vid, fn in (("K", lab.v_k), ("K_HIGH", lab.v_k_high), ("K_LOW", lab.v_k_low)):
        g = lab.growth_summary(words, fn)
        st = g["stats"]
        if st["trades"] and st["lo"] is not None and st["avg_px"] is not None:
            kl = lab.kelly_fraction(st["lo"], st["avg_px"], st["fee_pc"] or 0)
            lines.append(f"{vid:<12}{st['trades']:>7}{g['trades_per_night']:>13.1f}{_fx(st['hit'], 0):>6}{_fx(st['lo'], 0):>9}{_fx(st['avg_px'], 1):>8}"
                         f"{100 * kl:>9.0f}%{25 * kl:>9.1f}%")
        else:
            lines.append(f"{vid:<12}{0:>7}")
    lines.append("Bankroll simulator, Monte Carlo spread and the week picker are on the Streamlit 'Strategy lab' tab.")
    return lines


def _fill_selection_block(data) -> list[str]:
    """Do orders fill mostly when the market moves against us? Compare what FILLED with
    what would have happened had the UNFILLED ones filled at their limit (gross, no fees)."""
    inc = _included_orders(data)
    lines = [
        "For each book: filled orders vs orders that never filled (had they filled at their limit).",
        "If unfilled orders would have done clearly better than filled ones, fills are selecting against us.",
        f"{'book':<6}{'filled n':>9}{'hit%':>7}{'gross ROI%':>12}   {'unfilled n':>11}{'would-hit%':>11}{'would-be ROI%':>14}",
    ]
    for spec in C.VARIANTS:
        os_ = [o for o in inc if o["variant"] == spec["id"]]
        f = [o for o in os_ if o["state"] in ("won", "lost")]
        u = [o for o in os_ if o["state"] == "unfilled" and o["hyp"] is not None]
        fw = sum(1 for o in f if o["state"] == "won")
        risk = sum(o["cost"] for o in f)
        groi = (100.0 * sum(o["gross"] or 0 for o in f) / risk) if risk else None
        uw = sum(1 for o in u if (o["hyp"] or 0) > 0)
        urisk = sum(o["intended"] * o["our_px"] for o in u)
        uroi = (100.0 * sum(o["hyp"] for o in u) / urisk) if urisk else None
        lines.append(f"{spec['id']:<6}{len(f):>9}{_fx(100.0 * fw / len(f) if f else None, 0):>7}{_fx(groi, 1):>12}   "
                     f"{len(u):>11}{_fx(100.0 * uw / len(u) if u else None, 0):>11}{_fx(uroi, 1):>14}")
    lines.append("Small samples. It is evidence for a question, not an answer.")
    return lines


# --------------------------------------------------------------------------
# HOW GOOD IS GROK (calibration etc.)
# --------------------------------------------------------------------------
def _calibration_block(data) -> list[str]:
    lines = []
    wr = _week_rows(data)
    sc = _scored(wr)
    if not sc:
        return ["no scored words yet"]
    vs = _valid_scored(wr)
    n = len(sc)
    yes = sum(r["y"] for r in sc)
    base = yes / n
    lines.append(f"words scored: {n}   said YES: {yes} ({100 * base:.0f}%)   said NO: {n - yes}")
    lines.append(f"mean Grok probability: {_fx(_mean([r['grok'] for r in sc]), 1)}%   actual YES rate: {100 * base:.1f}%")
    lines.append(f"Brier, Grok on all {n} scored words: {_fx(_brier([(r['grok'] / 100.0, r['y']) for r in sc]), 4)}")
    lines.append(f"(market comparisons below use only the {len(vs)} words with a VALID quote)")
    closer = sum(1 for r in vs if abs(r["grok"] / 100.0 - r["y"]) < abs(r["mid"] / 100.0 - r["y"]))
    lines.append(f"Grok closer to the truth than the market mid: {_rate(closer, len(vs))}")

    def dir_ok(p, y):
        if p >= 51:
            return y == 1
        if p <= 49:
            return y == 0
        return None

    d = [dir_ok(r["grok"], r["y"]) for r in sc]
    d = [x for x in d if x is not None]
    lines.append(f"Grok side-of-50 correct (p>=51 -> YES, p<=49 -> NO): {_rate(sum(d), len(d))}")
    dm = [(r["mid"] > 50 and r["y"] == 1) or (r["mid"] < 50 and r["y"] == 0) for r in vs if r["mid"] != 50]
    lines.append(f"Market side-of-50 correct (valid quotes):            {_rate(sum(dm), len(dm))}")

    lines.append("")
    lines.append("CALIBRATION, WIDE BUCKETS — when Grok says X%, how often did it really happen?")
    lines.append(f"{'Grok p':<9}{'n':>4}{'avg Grok':>10}{'said YES':>13}{'avg mkt':>9}{'Brier G':>9}")
    for lo, hi, label in WIDE_BINS:
        b = [r for r in sc if lo <= r["grok"] < hi]
        if not b:
            continue
        lines.append(f"{label:<9}{len(b):>4}{_fx(_mean([r['grok'] for r in b]), 1):>10}"
                     f"{_rate(sum(r['y'] for r in b), len(b)):>13}{_fx(_mean([r['mid'] for r in b if r['mid'] is not None]), 1):>9}"
                     f"{_fx(_brier([(r['grok'] / 100.0, r['y']) for r in b]), 3):>9}")
    lines.append("")
    lines.append("CALIBRATION, 10-POINT BUCKETS (thin: 4-10 words each)")
    lines.append(f"{'Grok p':<10}{'n':>4}{'avg Grok':>10}{'said YES':>13}{'avg mkt':>9}{'avg gap':>9}")
    for lo, hi in P_BINS:
        b = [r for r in sc if lo <= r["grok"] < hi]
        if not b:
            continue
        lines.append(f"{f'{lo}-{hi - 1}':<10}{len(b):>4}{_fx(_mean([r['grok'] for r in b]), 1):>10}"
                     f"{_rate(sum(r['y'] for r in b), len(b)):>13}{_fx(_mean([r['mid'] for r in b if r['mid'] is not None]), 1):>9}"
                     f"{_fx(_mean([r['gap'] for r in b if r['gap'] is not None]), 1):>9}")

    lines.append("")
    lines.append("THRESHOLD SCAN A — words Grok put AT OR ABOVE X%: what share said YES?")
    lines.append(f"{'X':>4}{'n':>5}{'said YES':>14}{'avg Grok':>10}{'mkt YES%':>10}")
    for t in THRESHOLDS:
        b = [r for r in sc if r["grok"] >= t]
        if not b:
            continue
        lines.append(f"{t:>4}{len(b):>5}{_rate(sum(r['y'] for r in b), len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b if r['mid'] is not None]), 1):>10}")
    lines.append("")
    lines.append("THRESHOLD SCAN B — words Grok put AT OR BELOW X%: what share said NO?")
    lines.append(f"{'X':>4}{'n':>5}{'said NO':>14}{'avg Grok':>10}{'mkt YES%':>10}")
    for t in THRESHOLDS:
        b = [r for r in sc if r["grok"] <= t]
        if not b:
            continue
        lines.append(f"{t:>4}{len(b):>5}{_rate(sum(1 - r['y'] for r in b), len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b if r['mid'] is not None]), 1):>10}")

    lines.append("")
    lines.append("GAP ANALYSIS (valid quotes only) — gap = Grok% minus market mid% (positive = Grok more bullish)")
    lines.append(f"{'gap bin':<12}{'n':>4}{'said YES':>14}{'avg Grok':>10}{'avg mkt':>9}")
    gsc = [r for r in vs if r["gap"] is not None]
    for lo, hi in GAP_BINS:
        b = [r for r in gsc if lo <= r["gap"] < hi]
        if not b:
            continue
        label = f"{max(lo, -100):+d}..{min(hi, 100):+d}"
        lines.append(f"{label:<12}{len(b):>4}{_rate(sum(r['y'] for r in b), len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b]), 1):>9}")
    above = [r for r in gsc if r["gap"] > C.GAP_THRESHOLD]
    below = [r for r in gsc if r["gap"] < -C.GAP_THRESHOLD]
    lines.append(f"Grok >{C.GAP_THRESHOLD} above market: said YES {_rate(sum(r['y'] for r in above), len(above))}   "
                 f"avg market-implied YES {_fx(_mean([r['mid'] for r in above]), 1)}%")
    lines.append(f"Grok >{C.GAP_THRESHOLD} below market: said NO  {_rate(sum(1 - r['y'] for r in below), len(below))}   "
                 f"avg market-implied NO {_fx(_mean([100 - r['mid'] for r in below]), 1)}%")
    lines.append("Read: the market-implied number is the break-even. A signal only pays if its hit rate beats it.")

    lines.append("")
    lines.append("WORD TYPE (valid quotes)")
    lines.append(f"{'type':<22}{'n':>4}{'said YES':>14}{'avg Grok':>10}{'Brier G':>9}{'Brier M':>9}")
    for label, flag in (("count words (5+ etc)", True), ("plain words", False)):
        b = [r for r in vs if r["is_count"] == flag]
        if not b:
            continue
        lines.append(f"{label:<22}{len(b):>4}{_rate(sum(r['y'] for r in b), len(b)):>14}{_fx(_mean([r['grok'] for r in b]), 1):>10}"
                     f"{_fx(_brier([(r['grok'] / 100.0, r['y']) for r in b]), 3):>9}{_fx(_brier([(r['mid'] / 100.0, r['y']) for r in b]), 3):>9}")

    lines.append("")
    lines.append("GROK'S OWN COMPONENTS (p_block_airs = a carrying line airs; p_said_given_airs = anchor says the exact word)")
    lines.append(f"{'group':<12}{'n':>4}{'avg p_block':>13}{'avg p_said':>12}{'avg Grok':>10}")
    for label, val in (("said YES", 1), ("said NO", 0)):
        b = [r for r in sc if r["y"] == val]
        lines.append(f"{label:<12}{len(b):>4}{_fx(_mean([r['p_block'] for r in b]), 2):>13}"
                     f"{_fx(_mean([r['p_said'] for r in b]), 2):>12}{_fx(_mean([r['grok'] for r in b]), 1):>10}")
    hb = [r for r in sc if r["p_block"] is not None and r["p_block"] >= 0.7]
    lines.append(f"p_block_airs >= 0.70: n={len(hb)}, said YES {_rate(sum(r['y'] for r in hb), len(hb))}")
    hs = [r for r in sc if r["p_said"] is not None and r["p_said"] >= 0.7]
    lines.append(f"p_said_given_airs >= 0.70: n={len(hs)}, said YES {_rate(sum(r['y'] for r in hs), len(hs))}")
    ls = [r for r in sc if r["p_said"] is not None and r["p_said"] < 0.5]
    lines.append(f"p_said_given_airs < 0.50: n={len(ls)}, said YES {_rate(sum(r['y'] for r in ls), len(ls))}")
    prod = [abs(r["grok"] / 100.0 - r["p_block"] * r["p_said"]) for r in sc if r["p_block"] is not None and r["p_said"] is not None]
    lines.append(f"avg |probability - p_block*p_said|: {_fx(_mean(prod), 3)}")

    lines.append("")
    lines.append("SUBSTITUTE-WORD RISK (Grok named something else the anchor might say)")
    for label, flag in (("substitute risk named", True), ("'none obvious'", False)):
        b = [r for r in sc if r["has_sub"] == flag]
        if b:
            lines.append(f"  {label:<24} n={len(b):<4} said YES {_rate(sum(r['y'] for r in b), len(b)):<14} avg Grok {_fx(_mean([r['grok'] for r in b]), 1)}%")
    return lines


def _trade_slices_block(data) -> list[str]:
    lines = []
    orders = [o for o in _included_orders(data) if o["state"] in ("won", "lost")]
    if not orders:
        return ["no settled trades yet"]

    def table(title, os_, bins, keyfn, fmt):
        lines.append(title)
        lines.append(f"{'bucket':<14}{'n':>4}{'W':>4}{'L':>4}{'hit%':>6}{'gross$':>9}{'fees$':>7}{'net$':>9}{'avg our px':>12}")
        for lo, hi in bins:
            b = [o for o in os_ if keyfn(o) is not None and lo <= keyfn(o) < hi]
            if not b:
                continue
            w = sum(1 for o in b if o["state"] == "won")
            lines.append(f"{fmt(lo, hi):<14}{len(b):>4}{w:>4}{len(b) - w:>4}{_fx(100.0 * w / len(b), 0):>6}"
                         f"{sum(o['gross'] or 0 for o in b) / 100:>+9.2f}{sum(o['fees'] or 0 for o in b) / 100:>7.2f}"
                         f"{sum(o['net'] or 0 for o in b) / 100:>+9.2f}{_fx(_mean([o['our_px'] for o in b]), 1, '¢'):>12}")
        lines.append("")

    for vid in ("A", "E", "I"):
        os_ = [o for o in orders if o["variant"] == vid]
        if os_:
            table(f"BOOK {vid}: by size of the gap/edge at booking (points)", os_, ABS_GAP_BINS,
                  lambda o: abs(o["gap_booked"]) if o["gap_booked"] is not None else None,
                  lambda lo, hi: f"{lo}-{min(hi, 100) - 1}")
    for vid in ("A", "E", "G", "I"):
        os_ = [o for o in orders if o["variant"] == vid]
        if os_:
            table(f"BOOK {vid}: by Grok probability", os_, P_BINS, lambda o: o["grok"],
                  lambda lo, hi: f"Grok {lo}-{hi - 1}")
    return lines


def _misses_block(data) -> list[str]:
    lines = []
    wr = _week_rows(data)
    sc = _scored(wr)
    if not sc:
        return ["no scored words yet"]
    lines.append("BIGGEST MISSES — Grok was confident and wrong (worst first)")
    worst = sorted(sc, key=lambda r: -abs(r["grok"] / 100.0 - r["y"]))[:10]
    for r in worst:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {_fx(r['mid'], 0)}%  -> {str(r['outcome']).upper()}")
        lines.append(f"    story: {_trunc(r['story'], 200)}")
        lines.append(f"    substitute: {_trunc(r['substitute_risk'], 160)}")
        lines.append(f"    reason: {_trunc(r['reasoning'], 420)}")
    vs = _valid_scored(wr)
    lines.append("")
    lines.append("GROK RIGHT, MARKET WRONG — where Grok added the most value (valid quotes)")
    for r in sorted(vs, key=lambda r: -(abs(r["mid"] / 100.0 - r["y"]) - abs(r["grok"] / 100.0 - r["y"])))[:6]:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {r['mid']:.0f}%  -> {str(r['outcome']).upper()}   ({_trunc(r['story'], 120)})")
    lines.append("")
    lines.append("MARKET RIGHT, GROK WRONG — where the market was smarter (valid quotes)")
    for r in sorted(vs, key=lambda r: -(abs(r["grok"] / 100.0 - r["y"]) - abs(r["mid"] / 100.0 - r["y"])))[:6]:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {r['mid']:.0f}%  -> {str(r['outcome']).upper()}   ({_trunc(r['story'], 120)})")
    return lines


# --------------------------------------------------------------------------
# DATA QUALITY
# --------------------------------------------------------------------------
def _quality_block(data) -> list[str]:
    lines = []
    rows = _all_rows(data)
    orders = _all_orders(data)
    flags = 0
    for n in data["nights"]:
        run = n["run"]
        if run.get("status") != "parsed":
            flags += 1
            lines.append(f"! {n['date']} run status={run.get('status')} parse_error={run.get('parse_error')}")
        elif run.get("parse_error"):
            flags += 1
            lines.append(f"! {n['date']} parse_error: {_trunc(run.get('parse_error'), 200)}")
        if len(n["rows"]) != len(n["markets"]):
            flags += 1
            lines.append(f"! {n['date']} forecasts={len(n['rows'])} but markets={len(n['markets'])} "
                         f"missing: {', '.join(n['missing_forecast']) or '?'}")
        if not n["raw"]:
            flags += 1
            lines.append(f"! {n['date']} no Grok JSON was pasted")

    bad = [r for r in rows if not r["quote_valid"]]
    if bad:
        flags += 1
        lines.append(f"! {len(bad)} of {len(rows)} words have an INVALID or missing quote "
                     "(they are left out of every Grok-vs-market number, and market-based books skip them):")
        for r in bad[:40]:
            res = str(r["outcome"] or "open").upper()
            lines.append(f"    INVALID QUOTE {r['date']} {r['word']}: bid {_fx(r['bid'], 0)} / ask {_fx(r['ask'], 0)} "
                         f"({r['quote_reason']}) [{r['quote_source']}] result {res}")
        if len(bad) > 40:
            lines.append(f"    ... and {len(bad) - 40} more")
        leak = [r for r in bad if r["y"] is not None and r["bid"] is not None and r["ask"] is not None and
                ((r["y"] == 1 and r["bid"] >= 99) or (r["y"] == 0 and r["bid"] <= 1 and r["ask"] <= 1))]
        if leak:
            lines.append(f"    HINT: {len(leak)} of these look like a POST-CLOSE / collapsed book (bid 99+ on words that said YES, "
                         "bid 0 / ask 1 on words that said NO), i.e. read after the answer was known. "
                         "(A dead market can also quote 0/1 legitimately, so this is a hint, not proof.)")

    legacy = [r for r in rows if r["quote_source"] == "legacy"]
    frozen = [r for r in rows if r["quote_source"] == "frozen"]
    multi = [r for r in legacy if r["quote_rows"] > 1]
    lines.append(f"quotes: {len(frozen)} frozen at decision time, {len(legacy)} old-style (the row written when booking ran, "
                 f"which can be 30+ minutes after the decision time), {sum(1 for r in rows if r['quote_source'] == 'none')} none")
    if multi:
        flags += 1
        lines.append(f"! {len(multi)} old-style quotes have MORE THAN ONE row. The row written when booking ran is used; "
                     "older code also logged quotes before booking and all evening after it (often after the show, when "
                     "the book had collapsed). Those other rows are ignored.")
    ages = [r["quote_age_s"] for r in frozen if r["quote_age_s"] is not None]
    if ages:
        lines.append(f"frozen quote age (decision moment minus snapshot time): min {min(ages)}s, avg {sum(ages) / len(ages):.0f}s, max {max(ages)}s")

    fe = {d: sum(1 for a in evs if a.get("kind") == "paper_fill") for d, evs in sorted(data["activity"].items())}
    fe = {d: c for d, c in fe.items() if d in {n["date"] for n in data["nights"]}}
    if fe:
        lines.append("paper_fill events per night (fills now run from the poll loop, not only when the app page is open): "
                     + ", ".join(f"{d[5:]}: {c}" for d, c in fe.items()))
    noo = [r for r in rows if r["y"] is None]
    if noo:
        flags += 1
        lines.append(f"! {len(noo)} words have no official Kalshi result yet (or void). Not in the statistics.")
    if data["fetch_errors"]:
        flags += 1
        lines.append(f"! {data['fetch_errors']} Kalshi result lookups failed or timed out")
    leg = [o for o in orders if o["legacy_px"]]
    if leg:
        flags += 1
        lines.append(f"! {len(leg)} orders have no our_price_cents (old rows; price re-derived by pricing.py)")
    nofees = [o for o in orders if o["state"] in ("won", "lost") and (o["fees"] or 0) == 0 and o["filled"] > 0]
    if nofees:
        flags += 1
        lines.append(f"! {len(nofees)} settled filled orders show fees = 0 (settled before the fee fix; run scripts/resettle.py)")
    pend = [o for o in orders if o["state"] == "pending"]
    if pend:
        flags += 1
        lines.append(f"! {len(pend)} filled orders still pending settlement")
    if not flags:
        lines.append("no problems found")
    lines.append("")
    lines.append("REMINDERS ABOUT WHAT THIS DATA CAN AND CANNOT SAY")
    lines.append("- Quote = best bid/ask from no-fade's depth table, frozen at the decision time (newest snapshot at or before it).")
    lines.append("- Paper fills are simulated against later depth snapshots. A/B, E/F and G/H are the same trades at two sizes; "
                 "the $100 books are not evidence that $100 fills.")
    lines.append("- One week is a tiny sample. Do not retune 15 / 10 / 50.01 from one week.")
    return lines


# --------------------------------------------------------------------------
# RULES, PROMPT
# --------------------------------------------------------------------------
def _config_block() -> list[str]:
    lines = [
        f"version {C.VERSION} | paper={C.PAPER} live={C.LIVE_TRADING} dry_run={C.DRY_RUN} demo={C.USE_DEMO}",
        f"series {C.SERIES} | model label {C.MODEL_LABEL} | harness {C.HARNESS}",
        f"gap threshold (strictly greater) {C.GAP_THRESHOLD} | limit offset {C.LIMIT_OFFSET_CENTS}¢ | grok10 offset {C.GROK10_OFFSET}¢ "
        f"| book I edge threshold {C.EDGE_EXEC_THRESHOLD}",
        f"file sent {C.DECISION_LAG_MIN} min after the bot first sees the event | cancel {C.CANCEL_AFTER_MIN} min after booking "
        f"| G/H cancel {C.SHOW_CANCEL_CT} CT",
        f"poll start {C.POLL_START_CT} CT | JSON deadline {C.JSON_DEADLINE_CT} CT | quote max age {C.QUOTE_MAX_AGE_S}s "
        f"| word history nights {C.WORD_HISTORY_NIGHTS} | background fills {C.BACKGROUND_FILLS}",
        f"fill take fraction {C.FILL_TAKE_FRACTION} | execution model {C.EXECUTION_MODEL}",
        f"weekly file goes out Saturday {SEND_AFTER_CT} CT",
        "review checklist: quotes are frozen at the decision time from Monday 09-21 (W38 quotes are old-style booking-time rows) | "
        "paper fills run every 30s from the poll loop (v1.5.1) and the same displayed liquidity is never counted twice (v1.5.6) | "
        "blend, WORD HISTORY and the prompt sha + diff are in this dump",
        f"books on: {', '.join(v['id'] for v in C.VARIANTS)}" + (f" | retired via DISABLED_BOOKS: {', '.join(sorted(C.DISABLED_BOOKS))}" if C.DISABLED_BOOKS else ""),
        "",
        "BOOKS",
    ]
    for v in C.VARIANTS:
        lines.append(f"  {v['id']}: ${v['notional']:.0f} | rule {v['rule']} | exit {v['exit']} | cancel {v['cancel']} | {v['label']}")
    lines += [
        "",
        "RULE MEANINGS",
        f"  fade15        : VALID quote required. Trade against the market when |Grok - mid| is strictly > {C.GAP_THRESHOLD}. "
        f"Rest limit {C.LIMIT_OFFSET_CENTS}¢ from mid toward Grok, never past Grok.",
        "  fade15_gate50 : same as fade15 but only if Grok's own side is >= 50.01 (YES if p>=51, NO if p<=49).",
        "  grok10        : ignore the market. Rest 10¢ cheaper than Grok on Grok's side. Cancel 5:29 PM CT.",
        f"  edge_exec (I) : VALID quote required. edge = Grok - ask (to buy YES) or bid - Grok (to buy NO). Trade if strictly > {C.EDGE_EXEC_THRESHOLD}; "
        "order placed AT the ask (or the bid), so it fills right away if size is there.",
        "  Quote validity: INVALID if bid<=1, ask<=1, bid>=99, bid>ask, spread>25, or a side is missing.",
        "  All books hold to Kalshi settlement. Fee = 0.07 x contracts x P x (1-P), rounded up to the cent.",
    ]
    return lines


def _dominant(items):
    """Most common item (first wins ties)."""
    counts: dict = {}
    for i in items:
        counts[i] = counts.get(i, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else None


def _prompt_block(data) -> list[str]:
    week_nights = [n for n in data["nights"] if n["status"] != "VOID" and n["run"].get("prompt_text")]
    heads = {n["date"]: _prompt_head(n["run"]["prompt_text"]) for n in week_nights}
    try:
        file_text = C.prompt_text()
    except RuntimeError:
        file_text = ""
    used = _dominant(list(heads.values())) or file_text
    lines = []
    if not used:
        return ["no prompt text on record"]
    versions = sorted({str(n["run"].get("prompt_version")) for n in week_nights})
    lines.append(f"PROMPT USED THIS WEEK: version {', '.join(versions) or 'n/a'} | sha1[:7] {_sha7(used)} | {len(used)} chars")
    if len({h for h in heads.values()}) > 1:
        lines.append("! MORE THAN ONE prompt was sent this week:")
    for d, h in sorted(heads.items()):
        lines.append(f"    {d}: sha {_sha7(h)}, {len(h)} chars" + ("" if h == used else "   <- differs from the main one"))
    if file_text and file_text != used:
        lines.append(f"note: prompts/system_prompt.txt right now (sha {_sha7(file_text)}) is DIFFERENT from what was sent this week "
                     "- probably already changed for next week.")

    # previous week
    prev = [r for r in data["all_runs"] if str(r.get("event_date"))[:10] < data["start"] and r.get("prompt_text")
            and r.get("status") == "parsed"]
    lines.append("")
    if not prev:
        lines.append("CHANGELOG vs last week: no earlier week on record")
    else:
        prev_last = max(str(r["event_date"])[:10] for r in prev)
        prev_wk = _week_id(prev_last)
        prev_heads = [_prompt_head(r["prompt_text"]) for r in prev if _week_id(str(r["event_date"])[:10]) == prev_wk]
        pprev = _dominant(prev_heads)
        prev_ver = _dominant([str(r.get("prompt_version")) for r in prev if _week_id(str(r["event_date"])[:10]) == prev_wk])
        if pprev == used:
            lines.append(f"CHANGELOG vs {prev_wk} ({prev_ver}): UNCHANGED (same prompt text)")
        else:
            old_l, new_l = pprev.splitlines(), used.splitlines()
            diff = list(difflib.unified_diff(old_l, new_l, lineterm="", n=0))
            add = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
            rem = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
            heads_old = {x.strip() for x in old_l if x.startswith("#")}
            heads_new = {x.strip() for x in new_l if x.startswith("#")}
            new_sections = sorted(heads_new - heads_old)
            gone = sorted(heads_old - heads_new)
            lines.append(
                f"CHANGELOG vs {prev_wk} ({prev_ver}, sha {_sha7(pprev)}): +{add} / -{rem} lines, "
                f"{len(pprev)} -> {len(used)} chars"
                + (f"; new sections: {', '.join(x.lstrip('# ') for x in new_sections)}" if new_sections else "")
                + (f"; removed sections: {', '.join(x.lstrip('# ') for x in gone)}" if gone else "")
            )
            lines.append("----- DIFF (old prompt -> this week's prompt; first 150 changed lines) -----")
            shown = 0
            for x in diff:
                if x.startswith(("---", "+++")):
                    continue
                lines.append(_trunc(x, 400) if x.startswith("@@") else x[:400])
                shown += 1
                if shown >= 150:
                    lines.append("... (diff truncated)")
                    break
            lines.append("----- END DIFF -----")
    lines.append("")
    lines.append("HOW THE USER MESSAGE IS BUILT (after a line with ---): Date, Event, numbered word list, then a WORD HISTORY block "
                 "(official results of the last nights, no prices) when word history is on. The exact message for each night is in section 11.")
    lines.append("Grok is never shown market prices.")
    lines.append("")
    lines.append("----- SYSTEM PROMPT SENT THIS WEEK (verbatim) -----")
    lines.append(used)
    lines.append("----- END SYSTEM PROMPT -----")
    return lines


# --------------------------------------------------------------------------
# ONE NIGHT IN FULL
# --------------------------------------------------------------------------
_TIMELINE_KINDS = ("detected", "upgraded", "prompt_sent", "quotes_frozen", "parse_reject", "parsed",
                   "expired", "void", "weekly")


def _night_block(n, activity) -> list[str]:
    run = n["run"]
    lines = []
    lines.append(f"NIGHT {n['date']}   {run.get('event_ticker')}   run status={run.get('status')}   NIGHT STATUS: {n['status']}")
    lines.append(f"  why: {n['status_why']}")
    lines.append(f"harness={run.get('harness')}  prompt={run.get('prompt_version')}  markets={run.get('markets_n')}  "
                 f"cycle_temp={n['cycle_temp'] or 'n/a'}")
    lines.append(
        "timeline (CT): bot first saw the event (row created) "
        f"{_ts(run.get('created_at'))} | first-seen / last upgrade {_ts(run.get('market_open_at'))} | "
        f"scheduled send = decision time {_ts(run.get('decision_at'))} | JSON submitted {_ts(run.get('submitted_at'))} | "
        f"last booking {_ts(run.get('parsed_at'))}"
    )
    lines.append("  (NOTE: 'first seen' is when OUR bot saw the event, not Kalshi's market-open time. An 'upgraded' event moves "
                 "first-seen and decision time later but keeps the original created time. A file can be sent before its "
                 "decision time with /gap_sendnow.)")
    ev = [a for a in activity.get(n["date"], []) if a.get("kind") in _TIMELINE_KINDS]
    fills_n = sum(1 for a in activity.get(n["date"], []) if a.get("kind") == "paper_fill")
    noev_n = sum(1 for a in activity.get(n["date"], []) if a.get("kind") == "no_event")
    if ev or fills_n:
        lines.append("ACTIVITY LOG (CT):")
        for a in ev[:30]:
            lines.append(f"  {_hm(a['at'])}  {a['kind']:<14} {_trunc(a.get('message'), 170)}")
        if noev_n:
            lines.append(f"  ({noev_n} 'no open event yet' polls not listed)")
        if fills_n:
            lines.append(f"  ({fills_n} paper_fill events not listed)")
    if run.get("grok_share_url"):
        lines.append(f"grok share url: {run.get('grok_share_url')}")
    if run.get("parse_error"):
        lines.append(f"parse_error: {run.get('parse_error')}")
    lines.append("")
    lines.append(f"WORD LIST SENT ({len(n['word_list'])} words)")
    for i, w in enumerate(n["word_list"], 1):
        if isinstance(w, dict):
            lines.append(f"  {i:>2}. {w.get('word')}   [{w.get('market_ticker')}]")
        else:
            lines.append(f"  {i:>2}. {w}")
    user_msg = _prompt_user_part(run.get("prompt_text"))
    lines.append("")
    lines.append("USER MESSAGE GROK SAW (exactly what was in the paste file after the system prompt)")
    lines.append(user_msg if user_msg else "(not stored)")
    lines.append("")
    lines.append("GROK RAW JSON (exactly as pasted)")
    lines.append(n["raw"] if n["raw"] else "(no JSON pasted this night)")
    lines.append("")
    rows = n["rows"]
    lines.append("PER-WORD TABLE (bid/ask/mid = the FROZEN decision-time quote; INV = INVALID QUOTE, no mid; gap = grok - mid)")
    lines.append(f"{'#':>2} {'word':<32}{'grok':>5}{'blk':>5}{'said':>5}{'bid':>5}{'ask':>5}{'mid':>6}{'gap':>7}  "
                 f"{'quote@':<9}{'result':<7}trades")
    for i, r in enumerate(rows, 1):
        trades = " ".join(f"{t['variant']}:{t['side']}" for t in r["trades"]) or "-"
        if r["quote_valid"]:
            mid, gap, qt = _fx(r["mid"], 1), _fx(r["gap"], 1), _hm(r["quote_time"]) if r["quote_time"] else "old"
        else:
            mid, gap, qt = "INV", "-", "INVALID"
        lines.append(
            f"{i:>2} {_trunc(r['word'], 31):<32}{_fx(r['grok'], 0):>5}{_fx(r['p_block'], 2):>5}{_fx(r['p_said'], 2):>5}"
            f"{_fx(r['bid'], 0):>5}{_fx(r['ask'], 0):>5}{mid:>6}{gap:>7}  "
            f"{qt:<9}{str(r['outcome'] or 'open').upper():<7}{trades}"
        )
    lines.append("(story / substitute / reasoning for every word are inside the raw JSON above)")
    lines.append("")
    lines.append("ORDERS (paper)  [gross / fees / net in dollars]")
    if not n["orders"]:
        lines.append("  (no order cleared any rule this night)")
    for o in n["orders"]:
        flag = f"  ** LEFT OUT: {o['excluded']} **" if o["excluded"] else ""
        lines.append(
            f"  [{o['variant']} ${_fx(o['notional'], 0)}] {o['word']} {o['side']} "
            f"{o['intended']:.2f}ct @ our {o['our_px']}¢ (YES ticket {_fx(o['yes_ticket'], 0)}¢) "
            f"| Grok {_fx(o['grok'], 0)} quote {_fx(o['q_bid'], 0)}/{_fx(o['q_ask'], 0)} booked gap {_fx(o['gap_booked'], 1)}{flag}"
        )
        if o["state"] == "unfilled":
            lines.append(f"      UNFILLED (filled {o['filled']:.2f} of {o['intended']:.2f}) status={o['status']}"
                         + (f"  [would have {'WON' if o['hyp'] > 0 else 'LOST'} {abs(o['hyp']) / 100:.2f} gross]" if o["hyp"] is not None else ""))
        elif o["state"] == "pending":
            lines.append(f"      PENDING filled {o['filled']:.2f} of {o['intended']:.2f}"
                         + (" (PARTIAL)" if o["partial"] else "") + ", no official result yet")
        elif o["state"] == "void":
            lines.append("      VOID")
        else:
            lines.append(
                f"      {o['state'].upper()}  result={o['outcome']}  filled {o['filled']:.2f}/{o['intended']:.2f}"
                + (f" (PARTIAL {o['fill_pct']:.0f}%)" if o["partial"] else "")
                + f"  gross={_money(o['gross'])} fees={(o['fees'] or 0) / 100:.2f} net={_money(o['net'])}"
            )
    return lines


# --------------------------------------------------------------------------
# building the two files
# --------------------------------------------------------------------------
def render_txt(data: dict, week_id: str, audit: dict | None = None) -> str:
    rows = _all_rows(data)
    orders = _all_orders(data)
    audit = audit if audit is not None else _audit(data)
    counts: dict = {}
    for n in data["nights"]:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    lines: list[str] = []
    lines.append("WNT GAP TRADER — WEEKLY PAPER BOOK (deep dump)")
    lines.append(f"generated {clock.fmt(clock.now_ct())} | week {week_id} | nights {data['start']} → {data['end']} | {C.VERSION}")
    lines.append("nights: " + (", ".join(f"{c} {k}" for k, c in sorted(counts.items())) or "none")
                 + f" | words: {len(rows)} | paper orders: {len(orders)} (kept in books: {len(_included_orders(data))}) | "
                 f"RULE AUDIT real violations: {len(audit['real'])} (+{len(audit['violations']) - len(audit['real'])} pre-rule)")
    lines.append(f"settle sweep: {_dumps(data['settle_totals'])}")
    lines.append("")
    lines.append("WHAT IS IN THIS FILE")
    lines.append(" 1 Night status             6 How good is Grok (calibration)   11 Prompt: version, sha, changelog, diff")
    lines.append(" 2 RULE AUDIT               7 Trade slices                      12 Full detail for every night")
    lines.append(" 3 Scoreboard vs market     8 Biggest misses / wins             13 Ask for Claude")
    lines.append(" 4 Books: gross/fees/net    9 Data-quality flags")
    lines.append(" 5 Fill-selection check    10 Rules and settings")
    lines.append(" 4B BOOK K, K_HIGH, K_LOW for A and B, A-vs-B  4C fills by Grok bucket  4D capacity, segment frequency, size sweep, long-rest shadow")
    lines.append(" 4E STRATEGY LAB: report-only variants taken at market, grid, growth check   4F BOOK M vs K_HIGH")

    _hdr(lines, "1. NIGHT STATUS")
    lines += _nights_status_block(data)
    _hdr(lines, "2. RULE AUDIT (every order recomputed against its own book's rule; should be empty)")
    lines += _audit_block(audit)
    _hdr(lines, "3. HEADLINE SCOREBOARD: GROK vs MARKET vs BASE RATE (valid quotes only) + WEEK OVER WEEK")
    lines += _scoreboard_block(data)
    lines.append("")
    lines += _wow_block(data)
    _hdr(lines, "4. BOOKS: COUNTS, GROSS / FEES / NET, PARTIAL FILLS")
    lines += _books_block(data)
    _hdr(lines, "4B. BOOK K, K_HIGH, K_LOW (pre-registered slices of Books A and B) AND THE SAME SLICE FOR BOOK I")
    lines += _book_k_block(data)
    _hdr(lines, "4C. FILLS BY GROK BUCKET (books A, B, I; after fees)")
    lines += _buckets_block(data)
    _hdr(lines, "4D. CAPACITY, SEGMENT FREQUENCY, K_HIGH SIZE SWEEP, LONG-REST SHADOW (all report-only)")
    lines += _capacity_block(data)
    _hdr(lines, "4E. STRATEGY LAB (report-only variants taken at market)")
    lines += _lab_block(data)
    _hdr(lines, "4F. BOOK M vs K_HIGH (report-only, $1 at market, decision-time book)")
    lines += _m_block(data)
    _hdr(lines, "5. FILL-SELECTION CHECK (do fills happen mostly when the market moves against us?)")
    lines += _fill_selection_block(data)
    _hdr(lines, "6. HOW GOOD IS GROK? (every word with a result; market comparisons use valid quotes only)")
    lines += _calibration_block(data)
    _hdr(lines, "7. TRADE SLICES (which kinds of trades made or lost money)")
    lines += _trade_slices_block(data)
    _hdr(lines, "8. BIGGEST MISSES AND WINS")
    lines += _misses_block(data)
    _hdr(lines, "9. DATA-QUALITY FLAGS")
    lines += _quality_block(data)
    _hdr(lines, "10. RULES AND SETTINGS")
    lines += _config_block()
    _hdr(lines, "11. PROMPT: VERSION, SHA, CHANGELOG vs LAST WEEK, THE PROMPT ITSELF")
    lines += _prompt_block(data)
    _hdr(lines, "12. FULL DETAIL FOR EVERY NIGHT")
    if not data["nights"]:
        lines.append("NO RUNS THIS WEEK")
    for n in data["nights"]:
        lines.append("")
        lines.append("#" * 72)
        lines += _night_block(n, data["activity"])
    _hdr(lines, "13. ASK FOR CLAUDE")
    lines += [
        "Read sections 1-5 first (are the numbers even trustworthy? then who won?), then 6-9, then use section 12 for specific words.",
        "If the RULE AUDIT shows violations, say so FIRST and do not draw money conclusions from the affected orders.",
        "Only TRADED nights and VALID-quote words count for trades and for Grok-vs-market. FORECAST-ONLY nights still count for Grok's calibration.",
        "Do not invent a bankroll, cluster cap, or $5 size. Scalp (C/D) was removed in v1.5.0.",
        "Questions to answer, in order:",
        " a) Is Grok calibrated? Use the calibration buckets and both threshold scans. Where does it break?",
        " b) Does Grok beat the market mid on Brier (section 3)? On which word types (count vs plain)? How is the trend across weeks, given each week's prompt?",
        " c) Which gap sizes and Grok-probability ranges made or lost money in A/E/G/I, after fees?",
        " d) Read the biggest misses. Is there a pattern in the wording/substitute-risk failures that the prompt could fix?",
        " e) Are p_block_airs and p_said_given_airs useful, or is only the final probability informative?",
        " f) Section 5: do fills select against us? Is G/H worth keeping?",
        " g) What ONE change to the prompt or the rules is worth testing next, and what would prove it wrong?",
        " h) Section 4B: what is Book K's status line, and is the margin above break-even? Do NOT change the K rule; only report.",
        " i) Section 3: does the BLEND (Grok + market)/2 beat the market and Grok? Where?",
        "One week is a small sample. Say how confident each conclusion is. Do not retune from one week.",
    ]
    return "\n".join(lines).replace("is in section 11", "is in section 12") + "\n"


def render_csv(data: dict) -> str:
    variants = [v["id"] for v in C.VARIANTS]
    cols = ["date", "night_status", "event_ticker", "market_ticker", "word", "is_count_market",
            "grok_p", "p_block_airs", "p_said_given_airs",
            "quote_bid", "quote_ask", "quote_time_ct", "quote_valid", "quote_invalid_reason", "quote_age_s", "quote_source",
            "mkt_mid", "blend_p", "K_in_slice", "K_HIGH_in_slice", "K_LOW_in_slice", "IK_in_slice", "M_in_slice",
            "dec_bid", "dec_ask", "no_dollars_yesbid60plus", "no_dollars_yesbid65plus", "no_dollars_yesbid70plus",
            "gap", "outcome", "said_yes", "cycle_temp", "has_substitute_risk",
            "carrying_story", "substitute_risk"]
    for v in variants:
        cols += [f"{v}_side", f"{v}_filled_pct", f"{v}_partial", f"{v}_state",
                 f"{v}_gross_cents", f"{v}_fees_cents", f"{v}_net_cents", f"{v}_left_out"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    wmap = {(w["run_id"], w["ticker"]): w for w in (data.get("lab") or {}).get("words", [])}
    for r in _all_rows(data):
        lw = wmap.get((r.get("run_id"), r["ticker"]), {})
        qt = clock.parse_dt(r["quote_time"])
        row = [r["date"], r.get("night_status"), r["event_ticker"], r["ticker"], r["word"], int(r["is_count"]),
               r["grok"], r["p_block"], r["p_said"],
               r["bid"], r["ask"],
               qt.astimezone(C.CT).strftime("%Y-%m-%d %H:%M:%S") if qt else "",
               int(r["quote_valid"]), r["quote_reason"] or "", r["quote_age_s"], r["quote_source"],
               None if r["mid"] is None else round(r["mid"], 2),
               None if blend(r) is None else round(blend(r), 2),
               int(any(_k_member(t, "A") for t in r["trades"])),
               int(any(_k_member(t, "A", "HIGH") for t in r["trades"])),
               int(any(_k_member(t, "A", "LOW") for t in r["trades"])),
               int(any(_k_member(t, "I") for t in r["trades"])),
               (int(bool(lab.v_m(lw))) if lw else None),
               lw.get("bid"), lw.get("ask"), lw.get("cap60"), lw.get("cap65"), lw.get("cap70"),
               None if r["gap"] is None else round(r["gap"], 2),
               r["outcome"], r["y"], r["cycle_temp"], int(r["has_sub"]),
               _trunc(r["story"], 300), _trunc(r["substitute_risk"], 300)]
        by_v = {t["variant"]: t for t in r["trades"]}
        for v in variants:
            t = by_v.get(v)
            if t:
                row += [t["side"], round(t["fill_pct"], 1), int(t["partial"]), t["state"],
                        t["gross"], t["fees"], t["net"], t["excluded"] or ""]
            else:
                row += ["", "", "", "", "", "", "", ""]
        w.writerow(["" if x is None else x for x in row])
    return buf.getvalue()


def build_week_bundle(start: str | None = None, end: str | None = None) -> dict:
    if not start or not end:
        start, end, week_id = clock.week_mon_fri()
    else:
        week_id = _week_id(start)
    data = collect(start, end)
    data["lab"] = lab.panel(data)
    audit = _audit(data)
    rows = _all_rows(data)
    orders = _all_orders(data)
    inc = _included_orders(data)
    net = sum(o["net"] or 0 for o in inc if o["net"] is not None)
    counts: dict = {}
    for n in data["nights"]:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    return {
        "start": start,
        "end": end,
        "week_id": week_id,
        "txt_name": f"gap-week-{week_id}.txt",
        "txt": render_txt(data, week_id, audit),
        "csv_name": f"gap-week-{week_id}-words.csv",
        "csv": render_csv(data),
        "n_nights": len(data["nights"]),
        "night_counts": counts,
        "n_words": len(rows),
        "n_orders": len(orders),
        "n_violations": len(audit["real"]),
        "net_cents": net,
    }


def build_week_report(start: str | None = None, end: str | None = None) -> tuple[str, str]:
    """Old signature kept: returns (filename, text)."""
    b = build_week_bundle(start, end)
    return b["txt_name"], b["txt"]


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------
def _due(now: datetime) -> bool:
    hh, mm = [int(x) for x in SEND_AFTER_CT.split(":")[:2]]
    wd = now.weekday()  # Mon=0 ... Sat=5, Sun=6
    if wd == 6:
        return True  # Sunday = catch-up if Saturday was missed
    if wd != 5:
        return False
    return (now.hour, now.minute) >= (hh, mm)


def _send_now(start: str, end: str, week_id: str, state_key: str) -> str:
    b = build_week_bundle(start, end)
    nc = ", ".join(f"{c} {k}" for k, c in sorted(b["night_counts"].items())) or "no nights"
    caption = (
        f"WNT gap weekly {week_id} ({start} → {end}). {nc}; "
        f"{b['n_words']} words, {b['n_orders']} paper orders, net after fees ${b['net_cents'] / 100:+.2f}; "
        f"RULE AUDIT violations: {b['n_violations']}. Paste the .txt into Claude. CSV is for your notebook."
    )
    msg_id = notify.send_document(b["txt_name"], b["txt"], caption)
    if msg_id is None:
        raise RuntimeError("Telegram did not accept the .txt file (check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    csv_id = notify.send_document(b["csv_name"], b["csv"], f"{week_id} one-row-per-word data")
    store.set_state(state_key, {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "telegram_msg_id": msg_id,
        "csv_msg_id": csv_id,
        "bytes": len(b["txt"]),
    })
    store.log_activity("weekly", f"{week_id} msg={msg_id} csv={csv_id} bytes={len(b['txt'])}")
    return (f"sent {b['txt_name']} ({len(b['txt'])} bytes) msg={msg_id}"
            + ("" if csv_id else " | csv NOT delivered"))


def send_week_report(force: bool = False) -> str:
    start, end, week_id = clock.week_mon_fri()
    state_key = f"weekly_sent_{week_id}"

    if not force:
        if not _due(clock.now_ct()):
            return f"not due yet (goes out Saturday {SEND_AFTER_CT} CT)"
        if store.get_state(state_key):
            return f"already sent {week_id}"
        if _AUTO["week"] != week_id:
            _AUTO.update(week=week_id, fails=0, next_try=0.0)
        if _AUTO["fails"] >= MAX_AUTO_FAILS:
            return f"auto-send gave up after {MAX_AUTO_FAILS} failures — send /gap_week"
        if time.time() < _AUTO["next_try"]:
            return "auto-send waiting to retry"

    if not _LOCK.acquire(blocking=False):
        return "weekly report is already being built"
    try:
        try:
            return _send_now(start, end, week_id, state_key)
        except Exception as exc:
            if not force:
                _AUTO["fails"] += 1
                _AUTO["next_try"] = time.time() + RETRY_SECONDS
                log.exception("weekly auto-send failed")
                try:
                    notify.send(
                        f"Weekly dump failed ({_AUTO['fails']}/{MAX_AUTO_FAILS}): {exc}\n"
                        + ("Retrying in 20 min." if _AUTO["fails"] < MAX_AUTO_FAILS
                           else "Giving up. Send /gap_week to try by hand.")
                    )
                except Exception:
                    pass
            raise
    finally:
        _LOCK.release()
