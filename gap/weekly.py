"""Saturday weekly dump.

Sends TWO files to Telegram every Saturday at 7:00 AM Central:
  1. gap-week-<week>.txt        -- the big report (paste this into Claude)
  2. gap-week-<week>-words.csv  -- one row per word per night (for your notebook)

Everything in the .txt is built from the database plus Kalshi's official
results. Nothing here places or changes a trade.

v1.5.1 fixes / changes
  * FIXED "Object of type Decimal is not JSON serializable". Postgres hands back
    Decimal numbers; every number is now converted to a plain float on the way in,
    and every json.dumps uses a safe default.
  * FIXED "sent" being recorded even when Telegram refused the file.
  * Sends only on Saturday at/after 07:00 CT (or Sunday as a catch-up).
    /gap_week (force=True) still sends any time.
  * If an automatic send fails it waits 20 minutes and tries again, max 3 tries,
    and tells you on Telegram. It no longer hammers Kalshi every 30 seconds.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal

from . import clock, config as C, notify, pricing, prompt, settle, store
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.weekly")

# When the Saturday file goes out (Central time, 24h "HH:MM"). Override with the
# WEEKLY_SEND_CT secret if you ever want a different time.
SEND_AFTER_CT = C._secret("WEEKLY_SEND_CT", "07:00")

_LOCK = threading.Lock()
_AUTO = {"week": None, "fails": 0, "next_try": 0.0}
MAX_AUTO_FAILS = 3
RETRY_SECONDS = 20 * 60
OUTCOME_FETCH_BUDGET_S = 120

COUNT_RE = re.compile(r"(\s+\d+\+$)|(\s+\[\d+\]$)")
THRESHOLDS = (10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90)
GAP_BINS = ((-101, -40), (-40, -25), (-25, -15), (-15, -5), (-5, 5),
            (5, 15), (15, 25), (25, 40), (40, 101))
ABS_GAP_BINS = ((15, 20), (20, 25), (25, 35), (35, 101))
P_BINS = tuple((lo, lo + 10) for lo in range(0, 100, 10))


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
    return dt.astimezone(C.CT).strftime("%a %m-%d %H:%M CT")


def _money(cents) -> str:
    c = _f(cents)
    if c is None:
        return "n/a"
    return f"{c / 100.0:+.2f}"


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


# --------------------------------------------------------------------------
# collecting data
# --------------------------------------------------------------------------
def _fetch_outcomes(tickers) -> tuple[dict, int]:
    """Official Kalshi result for each ticker. Returns ({ticker: 'yes'|'no'|'void'|None}, n_errors)."""
    out: dict = {}
    errors = 0
    tickers = list(tickers)
    if not tickers:
        return out, errors
    client = KalshiClient()

    def one(t):
        return t, market_result(client.get_market(t))

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(one, t) for t in tickers]
        try:
            for fut in as_completed(futs, timeout=OUTCOME_FETCH_BUDGET_S):
                try:
                    t, res = fut.result()
                    out[t] = res
                except Exception as exc:  # one bad ticker must not kill the report
                    errors += 1
                    log.warning("outcome fetch failed: %s", exc)
        except Exception:
            errors += sum(1 for f in futs if not f.done())
            for f in futs:
                f.cancel()
    for t in tickers:
        out.setdefault(t, None)
    return out, errors


def collect(start: str, end: str) -> dict:
    """Pull everything for the week into plain-Python structures (no Decimals)."""
    settle_totals = settle.settle_range(start, end)
    runs = store.runs_between(start, end)

    raw_nights = []
    outcome_by_ticker: dict = {}
    all_market_tickers: set = set()

    for run in runs:
        rid = run["id"]
        forecasts = store.forecasts_for_run(rid)
        markets = store.markets_for_run(rid)
        quotes = store.quotes_for_run(rid)
        orders = store.orders_for_run(rid)
        settles = store.settlements_for_order_ids([o["id"] for o in orders])
        for o in orders:
            s = settles.get(int(o["id"]))
            if s and s.get("outcome") in ("yes", "no"):
                outcome_by_ticker[o["market_ticker"]] = s["outcome"]
        for m in markets:
            all_market_tickers.add(m["market_ticker"])
        for f in forecasts:
            all_market_tickers.add(f["market_ticker"])
        raw_nights.append((run, forecasts, markets, quotes, orders, settles))

    need = [t for t in all_market_tickers if outcome_by_ticker.get(t) not in ("yes", "no")]
    fetched, fetch_errors = _fetch_outcomes(need)
    for t, res in fetched.items():
        if res is not None:
            outcome_by_ticker[t] = res

    nights = []
    for run, forecasts, markets, quotes, orders, settles in raw_nights:
        parsed = _obj(run.get("parsed")) or {}
        cycle_temp = parsed.get("cycle_temp") if isinstance(parsed, dict) else None

        q_by_ticker = {}
        for q in quotes:  # last quote wins (that is the one the books were sized from)
            q_by_ticker[q["market_ticker"]] = q

        rows = []
        for f in forecasts:
            t = f["market_ticker"]
            q = q_by_ticker.get(t, {})
            bid, ask = _f(q.get("yes_bid_cents")), _f(q.get("yes_ask_cents"))
            mid = _f(q.get("market_prob"))
            mid = mid * 100.0 if mid is not None else None
            grok = _f(f.get("probability"))
            outcome = outcome_by_ticker.get(t)
            y = 1 if outcome == "yes" else (0 if outcome == "no" else None)
            rows.append({
                "date": str(run.get("event_date"))[:10],
                "event_ticker": run.get("event_ticker"),
                "ticker": t,
                "word": f["word"],
                "is_count": bool(COUNT_RE.search(f["word"])),
                "grok": grok,
                "p_block": _f(f.get("p_block_airs")),
                "p_said": _f(f.get("p_said_given_airs")),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "gap": (grok - mid) if (grok is not None and mid is not None) else None,
                "outcome": outcome,
                "y": y,
                "cycle_temp": cycle_temp,
                "has_sub": not _is_none_text(f.get("substitute_risk")),
                "story": f.get("carrying_story"),
                "substitute_risk": f.get("substitute_risk"),
                "other_routes": f.get("other_routes"),
                "reasoning": f.get("reasoning"),
                "trades": [],
            })
        row_by_ticker = {r["ticker"]: r for r in rows}

        orows = []
        for o in orders:
            s = settles.get(int(o["id"]), {})
            filled = pricing.filled_contracts(o)
            intended = _f(o.get("contracts")) or 0.0
            side = str(o.get("side") or "").upper()
            our_px = pricing.entry_price_cents(o)
            status = str(o.get("status") or "")
            outcome = s.get("outcome") or row_by_ticker.get(o["market_ticker"], {}).get("outcome")
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
            trow = row_by_ticker.get(o["market_ticker"], {})
            orow = {
                "date": str(run.get("event_date"))[:10],
                "variant": o.get("variant_id") or "?",
                "notional": _f(o.get("notional_dollars")),
                "exit": o.get("exit_rule"),
                "word": o["word"],
                "ticker": o["market_ticker"],
                "side": side,
                "intended": intended,
                "filled": filled,
                "our_px": our_px,
                "yes_ticket": _f(o.get("limit_price_cents")),
                "gap_booked": _f(o.get("gap_points")),
                "status": status,
                "state": state,
                "outcome": outcome,
                "net": _f(s.get("net_cents")) if s else None,
                "fees": _f(s.get("fees_cents")) if s else None,
                "cost": float(pricing.cost_cents(o)),
                "grok": trow.get("grok"),
                "mid": trow.get("mid"),
                "gap_real": trow.get("gap"),
                "legacy_px": o.get("our_price_cents") in (None, ""),
            }
            orows.append(orow)
            if trow:
                trow["trades"].append(orow)

        wl = _obj(run.get("word_list")) or []
        nights.append({
            "run": run,
            "date": str(run.get("event_date"))[:10],
            "cycle_temp": cycle_temp,
            "rows": rows,
            "orders": orows,
            "markets": markets,
            "word_list": wl,
            "raw": run.get("raw_response") or "",
            "missing_forecast": sorted(
                {m["word"] for m in markets} - {r["word"] for r in rows}
            ),
        })

    return {
        "start": start,
        "end": end,
        "settle_totals": settle_totals,
        "nights": nights,
        "fetch_errors": fetch_errors,
        "n_fetch_needed": len(need),
    }


# --------------------------------------------------------------------------
# statistics blocks (each returns a list of text lines)
# --------------------------------------------------------------------------
def _all_rows(data):
    return [r for n in data["nights"] for r in n["rows"]]


def _all_orders(data):
    return [o for n in data["nights"] for o in n["orders"]]


def _score_rows(rows):
    return [r for r in rows if r["y"] is not None and r["grok"] is not None]


def _books_block(data) -> list[str]:
    lines = []
    orders = _all_orders(data)
    lines.append("Win = the side we held was right. Unfilled = the paper order never filled (no P&L).")
    lines.append("Pending = filled but Kalshi has not published a result yet.")
    lines.append("")
    lines.append(f"{'book':<28}{'booked':>7}{'filled':>7}{'unfill':>7}{'pend':>5}"
                 f"{'W':>4}{'L':>4}{'hit%':>6}{'at-risk$':>10}{'net$':>9}{'ROI%':>7}")
    tot = {"b": 0, "f": 0, "u": 0, "p": 0, "w": 0, "l": 0, "risk": 0.0, "net": 0.0}
    for spec in C.VARIANTS:
        vid = spec["id"]
        os_ = [o for o in orders if o["variant"] == vid]
        w = sum(1 for o in os_ if o["state"] == "won")
        l = sum(1 for o in os_ if o["state"] == "lost")
        unf = sum(1 for o in os_ if o["state"] == "unfilled")
        pend = sum(1 for o in os_ if o["state"] == "pending")
        filled_n = sum(1 for o in os_ if o["filled"] > 0)
        settled_filled = [o for o in os_ if o["state"] in ("won", "lost")]
        risk = sum(o["cost"] for o in settled_filled)
        net = sum(o["net"] or 0 for o in os_ if o["net"] is not None)
        roi = (100.0 * net / risk) if risk else None
        hit = (100.0 * w / (w + l)) if (w + l) else None
        lines.append(
            f"{spec['label'][:27]:<28}{len(os_):>7}{filled_n:>7}{unf:>7}{pend:>5}"
            f"{w:>4}{l:>4}{_fx(hit, 0):>6}{risk / 100:>10.2f}{net / 100:>+9.2f}{_fx(roi, 1):>7}"
        )
        tot["b"] += len(os_); tot["f"] += filled_n; tot["u"] += unf; tot["p"] += pend
        tot["w"] += w; tot["l"] += l; tot["risk"] += risk; tot["net"] += net
    lines.append("")
    lines.append(f"ALL BOOKS COMBINED: booked={tot['b']} filled={tot['f']} unfilled={tot['u']} "
                 f"pending={tot['p']} W={tot['w']} L={tot['l']} net=${tot['net'] / 100:+.2f}")
    lines.append("(books are independent paper copies of each other; adding them up is only a sanity check)")

    lines.append("")
    lines.append("BY SIDE (settled, filled orders only)")
    lines.append(f"{'book':<6}{'side':<5}{'n':>4}{'W':>4}{'L':>4}{'hit%':>6}{'net$':>9}{'avg our px':>12}")
    for spec in C.VARIANTS:
        for side in ("YES", "NO"):
            os_ = [o for o in orders if o["variant"] == spec["id"] and o["side"] == side
                   and o["state"] in ("won", "lost")]
            if not os_:
                continue
            w = sum(1 for o in os_ if o["state"] == "won")
            net = sum(o["net"] or 0 for o in os_)
            lines.append(
                f"{spec['id']:<6}{side:<5}{len(os_):>4}{w:>4}{len(os_) - w:>4}"
                f"{_fx(100.0 * w / len(os_), 0):>6}{net / 100:>+9.2f}"
                f"{_fx(_mean([o['our_px'] for o in os_]), 1, '¢'):>12}"
            )

    lines.append("")
    lines.append("NET $ BY NIGHT AND BOOK (settled only)")
    lines.append(f"{'night':<12}" + "".join(f"{s['id']:>9}" for s in C.VARIANTS) + f"{'orders':>8}")
    for n in data["nights"]:
        cells = []
        for spec in C.VARIANTS:
            nets = [o["net"] for o in n["orders"] if o["variant"] == spec["id"] and o["net"] is not None]
            cells.append(f"{sum(nets) / 100:>+9.2f}" if nets else f"{'-':>9}")
        lines.append(f"{n['date']:<12}" + "".join(cells) + f"{len(n['orders']):>8}")

    lines.append("")
    lines.append("FILL QUALITY (filled contracts / intended contracts)")
    for spec in C.VARIANTS:
        os_ = [o for o in orders if o["variant"] == spec["id"]]
        intended = sum(o["intended"] for o in os_)
        filled = sum(o["filled"] for o in os_)
        lines.append(f"  {spec['id']}: {filled:.1f} / {intended:.1f} = "
                     f"{_fx(100.0 * filled / intended if intended else None, 0, '%')}")
    return lines


def _nights_block(data) -> list[str]:
    lines = [f"{'night':<12}{'status':<10}{'words':>6}{'YES':>5}{'base%':>7}{'grok avg':>9}{'mkt avg':>8}"
             f"{'Brier G':>9}{'Brier M':>9}{'orders':>7}  temp"]
    for n in data["nights"]:
        sc = _score_rows(n["rows"])
        yes = sum(r["y"] for r in sc)
        base = (100.0 * yes / len(sc)) if sc else None
        bg = _brier([(r["grok"] / 100.0, r["y"]) for r in sc])
        bm = _brier([(r["mid"] / 100.0, r["y"]) for r in sc if r["mid"] is not None])
        lines.append(
            f"{n['date']:<12}{str(n['run'].get('status')):<10}{len(n['rows']):>6}{yes:>5}"
            f"{_fx(base, 0):>7}{_fx(_mean([r['grok'] for r in n['rows']]), 1):>9}"
            f"{_fx(_mean([r['mid'] for r in n['rows']]), 1):>8}{_fx(bg, 3):>9}{_fx(bm, 3):>9}"
            f"{len(n['orders']):>7}  {n['cycle_temp'] or '-'}"
        )
    lines.append("Brier: lower is better. G = Grok, M = market mid at booking time. 0.25 = coin flip on every word.")
    return lines


def _calibration_block(rows) -> list[str]:
    lines = []
    sc = _score_rows(rows)
    if not sc:
        return ["no scored words yet"]
    n = len(sc)
    yes = sum(r["y"] for r in sc)
    base = yes / n
    bg = _brier([(r["grok"] / 100.0, r["y"]) for r in sc])
    with_mkt = [r for r in sc if r["mid"] is not None]
    bg_m = _brier([(r["grok"] / 100.0, r["y"]) for r in with_mkt])
    bm = _brier([(r["mid"] / 100.0, r["y"]) for r in with_mkt])
    bbase = _brier([(base, r["y"]) for r in sc])
    lines.append(f"words scored: {n}   said YES: {yes} ({100 * base:.0f}%)   said NO: {n - yes}")
    lines.append(f"mean Grok probability: {_fx(_mean([r['grok'] for r in sc]), 1)}%   "
                 f"actual YES rate: {100 * base:.1f}%")
    lines.append(f"Brier score, Grok (all words):            {_fx(bg, 4)}")
    lines.append(f"Brier score, Grok (words that have quote): {_fx(bg_m, 4)}   n={len(with_mkt)}")
    lines.append(f"Brier score, market mid (same words):      {_fx(bm, 4)}")
    lines.append(f"Brier score, 'always say the base rate':   {_fx(bbase, 4)}")
    if bg_m is not None and bm is not None:
        verdict = "Grok BEAT the market" if bg_m < bm else "Market BEAT Grok"
        lines.append(f"-> {verdict} on these words (difference {abs(bg_m - bm):.4f}). Small sample: treat as a hint.")
    closer = sum(1 for r in with_mkt if abs(r["grok"] / 100.0 - r["y"]) < abs(r["mid"] / 100.0 - r["y"]))
    lines.append(f"Grok closer to the truth than the market: {_rate(closer, len(with_mkt))}")

    def dir_ok(p, y):
        if p >= 51:
            return y == 1
        if p <= 49:
            return y == 0
        return None

    d = [dir_ok(r["grok"], r["y"]) for r in sc]
    d = [x for x in d if x is not None]
    lines.append(f"Grok side-of-50 correct (p>=51 -> YES, p<=49 -> NO): {_rate(sum(d), len(d))}")
    dm = [(r["mid"] > 50 and r["y"] == 1) or (r["mid"] < 50 and r["y"] == 0)
          for r in with_mkt if r["mid"] != 50]
    lines.append(f"Market side-of-50 correct (same test):              {_rate(sum(dm), len(dm))}")

    lines.append("")
    lines.append("CALIBRATION BUCKETS — when Grok says X%, how often did it really happen?")
    lines.append(f"{'Grok p':<10}{'n':>4}{'avg Grok':>10}{'said YES':>12}{'avg mkt':>9}{'avg gap':>9}")
    for lo, hi in P_BINS:
        b = [r for r in sc if lo <= r["grok"] < hi or (hi == 100 and r["grok"] == 100)]
        if not b:
            continue
        yy = sum(r["y"] for r in b)
        lines.append(
            f"{f'{lo}-{hi - 1}':<10}{len(b):>4}{_fx(_mean([r['grok'] for r in b]), 1):>10}"
            f"{_rate(yy, len(b)):>12}{_fx(_mean([r['mid'] for r in b]), 1):>9}"
            f"{_fx(_mean([r['gap'] for r in b]), 1):>9}"
        )

    lines.append("")
    lines.append("THRESHOLD SCAN A — words Grok put AT OR ABOVE X%: what share said YES?")
    lines.append(f"{'X':>4}{'n':>5}{'said YES':>14}{'avg Grok':>10}{'mkt YES%':>10}")
    for t in THRESHOLDS:
        b = [r for r in sc if r["grok"] >= t]
        if not b:
            continue
        yy = sum(r["y"] for r in b)
        lines.append(f"{t:>4}{len(b):>5}{_rate(yy, len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b]), 1):>10}")
    lines.append("")
    lines.append("THRESHOLD SCAN B — words Grok put AT OR BELOW X%: what share said NO?")
    lines.append(f"{'X':>4}{'n':>5}{'said NO':>14}{'avg Grok':>10}{'mkt YES%':>10}")
    for t in THRESHOLDS:
        b = [r for r in sc if r["grok"] <= t]
        if not b:
            continue
        nn = sum(1 - r["y"] for r in b)
        lines.append(f"{t:>4}{len(b):>5}{_rate(nn, len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b]), 1):>10}")
    lines.append("Read: if 'AT OR ABOVE 70' says YES only ~50% of the time, Grok is overconfident up there.")

    lines.append("")
    lines.append("GAP ANALYSIS — gap = Grok% minus market mid% at booking (positive = Grok more bullish)")
    lines.append(f"{'gap bin':<12}{'n':>4}{'said YES':>14}{'avg Grok':>10}{'avg mkt':>9}")
    gsc = [r for r in sc if r["gap"] is not None]
    for lo, hi in GAP_BINS:
        b = [r for r in gsc if lo <= r["gap"] < hi]
        if not b:
            continue
        yy = sum(r["y"] for r in b)
        label = f"{max(lo, -100):+d}..{min(hi, 100):+d}"
        lines.append(f"{label:<12}{len(b):>4}{_rate(yy, len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(_mean([r['mid'] for r in b]), 1):>9}")
    above = [r for r in gsc if r["gap"] > C.GAP_THRESHOLD]
    below = [r for r in gsc if r["gap"] < -C.GAP_THRESHOLD]
    lines.append(f"Grok >{C.GAP_THRESHOLD} above market (buy-YES signals): said YES {_rate(sum(r['y'] for r in above), len(above))}"
                 f"   avg market implied {_fx(_mean([r['mid'] for r in above]), 1)}%")
    lines.append(f"Grok >{C.GAP_THRESHOLD} below market (fade signals):   said NO  {_rate(sum(1 - r['y'] for r in below), len(below))}"
                 f"   avg market implied NO {_fx(_mean([100 - r['mid'] for r in below]), 1)}%")
    lines.append("Read: the market-implied number is the break-even. Signal only pays if the hit rate beats it.")

    lines.append("")
    lines.append("WORD TYPE")
    lines.append(f"{'type':<24}{'n':>4}{'said YES':>14}{'avg Grok':>10}{'Brier G':>9}{'Brier M':>9}")
    for label, flag in (("count words (3+, [3])", True), ("plain words", False)):
        b = [r for r in sc if r["is_count"] == flag]
        if not b:
            continue
        bgx = _brier([(r["grok"] / 100.0, r["y"]) for r in b])
        bmx = _brier([(r["mid"] / 100.0, r["y"]) for r in b if r["mid"] is not None])
        lines.append(f"{label:<24}{len(b):>4}{_rate(sum(r['y'] for r in b), len(b)):>14}"
                     f"{_fx(_mean([r['grok'] for r in b]), 1):>10}{_fx(bgx, 3):>9}{_fx(bmx, 3):>9}")

    lines.append("")
    lines.append("GROK'S OWN COMPONENTS (p_block_airs = does a carrying segment air; p_said_given_airs = would anchor say the word)")
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
    prod = [abs(r["grok"] / 100.0 - r["p_block"] * r["p_said"]) for r in sc
            if r["p_block"] is not None and r["p_said"] is not None]
    lines.append(f"avg |probability - p_block*p_said| (does Grok's math add up?): {_fx(_mean(prod), 3)}")

    lines.append("")
    lines.append("SUBSTITUTE-WORD RISK (Grok named something else the anchor might say)")
    for label, flag in (("substitute risk named", True), ("'none obvious'", False)):
        b = [r for r in sc if r["has_sub"] == flag]
        if b:
            lines.append(f"  {label:<24} n={len(b):<4} said YES {_rate(sum(r['y'] for r in b), len(b)):<14}"
                         f" avg Grok {_fx(_mean([r['grok'] for r in b]), 1)}%")
    return lines


def _trade_slices_block(data) -> list[str]:
    lines = []
    orders = [o for o in _all_orders(data) if o["state"] in ("won", "lost")]
    if not orders:
        return ["no settled trades yet"]

    def table(title, os_, bins, keyfn, fmt):
        lines.append(title)
        lines.append(f"{'bucket':<14}{'n':>4}{'W':>4}{'L':>4}{'hit%':>6}{'net$':>9}{'avg our px':>12}")
        for lo, hi in bins:
            b = [o for o in os_ if keyfn(o) is not None and lo <= keyfn(o) < hi]
            if not b:
                continue
            w = sum(1 for o in b if o["state"] == "won")
            lines.append(f"{fmt(lo, hi):<14}{len(b):>4}{w:>4}{len(b) - w:>4}"
                         f"{_fx(100.0 * w / len(b), 0):>6}{sum(o['net'] or 0 for o in b) / 100:>+9.2f}"
                         f"{_fx(_mean([o['our_px'] for o in b]), 1, '¢'):>12}")
        lines.append("")

    for vid in ("A", "E"):
        os_ = [o for o in orders if o["variant"] == vid]
        if os_:
            table(f"BOOK {vid}: by size of the gap at booking (|gap| in points)", os_, ABS_GAP_BINS,
                  lambda o: abs(o["gap_booked"]) if o["gap_booked"] is not None else None,
                  lambda lo, hi: f"{lo}-{min(hi, 100) - 1}")
    for vid in ("A", "E", "G"):
        os_ = [o for o in orders if o["variant"] == vid]
        if os_:
            table(f"BOOK {vid}: by Grok probability", os_, P_BINS, lambda o: o["grok"],
                  lambda lo, hi: f"Grok {lo}-{hi - 1}")
    return lines


def _misses_block(rows) -> list[str]:
    lines = []
    sc = [r for r in _score_rows(rows)]
    if not sc:
        return ["no scored words yet"]
    lines.append("BIGGEST MISSES — Grok was confident and wrong (worst first)")
    worst = sorted(sc, key=lambda r: -abs(r["grok"] / 100.0 - r["y"]))[:10]
    for r in worst:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {_fx(r['mid'], 0)}%  "
                     f"-> {str(r['outcome']).upper()}")
        lines.append(f"    story: {_trunc(r['story'], 200)}")
        lines.append(f"    substitute: {_trunc(r['substitute_risk'], 160)}")
        lines.append(f"    reason: {_trunc(r['reasoning'], 420)}")
    lines.append("")
    lines.append("GROK RIGHT, MARKET WRONG — where Grok added the most value (best first)")
    wm = [r for r in sc if r["mid"] is not None]
    best = sorted(wm, key=lambda r: -(abs(r["mid"] / 100.0 - r["y"]) - abs(r["grok"] / 100.0 - r["y"])))[:6]
    for r in best:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {r['mid']:.0f}%  "
                     f"-> {str(r['outcome']).upper()}   ({_trunc(r['story'], 120)})")
    lines.append("")
    lines.append("MARKET RIGHT, GROK WRONG — where the market was smarter (worst first)")
    worse = sorted(wm, key=lambda r: -(abs(r["grok"] / 100.0 - r["y"]) - abs(r["mid"] / 100.0 - r["y"])))[:6]
    for r in worse:
        lines.append(f"- {r['date']} \"{r['word']}\"  Grok {r['grok']:.0f}%  market {r['mid']:.0f}%  "
                     f"-> {str(r['outcome']).upper()}   ({_trunc(r['story'], 120)})")
    return lines


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
    noq = [r for r in rows if r["mid"] is None]
    if noq:
        flags += 1
        lines.append(f"! {len(noq)} words have NO market quote at booking (no gap, no fade order possible): "
                     + ", ".join(f"{r['date'][5:]} {r['word']}" for r in noq[:12]) + (" ..." if len(noq) > 12 else ""))
    noo = [r for r in rows if r["y"] is None]
    if noo:
        flags += 1
        lines.append(f"! {len(noo)} words have no official Kalshi result yet (or void). Not in the statistics.")
    if data["fetch_errors"]:
        flags += 1
        lines.append(f"! {data['fetch_errors']} of {data['n_fetch_needed']} Kalshi result lookups failed or timed out")
    leg = [o for o in orders if o["legacy_px"]]
    if leg:
        flags += 1
        lines.append(f"! {len(leg)} orders have no our_price_cents (old rows; price re-derived by pricing.py)")
    pend = [o for o in orders if o["state"] == "pending"]
    if pend:
        flags += 1
        lines.append(f"! {len(pend)} filled orders still pending settlement")
    if not flags:
        lines.append("no problems found")
    lines.append("")
    lines.append("REMINDERS ABOUT WHAT THIS DATA CAN AND CANNOT SAY")
    lines.append("- Market quote = best bid/ask copied from no-fade's depth table at the moment the JSON was parsed.")
    lines.append("- Paper fills are simulated. A and B are the same trades at different size; B is not proof $100 fills.")
    lines.append("- One week is a tiny sample. Do not retune 15¢ / 10¢ / 50.01 from one week.")
    return lines


def _config_block() -> list[str]:
    lines = [
        f"version {C.VERSION} | paper={C.PAPER} live={C.LIVE_TRADING} dry_run={C.DRY_RUN} demo={C.USE_DEMO}",
        f"series {C.SERIES} | model label {C.MODEL_LABEL} | harness {C.HARNESS} | prompt version {C.PROMPT_VERSION}",
        f"gap threshold {C.GAP_THRESHOLD}¢ | limit offset {C.LIMIT_OFFSET_CENTS}¢ | grok10 offset {C.GROK10_OFFSET}¢",
        f"file sent {C.DECISION_LAG_MIN} min after market first seen | cancel {C.CANCEL_AFTER_MIN} min after send "
        f"| show cancel {C.SHOW_CANCEL_CT} CT",
        f"market open (fallback) {C.MARKET_OPEN_CT} CT | poll start {C.POLL_START_CT} CT | JSON deadline {C.JSON_DEADLINE_CT} CT",
        f"fill take fraction {C.FILL_TAKE_FRACTION} | execution model {C.EXECUTION_MODEL}",
        f"weekly file goes out Saturday {SEND_AFTER_CT} CT",
        "",
        "BOOKS",
    ]
    for v in C.VARIANTS:
        lines.append(f"  {v['id']}: ${v['notional']:.0f} | rule {v['rule']} | exit {v['exit']} | cancel {v['cancel']} | {v['label']}")
    lines += [
        "",
        "RULE MEANINGS",
        "  fade15        : trade against the market when |Grok - market mid| > 15 points. Rest limit 8¢ from mid toward Grok, never past Grok.",
        "  fade15_gate50 : same as fade15 but only if Grok's own side is >= 50.01 (YES if p>=51, NO if p<=49).",
        "  grok10        : ignore the market. Rest 10¢ cheaper than Grok on Grok's side. Cancel 5:29 PM CT.",
        "  All books hold to Kalshi settlement. No early exits. Fee = 0.07 x contracts x P x (1-P), rounded up.",
    ]
    return lines


def _prompt_block(data) -> list[str]:
    cur = prompt.SYSTEM_PROMPT.rstrip()
    sha = hashlib.sha1(cur.encode("utf-8")).hexdigest()[:10]
    lines = [f"prompt version: {C.PROMPT_VERSION}   sha1[:10]: {sha}   chars: {len(cur)}"]
    mism = []
    for n in data["nights"]:
        stored = str(n["run"].get("prompt_text") or "")
        if not stored:
            mism.append((n["date"], "no prompt_text stored"))
        elif not stored.startswith(cur):
            mism.append((n["date"], stored))
    if not mism:
        lines.append("check: every night this week was sent EXACTLY this system prompt (stored prompt_text starts with it).")
    else:
        for d, s in mism:
            lines.append(f"check: {d} does NOT match the current system prompt.")
            if s != "no prompt_text stored":
                head = s.split("\n\n---\n\n")[0]
                lines.append(f"----- system prompt actually stored for {d} -----")
                lines.append(head)
                lines.append(f"----- end {d} -----")
            else:
                lines.append("   (nothing stored for that night)")
    lines.append("")
    lines.append("USER MESSAGE FORMAT (appended after the system prompt each night, after a line with ---):")
    lines.append("  Date: <date> / Event: <event ticker> / numbered list of exact words / 'Output valid JSON only...'")
    lines.append("Grok is never shown market prices.")
    lines.append("")
    lines.append("----- CURRENT SYSTEM PROMPT (verbatim) -----")
    lines.append(cur)
    lines.append("----- END SYSTEM PROMPT -----")
    return lines


def _night_block(n) -> list[str]:
    run = n["run"]
    lines = []
    lines.append(f"NIGHT {n['date']}   {run.get('event_ticker')}   status={run.get('status')}")
    lines.append(f"harness={run.get('harness')}  prompt={run.get('prompt_version')}  markets={run.get('markets_n')}  "
                 f"cycle_temp={n['cycle_temp'] or 'n/a'}")
    lines.append(f"timeline (CT): market open {_ts(run.get('market_open_at'))} | file created {_ts(run.get('created_at'))} | "
                 f"decision {_ts(run.get('decision_at'))} | JSON submitted {_ts(run.get('submitted_at'))} | "
                 f"parsed {_ts(run.get('parsed_at'))}")
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
    lines.append("")
    lines.append("GROK RAW JSON (exactly as pasted)")
    lines.append(n["raw"] if n["raw"] else "(no JSON pasted this night)")
    lines.append("")
    rows = n["rows"]
    lines.append("PER-WORD TABLE (grok/blk/said = Grok numbers; bid/ask/mid = market at booking; gap = grok - mid)")
    lines.append(f"{'#':>2} {'word':<34}{'grok':>5}{'blk':>5}{'said':>5}{'bid':>5}{'ask':>5}{'mid':>6}{'gap':>7}  "
                 f"{'result':<7}trades")
    for i, r in enumerate(rows, 1):
        trades = " ".join(f"{t['variant']}:{t['side']}" for t in r["trades"]) or "-"
        lines.append(
            f"{i:>2} {_trunc(r['word'], 33):<34}{_fx(r['grok'], 0):>5}{_fx(r['p_block'], 2):>5}{_fx(r['p_said'], 2):>5}"
            f"{_fx(r['bid'], 0):>5}{_fx(r['ask'], 0):>5}{_fx(r['mid'], 1):>6}{_fx(r['gap'], 1):>7}  "
            f"{str(r['outcome'] or 'open').upper():<7}{trades}"
        )
    lines.append("(story / substitute / reasoning for every word are inside the raw JSON above)")
    lines.append("")
    lines.append("ORDERS (paper)")
    if not n["orders"]:
        lines.append("  (no order cleared any rule this night)")
    for o in n["orders"]:
        lines.append(
            f"  [{o['variant']} ${_fx(o['notional'], 0)}] {o['word']} {o['side']} "
            f"{o['intended']:.2f}ct @ our {o['our_px']}¢ (YES ticket {_fx(o['yes_ticket'], 0)}¢) "
            f"| Grok {_fx(o['grok'], 0)} mkt {_fx(o['mid'], 0)} gap {_fx(o['gap_real'], 1)}"
        )
        if o["state"] == "unfilled":
            lines.append(f"      UNFILLED (filled {o['filled']:.2f} of {o['intended']:.2f}) status={o['status']}")
        elif o["state"] == "pending":
            lines.append(f"      PENDING filled {o['filled']:.2f} of {o['intended']:.2f}, no official result yet")
        elif o["state"] == "void":
            lines.append("      VOID")
        else:
            lines.append(
                f"      {o['state'].upper()}  result={o['outcome']}  filled {o['filled']:.2f}/{o['intended']:.2f}  "
                f"net={_money(o['net'])} fees={(o['fees'] or 0) / 100:.2f}"
            )
    return lines


# --------------------------------------------------------------------------
# building the two files
# --------------------------------------------------------------------------
def _week_id(start: str) -> str:
    d = datetime.strptime(start, "%Y-%m-%d").date()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def render_txt(data: dict, week_id: str) -> str:
    rows = _all_rows(data)
    orders = _all_orders(data)
    lines: list[str] = []
    lines.append("WNT GAP TRADER — WEEKLY PAPER BOOK (deep dump)")
    lines.append(f"generated {clock.fmt(clock.now_ct())} | week {week_id} | nights {data['start']} → {data['end']}")
    lines.append(f"nights with a run: {len(data['nights'])} | words: {len(rows)} | paper orders: {len(orders)} | "
                 f"settle sweep: {_dumps(data['settle_totals'])}")
    lines.append("")
    lines.append("WHAT IS IN THIS FILE")
    lines.append(" 1 Week results by book        6 Data-quality flags")
    lines.append(" 2 Night-by-night scoreboard   7 Rules and settings, then the system prompt")
    lines.append(" 3 How good is Grok (all words) 8 Full detail for every night: word list,")
    lines.append(" 4 Trade slices                   raw Grok JSON, per-word table, orders")
    lines.append(" 5 Biggest misses / wins       9 Ask for Claude")

    _hdr(lines, "1. WEEK RESULTS BY BOOK")
    lines += _books_block(data)
    _hdr(lines, "2. NIGHT-BY-NIGHT SCOREBOARD (all words, traded or not)")
    lines += _nights_block(data)
    _hdr(lines, "3. HOW GOOD IS GROK? (every word that has an official result, traded or not)")
    lines += _calibration_block(rows)
    _hdr(lines, "4. TRADE SLICES (which kinds of trades made or lost money)")
    lines += _trade_slices_block(data)
    _hdr(lines, "5. BIGGEST MISSES AND WINS")
    lines += _misses_block(rows)
    _hdr(lines, "6. DATA-QUALITY FLAGS")
    lines += _quality_block(data)
    _hdr(lines, "7A. RULES AND SETTINGS")
    lines += _config_block()
    _hdr(lines, "7B. SYSTEM PROMPT SENT TO GROK")
    lines += _prompt_block(data)
    _hdr(lines, "8. FULL DETAIL FOR EVERY NIGHT")
    if not data["nights"]:
        lines.append("NO RUNS THIS WEEK")
    for n in data["nights"]:
        lines.append("")
        lines.append("#" * 72)
        lines += _night_block(n)
    _hdr(lines, "9. ASK FOR CLAUDE")
    lines += [
        "Read sections 1-6 first, then use section 8 to look at specific words.",
        "Do not invent a bankroll, cluster cap, or $5 size. Scalp (C/D) was removed in v1.5.0.",
        "Questions to answer, in order:",
        " a) Is Grok calibrated? Use the calibration buckets and both threshold scans. Where does it break?",
        " b) Does Grok beat the market mid on Brier? On which word types (count vs plain)?",
        " c) Which gap sizes and Grok-probability ranges made or lost money in A/E/G?",
        " d) Read the biggest misses. Is there a pattern in the wording/substitute-risk failures that the prompt could fix?",
        " e) Are p_block_airs and p_said_given_airs useful, or is only the final probability informative?",
        " f) What ONE change to the prompt or the rules is worth testing next, and what would prove it wrong?",
        "One week is a small sample. Say how confident each conclusion is. Do not retune from one week.",
    ]
    return "\n".join(lines) + "\n"


def render_csv(data: dict) -> str:
    variants = [v["id"] for v in C.VARIANTS]
    cols = ["date", "event_ticker", "market_ticker", "word", "is_count_market", "grok_p", "p_block_airs",
            "p_said_given_airs", "mkt_bid", "mkt_ask", "mkt_mid", "gap", "outcome", "said_yes",
            "cycle_temp", "has_substitute_risk"]
    for v in variants:
        cols += [f"{v}_side", f"{v}_filled", f"{v}_state", f"{v}_net_cents"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in _all_rows(data):
        row = [r["date"], r["event_ticker"], r["ticker"], r["word"], int(r["is_count"]), r["grok"],
               r["p_block"], r["p_said"], r["bid"], r["ask"],
               None if r["mid"] is None else round(r["mid"], 2),
               None if r["gap"] is None else round(r["gap"], 2),
               r["outcome"], r["y"], r["cycle_temp"], int(r["has_sub"])]
        by_v = {t["variant"]: t for t in r["trades"]}
        for v in variants:
            t = by_v.get(v)
            if t:
                row += [t["side"], t["filled"], t["state"], t["net"]]
            else:
                row += ["", "", "", ""]
        w.writerow(["" if x is None else x for x in row])
    return buf.getvalue()


def build_week_bundle(start: str | None = None, end: str | None = None) -> dict:
    if not start or not end:
        start, end, week_id = clock.week_mon_fri()
    else:
        week_id = _week_id(start)
    data = collect(start, end)
    rows = _all_rows(data)
    orders = _all_orders(data)
    net = sum(o["net"] or 0 for o in orders if o["net"] is not None)
    return {
        "start": start,
        "end": end,
        "week_id": week_id,
        "txt_name": f"gap-week-{week_id}.txt",
        "txt": render_txt(data, week_id),
        "csv_name": f"gap-week-{week_id}-words.csv",
        "csv": render_csv(data),
        "n_nights": len(data["nights"]),
        "n_words": len(rows),
        "n_orders": len(orders),
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
    caption = (
        f"WNT gap weekly {week_id} ({start} → {end}). "
        f"{b['n_nights']} nights, {b['n_words']} words, {b['n_orders']} paper orders, "
        f"net ${b['net_cents'] / 100:+.2f}. Paste the .txt into Claude. CSV is for your notebook."
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
