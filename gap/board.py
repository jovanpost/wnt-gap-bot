"""Board = pending orders + officially-settled P&L. NO live marks.

v1.5.0: every live-quote-dependent feature has been removed. Four separate
bugs (board's own original mark-from-mid, tape.py's false scalp hits, and
two bugs in score.py) all traced back to the same root cause: code reading
a Kalshi quote and treating it as trustworthy when it was either stale,
post-close, or simply not yet a real answer. The fix that actually holds is
structural, not another guard clause: this file no longer computes an
unrealized mark at all.

An order is either:
  - PENDING  -- no official result yet. No P&L is shown. Not "$0", not an
    estimate -- genuinely blank, because there is nothing true to show.
  - SETTLED  -- Kalshi has published yes/no/void. P&L is the real binary
    payout via pricing.hold_pnl_cents, the same function for every book.

There is no third state. Scalp (C/D) is gone; it was the only feature that
needed a live price mid-flight, and removing it is what makes "no live
marks" possible at all.
"""
from __future__ import annotations

import logging
from typing import Any

from . import config as C, fills, pricing, store
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.board")

OPEN = {"paper_sweep", "paper_booked", "working"}
CLOSED = {"settled", "void", "cancelled", "canceled"}


def _our_entry(order: dict) -> int:
    return pricing.entry_price_cents(order)


def _filled(order: dict) -> float:
    return pricing.filled_contracts(order)


def _status_label(order: dict, result: str | None) -> str:
    status = str(order.get("status") or "")
    sim = order.get("_fill") or {}
    if status == "settled":
        return f"settled · {result or order.get('result') or '?'}"
    if status in ("void", "cancelled", "canceled"):
        return status
    fill = sim.get("fill_status")
    pct = sim.get("fill_pct")
    if fill:
        extra = f" · {pct:.0f}%" if pct is not None else ""
        return f"{fill}{extra} · pending"
    return "pending"


def fetch_results(tickers: list[str]) -> dict[str, dict]:
    """The ONLY thing this file asks Kalshi for: has this market settled.
    No bid/ask/mid -- there is no display or calculation left that uses a
    live price, so there is nothing to gain from fetching one and a proven
    history of bugs from doing so."""
    out: dict[str, dict] = {}
    if not tickers:
        return out
    client = KalshiClient()
    for ticker in tickers:
        try:
            mkt = client.get_market(ticker)
        except Exception as exc:
            log.warning("board result %s: %s", ticker, exc)
            out[ticker] = {"result": None, "ok": False}
            continue
        out[ticker] = {"result": market_result(mkt), "ok": True}
    return out


def enrich_orders(orders: list[dict], results: dict[str, dict] | None = None) -> list[dict]:
    tickers = sorted({o.get("market_ticker") for o in orders if o.get("market_ticker")})
    results = results if results is not None else fetch_results(tickers)
    settlements = store.settlements_for_order_ids(
        [int(o["id"]) for o in orders if o.get("id") is not None]
    )
    rows = []
    for o in orders:
        q = results.get(o.get("market_ticker") or "", {})
        # Result comes from what settlement already wrote, or the exchange.
        # Never a live quote, never a local table.
        result = o.get("result") or q.get("result")

        filled = _filled(o)
        sim = o.get("_fill") or {}
        intended = float(sim.get("intended_ct") or o.get("contracts") or 0)
        cost = pricing.cost_cents(o) if filled > 0 else 0

        realized = o.get("realized_pnl_cents")
        sett = settlements.get(int(o["id"])) if o.get("id") is not None else None
        if realized is None and sett:
            realized = sett.get("net_cents")

        status = str(o.get("status") or "")
        closed = status in CLOSED

        # Only two outcomes: a frozen settled number, or nothing at all.
        if realized is not None and (closed or result in ("yes", "no")):
            pnl = int(realized)
            mark_val = cost + pnl
        elif filled > 0 and result in ("yes", "no"):
            pnl = pricing.hold_pnl_cents(o, result)
            mark_val = cost + pnl
        else:
            pnl = None
            mark_val = None

        pct = (pnl / cost * 100.0) if (pnl is not None and cost) else None
        action = (
            f"BUY YES @ {int(o['limit_price_cents'])}¢"
            if o.get("side") == "YES"
            else f"SELL YES @ {int(o.get('limit_price_cents') or 0)}¢"
        )
        rows.append({
            **o,
            "action": action,
            "fill_label": _status_label(o, result),
            "intended_ct": round(intended, 2),
            "filled_ct": round(filled, 2),
            "unfilled_ct": round(max(0.0, intended - filled), 2),
            "fill_pct": sim.get("fill_pct"),
            "tape_ct": sim.get("tape_ct"),
            "book_cross_ct": sim.get("book_cross_ct"),
            "entry_yes": int(o.get("limit_price_cents") or 0),
            "cost_dollars": cost / 100.0,
            "mark_dollars": None if mark_val is None else mark_val / 100.0,
            "pnl_dollars": None if pnl is None else pnl / 100.0,
            "pnl_pct": pct,
            "closed": closed,
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
        settled_n = sum(1 for r in sub if r.get("pnl_dollars") is not None)
        out.append({
            "id": spec["id"], "label": spec["label"], "exit": spec["exit"],
            "notional": spec["notional"], "n": n, "filled": filled,
            "cost": cost, "mark": mark, "pnl": pnl, "pct": pct,
            "wins": wins, "losses": losses, "settled_n": settled_n,
        })
    return out


def tonight(date_str: str) -> dict[str, Any]:
    orders = store.orders_for_date(date_str)
    run = store.get_run_for_date(date_str)
    try:
        fills.apply_to_orders(orders, event_ticker=(run or {}).get("event_ticker"))
    except Exception:
        log.exception("apply fills")
    try:
        from . import settle
        settle.apply_official(date_str)
        orders = store.orders_for_date(date_str)
    except Exception:
        log.exception("apply official")
    rows = enrich_orders(orders)
    return {
        "date": date_str,
        "rows": rows,
        "books": summarize(rows),
        "n_orders": len(rows),
        "n_words": len({r.get("word") for r in rows}),
    }


def as_markdown(snap: dict) -> str:
    lines = [
        f"# WNT Gap Bot {C.VERSION} · {snap.get('date')}",
        "",
        f"{snap.get('n_words')} words · {snap.get('n_orders')} tickets",
        "",
        "## Books",
        "",
        "| book | tickets | filled | settled | cost $ | P&L $ | P&L % | W | L |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for b in snap.get("books") or []:
        lines.append(
            f"| {b['label']} | {b['n']} | {b['filled']} | {b.get('settled_n', 0)} | "
            f"{b['cost']:.2f} | {b['pnl']:+.2f} | "
            f"{b['pct']:+.1f}% | {b['wins']} | {b['losses']} |"
        )
    for b in snap.get("books") or []:
        sub = [r for r in snap.get("rows") or [] if r.get("variant_id") == b["id"]]
        lines += ["", f"## {b['label']}", ""]
        lines.append(
            "| word | side | status | want | filled | left | entry | "
            "cost $ | P&L $ | P&L % | gap |"
        )
        lines.append("|" + "---|" * 11)
        for r in sub:
            pnl_str = "pending" if r.get("pnl_dollars") is None else f"{r.get('pnl_dollars'):+.2f}"
            pct_str = "—" if r.get("pnl_pct") is None else f"{round(r['pnl_pct'], 1)}"
            lines.append(
                f"| {r.get('word')} | {r.get('action')} | {r.get('fill_label')} | "
                f"{r.get('intended_ct')} | {r.get('filled_ct')} | {r.get('unfilled_ct')} | "
                f"{r.get('entry_yes')} | "
                f"{r.get('cost_dollars')} | {pnl_str} | {pct_str} | "
                f"{r.get('gap_points')} |"
            )
    lines += ["", "_P&L shows only once Kalshi has published an official result. No live marks._", ""]
    return "\n".join(lines)


def history() -> dict:
    """All paper nights, six books accumulated."""
    orders = store.all_paper_orders()
    for o in orders:
        o["_fill"] = {
            "filled_ct": float(o.get("filled_contracts") or 0),
            "intended_ct": float(o.get("contracts") or 0),
            "fill_pct": None,
        }
    rows = enrich_orders(orders, results={})
    books = summarize(rows)
    nights = sorted({str(r.get("event_date"))[:10] for r in rows if r.get("event_date")})
    return {
        "rows": rows,
        "books": books,
        "nights": nights,
        "n_orders": len(rows),
        "n_words": len({(str(r.get("event_date"))[:10], r.get("word")) for r in rows}),
    }
