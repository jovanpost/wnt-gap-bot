"""Paper fills the nofade way: orderbook crossing size + public trades.

Not mid. Not candle range. A SELL YES @ L only eats:
  1) YES bids sitting at >= L on the live book (would lift us right now)
  2) public prints at YES >= L since the ticket was booked
Each book sees the full tape. Unfilled size dies at cancel+60m.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import config as C, store
from .kalshi import KalshiClient, _to_cents, _to_count, book_metrics

log = logging.getLogger("gap.fills")


def _as_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_yes_px(tr: dict) -> int | None:
    for k in ("yes_price_dollars", "yes_price", "yes_price_cents", "price"):
        if tr.get(k) is not None:
            return _to_cents(tr[k])
    return None


def _trade_count(tr: dict) -> float:
    for k in ("count_fp", "count", "contracts"):
        if tr.get(k) is not None:
            return _to_count(tr[k])
    return 0.0


def _trade_ts(tr: dict) -> datetime | None:
    return _as_dt(tr.get("created_time") or tr.get("created_ts") or tr.get("ts"))


def tape_contracts(trades: list[dict], side: str, yes_limit: int, start: datetime) -> float:
    total = 0.0
    for tr in trades:
        ts = _trade_ts(tr)
        if ts and ts < start:
            continue
        px = _trade_yes_px(tr)
        if px is None:
            continue
        if side == "YES" and px > yes_limit:
            continue
        if side != "YES" and px < yes_limit:
            continue
        total += _trade_count(tr)
    return total


def book_available(book: dict, side: str, yes_limit: int) -> tuple[float, int | None]:
    metrics = book_metrics(book, yes_limit)
    if side == "YES":
        avail = float(metrics.get("yes_size_that_would_fill_buy") or 0)
    else:
        avail = float(metrics.get("yes_size_that_would_fill_sell") or 0)
    best_yes = metrics.get("best_yes_bid")
    return avail, (int(best_yes) if best_yes is not None else None)


def simulate_order(
    order: dict,
    book: dict | None,
    trades: list[dict],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    intended = float(order.get("contracts") or 0)
    side = order.get("side") or "NO"
    yes_limit = int(order.get("limit_price_cents") or 0)
    our_px = yes_limit if side == "YES" else max(1, 100 - yes_limit)
    start = _as_dt(order.get("placed_at")) or now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    deadline = datetime.fromtimestamp(
        start.timestamp() + C.CANCEL_AFTER_MIN * 60, tz=timezone.utc
    )
    window_closed = now >= deadline

    printed = tape_contracts(trades, side, yes_limit, start)
    book_sz, best_yes = book_available(book or {}, side, yes_limit)
    available = max(printed, book_sz)
    if window_closed:
        available = printed

    filled = min(intended, available)
    remaining = max(0.0, intended - filled)
    fill_pct = (filled / intended * 100.0) if intended else 0.0

    if filled <= 0 and window_closed:
        status = "unfilled"
    elif filled + 1e-6 >= intended:
        status = "filled"
    elif window_closed:
        status = "partial · rest cancelled"
    else:
        status = "partial · working"

    if side != "YES" and best_yes is not None and best_yes >= yes_limit:
        avg_yes = min(max(yes_limit, best_yes), 99)
        avg_our = max(1, 100 - avg_yes)
    else:
        avg_our = our_px

    return {
        "intended_ct": round(intended, 2),
        "filled_ct": round(filled, 2),
        "unfilled_ct": round(remaining, 2),
        "fill_pct": round(fill_pct, 1),
        "avg_fill_cents": avg_our,
        "filled_cost_cents": int(round(filled * avg_our)),
        "fill_status": status,
        "window_closed": window_closed,
        "book_cross_ct": round(book_sz, 2),
        "tape_ct": round(printed, 2),
        "best_yes_bid": best_yes,
        "bars_used": len(trades),
        "last_bar": None,
    }


def apply_to_orders(orders: list[dict], event_ticker: str | None = None) -> dict:
    if not orders:
        return {}
    client = KalshiClient()
    tickers = sorted({o["market_ticker"] for o in orders if o.get("market_ticker")})
    starts = [_as_dt(o.get("placed_at")) for o in orders]
    starts = [s for s in starts if s]
    min_start = min(starts) if starts else datetime.now(timezone.utc)
    min_ts = int(min_start.timestamp()) - 30

    books: dict[str, dict] = {}
    trades: dict[str, list] = {}
    for ticker in tickers:
        try:
            books[ticker] = client.get_orderbook(ticker, depth=10)
        except Exception as exc:
            log.warning("orderbook %s: %s", ticker, exc)
            books[ticker] = {"yes": [], "no": []}
        try:
            trades[ticker] = client.get_trades(ticker, min_ts=min_ts)
        except Exception as exc:
            log.warning("trades %s: %s", ticker, exc)
            trades[ticker] = []

    for o in orders:
        ticker = o.get("market_ticker") or ""
        sim = simulate_order(o, books.get(ticker), trades.get(ticker) or [])
        o["_fill"] = sim
        try:
            if sim.get("filled_ct") is not None:
                store.update_order(o["id"], filled_contracts=sim["filled_ct"])
        except Exception:
            log.exception("persist fill %s", o.get("id"))
    return {"books": books, "trades": {k: len(v) for k, v in trades.items()}}
