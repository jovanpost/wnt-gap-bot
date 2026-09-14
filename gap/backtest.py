"""
Four-way capped-sweep backtest.

Each variant is a pure function of (decision, tape slice, notional, exit_rule,
cancel_min). The tape is never mutated. Variants do not share remaining volume.

Fixed-batch rule: one list of decision points is built first; every cancel
window and every variant walks THAT list.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Iterable

from . import fees, strategy

VARIANTS = (
    {"id": "A", "notional": 1.0, "exit": "hold", "label": "A · $1 hold"},
    {"id": "B", "notional": 100.0, "exit": "hold", "label": "B · $100 hold"},
    {"id": "C", "notional": 1.0, "exit": "scalp", "label": "C · $1 scalp"},
    {"id": "D", "notional": 100.0, "exit": "scalp", "label": "D · $100 scalp"},
)

CANCEL_WINDOWS = (1, 5, 15, 30, 60, 90, 120)

# Informal prototype numbers from the addendum — NOT the fixed-batch result.
INFORMAL_FILL_TABLE = [
    {"notional": 20, "cancel_min": 5, "avg_fill_pct": 72, "fully_filled_pct": 55},
    {"notional": 20, "cancel_min": 15, "avg_fill_pct": 90, "fully_filled_pct": 82},
    {"notional": 20, "cancel_min": 60, "avg_fill_pct": 95, "fully_filled_pct": 90},
    {"notional": 100, "cancel_min": 5, "avg_fill_pct": 43, "fully_filled_pct": 23},
    {"notional": 100, "cancel_min": 15, "avg_fill_pct": 69, "fully_filled_pct": 51},
    {"notional": 100, "cancel_min": 60, "avg_fill_pct": 84, "fully_filled_pct": 72},
]


@dataclass(frozen=True)
class Bar:
    ts: datetime
    ticker: str
    yes_low: int
    yes_high: int
    yes_close: int
    volume: float  # contracts that printed this minute (YES-side tape)


@dataclass(frozen=True)
class Decision:
    decision_id: str
    ticker: str
    word: str
    event_date: str
    decision_ts: datetime
    model_prob: int
    market_yes_cents: int
    outcome: str  # 'yes' | 'no'
    cluster: str


def _side_and_limit(dec: Decision, threshold: int = 15) -> dict | None:
    mid = dec.market_yes_cents / 100.0
    return strategy.decide(
        dec.model_prob, mid, dec.market_yes_cents, dec.market_yes_cents, threshold
    )


def _bar_fill_yes_buy(bar: Bar, limit: int, remaining: float) -> tuple[float, float]:
    """Contracts filled buying YES at <= limit this minute, and notional-cents paid."""
    if remaining <= 0 or bar.volume <= 0:
        return 0.0, 0.0
    if bar.yes_low > limit:
        return 0.0, 0.0
    # Share of the bar that traded through the limit.
    span = max(bar.yes_high - bar.yes_low, 1)
    tradable_frac = min(1.0, (limit - bar.yes_low + 1) / (span + 1))
    available = bar.volume * tradable_frac
    take = min(remaining, available)
    # Fill price: volume-weighted inside [low, min(high, limit)], pulled toward close.
    cap = min(bar.yes_high, limit)
    px = int(round((bar.yes_low + cap + min(bar.yes_close, cap)) / 3))
    px = max(bar.yes_low, min(px, limit))
    return take, take * px


def _bar_fill_no_buy(bar: Bar, no_limit: int, remaining: float) -> tuple[float, float]:
    """Buy NO at NO-price <= no_limit ⇔ YES traded at >= 100-no_limit."""
    yes_floor = 100 - no_limit
    if remaining <= 0 or bar.volume <= 0:
        return 0.0, 0.0
    if bar.yes_high < yes_floor:
        return 0.0, 0.0
    span = max(bar.yes_high - bar.yes_low, 1)
    tradable_frac = min(1.0, (bar.yes_high - yes_floor + 1) / (span + 1))
    available = bar.volume * tradable_frac
    take = min(remaining, available)
    yes_px = int(round((max(bar.yes_low, yes_floor) + bar.yes_high + max(bar.yes_close, yes_floor)) / 3))
    yes_px = min(bar.yes_high, max(yes_px, yes_floor))
    no_px = 100 - yes_px
    return take, take * no_px


def simulate_sweep(
    dec: Decision,
    bars: Iterable[Bar],
    notional: float,
    exit_rule: str,
    cancel_min: int,
    threshold: int = 15,
) -> dict:
    """
    Pure. `bars` is the forward tape from decision_ts inclusive.
    Does not write back into bars.
    """
    plan = _side_and_limit(dec, threshold)
    empty = {
        "decision_id": dec.decision_id,
        "ticker": dec.ticker,
        "word": dec.word,
        "event_date": dec.event_date,
        "side": None,
        "triggered": False,
        "filled": 0.0,
        "intended": 0.0,
        "fill_pct": 0.0,
        "fully_filled": False,
        "avg_fill_cents": None,
        "entry_fee_cents": 0,
        "exit_fee_cents": 0,
        "exit": exit_rule,
        "exit_reason": "no_gap",
        "net_cents": 0,
        "intended_notional": notional,
        "deployed_dollars": 0.0,
        "cancel_min": cancel_min,
    }
    if plan is None:
        return empty
    side = plan["side"]
    yes_limit = plan["yes_price_cents"]
    our_px = plan["our_price_cents"]
    intended = round(notional / (our_px / 100.0), 4)
    remaining = intended
    filled = 0.0
    paid = 0.0  # contracts * our-side cents
    deadline = dec.decision_ts + timedelta(minutes=cancel_min)
    scalp_hit: tuple[datetime, int] | None = None
    model_yes = dec.model_prob

    for bar in bars:
        if bar.ticker != dec.ticker:
            continue
        if bar.ts < dec.decision_ts:
            continue
        if bar.ts > deadline and remaining > 0:
            break
        if remaining > 0 and bar.ts <= deadline:
            if side == "YES":
                take, cost = _bar_fill_yes_buy(bar, yes_limit, remaining)
            else:
                take, cost = _bar_fill_no_buy(bar, our_px, remaining)
            filled += take
            paid += cost
            remaining -= take

        # Scalp watch uses mid = close, independent of our own fill.
        if exit_rule == "scalp" and scalp_hit is None and filled > 0:
            if side == "YES" and bar.yes_close >= model_yes:
                scalp_hit = (bar.ts, bar.yes_close)
            if side == "NO" and bar.yes_close <= model_yes:
                scalp_hit = (bar.ts, 100 - bar.yes_close)

    avg = int(round(paid / filled)) if filled > 0 else None
    entry_fee = fees.fee_cents(filled, avg or our_px) if filled > 0 else 0
    exit_fee = 0
    exit_reason = "unfilled"
    net = 0

    if filled > 0 and avg is not None:
        if exit_rule == "scalp" and scalp_hit is not None:
            exit_px = scalp_hit[1]
            exit_fee = fees.fee_cents(filled, exit_px)
            net = fees.scalp_pnl_cents(side, filled, avg, exit_px, entry_fee, exit_fee)
            exit_reason = "scalp"
        else:
            net = fees.hold_pnl_cents(side, filled, avg, dec.outcome, entry_fee)
            exit_reason = "settlement" if exit_rule == "hold" or scalp_hit is None else "settlement_fallback"

    fill_pct = (filled / intended * 100.0) if intended else 0.0
    deployed = (filled * (avg or 0)) / 100.0
    return {
        "decision_id": dec.decision_id,
        "ticker": dec.ticker,
        "word": dec.word,
        "event_date": dec.event_date,
        "side": side,
        "triggered": True,
        "filled": round(filled, 4),
        "intended": intended,
        "fill_pct": round(fill_pct, 2),
        "fully_filled": filled >= intended * 0.999,
        "avg_fill_cents": avg,
        "limit_cents": our_px,
        "yes_price_cents": yes_limit,
        "kalshi_action": plan["kalshi_action"],
        "entry_fee_cents": entry_fee,
        "exit_fee_cents": exit_fee,
        "exit": exit_rule,
        "exit_reason": exit_reason,
        "net_cents": net,
        "intended_notional": notional,
        "deployed_dollars": round(deployed, 4),
        "cancel_min": cancel_min,
        "outcome": dec.outcome,
        "model_prob": dec.model_prob,
        "market_yes_cents": dec.market_yes_cents,
    }


def summarize(trades: list[dict], variant_id: str, label: str) -> dict:
    trig = [t for t in trades if t.get("triggered")]
    filled = [t for t in trig if t["filled"] > 0]
    n = len(trig)
    n_fill = len(filled)
    avg_fill = sum(t["fill_pct"] for t in trig) / n if n else 0.0
    fully = sum(1 for t in trig if t["fully_filled"]) / n * 100 if n else 0.0
    intended = sum(t["intended_notional"] for t in trig)
    deployed = sum(t["deployed_dollars"] for t in filled)
    net = sum(t["net_cents"] for t in filled) / 100.0
    per_intended = net / intended if intended else 0.0
    per_deployed = net / deployed if deployed else 0.0
    # Addendum compare: per-trade edge scaled by fill completeness.
    fill_adj = per_deployed * (avg_fill / 100.0)
    return {
        "variant": variant_id,
        "label": label,
        "n_triggered": n,
        "n_filled": n_fill,
        "avg_fill_pct": round(avg_fill, 2),
        "fully_filled_pct": round(fully, 2),
        "intended_dollars": round(intended, 2),
        "deployed_dollars": round(deployed, 2),
        "net_dollars": round(net, 4),
        "edge_per_intended_dollar": round(per_intended, 4),
        "edge_per_deployed_dollar": round(per_deployed, 4),
        "fill_adjusted_edge": round(fill_adj, 4),
        "wins": sum(1 for t in filled if t["net_cents"] > 0),
        "losses": sum(1 for t in filled if t["net_cents"] < 0),
    }


def run_four_way(
    decisions: list[Decision],
    bars_by_ticker: dict[str, list[Bar]],
    cancel_min: int = 60,
    threshold: int = 15,
) -> dict:
    """Independent passes. Tape is read-only."""
    summaries = []
    books: dict[str, list[dict]] = {}
    for spec in VARIANTS:
        book = []
        for dec in decisions:
            tape = bars_by_ticker.get(dec.ticker, [])
            book.append(
                simulate_sweep(
                    dec, tape, spec["notional"], spec["exit"], cancel_min, threshold
                )
            )
        books[spec["id"]] = book
        summaries.append(summarize(book, spec["id"], spec["label"]))
    ranked = sorted(summaries, key=lambda s: s["fill_adjusted_edge"], reverse=True)
    return {
        "cancel_min": cancel_min,
        "n_decisions": len(decisions),
        "summaries": summaries,
        "winner": ranked[0]["variant"] if ranked else None,
        "books": books,
    }


def cancel_window_curve(
    decisions: list[Decision],
    bars_by_ticker: dict[str, list[Bar]],
    notional: float,
    exit_rule: str = "hold",
    windows: tuple[int, ...] = CANCEL_WINDOWS,
    threshold: int = 15,
) -> list[dict]:
    """Same batch, extending cancel window. Price-paid is comparable."""
    rows = []
    for w in windows:
        book = [
            simulate_sweep(dec, bars_by_ticker.get(dec.ticker, []), notional, exit_rule, w, threshold)
            for dec in decisions
        ]
        trig = [t for t in book if t["triggered"]]
        filled = [t for t in trig if t["avg_fill_cents"] is not None]
        rows.append({
            "notional": notional,
            "exit": exit_rule,
            "cancel_min": w,
            "avg_fill_pct": round(sum(t["fill_pct"] for t in trig) / len(trig), 2) if trig else 0,
            "fully_filled_pct": round(100 * sum(1 for t in trig if t["fully_filled"]) / len(trig), 2) if trig else 0,
            "avg_price_paid_cents": round(sum(t["avg_fill_cents"] for t in filled) / len(filled), 2) if filled else None,
            "net_dollars": round(sum(t["net_cents"] for t in book) / 100.0, 4),
        })
    return rows


def _stable_rng(seed: str) -> random.Random:
    n = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)
    return random.Random(n)


def build_fixture(nights: int = 16, words_per_night: int = 8) -> tuple[list[Decision], dict[str, list[Bar]]]:
    """
    Deterministic synthetic tape so the board has four real-looking columns
    before historical candles are dropped in data/. Labeled FIXTURE in the UI.
    """
    decisions: list[Decision] = []
    bars_by_ticker: dict[str, list[Bar]] = {}
    start = datetime(2026, 8, 13, 10, 0, 0)
    word_pool = [
        "Iran", "Vance", "Trump", "Hurricane", "NFL", "Israel", "China",
        "Border", "Ukraine", "Tariff", "FEMA", "NASA", "Yemen", "Gaza",
        "Congress", "CPI",
    ]
    day = 0
    built = 0
    while built < nights:
        session = start + timedelta(days=day)
        if session.weekday() >= 5:
            day += 1
            continue
        date_str = session.strftime("%Y-%m-%d")
        open_ts = session
        decision_ts = open_ts + timedelta(minutes=60)
        for w_i in range(words_per_night):
            word = word_pool[(built + w_i) % len(word_pool)]
            ticker = f"KXWNT-{date_str.replace('-', '')}-{word.upper()}"
            rng = _stable_rng(f"{ticker}|fixture-v11")
            model = rng.randint(8, 92)
            # Market at decision: sometimes agrees, sometimes a real gap.
            drift = rng.choice([-28, -22, -18, -8, -3, 0, 4, 12, 20, 26])
            mkt = max(8, min(92, model + drift + rng.randint(-3, 3)))
            # Outcome tilted toward the model so the edge is visible but not fake-perfect.
            p_yes = (0.55 * (model / 100.0) + 0.45 * (mkt / 100.0))
            outcome = "yes" if rng.random() < p_yes else "no"
            dec = Decision(
                decision_id=f"{ticker}|{decision_ts.isoformat()}",
                ticker=ticker,
                word=word,
                event_date=date_str,
                decision_ts=decision_ts,
                model_prob=model,
                market_yes_cents=mkt,
                outcome=outcome,
                cluster=word.lower(),
            )
            decisions.append(dec)
            # Minute tape from open through +8h. Liquidity is thin so $100 partials.
            path = []
            px = mkt + rng.randint(-6, 6)
            px = max(5, min(95, px))
            for minute in range(0, 8 * 60):
                ts = open_ts + timedelta(minutes=minute)
                shock = rng.choice([-2, -1, -1, 0, 0, 0, 1, 1, 2])
                # Mean-revert a bit toward model after the first hour (signal forms).
                if minute >= 60:
                    shock += 1 if px < model else (-1 if px > model else 0)
                px = max(3, min(97, px + shock))
                hi = min(99, px + rng.randint(0, 2))
                lo = max(1, px - rng.randint(0, 2))
                # Thin book: 2–18 contracts/min, fatter near open.
                # Enough tape that $1 almost always fills and $100 still partials.
                base = 36 if minute < 30 else (20 if minute < 90 else 10)
                vol = float(max(2, int(rng.expovariate(1 / max(base, 1)))))
                path.append(Bar(ts, ticker, lo, hi, px, vol))
            bars_by_ticker[ticker] = path
        built += 1
        day += 1
    # Only decisions that actually have a gap survive into the scored batch;
    # keep the raw list so sample size is honest.
    return decisions, bars_by_ticker


def fixed_batch(decisions: list[Decision], threshold: int = 15) -> list[Decision]:
    out = []
    for d in decisions:
        if _side_and_limit(d, threshold) is not None:
            out.append(d)
    return out
