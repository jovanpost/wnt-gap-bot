"""Mark-to-market for the four paper books. Read-only Kalshi quotes."""
from __future__ import annotations

import logging
from typing import Any

from . import config as C, fees, fills, store
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
    sim = order.get("_fill") or {}
    if sim.get("filled_ct") is not None:
        return float(sim["filled_ct"])
    stored = order.get("filled_contracts")
    if stored not in (None,):
        return float(stored or 0)
    return 0.0


def _status_label(order: dict, result: str | None) -> str:
    status = str(order.get("status") or "")
    sim = order.get("_fill") or {}
    if status == "scalp_hit":
        return "closed · scalp hit"
    if status == "scalp_miss":
        return "closed · scalp miss (last mid)"
    if status == "settled":
        return f"settled · {result or order.get('result') or '?'}"
    if status in ("void", "cancelled", "canceled"):
        return status
    fill = sim.get("fill_status")
    pct = sim.get("fill_pct")
    if fill:
        extra = f" · {pct:.0f}%" if pct is not None else ""
        if result in ("yes", "no"):
            return f"{fill}{extra} · settle {result}"
        return f"{fill}{extra}"
    if result in ("yes", "no"):
        return f"working · settle {result}"
    return "working · quoting tape"


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
        sim = o.get("_fill") or {}
        intended = float(sim.get("intended_ct") or o.get("contracts") or 0)
        if filled <= 0:
            cost = 0
        elif sim.get("filled_cost_cents"):
            cost = int(sim["filled_cost_cents"])
        else:
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
            "intended_ct": round(intended, 2),
            "filled_ct": round(filled, 2),
            "unfilled_ct": round(max(0.0, intended - filled), 2),
            "fill_pct": sim.get("fill_pct"),
            "tape_ct": sim.get("tape_ct"),
            "book_cross_ct": sim.get("book_cross_ct"),
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
    run = store.get_run_for_date(date_str)
    try:
        fills.apply_to_orders(orders, event_ticker=(run or {}).get("event_ticker"))
    except Exception:
        log.exception("apply fills")
    try:
        from . import score
        extra_n = sum(1 for o in orders if o.get("variant_id") in ("E", "F", "G", "H"))
        key = f"scored_{date_str}"
        if store.get_state(key) != "v1.4.4" or extra_n == 0:
            score.score_date(date_str, event_ticker=(run or {}).get("event_ticker"))
            orders = store.orders_for_date(date_str)
    except Exception:
        log.exception("score date")
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
        "| book | tickets | filled | cost $ | mark $ | P&L $ | P&L % | W | L |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for b in snap.get("books") or []:
        lines.append(
            f"| {b['label']} | {b['n']} | {b['filled']} | "
            f"{b['cost']:.2f} | {b['mark']:.2f} | {b['pnl']:+.2f} | "
            f"{b['pct']:+.1f}% | {b['wins']} | {b['losses']} |"
        )
    for b in snap.get("books") or []:
        sub = [r for r in snap.get("rows") or [] if r.get("variant_id") == b["id"]]
        lines += ["", f"## {b['label']}", ""]
        lines.append(
            "| word | side | status | want | filled | left | fill % | "
            "tape@L | book@L | entry | now | cost $ | mark $ | P&L $ | P&L % | gap |"
        )
        lines.append("|" + "---|" * 16)
        for r in sub:
            lines.append(
                f"| {r.get('word')} | {r.get('action')} | {r.get('fill_label')} | "
                f"{r.get('intended_ct')} | {r.get('filled_ct')} | {r.get('unfilled_ct')} | "
                f"{r.get('fill_pct')} | {r.get('tape_ct')} | {r.get('book_cross_ct')} | "
                f"{r.get('entry_yes')} | {r.get('now_yes')} | "
                f"{r.get('cost_dollars')} | {r.get('mark_dollars')} | "
                f"{r.get('pnl_dollars')} | {None if r.get('pnl_pct') is None else round(r['pnl_pct'], 1)} | "
                f"{r.get('gap_points')} |"
            )
    lines += ["", "_Fills = prints at limit±1¢ plus book size at limit±1¢. Mid is mark only._", ""]
    return "\n".join(lines)


def history() -> dict:
    """All paper nights, eight books accumulated."""
    orders = store.all_paper_orders()
    # skip fill poll on history; use stored filled + realized
    for o in orders:
        o["_fill"] = {
            "filled_ct": float(o.get("filled_contracts") or 0),
            "intended_ct": float(o.get("contracts") or 0),
            "fill_pct": None,
        }
    quotes = {}
    rows = enrich_orders(orders, quotes=quotes)
    books = summarize(rows)
    nights = sorted({str(r.get("event_date"))[:10] for r in rows if r.get("event_date")})
    return {
        "rows": rows,
        "books": books,
        "nights": nights,
        "n_orders": len(rows),
        "n_words": len({(str(r.get("event_date"))[:10], r.get("word")) for r in rows}),
    }
