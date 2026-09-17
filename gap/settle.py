"""Mark paper books against official Kalshi results.

Fill model for paper: full intended size at the limit. There is no live
resting order and no shared tape. Scalp books without a minute tape are
marked at settlement and tagged so the weekly file does not pretend a
scalp printed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import fees, pricing, store
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.settle")

FILL_MODEL = "paper_filled_only_v135"


def settle_order(order: dict, outcome: str) -> dict:
    intended = float(order.get("contracts") or 0)
    filled = float(order.get("filled_contracts") or 0)
    if filled > intended:
        filled = intended
    our_px = pricing.entry_price_cents(order)
    fee = pricing.entry_fee_cents(order)
    net = fees.hold_pnl_cents(order.get("side") or "NO", filled, our_px, outcome, fee)
    gross = net + fee
    exit_rule = order.get("exit_rule") or "hold"
    note = FILL_MODEL
    if exit_rule == "scalp":
        note = FILL_MODEL + "+scalp_unobserved_settled_as_hold"
    return {
        "forecast_id": order.get("forecast_id"),
        "order_id": order["id"],
        "settled_at": datetime.now(timezone.utc),
        "outcome": outcome,
        "gross_cents": int(gross),
        "fees_cents": int(fee),
        "net_cents": int(net),
        "fill_model": note,
    }


def settle_run(run: dict, client: KalshiClient | None = None) -> dict:
    client = client or KalshiClient()
    orders = store.orders_for_run(run["id"])
    if not orders:
        return {"ok": True, "settled": 0, "open": 0, "void": 0}
    by_ticker: dict[str, str | None] = {}
    settled = open_n = void_n = 0
    for order in orders:
        ticker = order["market_ticker"]
        if ticker not in by_ticker:
            try:
                mkt = client.get_market(ticker)
                by_ticker[ticker] = market_result(mkt)
            except Exception as exc:
                log.warning("settle quote %s: %s", ticker, exc)
                by_ticker[ticker] = None
        status = str(order.get("status") or "")
        # Keep a real scalp *hit*. A miss never printed Grok — still hold to settlement.
        if order.get("exit_rule") == "scalp" and status == "scalp_hit":
            settled += 1
            continue
        outcome = by_ticker[ticker]
        # ALIGNED TO wnt-nofade-bot: the exchange is the only authority. No
        # hand-maintained table. Unresolved rows stay pending for next sweep.
        if outcome not in ("yes", "no", "void"):
            open_n += 1
            continue
        if outcome == "void":
            void_n += 1
            store.update_order(order["id"], status="void", result="void", realized_pnl_cents=0)
            continue
        row = settle_order(order, outcome)
        store.upsert_settlement(row)
        filled = float(order.get("filled_contracts") or 0)
        store.update_order(
            order["id"],
            status="settled" if filled > 0 else "unfilled",
            result=outcome,
            filled_contracts=filled,
            fees_cents=row["fees_cents"],
            realized_pnl_cents=row["net_cents"],
        )
        settled += 1
    store.log_activity(
        "settled",
        f"run {run['id']} {run.get('event_date')} settled={settled} open={open_n} void={void_n}",
    )
    return {"ok": True, "settled": settled, "open": open_n, "void": void_n}


def sync_gh_fills(date_str: str) -> int:
    """H takes the same names G filled. Size is H's own intended, not a $1 clone of count."""
    orders = store.orders_for_date(date_str)
    g_fill = {
        o.get("word"): float(o.get("filled_contracts") or 0)
        for o in orders
        if o.get("variant_id") == "G"
    }
    n = 0
    for o in orders:
        if o.get("variant_id") != "H":
            continue
        g = g_fill.get(o.get("word"), 0.0)
        intended = float(o.get("contracts") or 0)
        want = intended if g > 0 else 0.0
        cur = float(o.get("filled_contracts") or 0)
        if abs(cur - want) < 1e-9:
            continue
        store.update_order(o["id"], filled_contracts=round(want, 4))
        n += 1
    return n


def apply_official(date_str: str) -> dict:
    run = store.get_run_for_date(date_str)
    if not run:
        return {"ok": False, "reason": "no_run"}
    synced = sync_gh_fills(date_str)
    out = settle_run(run)
    out["synced_h"] = synced
    return out


def settle_range(start_date: str, end_date: str) -> dict:
    client = KalshiClient()
    totals = {"settled": 0, "open": 0, "void": 0, "runs": 0}
    for run in store.runs_between(start_date, end_date):
        out = settle_run(run, client)
        totals["runs"] += 1
        totals["settled"] += out.get("settled", 0)
        totals["open"] += out.get("open", 0)
        totals["void"] += out.get("void", 0)
    return totals
