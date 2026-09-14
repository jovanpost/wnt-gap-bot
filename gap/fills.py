"""Paper fills from Kalshi 1-minute candles. Independent books, full tape each."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import clock, config as C, store
from .kalshi import KalshiClient, _to_cents
from .backtest import Bar, _bar_fill_yes_buy, _bar_fill_no_buy

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


def simulate_order(order: dict, bars: list[Bar], now: datetime | None = None) -> dict[str, Any]:
    """Walk 1-minute tape from placed_at until cancel window. Independent of other books."""
    now = now or datetime.now(timezone.utc)
    intended = float(order.get("contracts") or 0)
    side = order.get("side") or "NO"
    yes_limit = int(order.get("limit_price_cents") or 0)
    our_px = yes_limit if side == "YES" else max(1, 100 - yes_limit)
    start = _as_dt(order.get("placed_at")) or now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    deadline = start.timestamp() + C.CANCEL_AFTER_MIN * 60
    deadline_dt = datetime.fromtimestamp(deadline, tz=timezone.utc)

    remaining = intended
    filled = 0.0
    paid = 0.0
    last_ts = None
    if not bars:
        return {
            "intended_ct": round(intended, 2),
            "filled_ct": None,
            "unfilled_ct": None,
            "fill_pct": None,
            "avg_fill_cents": None,
            "filled_cost_cents": None,
            "fill_status": "no 1-min tape yet",
            "window_closed": False,
            "bars_used": 0,
            "last_bar": None,
        }
    for bar in bars:
        if bar.ts < start:
            continue
        if bar.ts.timestamp() > deadline and remaining > 0:
            break
        if remaining <= 0:
            break
        if side == "YES":
            take, cost = _bar_fill_yes_buy(bar, yes_limit, remaining)
        else:
            take, cost = _bar_fill_no_buy(bar, our_px, remaining)
        filled += take
        paid += cost
        remaining -= take
        last_ts = bar.ts

    fill_pct = (filled / intended * 100.0) if intended else 0.0
    window_closed = now >= deadline_dt
    if filled <= 0 and window_closed:
        status = "unfilled"
    elif filled + 1e-6 >= intended:
        status = "filled"
    elif window_closed:
        status = "partial · rest cancelled"
    else:
        status = "partial · working"

    avg = int(round(paid / filled)) if filled else None
    cost_cents = int(round(filled * (avg if avg is not None else our_px))) if filled else 0
    return {
        "intended_ct": round(intended, 2),
        "filled_ct": round(filled, 2),
        "unfilled_ct": round(max(0.0, intended - filled), 2),
        "fill_pct": round(fill_pct, 1),
        "avg_fill_cents": avg,
        "filled_cost_cents": cost_cents,
        "fill_status": status,
        "window_closed": window_closed,
        "bars_used": len(bars),
        "last_bar": last_ts.isoformat() if last_ts else None,
    }


def apply_to_orders(orders: list[dict], event_ticker: str | None = None) -> dict[str, list[Bar]]:
    """Recompute fills from candles and persist filled_contracts."""
    if not orders:
        return {}
    tickers = sorted({o["market_ticker"] for o in orders if o.get("market_ticker")})
    starts = [_as_dt(o.get("placed_at")) for o in orders]
    starts = [s for s in starts if s]
    start = min(starts) if starts else datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    bars_by = load_bars(tickers, start, now, event_ticker=event_ticker)
    for o in orders:
        sim = simulate_order(o, bars_by.get(o.get("market_ticker") or "", []))
        o["_fill"] = sim
        try:
            if sim.get("filled_ct") is not None:
                store.update_order(o["id"], filled_contracts=sim["filled_ct"])
        except Exception:
            log.exception("persist fill %s", o.get("id"))
    return bars_by
