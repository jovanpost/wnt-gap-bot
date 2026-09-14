"""Mark-to-market for the four paper books. Read-only Kalshi quotes."""
from __future__ import annotations

import logging
from typing import Any

from . import config as C, fees, store
from .kalshi import KalshiClient, market_result, market_yes_quotes, market_mid_prob

log = logging.getLogger("gap.board")

OPEN = {"paper_sweep", "paper_booked", "working"}
CLOSED = {"scalp_hit", "scalp_miss", "settled", "void", "cancelled", "canceled"}


def _our_entry(order: dict) -> int:
    yes = int(order.get("limit_price_cents") or 0)
    if (order.get("side") or "NO") == "YES":
        return yes
    return 100 - yes


def _filled(order: dict) -> float:
    status = str(order.get("status") or "")
    if status in ("cancelled", "canceled", "rejected", "void"):
        return float(order.get("filled_contracts") or 0)
    filled = order.get("filled_contracts")
    if filled not in (None, 0, 0.0):
        return float(filled)
    # Paper Phase 1: full at limit. Label that in the UI.
    if status in OPEN or status in CLOSED:
        return float(order.get("contracts") or 0)
    return 0.0


def _status_label(order: dict, result: str | None) -> str:
    status = str(order.get("status") or "")
    if status == "scalp_hit":
        return "closed · scalp hit"
    if status == "scalp_miss":
        return "closed · scalp miss (last mid)"
    if status == "settled":
        return f"settled · {result or order.get('result') or '?'}"
    if status in ("void", "cancelled", "canceled"):
        return status
    if result in ("yes", "no"):
        return f"filled · awaiting settle ({result})"
    return "filled · paper (full at limit)"


def _mark_yes(bid, ask, mid) -> int | None:
    if mid is not None:
        return int(round(float(mid) * 100.0))
    if bid is not None and ask is not None:
        return int(round((int(bid) + int(ask)) / 2.0))
    if bid is not None:
        return int(bid)
    if ask is not None:
        return int(ask)
    return None


def _unrealized_cents(order: dict, mark_yes: int | None) -> int | None:
    if mark_yes is None:
        return None
    filled = _filled(order)
    if filled <= 0:
        return 0
    entry_yes = int(order.get("limit_price_cents") or 0)
    entry_fee = fees.fee_cents(filled, entry_yes)
    if (order.get("side") or "NO") == "YES":
        gross = filled * (mark_yes - entry_yes)
    else:
        gross = filled * (entry_yes - mark_yes)
    return int(round(gross - entry_fee))


def _mark_value_cents(order: dict, mark_yes: int | None) -> int | None:
    if mark_yes is None:
        return None
    filled = _filled(order)
    if (order.get("side") or "NO") == "YES":
        return int(round(filled * mark_yes))
    return int(round(filled * (100 - mark_yes)))


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not tickers:
        return out
    client = KalshiClient()
    for ticker in tickers:
        try:
            mkt = client.get_market(ticker)
        except Exception as exc:
            log.warning("board quote %s: %s", ticker, exc)
            out[ticker] = {"bid": None, "ask": None, "mid": None, "result": None, "ok": False}
            continue
        bid, ask = market_yes_quotes(mkt)
        mid = market_mid_prob(bid, ask)
        out[ticker] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "result": market_result(mkt),
            "status": (mkt.get("status") or ""),
            "ok": True,
        }
    return out


def enrich_orders(orders: list[dict], quotes: dict[str, dict] | None = None) -> list[dict]:
    tickers = sorted({o.get("market_ticker") for o in orders if o.get("market_ticker")})
    quotes = quotes if quotes is not None else fetch_quotes(tickers)
    settlements = store.settlements_for_order_ids(
        [int(o["id"]) for o in orders if o.get("id") is not None]
    )
    rows = []
    for o in orders:
        q = quotes.get(o.get("market_ticker") or "", {})
        mark_yes = _mark_yes(q.get("bid"), q.get("ask"), q.get("mid"))
        filled = _filled(o)
        cost = int(o.get("cost_cents") or 0)
        realized = o.get("realized_pnl_cents")
        sett = settlements.get(int(o["id"])) if o.get("id") is not None else None
        if realized is None and sett:
            realized = sett.get("net_cents")
        closed = str(o.get("status") or "") in CLOSED
        if closed and realized is not None:
            pnl = int(realized)
            mark_val = cost + pnl
        else:
            pnl = _unrealized_cents(o, mark_yes)
            mark_val = _mark_value_cents(o, mark_yes)
        pct = (pnl / cost * 100.0) if (pnl is not None and cost) else None
        action = (
            f"BUY YES @ {int(o['limit_price_cents'])}¢"
            if o.get("side") == "YES"
            else f"SELL YES @ {int(o.get('limit_price_cents') or 0)}¢"
        )
        now = "—"
        if q.get("bid") is not None or q.get("ask") is not None:
            now = f"{q.get('bid') or '—'} / {q.get('ask') or '—'} mid {mark_yes if mark_yes is not None else '—'}"
        rows.append({
            **o,
            "action": action,
            "fill_label": _status_label(o, q.get("result")),
            "filled_ct": round(filled, 2),
            "entry_yes": int(o.get("limit_price_cents") or 0),
            "yes_bid": q.get("bid"),
            "yes_ask": q.get("ask"),
            "yes_mid": mark_yes,
            "now_yes": now,
            "cost_dollars": cost / 100.0,
            "mark_dollars": None if mark_val is None else mark_val / 100.0,
            "pnl_dollars": None if pnl is None else pnl / 100.0,
            "pnl_pct": pct,
            "closed": closed,
            "quote_ok": bool(q.get("ok")),
        })
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for spec in C.VARIANTS:
        sub = [r for r in rows if r.get("variant_id") == spec["id"]]
        n = len(sub)
        filled = sum(1 for r in sub if (r.get("filled_ct") or 0) > 0)
        cost = sum(r.get("cost_dollars") or 0 for r in sub)
        mark = sum(r.get("mark_dollars") or 0 for r in sub)
        pnls = [r.get("pnl_dollars") for r in sub if r.get("pnl_dollars") is not None]
        pnl = sum(pnls) if pnls else 0.0
        pct = (pnl / cost * 100.0) if cost else 0.0
        wins = sum(1 for r in sub if (r.get("pnl_dollars") or 0) > 0)
        losses = sum(1 for r in sub if (r.get("pnl_dollars") or 0) < 0)
        out.append({
            "id": spec["id"],
            "label": spec["label"],
            "exit": spec["exit"],
            "notional": spec["notional"],
            "n": n,
            "filled": filled,
            "cost": cost,
            "mark": mark,
            "pnl": pnl,
            "pct": pct,
            "wins": wins,
            "losses": losses,
        })
    return out


def tonight(date_str: str) -> dict[str, Any]:
    orders = store.orders_for_date(date_str)
    rows = enrich_orders(orders)
    return {
        "date": date_str,
        "rows": rows,
        "books": summarize(rows),
        "n_orders": len(rows),
        "n_words": len({r.get("word") for r in rows}),
    }
