"""Book L: LIVE real-money trading. Real Kalshi orders, real dollars.

Mirrors Book K's exact rule (side=NO, Grok<=30, valid quote, |Grok-mid| strictly
>15) via strategy.order_for_rule("fade15_gate30_no", ...) -- no new signal, no
new logic. Order mechanics mirror Book A: rest 8c from mid toward Grok, cancel
at 5:29 CT (show529) -- the SAME cancel every paper book (A/B/E/F/G/H/I) uses
since the v1.5.10 timing overhaul. An earlier draft of this spec gave L its
own send+60min window; that's been dropped on your instruction to keep every
strategy, paper and live, on one cancel time.

Real order placement/cancellation is gap/kalshi.py's create_no_order /
cancel_order / batch_cancel / get_fills -- ported from wnt-nofade-bot's
wnt/kalshi.py rather than invented fresh, per the explicit instruction to
reuse that bot's proven live-order plumbing. Use the SAME real Kalshi key
nofade already trades with (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM in this
app's own Streamlit secrets).

Deliberately kept OUT of gap_orders / C.VARIANTS / pipeline.book_from_forecasts:
that path deletes and reinserts every order on each (re)booking of a night,
which is fine for a paper row but would risk losing the order_id needed to
cancel or reconcile a REAL resting order. Book L lives in its own table
(gap_l_orders) and its own tick functions, called from pipeline.poll_once()
exactly like fills_tick()/scalp.tick() but touching nothing they touch.

SAFETY, all per the go-live spec:
  - L_LIVE_ON is a hard kill switch. Default OFF.
  - Nightly cap: first L_MAX_WORDS_PER_NIGHT qualifying words, in the order the
    bot naturally processes them. No scaling size down to fit more in; excess
    qualifying words are skipped and logged (gap_activity 'l_nightly_cap').
  - Circuit breaker: if L's net settled P&L for the current ISO week drops
    below -L_CIRCUIT_BREAKER_WEEKLY_LOSS, new order placement stops (existing
    resting orders still get polled/cancelled/settled normally) and a
    Telegram alert fires once per week. Clearing it is a manual call
    (store.l_weekly_unpause) -- nothing here auto-clears or auto-adjusts.
  - No rule/size/cancel changes for L_FREEZE_MIN_FILLED filled trades or
    L_FREEZE_WEEKS weeks, same discipline as Book K -- freeze_status() below
    just reports where L stands against that bar; it does not enforce it
    (there is nothing to tune here that isn't a Streamlit secret already
    covered by the "do not change these" rule in config.py's comment).
"""
from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import clock, config as C, notify, store, strategy
from .fees import fee_cents, hold_pnl_cents
from .fills import _show_cancel_utc  # same 5:29 CT deadline every paper book now uses
from .kalshi import KalshiClient, KalshiError, market_result, resolve_real_open
from .lab import verdict, wilson

log = logging.getLogger("gap.live")

VARIANT_ID = "L"
RULE = "fade15_gate30_no"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_week(dt: datetime | None = None) -> str:
    dt = dt or _now()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _client() -> KalshiClient:
    return KalshiClient()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def circuit_breaker_state(iso_week: str | None = None) -> dict:
    iso_week = iso_week or _iso_week()
    row = store.l_weekly_row(iso_week) or {"iso_week": iso_week, "net_cents": 0, "trades": 0, "paused": False}
    tripped = (row.get("net_cents") or 0) <= -int(round(C.L_CIRCUIT_BREAKER_WEEKLY_LOSS * 100))
    return {**row, "tripped": bool(tripped) or bool(row.get("paused"))}


def _maybe_trip_breaker(iso_week: str) -> None:
    state = circuit_breaker_state(iso_week)
    if state["tripped"] and not state.get("paused"):
        reason = (
            f"L net ${state['net_cents'] / 100.0:+.2f} this week "
            f"(limit -${C.L_CIRCUIT_BREAKER_WEEKLY_LOSS:g}) -- pausing new L orders"
        )
        store.l_weekly_pause(iso_week, reason)
        store.log_activity("l_circuit_breaker", reason)
        notify.send(
            f"\U0001F6D1 <b>Book L circuit breaker tripped</b>\n{reason}\n"
            f"No new live orders will be placed this week. Existing resting orders "
            f"still get cancelled/settled normally. Clear it manually once you've "
            f"looked -- nothing here auto-adjusts the rule or size."
        )


# ---------------------------------------------------------------------------
# Qualifying words + nightly cap
# ---------------------------------------------------------------------------

def _i(x):
    if x is None or x == "":
        return None
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None


def qualifying_words(run: dict, forecasts: list[dict], frozen: dict) -> list[dict]:
    """Same predicate as K, evaluated live. Order = the order `forecasts` is in,
    i.e. the same natural order pipeline.book_from_forecasts processes words in."""
    out = []
    for f in forecasts:
        q = frozen.get(f["market_ticker"]) or {}
        bid, ask = _i(q.get("yes_bid_cents")), _i(q.get("yes_ask_cents"))
        valid = bool(q.get("valid"))
        p = f.get("probability")
        if p is None:
            continue
        decision = strategy.order_for_rule(RULE, int(p), bid, ask, valid, C.L_NOTIONAL_DOLLARS)
        if not decision:
            continue
        out.append({
            "forecast": f,
            "bid": bid,
            "ask": ask,
            "captured_at": q.get("captured_at"),
            "decision": decision,
        })
    return out


def nightly_word_cap() -> int:
    if C.L_NOTIONAL_DOLLARS <= 0:
        return 0
    by_dollars = int(C.L_NIGHTLY_CAP_DOLLARS // C.L_NOTIONAL_DOLLARS)
    return max(0, min(C.L_MAX_WORDS_PER_NIGHT, by_dollars))


# ---------------------------------------------------------------------------
# Arm tonight: place real orders, once, for qualifying words
# ---------------------------------------------------------------------------

def _armed_key(event_date: str) -> str:
    return f"l_armed:{event_date}"


def _prepare_l_night(run: dict) -> dict:
    """All the early gates + candidate selection for tonight, no order calls.
    Shared by arm_tonight() (the normal-path fallback) and fast_arm_watch_tick()
    (the fast path) so the two can never disagree about who qualifies or how
    many -- same purpose as nofade's shared preflight in _fast_run step 1."""
    date_str = str(run.get("event_date") or "")[:10]
    out = {"date_str": date_str, "ready": False}
    if not date_str:
        out["reason"] = "no_date"
        return out
    if store.get_state(_armed_key(date_str)):
        out["reason"] = "already_armed"
        return out
    if not C.L_LIVE_ON:
        out["reason"] = "l_live_off"
        return out
    week = _iso_week()
    breaker = circuit_breaker_state(week)
    if breaker["tripped"]:
        out["reason"] = "circuit_breaker"
        out["week"] = week
        return out
    from . import quotes as quotes_mod
    forecasts = store.forecasts_for_run(run["id"])
    if not forecasts:
        out["reason"] = "no_forecasts_yet"
        return out
    frozen = quotes_mod.ensure_frozen(run)
    candidates = qualifying_words(run, forecasts, frozen)
    cap = nightly_word_cap()
    take = candidates[:cap]
    out.update(ready=True, take=take, cap=cap, candidates=len(candidates))
    return out


def _fire_l_orders(client: KalshiClient, run: dict, date_str: str, take: list[dict],
                    cap: int, n_candidates: int) -> dict:
    """Actually place tonight's L orders. Concurrent (small thread pool, same idea
    as nofade's _fast_send ThreadPoolExecutor) so a multi-word night doesn't lose
    time sending them one at a time right when the book is thinnest."""
    placed_at = _now()
    deadline = _show_cancel_utc(date_str) or (placed_at + timedelta(minutes=60))

    def _place_one(sig: dict) -> tuple[str, str] | None:
        f = sig["forecast"]
        d = sig["decision"]
        ticker = f["market_ticker"]
        if store.l_order_exists(date_str, ticker):
            return None
        coid = f"gapL-{date_str}-{ticker}-{uuid.uuid4().hex[:8]}"
        row = {
            "event_date": date_str,
            "market_ticker": ticker,
            "word": f["word"],
            "forecast_id": f.get("id"),
            "run_id": run["id"],
            "side": d["side"],
            "limit_price_cents": d["yes_price_cents"],
            "our_price_cents": int(d["our_price_cents"]),
            "contracts": d["contracts"],
            "cost_cents": d["cost_cents"],
            "gap_points": d["gap_points"],
            "quote_bid_cents": sig["bid"],
            "quote_ask_cents": sig["ask"],
            "quote_captured_at": sig["captured_at"],
            "client_order_id": coid,
            "status": "pending",
            "placed_at": placed_at,
            "cancel_deadline_at": deadline,
        }
        try:
            resp = client.create_no_order(
                ticker=ticker,
                no_price_cents=int(d["our_price_cents"]),
                count=d["contracts"],
                client_order_id=coid,
                post_only=C.POST_ONLY,
                expiration_epoch=int(deadline.timestamp()) if C.USE_SERVER_SIDE_EXPIRY else None,
            )
        except KalshiError as exc:
            row["status"] = "rejected"
            row["reject_reason"] = f"{exc.status}: {exc.body[:300]}"
            store.l_insert_order(row)
            log.warning("L order rejected for %s: %s", ticker, row["reject_reason"])
            return ("rejected", f["word"])
        saved = store.l_insert_order(row)
        if not saved:
            return None  # unique index caught a race; another tick already placed it
        fields = {"kalshi_order_id": resp.get("order_id"), "status": "resting"}
        if resp.get("fill_count"):
            fields["status"] = "filled" if resp["fill_count"] >= float(d["contracts"]) else "partially_filled"
            fields["filled_contracts"] = resp["fill_count"]
            fields["avg_fill_price_cents"] = resp.get("avg_fill_price_cents") or int(d["our_price_cents"])
            fields["first_fill_at"] = _now()
        store.l_update_order(saved["id"], **fields)
        return ("placed", f["word"])

    placed, rejected = [], []
    if take:
        with ThreadPoolExecutor(max_workers=min(C.L_FAST_MAX_WORKERS, len(take))) as pool:
            for res in pool.map(_place_one, take):
                if res is None:
                    continue
                kind, word = res
                (placed if kind == "placed" else rejected).append(word)

    store.set_state(_armed_key(date_str), {
        "armed_at": placed_at.isoformat(), "placed": placed, "rejected": rejected,
        "candidates": n_candidates, "cap": cap,
    })
    store.log_activity(
        "l_armed",
        f"{date_str}: placed {len(placed)} real L orders "
        f"({', '.join(placed) or 'none'}); rejected {len(rejected)}",
    )
    if placed or rejected:
        lines = [f"\U0001F4B5 <b>Book L -- LIVE real-money orders placed</b> ({date_str})"]
        for w in placed:
            lines.append(f"  rested NO {w} · ${C.L_NOTIONAL_DOLLARS:g}")
        for w in rejected:
            lines.append(f"  REJECTED {w}")
        lines.append("cancel at 5:29 CT if unfilled · hold to settlement")
        notify.send("\n".join(lines))
    return {"ok": True, "reason": "armed", "placed": placed, "rejected": rejected}


def arm_tonight(run: dict) -> dict:
    """Place tonight's real L orders. This is the NORMAL-PATH fallback, called
    every ~30s from the regular poll loop -- safe to call every tick, it no-ops
    instantly once armed. fast_arm_watch_tick() below is the fast path (its own
    dedicated thread, watches the real market and fires the instant it opens);
    this function is what fires if that fast path ever gives up, exactly like
    nofade's normal loop picks up after its own fast-open path times out."""
    prep = _prepare_l_night(run)
    date_str = prep["date_str"]
    if not prep["ready"]:
        reason = prep.get("reason", "not_ready")
        if reason in ("l_live_off", "circuit_breaker"):
            store.set_state(_armed_key(date_str), {"armed_at": _now().isoformat(), "reason": reason})
            if reason == "circuit_breaker":
                store.log_activity(
                    "l_skip_night",
                    f"{date_str}: circuit breaker active for {prep.get('week')}, no L orders tonight",
                )
        return {"ok": True, "reason": reason}

    client = _client()
    real_open = resolve_real_open(client, run["event_ticker"], date_str)
    if real_open is None:
        # Kalshi hasn't published tonight's real open time yet -- real money,
        # so we wait for the actual API value rather than assume any clock time.
        return {"ok": True, "reason": "waiting_real_open"}
    gate = real_open + timedelta(minutes=C.DECISION_LAG_MIN)
    if _now() < gate:
        return {"ok": True, "reason": "waiting_real_open", "opens_at": gate.isoformat()}

    take, cap, n_candidates = prep["take"], prep["cap"], prep["candidates"]
    skipped = n_candidates - len(take)
    if skipped > 0:
        store.log_activity(
            "l_nightly_cap",
            f"{date_str}: {n_candidates} words qualified for L, cap is {cap} "
            f"(${C.L_NIGHTLY_CAP_DOLLARS:g} / ${C.L_NOTIONAL_DOLLARS:g}/word); "
            f"took first {len(take)}, skipped {skipped} -- no size-scaling",
        )
    if not take:
        store.set_state(_armed_key(date_str), {"armed_at": _now().isoformat(), "reason": "no_qualifying_words"})
        return {"ok": True, "reason": "no_qualifying_words", "candidates": n_candidates}

    return _fire_l_orders(client, run, date_str, take, cap, n_candidates)


def _probe_market_status(client: KalshiClient, ticker: str) -> str | None:
    try:
        mkt = client.get_market(ticker)
    except Exception as exc:  # noqa: BLE001
        log.debug("L fast probe failed for %s: %s", ticker, exc)
        return None
    status = str((mkt or {}).get("status") or "").strip().lower()
    return status or None


def fast_arm_watch_tick() -> None:
    """One tick of the fast path, called every ~1s from its own dedicated thread
    (see streamlit_app.py) -- separate from the 30s poll loop, same two-thread
    shape as nofade-bot's runner/collector split. Ports nofade's
    _fast_arm/_fast_wait_and_watch/_fast_fire: once tonight's real open_time is
    known (resolve_real_open, cached), it does nothing until L_FAST_LEAD_SECONDS
    before that time, then watches the actual market status every
    L_FAST_WATCH_SECONDS and fires the instant Kalshi reports it active --
    not on the next poll tick. Gives up after L_FAST_GIVE_UP_SECONDS past
    open_time; arm_tonight() on the normal loop (same real-open gate) then
    takes over, exactly like nofade falling back to its normal path."""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run:
        return
    if store.get_state(_armed_key(date_str)):
        return
    if not C.L_LIVE_ON:
        return

    client = _client()
    real_open = resolve_real_open(client, run["event_ticker"], date_str)
    if real_open is None:
        return
    lead_at = real_open - timedelta(seconds=C.L_FAST_LEAD_SECONDS)
    give_up_at = real_open + timedelta(seconds=C.L_FAST_GIVE_UP_SECONDS)
    now = _now()
    if now < lead_at or now >= give_up_at:
        return  # too early to watch closely yet, or this window already passed

    prep = _prepare_l_night(run)
    if not prep["ready"] or not prep["take"]:
        return  # arm_tonight() on the normal loop logs/handles every other case

    take, cap, n_candidates = prep["take"], prep["cap"], prep["candidates"]
    probe_ticker = take[0]["forecast"]["market_ticker"]

    while _now() < give_up_at:
        if store.get_state(_armed_key(date_str)):
            return  # normal path (or a previous tick) already armed it
        status = _probe_market_status(client, probe_ticker)
        if status in ("active", "open"):
            gate = real_open + timedelta(minutes=C.DECISION_LAG_MIN)
            remaining = (gate - _now()).total_seconds()
            if remaining > 0:
                time.sleep(remaining)
            _fire_l_orders(client, run, date_str, take, cap, n_candidates)
            return
        time.sleep(C.L_FAST_WATCH_SECONDS)


# ---------------------------------------------------------------------------
# Poll real fills
# ---------------------------------------------------------------------------

def _to_count(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_cents(v):
    if v is None or v == "":
        return None
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    if 0 < abs(d) < 1:
        return int(round(d * 100))
    return int(round(d))


def poll_fills_tick() -> None:
    open_orders = [o for o in store.l_orders_open() if o.get("kalshi_order_id")]
    if not open_orders:
        return
    client = _client()
    try:
        recent = client.get_fills(limit=200)
    except Exception as exc:
        log.warning("L fill poll failed: %s", exc)
        return
    by_ticker = {o["market_ticker"]: o for o in open_orders}
    for fill in recent:
        ticker = fill.get("ticker") or fill.get("market_ticker")
        order = by_ticker.get(ticker)
        if not order:
            continue
        count = _to_count(fill.get("count_fp") or fill.get("count"))
        price = _to_cents(fill.get("no_price_dollars") or fill.get("no_price") or fill.get("price"))
        if not count or price is None:
            continue
        already = float(order.get("filled_contracts") or 0)
        intended = float(order.get("contracts") or 0)
        new_total = min(intended, already + count) if already + count > intended else already + count
        if new_total <= already + 1e-9:
            continue
        prev_cost = already * float(order.get("avg_fill_price_cents") or price)
        added_cost = (new_total - already) * price
        avg_px = (prev_cost + added_cost) / new_total if new_total > 0 else price
        fields = {"filled_contracts": round(new_total, 4), "avg_fill_price_cents": int(round(avg_px))}
        if not order.get("first_fill_at"):
            fields["first_fill_at"] = _now()
        fields["status"] = "filled" if new_total >= intended - 1e-6 else "partially_filled"
        store.l_update_order(order["id"], **fields)
        log.info("L fill: %s %.2f @ %dc (total %.2f/%.2f)", ticker, count, price, new_total, intended)


# ---------------------------------------------------------------------------
# Cancel at the deadline
# ---------------------------------------------------------------------------

# Kalshi is told to expire the order itself at exactly 5:29 CT
# (expiration_epoch in arm_tonight, from _show_cancel_utc). This is the
# Streamlit-side safety net for "what if Kalshi doesn't clear it" -- it waits
# an extra 45s past that same deadline before trying its own cancel, so it
# only ever fires as a backstop, never racing Kalshi's own expiry.
CANCEL_SAFETY_BUFFER_SECONDS = 45


def cancel_if_due_tick() -> None:
    now = _now()
    for order in store.l_orders_open():
        deadline = order.get("cancel_deadline_at")
        if deadline is None:
            continue
        if hasattr(deadline, "tzinfo") and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        deadline = deadline + timedelta(seconds=CANCEL_SAFETY_BUFFER_SECONDS)
        if now < deadline:
            continue
        remaining = float(order.get("contracts") or 0) - float(order.get("filled_contracts") or 0)
        if order.get("status") in ("filled",) or remaining <= 1e-6:
            continue
        if order.get("cancel_requested_at"):
            continue  # already asked Kalshi; wait for confirmation via order state next tick
        oid = order.get("kalshi_order_id")
        if not oid:
            store.l_update_order(order["id"], status="expired", cancel_requested_at=now, cancel_confirmed_at=now)
            continue
        client = _client()
        try:
            ok = client.cancel_order(oid)
        except Exception as exc:
            log.warning("L cancel failed for %s: %s", oid, exc)
            ok = False
        fields = {"cancel_requested_at": now}
        if ok:
            fields["cancel_confirmed_at"] = now
            fields["status"] = "partially_filled" if float(order.get("filled_contracts") or 0) > 0 else "cancelled"
        store.l_update_order(order["id"], **fields)
        store.log_activity(
            "l_cancel",
            f"{order['event_date']} {order['market_ticker']}: cancel at 5:29 CT "
            f"{'confirmed' if ok else 'FAILED, will retry'}",
        )


# ---------------------------------------------------------------------------
# Settle against Kalshi's official result
# ---------------------------------------------------------------------------

def settle_tick() -> None:
    pending = store.l_orders_unsettled_for_settlement()
    if not pending:
        return
    client = _client()
    cache: dict[str, str | None] = {}
    for order in pending:
        ticker = order["market_ticker"]
        if ticker not in cache:
            try:
                mkt = client.get_market(ticker)
                cache[ticker] = market_result(mkt)
            except Exception as exc:
                log.warning("L settle lookup %s: %s", ticker, exc)
                cache[ticker] = None
        outcome = cache[ticker]
        if outcome not in ("yes", "no", "void"):
            continue
        filled = float(order.get("filled_contracts") or 0)
        our_px = int(order.get("avg_fill_price_cents") or order.get("our_price_cents") or 0)
        fee = fee_cents(filled, our_px)
        if outcome == "void":
            net = 0
        else:
            net = hold_pnl_cents(order.get("side") or "NO", filled, our_px, outcome, fee)
        store.l_update_order(
            order["id"], status="settled", result=outcome, fees_cents=fee,
            realized_pnl_cents=net, settled_at=_now(),
        )
        week = _iso_week(order.get("placed_at") if isinstance(order.get("placed_at"), datetime) else _now())
        store.l_weekly_add(week, net, trades=1)
        _maybe_trip_breaker(week)


def tick() -> None:
    """Called from pipeline.poll_once(), same pattern as fills_tick()/scalp.tick().
    Each phase is independently guarded so one failure doesn't block the rest."""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if run:
        try:
            arm_tonight(run)
        except Exception:
            log.exception("l_arm_tonight")
    try:
        poll_fills_tick()
    except Exception:
        log.exception("l_poll_fills")
    try:
        cancel_if_due_tick()
    except Exception:
        log.exception("l_cancel_if_due")
    try:
        settle_tick()
    except Exception:
        log.exception("l_settle")


# ---------------------------------------------------------------------------
# Reporting: Streamlit "L (Live)" tab + weekly dump side-by-side
# ---------------------------------------------------------------------------

def freeze_status(all_orders: list[dict]) -> dict:
    filled = [o for o in all_orders if float(o.get("filled_contracts") or 0) > 0]
    first = min((o["placed_at"] for o in all_orders if o.get("placed_at")), default=None)
    weeks_running = None
    if first is not None:
        now = _now()
        if hasattr(first, "tzinfo") and first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        weeks_running = (now - first).days / 7.0
    frozen = len(filled) >= C.L_FREEZE_MIN_FILLED or (weeks_running is not None and weeks_running >= C.L_FREEZE_WEEKS)
    return {
        "filled_trades": len(filled),
        "min_filled": C.L_FREEZE_MIN_FILLED,
        "weeks_running": round(weeks_running, 1) if weeks_running is not None else None,
        "freeze_weeks": C.L_FREEZE_WEEKS,
        "frozen_window_over": frozen,
    }


def status_report(start_date: str | None = None, end_date: str | None = None) -> dict:
    """Everything the Streamlit L tab and the weekly dump need. Real money, real
    fills only -- no paper simulation anywhere in this function."""
    end_date = end_date or clock.today_ct()
    start_date = start_date or "2000-01-01"
    orders = store.l_orders_between(start_date, end_date)
    settled = [o for o in orders if o.get("status") == "settled" and o.get("result") in ("yes", "no")]
    n = len(settled)
    wins = sum(1 for o in settled if (o["side"] == "NO" and o["result"] == "no") or (o["side"] == "YES" and o["result"] == "yes"))
    hit_pct = (wins / n * 100.0) if n else None
    avg_px = (sum(float(o.get("avg_fill_price_cents") or o.get("our_price_cents") or 0) for o in settled) / n) if n else None
    # Break-even = avg price paid + avg fee per contract, same shape as lab.stats_for.
    break_even = None
    if n:
        avg_fee_per_contract = sum(
            (o.get("fees_cents") or 0) / max(float(o.get("filled_contracts") or 1), 1e-9) for o in settled
        ) / n
        break_even = avg_px + avg_fee_per_contract
    margin = (hit_pct - break_even) if (hit_pct is not None and break_even is not None) else None
    net_dollars = sum(o.get("realized_pnl_cents") or 0 for o in settled) / 100.0
    lo_pct, hi_pct = wilson(wins, n) if n else (None, None)  # wilson() already returns percent
    v = verdict(n, hit_pct, break_even, lo_pct) if n else "NO FILLS YET"

    week = _iso_week()
    breaker = circuit_breaker_state(week)
    freeze = freeze_status(orders)

    today = store.l_orders_for_date(clock.today_ct())

    return {
        "orders": orders,
        "today": today,
        "n_settled": n,
        "hit_pct": hit_pct,
        "avg_price_cents": avg_px,
        "break_even_pct": break_even,
        "margin_pts": margin,
        "net_dollars": net_dollars,
        "range_90": (lo_pct, hi_pct),
        "verdict": v,
        "circuit_breaker": breaker,
        "freeze": freeze,
        "live_on": C.L_LIVE_ON,
        "notional_dollars": C.L_NOTIONAL_DOLLARS,
        "nightly_cap_dollars": C.L_NIGHTLY_CAP_DOLLARS,
        "max_words_per_night": C.L_MAX_WORDS_PER_NIGHT,
        "cancel_time_ct": C.SHOW_CANCEL_CT,
    }
