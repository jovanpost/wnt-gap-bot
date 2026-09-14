"""Mark paper books against official Kalshi results.

Fill model for paper: full intended size at the limit. There is no live
resting order and no shared tape. Scalp books without a minute tape are
marked at settlement and tagged so the weekly file does not pretend a
scalp printed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import fees, store
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.settle")

FILL_MODEL = "paper_full_at_limit_SIZE_UNTESTED"


def _our_px(order: dict) -> int:
    yes_limit = int(order["limit_price_cents"])
    if order["side"] == "YES":
        return yes_limit
    return 100 - yes_limit


def settle_order(order: dict, outcome: str) -> dict:
    contracts = float(order.get("contracts") or 0)
    our_px = _our_px(order)
    fee = fees.fee_cents(contracts, our_px)
    net = fees.hold_pnl_cents(order["side"], contracts, our_px, outcome, fee)
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
        outcome = by_ticker[ticker]
        if outcome is None:
            open_n += 1
            continue
        if outcome == "void":
            void_n += 1
            store.update_order(order["id"], status="void", result="void", realized_pnl_cents=0)
            continue
        row = settle_order(order, outcome)
        store.upsert_settlement(row)
        store.update_order(
            order["id"],
            status="settled",
            result=outcome,
            filled_contracts=order.get("contracts") or 0,
            fees_cents=row["fees_cents"],
            realized_pnl_cents=row["net_cents"],
        )
        settled += 1
    store.log_activity(
        "settled",
        f"run {run['id']} {run.get('event_date')} settled={settled} open={open_n} void={void_n}",
    )
    return {"ok": True, "settled": settled, "open": open_n, "void": void_n}


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
