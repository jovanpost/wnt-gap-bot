"""Score hold vs scalp on *filled* size only.

Never promote leftover to a full clip because Kalshi printed 1¢.
Scalp covers the first minute the YES ask trades at/under Grok.
Hold rides official yes/no on the same filled size.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import config as C, fees, store, strategy
from .kalshi import KalshiClient, market_result

log = logging.getLogger("gap.score")

# Honest dry-book snapshot from the 2026-09-15 13:54 CT board,
# before settle.py wrote intended size into filled_contracts.
FREEZE_2026_09_15 = {
    "Anthropic / Claude": {"1": 0.0, "100": 0.0},
    "Emmy / Emmys": {"1": 2.63, "100": 263.16},
    "Fed / Federal Reserve / Interest Rate": {"1": 1.43, "100": 22.0},
    "Hoax": {"1": 1.39, "100": 138.89},
    "Diesel": {"1": 1.56, "100": 42.5},
    "Inflation": {"1": 1.72, "100": 10.0},
    "Space": {"1": 1.47, "100": 147.06},
    "OpenAI": {"1": 0.0, "100": 0.0},
    "Saudi": {"1": 2.38, "100": 238.1},
    "Supreme Court": {"1": 4.17, "100": 416.67},
    "Kennedy": {"1": 0.0, "100": 0.0},
    "AI / Artificial Intelligence": {"1": 2.78, "100": 277.78},
}

GROK_2026_09_15 = {
    "Kash / Patel": 61,
    "Anthropic / Claude": 18,
    "Emmy / Emmys": 41,
    "Yemen / Yemeni / Houthi": 39,
    "Fed / Federal Reserve / Interest Rate": 23,
    "Hoax": 21,
    "Oil / Gas / Gasoline": 78,
    "Trump (5+ times)": 81,
    "Iran (3+ times)": 71,
    "Diesel": 27,
    "Inflation": 16,
    "Space": 17,
    "OpenAI": 14,
    "Saudi": 34,
    "Supreme Court": 64,
    "Kennedy": 33,
    "AI / Artificial Intelligence": 44,
}


def _as_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _cents(raw) -> int | None:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return int(round(v * 100.0 if v <= 1.5 else v))


def freeze_filled(order: dict, date_str: str) -> float | None:
    if date_str != "2026-09-15":
        return None
    word = order.get("word") or ""
    bucket = FREEZE_2026_09_15.get(word)
    if bucket is None:
        return None
    notional = float(order.get("notional_dollars") or 0)
    key = "100" if notional >= 50 else "1"
    return float(bucket[key])


def model_yes(order: dict, forecasts: list[dict]) -> int | None:
    fid = order.get("forecast_id")
    ticker = order.get("market_ticker")
    for f in forecasts:
        if fid and f.get("id") == fid:
            try:
                return int(f["probability"])
            except Exception:
                return None
        if ticker and f.get("market_ticker") == ticker:
            try:
                return int(f["probability"])
            except Exception:
                return None
    entry = int(order.get("limit_price_cents") or 0)
    gap = order.get("gap_points")
    if gap is None:
        return None
    return int(round(entry + C.LIMIT_OFFSET_CENTS + float(gap)))


def load_ask_tape(event_ticker: str, start: datetime, end: datetime) -> dict[str, list[tuple[datetime, int, int]]]:
    """ticker -> [(ts, ask_low, ask_close), ...]"""
    client = KalshiClient()
    start_ts = int(start.timestamp()) - 60
    end_ts = int(end.timestamp()) + 60
    try:
        data = client.get_event_candlesticks(event_ticker, start_ts, end_ts, 1)
    except Exception as exc:
        log.warning("score candles: %s", exc)
        return {}
    out: dict[str, list[tuple[datetime, int, int]]] = {}
    # client may return dict ticker->list or raw
    if isinstance(data, dict):
        items = data.items()
    else:
        items = []
    for ticker, bars in items:
        rows = []
        for raw in bars or []:
            ts_raw = raw.get("end_period_ts") or raw.get("end_ts")
            try:
                ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            except Exception:
                continue
            ask = raw.get("yes_ask") or {}
            lo = _cents(ask.get("low_dollars") or ask.get("low"))
            cl = _cents(ask.get("close_dollars") or ask.get("close"))
            if lo is None and cl is None:
                continue
            rows.append((ts, lo if lo is not None else cl, cl if cl is not None else lo))
        rows.sort(key=lambda r: r[0])
        out[str(ticker)] = rows
    return out


def scalp_exit(order: dict, model: int, tape: list[tuple[datetime, int, int]]) -> tuple[int, str, datetime | None]:
    start = _as_dt(order.get("placed_at"))
    for ts, lo, cl in tape:
        if start and ts < start:
            continue
        if lo is not None and lo <= model:
            return model, "scalp_hit", ts
    last = tape[-1][2] if tape else None
    return (last if last is not None else model), "scalp_miss", tape[-1][0] if tape else None


def hold_pnl(order: dict, filled: float, outcome: str) -> int:
    yes_limit = int(order.get("limit_price_cents") or 0)
    our = yes_limit if order.get("side") == "YES" else max(1, 100 - yes_limit)
    fee = fees.fee_cents(filled, our)
    return fees.hold_pnl_cents(order.get("side") or "NO", filled, our, outcome, fee)


def scalp_pnl(order: dict, filled: float, exit_yes: int) -> int:
    entry_yes = int(order.get("limit_price_cents") or 0)
    entry_fee = fees.fee_cents(filled, entry_yes)
    exit_fee = fees.fee_cents(filled, exit_yes)
    if order.get("side") == "YES":
        gross = filled * (exit_yes - entry_yes)
    else:
        gross = filled * (entry_yes - exit_yes)
    return int(round(gross - entry_fee - exit_fee))


def score_date(date_str: str, event_ticker: str | None = None) -> dict[str, Any]:
    run = store.get_run_for_date(date_str)
    orders = store.orders_for_date(date_str)
    if not orders:
        return {"ok": True, "n": 0}
    forecasts = store.forecasts_for_run(run["id"]) if run else []
    event_ticker = event_ticker or (run or {}).get("event_ticker")
    starts = [_as_dt(o.get("placed_at")) for o in orders]
    starts = [s for s in starts if s]
    start = min(starts) if starts else datetime.now(timezone.utc)
    end = datetime.now(timezone.utc)
    tape_by = load_ask_tape(event_ticker, start, end) if event_ticker else {}

    client = KalshiClient()
    outcomes: dict[str, str | None] = {}
    n_hold = n_scalp = n_zero = 0
    for o in orders:
        ticker = o.get("market_ticker") or ""
        if ticker not in outcomes:
            try:
                outcomes[ticker] = market_result(client.get_market(ticker))
            except Exception:
                outcomes[ticker] = None
        intended = float(o.get("contracts") or 0)
        frozen = freeze_filled(o, date_str)
        if frozen is not None:
            filled = min(intended, frozen)
        else:
            filled = min(intended, float(o.get("filled_contracts") or 0))
            # if settle already wrote full intended, refuse it unless freeze exists
            if filled + 1e-9 >= intended and date_str == "2026-09-15":
                filled = 0.0
        outcome = outcomes.get(ticker)
        model = model_yes(o, forecasts)
        exit_rule = o.get("exit_rule") or "hold"

        if filled <= 0:
            store.update_order(
                o["id"],
                filled_contracts=0,
                status="unfilled",
                realized_pnl_cents=0,
                result=outcome,
            )
            n_zero += 1
            continue

        store.update_order(o["id"], filled_contracts=round(filled, 4))
        o["filled_contracts"] = filled

        if exit_rule == "scalp" and model is not None:
            exit_yes, tag, _ts = scalp_exit(o, model, tape_by.get(ticker) or [])
            net = scalp_pnl(o, filled, exit_yes)
            store.update_order(
                o["id"],
                status=tag,
                result=f"{tag}_{model}_exit{exit_yes}",
                realized_pnl_cents=net,
                fees_cents=fees.fee_cents(filled, int(o["limit_price_cents"]))
                + fees.fee_cents(filled, exit_yes),
            )
            store.upsert_settlement({
                "forecast_id": o.get("forecast_id"),
                "order_id": o["id"],
                "settled_at": datetime.now(timezone.utc),
                "outcome": tag,
                "gross_cents": net,
                "fees_cents": 0,
                "net_cents": net,
                "fill_model": f"score_v135_{tag}_filled={filled:g}_exit={exit_yes}",
            })
            n_scalp += 1
            continue

        if outcome in ("yes", "no"):
            net = hold_pnl(o, filled, outcome)
            store.update_order(
                o["id"],
                status="settled",
                result=outcome,
                realized_pnl_cents=net,
            )
            store.upsert_settlement({
                "forecast_id": o.get("forecast_id"),
                "order_id": o["id"],
                "settled_at": datetime.now(timezone.utc),
                "outcome": outcome,
                "gross_cents": net,
                "fees_cents": 0,
                "net_cents": net,
                "fill_model": f"score_v135_hold_filled={filled:g}",
            })
            n_hold += 1
    extra = ensure_eh_books(date_str, run, forecasts, tape_by, outcomes)
    store.set_state(f"scored_{date_str}", "v1.4.2")
    store.log_activity("score", f"{date_str} hold={n_hold} scalp={n_scalp} zero={n_zero} extra={extra}")
    return {"ok": True, "hold": n_hold, "scalp": n_scalp, "zero": n_zero, "extra": extra}


def _through_before_529(side: str, yes_limit: int, tape: list) -> bool:
    """tape rows (ts, ask_low, ask_close) — need bid too. Use ask_low for YES buy,
    and treat ask_close>=limit as sell-YES through proxy if we only have asks.
    """
    for ts, lo, cl in tape:
        if side == "YES":
            if lo is not None and lo <= yes_limit:
                return True
        else:
            # sell YES: need bid >= limit. Without bid, a high ask is not a fill.
            # Conservative: never invent a NO fill from ask-only tape.
            continue
    return False


def ensure_eh_books(date_str, run, forecasts, tape_by, outcomes):
    if not run:
        return {"inserted": 0}
    forecasts = list(forecasts or [])
    if not forecasts:
        forecasts = store.forecasts_for_run(run["id"])
    markets = {m.get("word"): m for m in store.markets_for_run(run["id"])}
    existing = store.orders_for_run(run["id"])
    have = {(o.get("variant_id"), o.get("word")) for o in existing}
    by_word_ab = {}
    for o in existing:
        if o.get("variant_id") in ("A", "B"):
            by_word_ab.setdefault(o.get("word"), {})[o.get("variant_id")] = o
    n_ins = 0
    for spec in C.VARIANTS:
        if spec["id"] not in ("E", "F", "G", "H"):
            continue
        for f in forecasts:
            word = f.get("word")
            p = int(f.get("probability") or GROK_2026_09_15.get(word) or 0)
            if not p:
                p = GROK_2026_09_15.get(word) or 0
            if (spec["id"], word) in have:
                continue
            if spec["rule"] == "fade15_gate50":
                src = by_word_ab.get(word, {}).get("A" if spec["id"] == "E" else "B")
                if not src:
                    continue
                if not strategy.fade_gate_ok(p, src["side"]):
                    continue
                row = {k: src[k] for k in src if k not in ("id",)}
                row["variant_id"] = spec["id"]
                row["exit_rule"] = "hold"
                row["notional_dollars"] = spec["notional"]
                row["status"] = "paper_sweep"
                try:
                    store.insert_order({
                        "forecast_id": src.get("forecast_id"),
                        "run_id": src["run_id"],
                        "event_date": str(src.get("event_date") or date_str)[:10],
                        "market_ticker": src["market_ticker"],
                        "word": word,
                        "side": src["side"],
                        "limit_price_cents": src["limit_price_cents"],
                        "contracts": src["contracts"],
                        "cost_cents": src["cost_cents"],
                        "gap_points": src.get("gap_points"),
                        "threshold": src.get("threshold") or 15,
                        "cluster_key": src.get("cluster_key"),
                        "paper": True,
                        "status": "paper_sweep",
                        "variant_id": spec["id"],
                        "exit_rule": "hold",
                        "notional_dollars": spec["notional"],
                        "execution_model": src.get("execution_model"),
                    })
                    n_ins += 1
                except Exception:
                    log.exception("insert %s %s", spec["id"], word)
            elif spec["rule"] == "grok10":
                g = strategy.grok10_limit(p)
                if not g:
                    continue
                our = g["our_price_cents"]
                contracts = round(spec["notional"] / (our / 100.0), 2)
                store.insert_order({
                    "forecast_id": f.get("id"),
                    "run_id": run["id"],
                    "event_date": date_str,
                    "market_ticker": f.get("market_ticker"),
                    "word": word,
                    "side": g["side"],
                    "limit_price_cents": g["yes_price_cents"],
                    "contracts": contracts,
                    "cost_cents": int(round(contracts * our)),
                    "gap_points": 0,
                    "threshold": 0,
                    "cluster_key": word,
                    "paper": True,
                    "status": "paper_sweep",
                    "variant_id": spec["id"],
                    "exit_rule": "hold",
                    "notional_dollars": spec["notional"],
                    "execution_model": "grok10",
                })
                n_ins += 1
    # score newly present E-H
    orders = store.orders_for_run(run["id"])
    for o in orders:
        if o.get("variant_id") not in ("E", "F", "G", "H"):
            continue
        word = o.get("word")
        intended = float(o.get("contracts") or 0)
        vid = o.get("variant_id")
        if vid in ("E", "F"):
            frozen = freeze_filled(o, date_str)
            filled = 0.0 if frozen is None else min(intended, frozen)
            p = GROK_2026_09_15.get(word, 0)
            if not strategy.fade_gate_ok(p, o.get("side")):
                filled = 0.0
        else:
            # G $1 fill only if YES-buy was through before 5:29. H stays 0.
            if vid == "H":
                filled = 0.0
            else:
                tape = tape_by.get(o.get("market_ticker") or "") or []
                filled = intended if _through_before_529(o.get("side"), int(o["limit_price_cents"]), tape) else 0.0
        outcome = outcomes.get(o.get("market_ticker") or "")
        if filled <= 0:
            store.update_order(o["id"], filled_contracts=0, status="unfilled", realized_pnl_cents=0, result=outcome)
            continue
        net = hold_pnl(o, filled, outcome or "no")
        store.update_order(o["id"], filled_contracts=round(filled, 4), status="settled", result=outcome, realized_pnl_cents=net)
        store.upsert_settlement({
            "forecast_id": o.get("forecast_id"),
            "order_id": o["id"],
            "settled_at": datetime.now(timezone.utc),
            "outcome": outcome or "no",
            "gross_cents": net,
            "fees_cents": 0,
            "net_cents": net,
            "fill_model": f"score_v140_{vid}_filled={filled:g}",
        })
    return {"inserted": n_ins}
