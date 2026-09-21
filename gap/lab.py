"""Strategy lab: REPORT-ONLY simulations on real data. No orders, no Kalshi calls.

Question it answers: which way of trading Grok's forecasts grows an account fastest and
highest, and how sure are we? Everything here is computed from three things already in
the database: Grok's forecasts, the ORDER BOOK AT THE DECISION TIME (gap_decision_books,
backfilled from the shared `depth` table), and Kalshi's official results.

How trades are simulated ("taking at market"):
  * NO  ticket: walk the YES-bid book from the best (highest) bid downward. A YES bid at
    price p sells us NO at (100 - p). Fill until the dollar target or the top-10 levels run out.
  * YES ticket: walk the NO-bid book the same way, paying (100 - NO bid).
  * Fee = 0.07 x contracts x P x (1-P), rounded up, on the total (same rule as the bot).
  * One snapshot, one walk: liquidity is never counted twice.

Honesty rules built in:
  * Every variant is listed in REGISTRY with the week it was frozen. Weeks BEFORE
    FROZEN_FROM are IN-SAMPLE (we looked at that data while designing). Only later weeks are
    out-of-sample, and only those can confirm a strategy.
  * Every table shows n and a 90% range on the hit rate. With a few dozen trades the range
    is wide, and the lab says so ("TOO FEW", "POSSIBLE", "CLEAR").
  * The grid of many variants is EXPLORATORY (hypotheses for next week), not a result.
"""
from __future__ import annotations

import logging
import math

from . import clock, config as C, fees, fills, quotes as Q, store

log = logging.getLogger("gap.lab")

# ---------------- frozen numbers (pre-registered in the W38 review) ----------------
SPLIT_MID = 55            # K_HIGH: booked/decision mid >= 55; K_LOW: mid < 55. Frozen 6 weeks / 30 filled K_HIGH.
SIZES = (10, 25, 50, 100)  # dollars per trade in the size sweep
SWEEP_PASS_SIZE = 50
SWEEP_PASS_FILL = 80.0     # filled % needed at $50
SWEEP_PASS_MARGIN = 10.0   # points above break-even needed at $50
FROZEN_FROM = "2026-W39"   # variants below were designed on W38: W38 is IN-SAMPLE, W39+ is out-of-sample
WINDOW_S = 900             # decision-book lookup window: +/- 15 minutes
BOOK_LEVELS = 10           # top-10 levels only
CAPACITY_FLOORS = (60, 65, 70)


# ---------------------------------------------------------------------------
# book math
# ---------------------------------------------------------------------------
def book_levels(book, top: int = BOOK_LEVELS) -> list[tuple[int, float]]:
    """[(price_cents, contracts)] best (highest) price FIRST, top levels only."""
    lv = [(int(p), float(c)) for p, c in (book or []) if c and float(c) > 0]
    lv.sort(key=lambda x: -x[0])
    return lv[:top]


def no_dollars(yes_book, floor: int):
    """Dollars of NO we could buy from YES bids at price >= floor: sum(contracts * (100-price)/100).
    None if the book is empty / missing (blank, never 0)."""
    lv = book_levels(yes_book)
    if not lv:
        return None
    return sum(c * (100 - p) / 100.0 for p, c in lv if p >= floor)


def decision_quote(yes_book, no_book):
    """(bid, ask) from the book: best YES bid, and 100 - best NO bid."""
    y, n = book_levels(yes_book), book_levels(no_book)
    bid = y[0][0] if y else None
    ask = (100 - n[0][0]) if n else None
    return bid, ask


def take(levels: list[tuple[int, float]], dollars: float) -> dict:
    """Buy from the best level down until `dollars` is spent or the book runs out.
    `levels` are the BIDS we hit (best first); we pay (100 - bid) cents per contract."""
    remaining = float(dollars)
    contracts = cost = 0.0
    used = 0
    for p, q in levels:
        price = 100 - p
        if price <= 0 or price >= 100 or remaining <= 1e-9:
            continue
        c = min(q, remaining / (price / 100.0))
        if c <= 0:
            continue
        contracts += c
        cost += c * price
        remaining -= c * price / 100.0
        used += 1
    avg = (cost / contracts) if contracts > 0 else None
    fee = fees.fee_cents(contracts, int(round(avg))) if avg else 0
    return {
        "contracts": contracts, "cost_cents": cost, "avg_px": avg, "fee_cents": fee,
        "filled_pct": 100.0 * (cost / 100.0) / dollars if dollars else 0.0, "levels": used,
        "fee_pc": (fee / contracts) if contracts > 0 else None,
    }


def settle_net(t: dict, side: str, y: int) -> float:
    """Net cents after fees for a simulated take, given the result (y=1 YES said)."""
    if t["contracts"] <= 0:
        return 0.0
    won = (side == "NO" and y == 0) or (side == "YES" and y == 1)
    gross = t["contracts"] * 100.0 - t["cost_cents"] if won else -t["cost_cents"]
    return gross - t["fee_cents"]


def simulate(word: dict, side: str, dollars: float) -> dict:
    lv = book_levels(word["yes_book"] if side == "NO" else word["no_book"])
    t = take(lv, dollars)
    t["side"] = side
    t["won"] = (side == "NO" and word["y"] == 0) or (side == "YES" and word["y"] == 1)
    t["net_cents"] = settle_net(t, side, word["y"])
    return t


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.645) -> tuple[float | None, float | None]:
    """90% Wilson interval for a hit rate, in percent."""
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return 100 * max(0.0, mid - half), 100 * min(1.0, mid + half)


def verdict(n: int, hit: float | None, be: float | None, lo: float | None) -> str:
    """Deliberately hard to satisfy: with under 30 trades nothing is ever called CLEAR."""
    if n < 10 or hit is None or be is None:
        return "TOO FEW (under 10 trades)"
    small = n < 30
    if lo is not None and lo > be:
        return "PROMISING (under 30 trades: not enough to trust)" if small else "CLEAR (even the low end beats break-even)"
    if hit > be:
        return "POSSIBLE (under 30 trades)" if small else "POSSIBLE (hit rate beats break-even, but the range still crosses it)"
    return "NO EDGE (hit rate at or below break-even)"


def stats_for(trades: list[dict], dollars: float) -> dict:
    """trades: results of simulate() for words that got a fill."""
    got = [t for t in trades if t["contracts"] > 0]
    n = len(got)
    wins = sum(1 for t in got if t["won"])
    hit = (100.0 * wins / n) if n else None
    px = (sum(t["avg_px"] for t in got) / n) if n else None
    fee_pc = (sum(t["fee_pc"] for t in got) / n) if n else None
    be = (px + fee_pc) if (px is not None and fee_pc is not None) else None
    lo, hi = wilson(wins, n)
    spent = sum(t["cost_cents"] for t in got)
    net = sum(t["net_cents"] for t in got)
    tried = len(trades)
    return {
        "signals": tried, "trades": n, "filled_pct": (100.0 * spent / 100.0 / (dollars * tried)) if tried and dollars else None,
        "wins": wins, "hit": hit, "lo": lo, "hi": hi, "avg_px": px, "fee_pc": fee_pc, "be": be,
        "margin": (hit - be) if (hit is not None and be is not None) else None,
        "spent": spent / 100.0, "net": net / 100.0, "roi": (100.0 * net / spent) if spent else None,
        "verdict": verdict(n, hit, be, lo),
    }


# ---------------------------------------------------------------------------
# the word dataset
# ---------------------------------------------------------------------------
def prepare(data: dict) -> dict:
    """Attach decision-time books to every scored word. Backfills gap_decision_books from the
    shared depth table (once per market). Returns {"words": [...], "books": {...}}."""
    run_info = data.get("run_info") or {}
    rows = [r for r in data.get("history_rows", []) if r.get("run_id") in run_info]
    saved = store.decision_books_for_runs(sorted({r["run_id"] for r in rows}))
    words = []
    for r in rows:
        info = run_info[r["run_id"]]
        key = (r["run_id"], r["ticker"])
        book = saved.get(key)
        if book is None and info.get("decision_at") is not None:
            dec = clock.parse_dt(info["decision_at"])
            try:
                snap = store.depth_near(r["ticker"], info["date"], dec, WINDOW_S) if dec else None
            except Exception as exc:
                log.warning("depth_near %s: %s", r["ticker"], exc)
                snap = None
            if snap:
                try:
                    store.insert_decision_book(r["run_id"], r["ticker"], dec, snap, "backfill")
                except Exception as exc:
                    log.warning("save decision book: %s", exc)
                book = {"yes_book": snap["yes_book"], "no_book": snap["no_book"], "captured_at": snap.get("ts")}
        yb = book["yes_book"] if book else None
        nb = book["no_book"] if book else None
        bid, ask = decision_quote(yb, nb) if book else (None, None)
        valid, why = Q.validate(bid, ask)
        w = {
            "run_id": r["run_id"], "date": r["date"], "week": r["week"], "prompt": info.get("prompt"),
            "word": r["word"], "ticker": r["ticker"], "grok": r["grok"], "is_count": r["is_count"], "y": r["y"],
            "has_book": bool(book and (yb or nb)), "yes_book": yb or [], "no_book": nb or [],
            "bid": bid, "ask": ask, "valid": bool(valid), "why_invalid": why,
            "mid": ((bid + ask) / 2.0) if valid else None,
            "cap60": no_dollars(yb, 60) if yb else None,
            "cap65": no_dollars(yb, 65) if yb else None,
            "cap70": no_dollars(yb, 70) if yb else None,
        }
        words.append(w)
    return {"words": words}


def _gap2(w) -> int:
    """2*Grok - (bid+ask): half-point units, exact."""
    return 2 * int(round(w["grok"])) - (int(w["bid"]) + int(w["ask"]))


def _ok(w) -> bool:
    return w["has_book"] and w["valid"] and w["grok"] is not None and w["y"] is not None


# ---------------------------------------------------------------------------
# the variants (each returns "YES", "NO" or None)
# ---------------------------------------------------------------------------
def v_k(w):
    return "NO" if _ok(w) and w["grok"] <= 30 and _gap2(w) < -30 else None


def v_k_high(w):
    return "NO" if v_k(w) and w["mid"] >= SPLIT_MID else None


def v_k_low(w):
    return "NO" if v_k(w) and w["mid"] < SPLIT_MID else None


M_MIN_YES_BID = 60   # Book M (pre-registered): buy NO at market on EVERY word with a valid frozen YES bid >= 60


def v_m(w):
    """Book M: ignore Grok completely. NO on every word whose (valid) decision-time YES bid is >= 60."""
    return "NO" if _ok(w) and int(w["bid"]) >= M_MIN_YES_BID else None


def v_m_groklow(w):
    """M words where Grok is also low (Grok <= 30 and market > 15 above): M and K together."""
    return "NO" if v_m(w) and v_k(w) else None


def v_m_rest(w):
    """M words where Grok is NOT low: the part of M that Grok would have skipped."""
    return "NO" if v_m(w) and not v_k(w) else None


def v_yes_mirror(w):
    return "YES" if _ok(w) and w["grok"] >= 70 and _gap2(w) > 30 else None


def v_yes_mirror_low(w):
    return "YES" if v_yes_mirror(w) and w["mid"] <= 100 - SPLIT_MID else None


def v_fade_both(w):
    if not _ok(w) or abs(_gap2(w)) <= 30:
        return None
    return "YES" if _gap2(w) > 0 else "NO"


def v_fade_gate(w):
    s = v_fade_both(w)
    if s == "YES" and w["grok"] >= 51:
        return "YES"
    if s == "NO" and w["grok"] <= 49:
        return "NO"
    return None


def v_edge_vs_ask(w):
    if not _ok(w):
        return None
    p, bid, ask = int(round(w["grok"])), int(w["bid"]), int(w["ask"])
    if p - ask > C.EDGE_EXEC_THRESHOLD and (p - ask) >= (bid - p):
        return "YES"
    if bid - p > C.EDGE_EXEC_THRESHOLD:
        return "NO"
    return None


def v_grok_only(w):
    if not (w["has_book"] and w["grok"] is not None and w["y"] is not None):
        return None
    return "YES" if w["grok"] >= 51 else ("NO" if w["grok"] <= 49 else None)


def v_all_no(w):
    return "NO" if w["has_book"] and w["y"] is not None else None


REGISTRY = [
    # id, label, fn, family, why it exists
    ("K", "K: NO when Grok<=30 and market >15 above", v_k, "pre-registered", "The W38-review slice."),
    ("K_HIGH", "K_HIGH: same, market mid >= 55", v_k_high, "pre-registered", "Cheap NO tickets (<= ~45c): break-even under ~47%."),
    ("K_LOW", "K_LOW: same, market mid < 55", v_k_low, "pre-registered", "The thin segment: NO costs 60c+. Kept apart on purpose."),
    ("M", "Book M: NO on EVERY word with YES bid >= 60 (ignore Grok)", v_m, "pre-registered", "Does taking at market work without Grok at all?"),
    ("M_GROKLOW", "M words where Grok is low (Grok <= 30, gap > 15)", v_m_groklow, "exploratory", "The Grok-low part of M."),
    ("M_REST", "M words where Grok is NOT low", v_m_rest, "exploratory", "The part of M Grok would skip."),
    ("YES_MIRROR", "YES mirror: YES when Grok>=70 and Grok >15 above market", v_yes_mirror, "exploratory", "Does the low-side edge also exist on the high side?"),
    ("YES_MIRROR_LOW", "YES mirror, market mid <= 45", v_yes_mirror_low, "exploratory", "Cheap YES tickets."),
    ("FADE_BOTH", "Fade both ways at market (|gap|>15)", v_fade_both, "exploratory", "Book A's rule, but taking at market."),
    ("FADE_GATE", "Fade + Grok side gate at market", v_fade_gate, "exploratory", "Book E's rule, at market."),
    ("EDGE_ASK", "Edge vs ask/bid > 10 (Book I) at market", v_edge_vs_ask, "exploratory", "Book I's rule."),
    ("GROK_ONLY", "Grok side only, ignore the market", v_grok_only, "baseline", "Is the market information adding anything?"),
    ("ALL_NO", "Buy NO on every word", v_all_no, "baseline", "Most words are NOT said: this is the do-nothing-clever baseline."),
]


def evaluate(words: list[dict], fn, dollars: float) -> dict:
    trades = []
    for w in words:
        side = fn(w)
        if side:
            trades.append(simulate(w, side, dollars))
    return stats_for(trades, dollars)


def leaderboard(words: list[dict], dollars: float, weeks: list[str] | None = None) -> list[dict]:
    """One row per registered variant: in-sample weeks vs out-of-sample weeks vs all."""
    def sub(ws, pred):
        return [w for w in ws if pred(w)]
    rows = []
    for vid, label, fn, family, why in REGISTRY:
        use = words if not weeks else [w for w in words if w["week"] in weeks]
        ins = sub(use, lambda w: w["week"] < FROZEN_FROM)
        oos = sub(use, lambda w: w["week"] >= FROZEN_FROM)
        rows.append({
            "id": vid, "label": label, "family": family, "why": why,
            "all": evaluate(use, fn, dollars),
            "in_sample": evaluate(ins, fn, dollars),
            "out_of_sample": evaluate(oos, fn, dollars),
            "note": ("IN-SAMPLE ONLY: needs " + FROZEN_FROM + " or later to confirm") if not any(w["week"] >= FROZEN_FROM for w in use) else "",
        })
    return rows


def grid_no(words: list[dict], dollars: float = 25.0) -> list[dict]:
    """Exploratory grid: NO when Grok <= g and market mid >= m (and market > Grok+15)."""
    out = []
    for g in (20, 25, 30, 35, 40):
        for m in (35, 45, 55, 65):
            def fn(w, g=g, m=m):
                return "NO" if _ok(w) and w["grok"] <= g and _gap2(w) < -30 and w["mid"] >= m else None
            st = evaluate(words, fn, dollars)
            out.append({"grok_max": g, "mid_min": m, **st})
    return out


# ---------------------------------------------------------------------------
# K_HIGH size sweep, capacity, segment frequency, fills by bucket
# ---------------------------------------------------------------------------
def k_high_words(words):
    return [w for w in words if v_k_high(w)]


SWEEP_VARIANTS = (("K_HIGH", "v_k_high"), ("K", "v_k"), ("K_LOW", "v_k_low"), ("M", "v_m"))


def size_sweep(words: list[dict], sizes=SIZES, variant: str = "K_HIGH") -> list[dict]:
    """Take NO at the decision-time book on every candidate of a variant, at each dollar size.
    This is the honest test of REAL SIZE: it walks the displayed book, so a big size only fills
    what is really there (and pays worse prices as it goes)."""
    fn = {"K_HIGH": v_k_high, "K": v_k, "K_LOW": v_k_low, "M": v_m}[variant]
    cands = [w for w in words if fn(w)]
    weeks = sorted({w["week"] for w in cands})
    rows = []
    for scope in weeks + ["CUMULATIVE"]:
        ws = cands if scope == "CUMULATIVE" else [w for w in cands if w["week"] == scope]
        for d in sizes:
            st = evaluate(ws, fn, d)
            rows.append({"scope": scope, "size": d, "variant": variant, **st})
    return rows


def m_vs_khigh(words: list[dict], dollars: float = 1.0) -> list[dict]:
    """Book M next to K_HIGH (and the Grok-low / not-Grok-low halves of M), $1 per trade, week by week + cumulative."""
    weeks = sorted({w["week"] for w in words})
    rows = []
    for scope in weeks + ["CUMULATIVE"]:
        ws = words if scope == "CUMULATIVE" else [w for w in words if w["week"] == scope]
        for vid, fn in (("M", v_m), ("K_HIGH", v_k_high), ("M_GROKLOW", v_m_groklow), ("M_REST", v_m_rest)):
            rows.append({"scope": scope, "variant": vid, **evaluate(ws, fn, dollars)})
    return rows


def m_reading(rows: list[dict]) -> str:
    """Plain-words reading of M vs K_HIGH (cumulative). Deliberately cautious about small n."""
    cum = {r["variant"]: r for r in rows if r["scope"] == "CUMULATIVE"}
    m, k = cum.get("M"), cum.get("K_HIGH")
    if not m or not k or m["trades"] < 10 or k["trades"] < 10:
        return (f"TOO EARLY: M has {m['trades'] if m else 0} trades and K_HIGH has {k['trades'] if k else 0}; "
                "each needs about 30 before this comparison means anything.")
    m_clear = m["lo"] is not None and m["be"] is not None and m["lo"] > m["be"]
    if m_clear:
        return (f"M clears break-even by itself (hit {m['hit']:.0f}% vs {m['be']:.0f}%, low end {m['lo']:.0f}%): "
                "a bigger, deeper edge exists even without Grok. Check whether K_HIGH adds on top of it.")
    if (m["margin"] or 0) <= 3 and (k["margin"] or 0) >= 5:
        return (f"M sits at break-even (margin {m['margin']:+.1f}) while K_HIGH is above it (margin {k['margin']:+.1f}): "
                "Grok is what makes taking at market work.")
    return (f"No clear reading: M margin {m['margin']:+.1f}, K_HIGH margin {k['margin']:+.1f}. "
            "Neither pattern (M flat + K_HIGH up, or M clearly up) is showing yet.")


def sweep_pass(rows: list[dict]) -> dict:
    cum = [r for r in rows if r["scope"] == "CUMULATIVE" and r["size"] == SWEEP_PASS_SIZE]
    if not cum or cum[0]["trades"] == 0:
        return {"status": "TOO EARLY (no K_HIGH candidates with a decision-time book yet)", "ok": None}
    r = cum[0]
    ok = (r["filled_pct"] or 0) >= SWEEP_PASS_FILL and (r["margin"] or -99) >= SWEEP_PASS_MARGIN
    tag = "PASSING" if ok else "NOT PASSING"
    line = (f"at ${SWEEP_PASS_SIZE}: filled {r['filled_pct']:.0f}% (needs >= {SWEEP_PASS_FILL:.0f}) and "
            f"margin {r['margin']:+.1f} pts (needs >= +{SWEEP_PASS_MARGIN:.0f}); n={r['trades']} trades")
    if r["trades"] < 30:
        return {"status": f"TOO EARLY (n={r['trades']} of 30 trades). So far, {tag.lower()} " + line, "ok": None}
    return {"status": f"{tag} " + line, "ok": ok}


def _pctile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def capacity_summary(words: list[dict]) -> list[dict]:
    """median / p75 / p90 of NO dollars available at YES bid >= 60 (also 65, 70)."""
    valid = [w for w in words if w["valid"] and w["has_book"]]
    kh = k_high_words(words)
    rows = []
    for label, ws in (("all valid words", valid), ("K_HIGH candidates", kh)):
        for floor, key in ((60, "cap60"), (65, "cap65"), (70, "cap70")):
            vals = [w[key] for w in ws]
            rows.append({"group": label, "floor": floor, "n": len([v for v in vals if v is not None]),
                         "median": _pctile(vals, 0.5), "p75": _pctile(vals, 0.75), "p90": _pctile(vals, 0.90)})
    return rows


def segment_frequency(words: list[dict], slice_orders: list[dict]) -> list[dict]:
    """Per night: K_HIGH candidates (Grok<=30, valid decision mid >= 55), how many the books booked / filled."""
    nights = sorted({w["date"] for w in words})
    rows = []
    for d in nights:
        cands = [w for w in words if w["date"] == d and v_k_high(w)]
        booked = [o for o in slice_orders if o["date"] == d and o["variant"] == "A" and not o["excluded"]
                  and o["side"] == "NO" and o["grok"] is not None and o["grok"] <= 30
                  and ((o["q_bid"] or 0) + (o["q_ask"] or 0)) / 2.0 >= SPLIT_MID]
        rows.append({"date": d, "candidates": len(cands), "words": ", ".join(w["word"] for w in cands) or "-",
                     "booked_by_A": len(booked), "filled": sum(1 for o in booked if o["filled"] > 0),
                     "no_book": sum(1 for w in words if w["date"] == d and not w["has_book"])})
    return rows


BUCKETS = ((1, 19, "Grok 1-19"), (20, 30, "Grok 20-30"), (31, 49, "Grok 31-49"), (50, 100, "Grok 50+"))


def fills_by_bucket(slice_orders: list[dict], books=("A", "B", "I")) -> list[dict]:
    rows = []
    allo = [o for o in slice_orders if not o["excluded"] and o["filled"] > 0]
    weeks = sorted({o["week"] for o in allo})
    for book in books:
        for scope in weeks + ["CUMULATIVE"]:
            for lo, hi, label in BUCKETS:
                os_ = [o for o in allo if o["variant"] == book and o["grok"] is not None and lo <= o["grok"] <= hi
                       and (scope == "CUMULATIVE" or o["week"] == scope)]
                settled = [o for o in os_ if o["state"] in ("won", "lost")]
                wins = sum(1 for o in settled if o["state"] == "won")
                rows.append({
                    "book": book, "scope": scope, "bucket": label, "fills": len(os_), "settled": len(settled), "wins": wins,
                    "hit": (100.0 * wins / len(settled)) if settled else None,
                    "avg_px": (sum(o["our_px"] for o in settled) / len(settled)) if settled else None,
                    "net": sum((o["net"] or 0) for o in settled) / 100.0,
                })
    return rows


# ---------------------------------------------------------------------------
# long-rest shadow (item 12): would K_HIGH orders that keep resting until 17:28 CT fill more?
# ---------------------------------------------------------------------------
def _replay_one(o: dict, until) -> dict | None:
    """Replay one order against the stored depth history with the no-double-counting rule:
    it can take the FIRST displayed size at its limit, plus later INCREASES. Returns None if unusable."""
    placed = clock.parse_dt(o.get("placed_at"))
    if placed is None or not o.get("ticker") or o.get("yes_ticket") is None:
        return None
    try:
        series = store.depth_series(o["ticker"], o["date"], placed, until)
    except Exception as exc:
        log.warning("depth series: %s", exc)
        series = []
    if not series:
        return {"note": "no depth history left for this market"}
    yes_limit = int(o["yes_ticket"])
    remaining, filled, credit, last, cost = float(o["intended"]), 0.0, 0.0, None, 0.0
    for snap in series:
        size, best_yes = fills.crossing({"yes": snap["yes_book"], "no": snap["no_book"]}, o["side"], yes_limit)
        if size != (last if last is not None else 0.0):
            credit += size if last is None else max(0.0, size - last)
            last = size
        take_c = min(remaining, max(0.0, credit - filled))
        if take_c > 0:
            px = fills.slice_price(o["side"], yes_limit, best_yes)
            filled += take_c
            remaining -= take_c
            cost += take_c * px
    avg = (cost / filled) if filled > 0 else None
    fee = fees.fee_cents(filled, int(round(avg))) if avg else 0
    net = None
    if avg is not None and o.get("outcome") in ("yes", "no"):
        won = (o["side"] == "NO" and o["outcome"] == "no") or (o["side"] == "YES" and o["outcome"] == "yes")
        gross = filled * (100 - avg) if won else -filled * avg
        net = (gross - fee) / 100.0
    return {"filled": round(filled, 2), "avg_px": avg, "net": net, "note": ""}


def long_rest_shadow(orders: list[dict], until_hhmm: str = "17:28") -> list[dict]:
    """K_HIGH orders that keep resting until 17:28 CT instead of 60 minutes after booking."""
    from datetime import datetime, timezone
    out = []
    for o in orders:
        hh, mm = [int(x) for x in until_hhmm.split(":")]
        d = datetime.strptime(o["date"], "%Y-%m-%d").date()
        until = datetime(d.year, d.month, d.day, hh, mm, tzinfo=C.CT).astimezone(timezone.utc)
        r = _replay_one(o, until)
        if r is None:
            continue
        if r["note"]:
            out.append({**_shadow_id(o), "note": r["note"]})
            continue
        out.append({**_shadow_id(o), "actual_filled": o["filled"],
                    "actual_net": (o["net"] or 0) / 100.0 if o["net"] is not None else None,
                    "long_filled": r["filled"], "long_avg_px": r["avg_px"], "long_net": r["net"], "note": ""})
    return out


def recompute_fills(orders: list[dict]) -> list[dict]:
    """The SAME orders, replayed with the no-double-counting fill rule inside their normal window
    (booking time + the cancel window). Shows what a $100 order REALLY would have filled when the
    old paper model let the same displayed liquidity be counted on every poll."""
    from datetime import timedelta
    out = []
    for o in orders:
        placed = clock.parse_dt(o.get("placed_at"))
        if placed is None:
            continue
        r = _replay_one(o, placed + timedelta(minutes=C.CANCEL_AFTER_MIN))
        if r is None:
            continue
        base = {**_shadow_id(o), "paper_filled": o["filled"],
                "paper_net": (o["net"] or 0) / 100.0 if o["net"] is not None else None}
        if r["note"]:
            out.append({**base, "note": r["note"]})
        else:
            out.append({**base, "real_filled": r["filled"], "real_avg_px": r["avg_px"], "real_net": r["net"], "note": ""})
    return out


def _shadow_id(o):
    return {"date": o["date"], "book": o["variant"], "word": o["word"], "intended": o["intended"]}


# ---------------------------------------------------------------------------
# growth: replay + Monte Carlo + Kelly
# ---------------------------------------------------------------------------
def kelly_fraction(hit_pct: float, avg_px: float, fee_pc: float) -> float:
    """Full-Kelly fraction of bankroll for a $1-payout contract bought at avg_px cents plus fee."""
    q = (avg_px + fee_pc) / 100.0
    p = hit_pct / 100.0
    if q >= 1 or q <= 0:
        return 0.0
    return max(0.0, min(1.0, (p - q) / (1 - q)))


def replay(words: list[dict], fn, bankroll0: float, mode: str, value: float, night_cap_pct: float = 100.0) -> dict:
    """Compound through the real nights in order.
    mode 'flat': `value` dollars per trade. mode 'pct': `value` % of current bankroll per trade.
    night_cap_pct: never risk more than this % of the bankroll across one night's trades.
    Each trade is walked through the actual decision-time book, so size is limited by depth."""
    nights = sorted({w["date"] for w in words})
    bank = float(bankroll0)
    curve = [{"date": "start", "bankroll": bank}]
    peak = bank
    worst_dd = 0.0
    worst_night = 0.0
    n_trades = 0
    for d in nights:
        todays = [w for w in words if w["date"] == d and fn(w)]
        if not todays:
            curve.append({"date": d, "bankroll": bank})
            continue
        want = (value if mode == "flat" else bank * value / 100.0)
        cap = bank * night_cap_pct / 100.0
        per = min(want, cap / len(todays))
        if per <= 0:
            curve.append({"date": d, "bankroll": bank})
            continue
        net_c = 0.0
        for w in todays:
            t = simulate(w, fn(w), per)
            if t["contracts"] > 0:
                n_trades += 1
            net_c += t["net_cents"]
        bank += net_c / 100.0
        worst_night = min(worst_night, net_c / 100.0)
        peak = max(peak, bank)
        worst_dd = min(worst_dd, (bank - peak) / peak if peak else 0.0)
        curve.append({"date": d, "bankroll": bank})
    return {"curve": curve, "final": bank, "return_pct": 100.0 * (bank / bankroll0 - 1.0) if bankroll0 else None,
            "max_drawdown_pct": 100.0 * worst_dd, "worst_night": worst_night, "trades": n_trades}


def monte_carlo(hit_pct: float, avg_px: float, fee_pc: float, trades_per_night: float, nights: int,
                bankroll0: float, stake_pct: float, paths: int = 2000, seed: int = 7) -> dict:
    """Forward simulation: each night has ~trades_per_night trades, each risking stake_pct of the
    bankroll at avg_px, winning with probability hit_pct. Shows the SPREAD of outcomes (luck), and is
    only as good as the hit rate you feed it."""
    import numpy as np
    rng = np.random.default_rng(seed)
    per_night = max(0, int(round(trades_per_night)))
    bank = np.full(paths, float(bankroll0))
    q = avg_px / 100.0
    fee_frac = fee_pc / 100.0            # dollars of fee per contract
    for _ in range(nights):
        for _t in range(per_night):
            stake = bank * stake_pct / 100.0
            contracts = stake / q
            win = rng.random(paths) < (hit_pct / 100.0)
            pnl = np.where(win, contracts * (1.0 - q), -stake) - contracts * fee_frac
            bank = np.maximum(bank + pnl, 0.0)
    pct = lambda x: float(np.percentile(bank, x))
    return {"p10": pct(10), "p50": pct(50), "p90": pct(90), "mean": float(bank.mean()),
            "prob_loss": float((bank < bankroll0).mean()), "prob_2x": float((bank >= 2 * bankroll0).mean()),
            "per_night": per_night}


def growth_summary(words: list[dict], fn) -> dict:
    """What a variant looks like at market: trades per night, hit, average price (feeds the Monte Carlo)."""
    st = evaluate(words, fn, 25.0)
    nights = len({w["date"] for w in words}) or 1
    return {"stats": st, "trades_per_night": st["trades"] / nights, "nights": nights}


# ---------------------------------------------------------------------------
# everything the dump and Streamlit need, in one call
# ---------------------------------------------------------------------------
def panel(data: dict) -> dict:
    prep = prepare(data)
    words = prep["words"]
    slice_orders = data.get("slice_orders", [])
    sweeps = {v: size_sweep(words, variant=v) for v, _ in SWEEP_VARIANTS}
    sweep = sweeps["K_HIGH"]
    mrows = m_vs_khigh(words)
    return {
        "m": mrows, "m_reading": m_reading(mrows),
        "words": words,
        "sweep": sweep, "sweeps": sweeps, "sweep_pass": sweep_pass(sweep),
        "capacity": capacity_summary(words),
        "segments": segment_frequency(words, slice_orders),
        "buckets": fills_by_bucket(slice_orders),
        "coverage": {"words": len(words), "with_book": sum(1 for w in words if w["has_book"]),
                     "valid": sum(1 for w in words if w["valid"] and w["has_book"])},
    }
