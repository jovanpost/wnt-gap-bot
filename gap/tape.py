"""60s quote tape for C/D scalp marks.

Entry is still assumed full at the limit (SIZE_UNTESTED).
Exit is observed: YES scalp hits when the YES bid reaches Grok.
NO scalp (short YES) hits when the YES ask falls to Grok.
If the model never prints, flatten at last mid and tag scalp_miss_last_mid.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import clock, config as C, fees, notify, store
from .kalshi import KalshiClient, market_result, market_yes_quotes, market_mid_prob

log = logging.getLogger("gap.tape")

SCALP_HIT = "paper_60s_tape_scalp_hit_SIZE_UNTESTED"
SCALP_MISS = "paper_60s_tape_scalp_miss_last_mid_SIZE_UNTESTED"


def _model_for(order: dict, forecasts: list[dict]) -> int | None:
    fid = order.get("forecast_id")
    ticker = order.get("market_ticker")
    for f in forecasts:
        if fid and f.get("id") == fid:
            return int(f["probability"])
        if f.get("market_ticker") == ticker:
            return int(f["probability"])
    return None


def _hit(side: str, model: int, bid, ask) -> tuple[bool, int | None]:
    if side == "YES":
        if bid is not None and int(bid) >= model:
            return True, int(bid)
        return False, None
    if ask is not None and int(ask) <= model:
        return True, int(ask)
    return False, None


def _scalp_net(order: dict, exit_yes: int) -> tuple[int, int]:
    filled = float(order.get("contracts") or 0)
    entry_yes = int(order["limit_price_cents"])
    entry_fee = fees.fee_cents(filled, entry_yes)
    exit_fee = fees.fee_cents(filled, exit_yes)
    if order["side"] == "YES":
        gross = filled * (exit_yes - entry_yes)
    else:
        gross = filled * (entry_yes - exit_yes)
    net = int(round(gross - entry_fee - exit_fee))
    return net, entry_fee + exit_fee


def _close(order: dict, exit_yes: int, tag: str, outcome: str) -> None:
    net, fee = _scalp_net(order, exit_yes)
    store.upsert_settlement({
        "forecast_id": order.get("forecast_id"),
        "order_id": order["id"],
        "settled_at": datetime.now(timezone.utc),
        "outcome": outcome,
        "gross_cents": net + fee,
        "fees_cents": fee,
        "net_cents": net,
        "fill_model": tag,
    })
    store.update_order(
        order["id"],
        status="scalp_hit" if "hit" in tag else "scalp_miss",
        result=outcome,
        filled_contracts=order.get("contracts") or 0,
        fees_cents=fee,
        realized_pnl_cents=net,
    )
    store.log_activity(
        "scalp",
        f"{order.get('variant_id')} {order.get('word')} {tag} exit={exit_yes} net={net}",
    )


def track_tape(client: KalshiClient | None = None) -> dict:
    """Snapshot every open C/D book. Call from the 60s poll even after 16:30."""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run:
        return {"ok": True, "reason": "no_run"}
    orders = [
        o for o in store.orders_for_run(run["id"])
        if (o.get("exit_rule") == "scalp"
            and o.get("status") in ("paper_sweep", "paper_booked"))
    ]
    if not orders:
        return {"ok": True, "reason": "no_open_scalp"}

    client = client or KalshiClient()
    forecasts = store.forecasts_for_run(run["id"])
    late = clock.now_ct().hour >= 23
    hits = misses = snaps = 0

    by_ticker: dict[str, dict] = {}
    for o in orders:
        ticker = o["market_ticker"]
        if ticker not in by_ticker:
            try:
                mkt = client.get_market(ticker)
            except Exception as exc:
                log.warning("tape %s: %s", ticker, exc)
                continue
            bid, ask = market_yes_quotes(mkt)
            mid = market_mid_prob(bid, ask)
            store.insert_quote(run["id"], o.get("forecast_id"), ticker, bid, ask, mid)
            snaps += 1
            by_ticker[ticker] = {
                "bid": bid, "ask": ask, "mid": mid,
                "result": market_result(mkt),
            }
        q = by_ticker.get(ticker)
        if not q:
            continue
        model = _model_for(o, forecasts)
        if model is None:
            continue
        hit, exit_px = _hit(o["side"], model, q["bid"], q["ask"])
        if hit and exit_px is not None:
            _close(o, exit_px, SCALP_HIT, f"hit_model_{model}")
            hits += 1
            continue
        if late or q["result"] in ("yes", "no", "void"):
            if q["mid"] is not None:
                last = int(round(float(q["mid"]) * 100.0))
            else:
                last = int(o["limit_price_cents"])
            tag = SCALP_MISS
            if q["result"] == "void":
                tag = SCALP_MISS + "+void"
            _close(o, last, tag, q["result"] or "miss")
            misses += 1

    if hits:
        notify.send(f"scalp tape: {hits} hit / {misses} miss / {snaps} quotes")
    return {"ok": True, "hits": hits, "misses": misses, "snaps": snaps}
