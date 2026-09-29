"""Book L: LIVE real-money trading. Real Kalshi orders, real dollars.

v1.7.0 rule ("grok15_no", strategy.order_for_rule -- paper book N uses the same
function): every word with Grok <= L_MAX_GROK gets ONE limit order to SELL YES at
Grok + L_OFFSET_CENTS (= BUY NO at 100 - Grok - offset), $L_NOTIONAL_DOLLARS each.
No market price is read. Orders go out the moment the market opens (like nofade's
fast open) or right after the JSON is parsed if the market is already open. A limit
that crosses fills at once at the buyers' better price (taker fee); the rest rests
until the 5:29 CT cancel, the same cancel every paper book uses.

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
RULE = "grok15_no"


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


def qualifying_words(run: dict, forecasts: list[dict], frozen: dict | None = None) -> list[dict]:
    """v1.7.0: every word with Grok <= L_MAX_GROK, priced at Grok + L_OFFSET_CENTS by the
    SAME function paper book N uses (strategy.order_for_rule "grok15_no"). No market quote
    is read. Sorted lowest Grok first, so the nightly cap keeps the most confident NO words.
    `frozen` is ignored (kept so old callers still work)."""
    out = []
    for f in forecasts:
        p = f.get("probability")
        if p is None:
            continue
        decision = strategy.order_for_rule(RULE, int(p), None, None, False, C.L_NOTIONAL_DOLLARS)
        if not decision:
            continue
        out.append({"forecast": f, "bid": None, "ask": None, "captured_at": None, "decision": decision})
    out.sort(key=lambda s: (int(s["forecast"]["probability"]), str(s["forecast"].get("word") or "")))
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


def l_client_order_id(event_date: str, ticker: str, decision: dict) -> str:
    """Kalshi's v2 order endpoint wants a UUID client_order_id -- nofade learned this on its
    first live day (Sep 9) and switched to uuid5. Deterministic per (night, ticker, price,
    size) exactly like nofade's, so a retry is refused by Kalshi as a duplicate instead of
    resting a second real order."""
    seed = (f"gapL|{event_date}|{ticker}|{int(decision['our_price_cents'])}|"
            f"{float(decision['contracts']):.2f}|v2")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _looks_like_duplicate(exc: Exception) -> bool:
    """Same test as nofade's strategy._looks_like_duplicate."""
    text = str(getattr(exc, "body", "") or exc).lower()
    return "already_exist" in text or "already exist" in text or "duplicate" in text


def _find_resting_by_coid(client: KalshiClient, coid: str) -> str | None:
    try:
        for o in client.get_resting_orders(series_prefix=C.SERIES):
            if str(o.get("client_order_id") or "") == coid and o.get("order_id"):
                return str(o["order_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("L resting-order lookup failed: %s", exc)
    return None


def _prepare_l_night(run: dict) -> dict:
    """All the early gates + candidate selection for tonight, no order calls.
    Shared by arm_tonight() (the normal-path fallback) and fast_arm_watch_tick()
    (the fast path) so the two can never disagree about who qualifies or how many."""
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
    forecasts = store.forecasts_for_run(run["id"])
    if not forecasts:
        out["reason"] = "no_forecasts_yet"
        return out
    candidates = qualifying_words(run, forecasts)
    cap = nightly_word_cap()
    take = candidates[:cap]
    out.update(ready=True, take=take, cap=cap, candidates=len(candidates))
    return out


def _fire_l_orders(client: KalshiClient, run: dict, date_str: str, take: list[dict],
                    cap: int, n_candidates: int) -> dict:
    """Actually place tonight's L orders. Concurrent (small thread pool, same idea
    as nofade's _fast_send ThreadPoolExecutor) so a multi-word night doesn't lose
    time sending them one at a time right at the open."""
    placed_at = _now()
    deadline = _show_cancel_utc(date_str) or (placed_at + timedelta(minutes=60))

    def _place_one(sig: dict) -> dict | None:
        f = sig["forecast"]
        d = sig["decision"]
        ticker = f["market_ticker"]
        if store.l_order_exists(date_str, ticker):
            return None
        coid = l_client_order_id(date_str, ticker, d)
        yes_limit = int(d["yes_price_cents"])
        info = {"word": f["word"], "grok": int(f.get("probability") or 0), "yes": yes_limit,
                "no": int(d["our_price_cents"]), "filled": 0.0, "kind": "placed"}
        row = {
            "event_date": date_str,
            "market_ticker": ticker,
            "word": f["word"],
            "forecast_id": f.get("id"),
            "run_id": run["id"],
            "side": d["side"],
            "limit_price_cents": yes_limit,
            "our_price_cents": int(d["our_price_cents"]),
            "contracts": d["contracts"],
            "cost_cents": d["cost_cents"],
            "gap_points": d.get("gap_points"),
            "quote_bid_cents": None,
            "quote_ask_cents": None,
            "quote_captured_at": None,
            "client_order_id": coid,
            "status": "pending",
            "placed_at": placed_at,
            "cancel_deadline_at": deadline,
        }
        # Claim the (night, ticker) row FIRST. The unique index lets exactly one caller win,
        # so the fast-watch thread and the 30s poll thread can never both send a real order
        # for the same word. Only the winner talks to Kalshi.
        saved = store.l_insert_order(row)
        if not saved:
            return None
        try:
            resp = client.create_no_order(
                ticker=ticker,
                no_price_cents=int(d["our_price_cents"]),
                count=d["contracts"],
                client_order_id=coid,
                post_only=bool(C.L_POST_ONLY),
                expiration_epoch=int(deadline.timestamp()) if C.USE_SERVER_SIDE_EXPIRY else None,
            )
        except KalshiError as exc:
            reason = f"{exc.status}: {exc.body[:300]}"
            found = _find_resting_by_coid(client, coid) if _looks_like_duplicate(exc) else None
            if found:
                store.l_update_order(saved["id"], kalshi_order_id=found, status="resting",
                                     reject_reason=f"duplicate on send, adopted resting order: {reason}"[:400])
                return info
            store.l_update_order(saved["id"], status="rejected", reject_reason=reason)
            log.warning("L order rejected for %s: %s", ticker, reason)
            return dict(info, kind="rejected", reason=reason[:120])
        except Exception as exc:  # noqa: BLE001  network error: the order MAY exist on Kalshi
            found = _find_resting_by_coid(client, coid)
            if found:
                store.l_update_order(saved["id"], kalshi_order_id=found, status="resting",
                                     reject_reason=f"send error, adopted resting order: {exc}"[:400])
                return info
            store.l_update_order(saved["id"], status="rejected",
                                 reject_reason=f"send error, nothing resting found: {exc}"[:400])
            log.warning("L order send error for %s: %s", ticker, exc)
            return dict(info, kind="rejected", reason=str(exc)[:120])
        fields = {"kalshi_order_id": resp.get("order_id"), "status": "resting"}
        if resp.get("fill_count"):
            fields["status"] = "filled" if resp["fill_count"] >= float(d["contracts"]) - 1e-6 else "partially_filled"
            fields["filled_contracts"] = resp["fill_count"]
            fields["avg_fill_price_cents"] = resp.get("avg_fill_price_cents") or int(d["our_price_cents"])
            fields["first_fill_at"] = _now()
            info["filled"] = float(resp["fill_count"])
        store.l_update_order(saved["id"], **fields)
        return info

    results: list[dict] = []
    if take:
        with ThreadPoolExecutor(max_workers=max(1, min(C.L_FAST_MAX_WORKERS, len(take)))) as pool:
            for res in pool.map(_place_one, take):
                if res is not None:
                    results.append(res)
    placed = [r["word"] for r in results if r["kind"] == "placed"]
    rejected = [r["word"] for r in results if r["kind"] == "rejected"]

    store.set_state(_armed_key(date_str), {
        "armed_at": placed_at.isoformat(), "placed": placed, "rejected": rejected,
        "candidates": n_candidates, "cap": cap,
    })
    store.log_activity(
        "l_armed",
        f"{date_str}: placed {len(placed)} real L orders "
        f"({', '.join(placed) or 'none'}); rejected {len(rejected)}",
    )
    if results:
        lines = [f"\U0001F4B5 <b>Book L -- LIVE real-money orders placed</b> ({date_str})",
                 f"rule: Grok &lt;= {C.L_MAX_GROK} -> sell YES at Grok+{C.L_OFFSET_CENTS} · ${C.L_NOTIONAL_DOLLARS:g}/word"]
        for r in sorted(results, key=lambda x: x["grok"]):
            if r["kind"] == "rejected":
                lines.append(f"  REJECTED {r['word']}: {r.get('reason', '')}")
                continue
            tail = f" · filled now {r['filled']:g}" if r["filled"] else " · resting"
            lines.append(f"  {r['word']} (Grok {r['grok']}): NO &lt;= {r['no']}¢ (sell YES {r['yes']}¢){tail}")
        skipped = n_candidates - len(take)
        if skipped > 0:
            lines.append(f"({skipped} more qualified but the nightly cap is {cap} words)")
        lines.append("cancel at 5:29 CT if unfilled · hold to settlement")
        notify.send("\n".join(lines))
    return {"ok": True, "reason": "armed", "placed": placed, "rejected": rejected}


def _probe_market_status(client: KalshiClient, ticker: str) -> str | None:
    try:
        mkt = client.get_market(ticker)
    except Exception as exc:  # noqa: BLE001
        log.debug("L probe failed for %s: %s", ticker, exc)
        return None
    status = str((mkt or {}).get("status") or "").strip().lower()
    return status or None


def arm_tonight(run: dict) -> dict:
    """Place tonight's real L orders. NORMAL-PATH fallback, called every ~30s from the
    poll loop and right after the JSON is parsed -- safe to call any time, it no-ops
    once armed. Like nofade: no market price is needed, only an OPEN market. If the JSON
    arrives before the open it waits; fast_arm_watch_tick() fires at the open itself."""
    prep = _prepare_l_night(run)
    date_str = prep["date_str"]
    if not prep["ready"]:
        reason = prep.get("reason", "not_ready")
        # "l_live_off" never writes the armed flag, so flipping L_LIVE_ON later that
        # day still works.
        if reason == "circuit_breaker":
            store.set_state(_armed_key(date_str), {"armed_at": _now().isoformat(), "reason": reason})
            store.log_activity(
                "l_skip_night",
                f"{date_str}: circuit breaker active for {prep.get('week')}, no L orders tonight",
            )
        return {"ok": True, "reason": reason}

    take, cap, n_candidates = prep["take"], prep["cap"], prep["candidates"]
    if not take:
        store.set_state(_armed_key(date_str), {"armed_at": _now().isoformat(), "reason": "no_qualifying_words"})
        store.log_activity("l_no_words", f"{date_str}: no word with Grok <= {C.L_MAX_GROK}")
        return {"ok": True, "reason": "no_qualifying_words", "candidates": n_candidates}

    client = _client()
    real_open = resolve_real_open(client, run["event_ticker"], date_str)
    if real_open is None or _now() < real_open:
        return {"ok": True, "reason": "waiting_open"}
    status = _probe_market_status(client, take[0]["forecast"]["market_ticker"])
    if status not in ("active", "open"):
        return {"ok": True, "reason": "waiting_market_active", "status": status}
    if n_candidates > len(take):
        store.log_activity(
            "l_nightly_cap",
            f"{date_str}: {n_candidates} words qualified for L, cap is {cap}; "
            f"took the {len(take)} lowest-Grok words",
        )
    return _fire_l_orders(client, run, date_str, take, cap, n_candidates)


def fast_arm_watch_tick() -> None:
    """One tick of the fast path, called every ~1s from its own thread (see
    streamlit_app.py). Same idea as nofade's fast open: once tonight's real open_time
    is known, do nothing until L_FAST_LEAD_SECONDS before it, then watch the market
    status every L_FAST_WATCH_SECONDS and send every order the instant Kalshi says it
    is active. Gives up L_FAST_GIVE_UP_SECONDS after open_time; arm_tonight() on the
    normal loop then takes over."""
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
        return

    prep = _prepare_l_night(run)
    if not prep["ready"] or not prep["take"]:
        return  # no JSON yet (or nothing qualifies): arm_tonight() handles it

    take, cap, n_candidates = prep["take"], prep["cap"], prep["candidates"]
    probe_ticker = take[0]["forecast"]["market_ticker"]
    while _now() < give_up_at:
        if store.get_state(_armed_key(date_str)):
            return
        if _probe_market_status(client, probe_ticker) in ("active", "open"):
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


def _fill_no_cents(fill: dict, fallback: int) -> int:
    """NO cents paid on one fill. Same field order nofade's poll_fills uses."""
    price = _to_cents(fill.get("no_price_dollars") or fill.get("no_price") or fill.get("price"))
    if price is None and fill.get("yes_price_dollars") is not None:
        yes_px = _to_cents(fill.get("yes_price_dollars"))
        if yes_px is not None:
            price = max(1, 100 - yes_px)
    return int(price) if price is not None else int(fallback)


def poll_fills_tick() -> None:
    """v1.6.6 rewrite. Matches fills to OUR order by Kalshi order_id (not by ticker: nofade
    rests NO on every word from the same account, so a ticker match counted its fills as
    L's). And it SETS the total from the full list of that order's fills instead of adding
    every fill again on every 30s tick (the old code re-added the same fill each tick until
    a partial fill looked like a full one)."""
    open_orders = [o for o in store.l_orders_open() if o.get("kalshi_order_id")]
    if not open_orders:
        return
    client = _client()
    by_oid = {str(o["kalshi_order_id"]): o for o in open_orders}
    tickers = sorted({o["market_ticker"] for o in open_orders})
    totals: dict[str, dict] = {}
    for ticker in tickers:
        try:
            fills = client.get_fills(ticker=ticker, limit=200)
        except Exception as exc:  # noqa: BLE001
            log.warning("L fill poll failed for %s: %s", ticker, exc)
            continue
        for fill in fills:
            oid = str(fill.get("order_id") or "")
            order = by_oid.get(oid)
            if not order:
                continue
            fid = str(fill.get("trade_id") or fill.get("fill_id")
                      or f"{oid}-{fill.get('created_time')}-{fill.get('count_fp') or fill.get('count')}")
            t = totals.setdefault(oid, {"seen": set(), "count": 0.0, "cost": 0.0, "first": None})
            if fid in t["seen"]:
                continue
            t["seen"].add(fid)
            count = _to_count(fill.get("count_fp") or fill.get("count"))
            if count <= 0:
                continue
            px = _fill_no_cents(fill, int(order.get("our_price_cents") or 0))
            t["count"] += count
            t["cost"] += count * px
            created = fill.get("created_time")
            if created and (t["first"] is None or str(created) < str(t["first"])):
                t["first"] = created
    for oid, t in totals.items():
        order = by_oid[oid]
        already = float(order.get("filled_contracts") or 0)
        intended = float(order.get("contracts") or 0)
        new_total = min(t["count"], intended) if intended > 0 else t["count"]
        if new_total <= already + 1e-9:
            continue  # never move backwards; nothing new
        avg_px = t["cost"] / t["count"] if t["count"] > 0 else float(order.get("our_price_cents") or 0)
        fields = {"filled_contracts": round(new_total, 4), "avg_fill_price_cents": int(round(avg_px))}
        if not order.get("first_fill_at"):
            fields["first_fill_at"] = _now()
        if order.get("status") not in ("cancelled", "expired"):
            fields["status"] = "filled" if new_total >= intended - 1e-6 else "partially_filled"
        store.l_update_order(order["id"], **fields)
        log.info("L fill: %s %.2f/%.2f @ %.1fc", order["market_ticker"], new_total, intended, avg_px)


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
        # v1.6.6: only stamp cancel_requested_at once Kalshi confirmed. Before, a failed
        # cancel was stamped too, and the "already asked" check above then skipped it forever.
        if ok:
            store.l_update_order(
                order["id"], cancel_requested_at=now, cancel_confirmed_at=now,
                status="partially_filled" if float(order.get("filled_contracts") or 0) > 0 else "cancelled",
            )
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
