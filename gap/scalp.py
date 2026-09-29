"""SCALP: a pre-registered book that trades the PRICE MOVE, not the settlement outcome.

Backed by a trade-by-trade Kalshi backtest (Aug 17 - Sep 23, 29 qualifying words, 14
nights): buy YES cheap on words Grok likes, flip the contract for a quick profit once
the market catches up, never hold to settlement if it doesn't have to.

FROZEN RULE (do not tune before 30 filled trades or 6 weeks, whichever comes first):
  qualify: Grok >= SCALP_QUALIFY_PROB (currently 70) for this word
  buy:     YES in batches, any poll where the ask is <= SCALP_BUY_MAX_CENTS (70), from
           the decision time until SCALP_BUY_CUTOFF_HHMM (16:30 CT), walking the real
           order book, until SCALP_BUDGET_DOLLARS (100) total is spent on this word or
           the buy cutoff hits or the book has nothing left at <= 70c
  sell:    every batch gets its own resting sell the moment it fills, at SCALP_SELL_CENTS
           (85)
  fallback: anything still unsold at SCALP_FALLBACK_HHMM (17:00 CT) sells at market
           (best available bid), fees included

Runs automatically from the poll loop (gap/pipeline.py:poll_once), same as the other
books -- no extra manual step. A word can get several batches over the evening; each one
is its own row in gap_scalp_batches (a resting hold order can only have one row per book
per market, a scalp batch is not a hold order).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import clock, config as C, fees, fills, store

log = logging.getLogger("gap.scalp")


def _hhmm_utc(date_str: str, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=C.CT).astimezone(timezone.utc)


def _now() -> datetime:
    return fills._now()


def _ask_levels(no_book: list) -> list[tuple[int, float]]:
    """Cheapest-first YES-ask levels, derived from the NO-bid side of the book
    (a NO bid at price p is a YES ask at 100-p)."""
    lv = [(int(p), float(c)) for p, c in (no_book or []) if c and float(c) > 0]
    lv.sort(key=lambda x: -x[0])   # highest NO bid first = cheapest YES ask first
    return [(100 - p, c) for p, c in lv[:10]]


def _walk_buy(no_book: list, cap_cents: int, budget_dollars: float) -> dict:
    """Buy YES from the cheapest ask up, stop at cap_cents or when the budget runs out."""
    remaining = float(budget_dollars) * 100.0   # cents
    contracts = cost = 0.0
    for price, qty in _ask_levels(no_book):
        if price <= 0 or price > cap_cents or remaining <= 1e-9:
            continue
        c = min(qty, remaining / price)
        if c <= 0:
            continue
        contracts += c
        cost += c * price
        remaining -= c * price
    avg = (cost / contracts) if contracts > 0 else None
    return {"contracts": contracts, "cost_cents": cost, "avg_px": avg}


def _spent_by_ticker(batches: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in batches:
        out[b["market_ticker"]] = out.get(b["market_ticker"], 0) + int(b["buy_cost_cents"] or 0) + int(b["buy_fee_cents"] or 0)
    return out


def _buy_tick(run: dict, forecasts: list[dict], batches: list[dict], now: datetime) -> None:
    if now > _hhmm_utc(run["event_date_str"], C.SCALP_BUY_CUTOFF_HHMM):
        return
    spent = _spent_by_ticker(batches)
    for f in forecasts:
        p = f.get("probability")
        if p is None or int(p) < C.SCALP_QUALIFY_PROB:
            continue
        ticker = f["market_ticker"]
        remaining_dollars = C.SCALP_BUDGET_DOLLARS - spent.get(ticker, 0) / 100.0
        if remaining_dollars <= 0.01:
            continue
        try:
            snap = store.latest_nofade_depth(ticker, run["event_date_str"])
        except Exception as exc:
            log.warning("scalp buy depth %s: %s", ticker, exc)
            continue
        if not snap:
            continue
        bought = _walk_buy(snap.get("no_book") or [], C.SCALP_BUY_MAX_CENTS, remaining_dollars)
        if bought["contracts"] <= 0:
            continue
        px = int(round(bought["avg_px"]))
        fee = fees.fee_cents(bought["contracts"], px)
        row = store.insert_scalp_batch({
            "run_id": run["id"], "event_date": run["event_date_str"], "market_ticker": ticker, "word": f["word"],
            "grok_probability": int(p), "buy_at": now, "buy_price_cents": px,
            "buy_contracts": round(bought["contracts"], 4), "buy_cost_cents": int(round(bought["cost_cents"])),
            "buy_fee_cents": fee,
        })
        spent[ticker] = spent.get(ticker, 0) + row["buy_cost_cents"] + row["buy_fee_cents"]
        batches.append(row)
        store.log_activity("scalp_buy", f"{f['word']} +{bought['contracts']:.2f} @ {px}c (grok {p}%)")


def _sell_tick(run: dict, batches: list[dict], now: datetime) -> None:
    fallback_at = _hhmm_utc(run["event_date_str"], C.SCALP_FALLBACK_HHMM)
    if now >= fallback_at:
        return   # the fallback phase handles these
    for b in batches:
        if b["status"] != "resting_sell":
            continue
        ticker = b["market_ticker"]
        try:
            snap = store.latest_nofade_depth(ticker, run["event_date_str"])
        except Exception as exc:
            log.warning("scalp sell depth %s: %s", ticker, exc)
            continue
        if not snap:
            continue
        yes_book = snap.get("yes_book") or []
        size, best_yes = fills.crossing({"yes": yes_book, "no": []}, "NO", C.SCALP_SELL_CENTS)
        if size <= 0 or best_yes is None:
            continue
        credit_key = f"scalp_sell_credit:{b['id']}"
        cstate = store.get_state(credit_key) or {}
        last = float(cstate.get("last") or 0.0)
        credit = float(cstate.get("credit") or 0.0)
        if size != last:
            credit += size if not cstate else max(0.0, size - last)
            store.set_state(credit_key, {"last": size, "credit": credit})
        already = float(b["sell_contracts"] or 0)
        want = float(b["buy_contracts"]) - already
        take = min(want, max(0.0, credit - already))
        if take <= 0:
            continue
        px = int(best_yes)
        inc_fee = fees.fee_cents(take, px)
        new_sold = already + take
        fields = {
            "sell_contracts": round(new_sold, 4),
            "sell_proceeds_cents": int(b["sell_proceeds_cents"] or 0) + int(round(take * px)),
            "sell_fee_cents": int(b["sell_fee_cents"] or 0) + inc_fee,
        }
        if new_sold + 1e-6 >= float(b["buy_contracts"]):
            _finish(b, fields, "scalp_hit", now)
        store.update_scalp_batch(b["id"], **fields)
        b.update(fields)
        store.log_activity("scalp_sell", f"{b['word']} +{take:.2f} @ {px}c ({b['status'] if new_sold+1e-6 < float(b['buy_contracts']) else 'scalp_hit'})")


def _bid_levels(yes_book: list) -> list[list[float]]:
    """YES bids, best (highest) first, as [price, size] pairs we can use up."""
    lv = [[int(p), float(c)] for p, c in (yes_book or []) if c and float(c) > 0 and int(p) > 0]
    lv.sort(key=lambda x: -x[0])
    return lv


def _fb_key(date_str: str, ticker: str) -> str:
    return f"scalp_fb_book:{date_str}:{ticker}"


_SHOWN: dict = {}


def _unused_bids(date_str: str, ticker: str, levels: list[list[float]]) -> list[list[float]]:
    """v1.7.2: YES bids the paper market-sell has NOT already used. The first look counts the
    whole book. Every later look counts only what is NEW at each price (size above what the
    last look showed) plus what was left unused -- so a snapshot that has not changed (no-fade
    saves one a minute, we look every 30s) can never be sold into twice."""
    _SHOWN[(date_str, ticker)] = {str(int(p)): float(c) for p, c in levels}
    st = store.get_state(_fb_key(date_str, ticker)) or {}
    if not st.get("last") and not st.get("seen"):
        return [[p, c] for p, c in levels]
    last = {int(k): float(v) for k, v in (st.get("last") or {}).items()}
    avail = {int(k): float(v) for k, v in (st.get("avail") or {}).items()}
    out = []
    for p, size in levels:
        fresh = max(0.0, size - last.get(p, 0.0))
        usable = min(size, avail.get(p, 0.0) + fresh)
        if usable > 1e-9:
            out.append([p, usable])
    return out


def _save_unused(date_str: str, ticker: str, levels_left: list[list[float]]) -> None:
    """Remember what the book showed on this look and which of those bids are still unused."""
    store.set_state(_fb_key(date_str, ticker), {
        "seen": True,
        "last": _SHOWN.get((date_str, ticker), {}),
        "avail": {str(int(p)): float(c) for p, c in levels_left},
    })


def _fallback_tick(run: dict, batches: list[dict], now: datetime) -> None:
    """Unsold at SCALP_FALLBACK_HHMM (17:00 CT): sell at market. v1.7.1: walks the YES bids from the best price
    down and USES UP the size as it goes, shared by every batch of the same word, so five
    batches can no longer all 'sell' into the same bids at the best price. Whatever the book
    cannot absorb stays resting and is tried again on the next poll."""
    if now < _hhmm_utc(run["event_date_str"], C.SCALP_FALLBACK_HHMM):
        return
    levels_by_ticker: dict[str, list] = {}
    for b in batches:
        if b["status"] != "resting_sell":
            continue
        ticker = b["market_ticker"]
        if ticker not in levels_by_ticker:
            try:
                snap = store.latest_nofade_depth(ticker, run["event_date_str"])
            except Exception as exc:
                log.warning("scalp fallback depth %s: %s", ticker, exc)
                snap = None
            levels_by_ticker[ticker] = _unused_bids(run["event_date_str"], ticker,
                                                    _bid_levels((snap or {}).get("yes_book") or []))
        levels = levels_by_ticker[ticker]
        already = float(b["sell_contracts"] or 0)
        want = float(b["buy_contracts"]) - already
        if want <= 1e-9 or not levels:
            continue   # no bid at all right now: leave resting, try again next poll
        took = 0.0
        proceeds = 0.0
        for lv in levels:
            if want - took <= 1e-9:
                break
            q = min(lv[1], want - took)
            if q <= 0:
                continue
            took += q
            proceeds += q * lv[0]
            lv[1] -= q
        levels[:] = [lv for lv in levels if lv[1] > 1e-9]
        if took <= 1e-9:
            continue
        px = int(round(proceeds / took))
        fee = fees.fee_cents(took, px)
        new_sold = already + took
        fields = {
            "sell_contracts": round(new_sold, 4),
            "sell_proceeds_cents": int(b["sell_proceeds_cents"] or 0) + int(round(proceeds)),
            "sell_fee_cents": int(b["sell_fee_cents"] or 0) + fee,
        }
        if new_sold + 1e-6 >= float(b["buy_contracts"]):
            _finish(b, fields, "fallback_sold", now)
        store.update_scalp_batch(b["id"], **fields)
        b.update(fields)
        _save_unused(run["event_date_str"], ticker, levels)
        store.log_activity("scalp_fallback", f"{b['word']} sold {took:.2f} @ avg {px}c (fallback, unsold at {C.SCALP_FALLBACK_HHMM} CT)")


def _finish(b: dict, fields: dict, status: str, now: datetime) -> None:
    contracts = float(fields["sell_contracts"])
    proceeds = int(fields["sell_proceeds_cents"])
    sell_fee = int(fields["sell_fee_cents"])
    buy_cost = int(b["buy_cost_cents"] or 0)
    buy_fee = int(b["buy_fee_cents"] or 0)
    fields["status"] = status
    fields["sell_at"] = now
    fields["sell_price_cents"] = int(round(proceeds / contracts)) if contracts > 0 else None
    fields["net_cents"] = proceeds - sell_fee - buy_cost - buy_fee


def tick(now: datetime | None = None) -> None:
    """Call every poll (from pipeline.poll_once). Cheap no-op on non-trading days."""
    if not C.SCALP_ON:
        return
    now = now or _now()
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run:
        return
    run = {**run, "event_date_str": str(run["event_date"])[:10]}
    forecasts = store.forecasts_for_run(run["id"])
    if not forecasts:
        return
    batches = store.scalp_batches_for_run(run["id"])
    try:
        _buy_tick(run, forecasts, batches, now)
    except Exception:
        log.exception("scalp buy")
    try:
        _sell_tick(run, batches, now)
    except Exception:
        log.exception("scalp sell")
    try:
        _fallback_tick(run, batches, now)
    except Exception:
        log.exception("scalp fallback")
