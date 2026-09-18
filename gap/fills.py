"""Paper fills = nofade dry poll. Not last trade. Not candle volume.

Live: Kalshi get_fills on a resting order. Paper has no order on the matcher.
Each poll: if the current book has YES bids at/through our Sell-YES limit,
take min(remaining, that size) as a slice. Leave leftover resting and poll
again. Do not invent fills from last price or volume.

The book itself comes from no-fade's `depth` table (store.latest_nofade_depth),
not a fresh Kalshi call -- no-fade already snapshots every market in the
event every 60s, so asking Kalshi ourselves on top of that was pure
duplication. If no-fade hasn't snapshotted a ticker yet (e.g. before 11:00
CT), we get an empty book back and simply skip that tick, same as an
exception used to be handled below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import clock, config as C, store
from .kalshi import book_metrics

log = logging.getLogger("gap.fills")

POLL_KEY = "fill_slice_at:{oid}"


def _as_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _show_cancel_utc(event_date) -> datetime | None:
    if not event_date:
        return None
    raw = str(event_date)[:10]
    try:
        y, m, d = [int(x) for x in raw.split("-")]
    except ValueError:
        return None
    hh, mm = (C.SHOW_CANCEL_CT if hasattr(C, "SHOW_CANCEL_CT") else "17:29").split(":")
    from datetime import date, time
    local = datetime.combine(date(y, m, d), time(int(hh), int(mm)), tzinfo=C.CT)
    return local.astimezone(timezone.utc)


def window_open(order: dict, now: datetime | None = None) -> bool:
    now = now or _now()
    vid = str(order.get("variant_id") or "")
    cancel = None
    for spec in C.VARIANTS:
        if spec["id"] == vid:
            cancel = spec.get("cancel")
            break
    if cancel == "show529" or vid in ("G", "H"):
        deadline = _show_cancel_utc(order.get("event_date"))
        if deadline is None:
            return False
        return now <= deadline
    start = _as_dt(order.get("placed_at")) or now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    deadline = start.timestamp() + C.CANCEL_AFTER_MIN * 60
    return now.timestamp() <= deadline


def crossing(book: dict, side: str, yes_limit: int) -> tuple[float, int | None]:
    metrics = book_metrics(book or {}, yes_limit)
    best_yes = metrics.get("best_yes_bid")
    if side == "YES":
        size = float(metrics.get("yes_size_that_would_fill_buy") or 0)
    else:
        size = float(metrics.get("yes_size_that_would_fill_sell") or 0)
    return size, (int(best_yes) if best_yes is not None else None)


def slice_price(side: str, yes_limit: int, best_yes: int | None) -> int:
    """NO cents we paid / YES cents we sold. Gap-through is a better fill."""
    our = yes_limit if side == "YES" else max(1, 100 - yes_limit)
    if side != "YES" and best_yes is not None and best_yes >= yes_limit:
        return max(1, 100 - int(best_yes))
    if side == "YES" and best_yes is not None and best_yes <= yes_limit:
        return int(best_yes)
    return our


def _due(order_id: int, now: datetime) -> bool:
    raw = store.get_state(POLL_KEY.format(oid=order_id))
    last = _as_dt(raw)
    if last is None:
        return True
    gap = (now - last).total_seconds()
    return gap >= float(getattr(C, "FILL_POLL_SECONDS", 5))


def apply_slice(order: dict, book: dict, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    intended = float(order.get("contracts") or 0)
    already = float(order.get("filled_contracts") or 0)
    remaining = max(0.0, intended - already)
    side = order.get("side") or "NO"
    yes_limit = int(order.get("limit_price_cents") or 0)
    size, best_yes = crossing(book, side, yes_limit)
    px = slice_price(side, yes_limit, best_yes)
    took = 0.0
    open_win = window_open(order, now)

    if open_win and remaining > 0 and size > 0 and _due(order["id"], now):
        took = min(remaining, size)
        already = already + took
        remaining = max(0.0, intended - already)
        store.update_order(order["id"], filled_contracts=round(already, 4))
        store.set_state(POLL_KEY.format(oid=order["id"]), now.isoformat())
        order["filled_contracts"] = already
        store.log_activity(
            "paper_fill",
            f"{order.get('variant_id')} {order.get('word')} "
            f"+{took:g} -> {already:g}/{intended:g} NO@ {px}c "
            f"book_cross={size:g}",
        )

    fill_pct = (already / intended * 100.0) if intended else 0.0
    if already + 1e-6 >= intended:
        status = "filled"
    elif already > 0 and not open_win:
        status = "partial · rest cancelled"
    elif already > 0:
        status = "partial · resting"
    elif not open_win:
        status = "unfilled"
    else:
        status = "resting · watching book"

    return {
        "intended_ct": round(intended, 2),
        "filled_ct": round(already, 2),
        "unfilled_ct": round(remaining, 2),
        "fill_pct": round(fill_pct, 1),
        "avg_fill_cents": px,
        "filled_cost_cents": int(round(already * px)),
        "fill_status": status,
        "window_closed": not open_win,
        "book_cross_ct": round(size, 2),
        "tape_ct": round(took, 2),
        "best_yes_bid": best_yes,
        "bars_used": 0,
        "last_bar": None,
        "take_frac": 1.0,
    }


def apply_to_orders(orders: list[dict], event_ticker: str | None = None) -> dict:
    if not orders:
        return {}
    if store.get_state("fill_model") != "dry_book_v134":
        for o in orders:
            try:
                store.update_order(o["id"], filled_contracts=0)
            except Exception:
                pass
            o["filled_contracts"] = 0
        store.set_state("fill_model", "dry_book_v134")
        store.log_activity("fills", "reset paper fills to dry-book model")
    tickers = sorted({o["market_ticker"] for o in orders if o.get("market_ticker")})
    event_date = str((orders[0] or {}).get("event_date") or "")[:10]
    books: dict[str, dict] = {}
    for ticker in tickers:
        try:
            snap = store.latest_nofade_depth(ticker, event_date)
        except Exception as exc:
            log.warning("depth lookup %s: %s", ticker, exc)
            snap = None
        if snap and (snap.get("yes_book") or snap.get("no_book")):
            books[ticker] = {
                "yes": snap.get("yes_book") or [],
                "no": snap.get("no_book") or [],
            }
        else:
            # No snapshot yet (e.g. before no-fade's 11:00 CT depth window
            # opens for the day). Empty book -- apply_slice below already
            # treats an empty book as "nothing crossed, keep resting".
            books[ticker] = {"yes": [], "no": []}
    for o in orders:
        ticker = o.get("market_ticker") or ""
        try:
            o["_fill"] = apply_slice(o, books.get(ticker) or {})
        except Exception:
            log.exception("slice %s", o.get("id"))
            o["_fill"] = {}
    return {"books": {k: len((v or {}).get("yes") or []) for k, v in books.items()}}
