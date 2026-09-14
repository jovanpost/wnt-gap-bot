"""Minute-by-minute paper fills, nofade style.

Every minute after the ticket is booked:
  available = contracts at our limit +/- 1c that minute
              (1-min candle volume if the bar traded that price,
               else live book size at that price)
  take      = min(remaining, available * FILL_TAKE_FRACTION)
  filled   += take

Not mid. Not the whole bid stack through 94c. A 94c print does not
fill an 82c rest. Each of A/B/C/D walks the same tape independently.
Unfilled size dies at send+60m.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import config as C, store
from .kalshi import KalshiClient, _to_cents, book_metrics
from .backtest import Bar

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


def _ohlc_cents(blob: dict, key: str) -> tuple[int | None, int | None, int | None, int | None]:
    part = blob.get(key) or {}
    if not isinstance(part, dict):
        return None, None, None, None

    def grab(*names: str) -> int | None:
        for n in names:
            if part.get(n) is not None:
                return _to_cents(part[n])
        return None

    return (
        grab("open_dollars", "open", "open_cents"),
        grab("high_dollars", "high", "high_cents"),
        grab("low_dollars", "low", "low_cents"),
        grab("close_dollars", "close", "close_cents"),
    )


def _volume(blob: dict) -> float:
    for k in ("volume", "volume_fp", "volume_count"):
        if blob.get(k) is not None:
            try:
                return float(blob[k])
            except (TypeError, ValueError):
                continue
    return 0.0


def candle_to_bar(ticker: str, raw: dict) -> Bar | None:
    ts_raw = raw.get("end_period_ts") or raw.get("end_ts") or raw.get("period_end")
    try:
        ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
    except Exception:
        return None
    _o, high, low, close = _ohlc_cents(raw, "price")
    if close is None:
        _o2, high2, low2, close = _ohlc_cents(raw, "yes_bid")
        high = high or high2
        low = low or low2
    if close is None:
        return None
    high = high if high is not None else close
    low = low if low is not None else close
    return Bar(
        ts=ts,
        ticker=ticker,
        yes_high=int(high),
        yes_low=int(low),
        yes_close=int(close),
        volume=_volume(raw),
    )


def load_bars(
    tickers: list[str],
    start: datetime,
    end: datetime,
    event_ticker: str | None = None,
    client: KalshiClient | None = None,
) -> dict[str, list[Bar]]:
    client = client or KalshiClient()
    start_ts = int(start.timestamp()) - 60
    end_ts = int(end.timestamp()) + 60
    grouped: dict[str, list[dict]] = {}
    if event_ticker:
        try:
            grouped = client.get_event_candlesticks(event_ticker, start_ts, end_ts, 1)
        except Exception as exc:
            log.warning("event candles: %s", exc)
            grouped = {}
    out: dict[str, list[Bar]] = {t: [] for t in tickers}
    for ticker in tickers:
        raws = grouped.get(ticker) or []
        if not raws:
            try:
                raws = client.get_market_candlesticks(ticker, start_ts, end_ts, 1)
            except Exception as exc:
                log.warning("candles %s: %s", ticker, exc)
                raws = []
        bars = []
        for raw in raws:
            bar = candle_to_bar(ticker, raw)
            if bar:
                bars.append(bar)
        bars.sort(key=lambda b: b.ts)
        out[ticker] = bars
    return out


def book_at_limit(book: dict, side: str, yes_limit: int) -> tuple[float, int | None]:
    metrics = book_metrics(book or {}, yes_limit)
    best_yes = metrics.get("best_yes_bid")
    yes = (book or {}).get("yes") or []
    no = (book or {}).get("no") or []
    if side == "YES":
        want = {100 - yes_limit - 1, 100 - yes_limit, 100 - yes_limit + 1}
        avail = sum(c for p, c in no if p in want)
    else:
        want = {yes_limit - 1, yes_limit, yes_limit + 1}
        avail = sum(c for p, c in yes if p in want)
    return float(avail), (int(best_yes) if best_yes is not None else None)


def _bar_at_limit(bar: Bar, yes_limit: int) -> bool:
    lo, hi = yes_limit - 1, yes_limit + 1
    return not (bar.yes_high < lo or bar.yes_low > hi)


def simulate_order(
    order: dict,
    bars: list[Bar],
    book: dict | None = None,
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
    frac = float(getattr(C, "FILL_TAKE_FRACTION", 0.50))
    end = min(now, deadline)

    filled = 0.0
    minutes = 0
    last_ts = None
    for bar in bars:
        if bar.ts < start or bar.ts > end:
            continue
        if not _bar_at_limit(bar, yes_limit):
            continue
        minutes += 1
        last_ts = bar.ts
        avail = max(0.0, float(bar.volume))
        take = min(intended - filled, avail * frac)
        if take > 0:
            filled += take
        if filled + 1e-9 >= intended:
            filled = intended
            break

    book_sz, best_yes = book_at_limit(book or {}, side, yes_limit)
    if not window_closed and filled < intended and book_sz > 0:
        take = min(intended - filled, book_sz * frac)
        filled += take

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

    return {
        "intended_ct": round(intended, 2),
        "filled_ct": round(filled, 2),
        "unfilled_ct": round(remaining, 2),
        "fill_pct": round(fill_pct, 1),
        "avg_fill_cents": our_px,
        "filled_cost_cents": int(round(filled * our_px)),
        "fill_status": status,
        "window_closed": window_closed,
        "book_cross_ct": round(book_sz, 2),
        "tape_ct": round(filled, 2),
        "best_yes_bid": best_yes,
        "bars_used": minutes,
        "last_bar": last_ts.isoformat() if last_ts else None,
        "take_frac": frac,
    }


def apply_to_orders(orders: list[dict], event_ticker: str | None = None) -> dict:
    """Rebuild fills from 1-min bars + current book. Idempotent."""
    if not orders:
        return {}
    client = KalshiClient()
    tickers = sorted({o["market_ticker"] for o in orders if o.get("market_ticker")})
    starts = [_as_dt(o.get("placed_at")) for o in orders]
    starts = [s for s in starts if s]
    start = min(starts) if starts else datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    bars_by = load_bars(tickers, start, now, event_ticker=event_ticker, client=client)
    books: dict[str, dict] = {}
    for ticker in tickers:
        try:
            books[ticker] = client.get_orderbook(ticker, depth=10)
        except Exception as exc:
            log.warning("orderbook %s: %s", ticker, exc)
            books[ticker] = {"yes": [], "no": []}

    for o in orders:
        ticker = o.get("market_ticker") or ""
        sim = simulate_order(o, bars_by.get(ticker) or [], books.get(ticker))
        o["_fill"] = sim
        try:
            store.update_order(o["id"], filled_contracts=sim["filled_ct"])
        except Exception:
            log.exception("persist fill %s", o.get("id"))
    return {"bars": {k: len(v) for k, v in bars_by.items()}}
