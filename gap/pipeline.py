"""Catch event → Telegram file → parse JSON → quote → paper book."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import clock, config as C, fills, notify, parser, prompt, quotes, settle, store, strategy
from .kalshi import (
    KalshiClient,
    uniquify_words,
    word_from_market,
)

log = logging.getLogger("gap.pipeline")


def pick_tonight_event(events: list[dict], date_str: str) -> dict | None:
    tokens = clock.event_date_tokens(date_str)
    scored = []
    for ev in events:
        ticker = (ev.get("event_ticker") or "").upper()
        title = (ev.get("title") or "").upper()
        hay = ticker + " " + title
        hits = sum(1 for t in tokens if t in hay)
        scored.append((hits, ev))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    open_ones = [e for e in events if (e.get("status") or "").lower() == "open"]
    if len(open_ones) == 1:
        return open_ones[0]
    return open_ones[0] if open_ones else (events[0] if events else None)


def snapshot_markets(client: KalshiClient, event: dict) -> list[dict]:
    raw = client.get_markets(event["event_ticker"])
    rows = []
    for m in raw:
        ticker = m.get("ticker") or m.get("market_ticker")
        if not ticker:
            continue
        status = (m.get("status") or "").lower()
        if status in ("settled", "closed", "finalized"):
            continue
        rows.append({
            "market_ticker": ticker,
            "title": m.get("title") or "",
            "word": word_from_market(m),
            "raw": m,
        })
    return uniquify_words(rows)


def _snapshot_words(client: KalshiClient, event: dict) -> list[dict]:
    markets = snapshot_markets(client, event)
    return [
        {"word": m["word"], "market_ticker": m["market_ticker"], "title": m["title"]}
        for m in markets
    ]



def _event_score(event: dict, date_str: str) -> tuple[int, int]:
    ticker = (event.get("event_ticker") or "").upper()
    title = (event.get("title") or "").upper()
    hay = ticker + " " + title
    date_hits = sum(1 for tok in clock.event_date_tokens(date_str) if tok in hay)
    return date_hits, 0


def maybe_upgrade_event(client: KalshiClient | None = None) -> dict:
    """If a better same-day event appears before JSON, switch and reschedule +60m."""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run or run.get("status") not in ("detected", "awaiting_json"):
        return {"ok": True, "reason": "no_upgrade"}
    client = client or KalshiClient()
    events = client.get_events(C.SERIES, status="open")
    event = pick_tonight_event(events, date_str)
    if not event:
        return {"ok": True, "reason": "no_open"}
    new_ticker = event["event_ticker"]
    words = _snapshot_words(client, event)
    old_ticker = run.get("event_ticker")
    old_n = int(run.get("markets_n") or 0)
    new_n = len(words)
    old_hits, _ = _event_score({"event_ticker": old_ticker or ""}, date_str)
    new_hits, _ = _event_score(event, date_str)
    better_date = new_hits > old_hits
    better_book = new_n >= 5 and new_n > old_n
    stub = old_n <= 2 and new_n > old_n
    if new_ticker == old_ticker and not better_book:
        return {"ok": True, "reason": "same_event"}
    if not (better_date or better_book or stub):
        return {"ok": True, "reason": "not_better"}
    if not words:
        return {"ok": False, "reason": "upgrade_no_markets"}

    seen_at = datetime.now(timezone.utc)
    send_at = seen_at + timedelta(minutes=C.DECISION_LAG_MIN)
    paste = prompt.build_paste_file(date_str, new_ticker, words)
    from sqlalchemy import text
    with store.engine().begin() as conn:
        conn.execute(text("delete from gap_markets where run_id = :id"), {"id": run["id"]})
    store.insert_markets(run["id"], date_str, new_ticker, words)
    store.update_run(
        run["id"],
        event_ticker=new_ticker,
        word_list=words,
        prompt_text=paste,
        markets_n=new_n,
        market_open_at=seen_at,
        decision_at=send_at,
        status="detected",
        telegram_msg_id=None,
        parse_error=None,
    )
    store.delete_frozen_quotes(run["id"])  # decision time moved: freeze again at the new one
    _FROZEN_SEEN.discard(run["id"])
    store.log_activity(
        "upgraded",
        f"{old_ticker} n={old_n} -> {new_ticker} n={new_n} send_at={clock.fmt(send_at)}",
    )
    notify.send(
        f"event upgraded\n{old_ticker} ({old_n} mkts) -> {new_ticker} ({new_n} mkts)\n"
        f"new file at {clock.fmt(send_at)} CT  (+{C.DECISION_LAG_MIN}m)\n"
        f"do not paste JSON for the old ticker"
    )
    return {"ok": True, "reason": "upgraded", "send_at": clock.fmt(send_at), "ticker": new_ticker}

def detect_event(client: KalshiClient | None = None) -> dict:
    """Record first sighting. Do not Telegram yet."""
    date_str = clock.today_ct()
    existing = store.get_run_for_date(date_str)
    if existing:
        return {"ok": True, "reason": "already_detected", "run": existing}

    client = client or KalshiClient()
    events = client.get_events(C.SERIES, status="open")
    event = pick_tonight_event(events, date_str)
    if not event:
        store.log_activity("no_event", f"no open {C.SERIES} event for {date_str}")
        return {"ok": False, "reason": "no_event"}

    markets = snapshot_markets(client, event)
    words = [
        {"word": m["word"], "market_ticker": m["market_ticker"], "title": m["title"]}
        for m in markets
    ]
    if not words:
        return {"ok": False, "reason": "no_markets", "event": event}

    seen_at = datetime.now(timezone.utc)
    send_at = seen_at + timedelta(minutes=C.DECISION_LAG_MIN)
    paste = prompt.build_paste_file(date_str, event["event_ticker"], words)
    run = store.insert_run({
        "event_date": date_str,
        "event_ticker": event["event_ticker"],
        "status": "detected",
        "prompt_version": C.PROMPT_VERSION,
        "harness": C.HARNESS,
        "word_list": words,
        "prompt_text": paste,
        "markets_n": len(words),
    })
    store.update_run(
        run["id"],
        market_open_at=seen_at,
        decision_at=send_at,
        status="detected",
    )
    store.insert_markets(run["id"], date_str, event["event_ticker"], words)
    store.log_activity(
        "detected",
        f"{event['event_ticker']} n={len(words)} send_at={clock.fmt(send_at)}",
    )
    return {"ok": True, "reason": "detected", "run": store.get_run_for_date(date_str), "n": len(words)}


def dispatch_prompt(force: bool = False, client: KalshiClient | None = None) -> dict:
    """Send the .txt only after detect + 60m, unless force."""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run:
        found = detect_event(client)
        if not found.get("ok"):
            return found
        run = found["run"]

    if run.get("status") not in ("detected", "awaiting_json") and not force:
        return {"ok": True, "reason": f"status_{run.get('status')}", "run": run}

    if run.get("telegram_msg_id") and run.get("status") == "awaiting_json" and not force:
        return {"ok": True, "reason": "already_sent", "run": run}

    detected = run.get("market_open_at") or run.get("created_at")
    due = clock.parse_dt(run.get("decision_at"))
    if due is None:
        due = clock.send_due_at(detected)
    if not force and due is not None and clock.now_ct() < due.astimezone(C.CT):
        return {
            "ok": True,
            "reason": "waiting_send",
            "run": run,
            "send_at": clock.fmt(due),
        }

    client = client or KalshiClient()
    events = client.get_events(C.SERIES, status="open")
    event = pick_tonight_event(events, date_str) or {"event_ticker": run["event_ticker"]}
    words = _snapshot_words(client, event) or (run.get("word_list") or [])
    if isinstance(words, str):
        import json as _json
        words = _json.loads(words)
    if not words:
        return {"ok": False, "reason": "no_markets", "run": run}

    paste = prompt.build_paste_file(date_str, event["event_ticker"], words)
    caption = prompt.build_telegram_caption(date_str, event["event_ticker"], len(words))
    caption = (
        f"{caption}\n"
        f"Detected {clock.fmt(clock.parse_dt(detected))}. "
        f"Wait was {C.DECISION_LAG_MIN}m. Paste this whole file into a NEW Expert chat."
    )
    store.update_run(
        run["id"],
        word_list=words,
        prompt_text=paste,
        markets_n=len(words),
        event_ticker=event["event_ticker"],
    )
    store.insert_markets(run["id"], date_str, event["event_ticker"], words)

    msg_id = notify.send_document(f"gap-{date_str}.txt", paste, caption)
    store.update_run(run["id"], telegram_msg_id=msg_id, status="awaiting_json")
    store.log_activity(
        "prompt_sent",
        f"{event['event_ticker']} n={len(words)} msg={msg_id} force={force} "
        f"scheduled_send={clock.fmt(due) if due else 'n/a'}",
    )
    return {"ok": True, "reason": "sent", "run": store.get_run_for_date(date_str), "n": len(words)}


def send_prompt_for_today(client: KalshiClient | None = None, force: bool = False) -> dict:
    found = detect_event(client)
    if not found.get("ok") and found.get("reason") != "already_detected":
        return found
    return dispatch_prompt(force=force, client=client)


def ingest_json(raw: str, _msg: dict | None = None) -> str:
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run:
        return "no run for today — send /gap_prep first"
    if run.get("status") == "detected":
        due = clock.send_due_at(run.get("market_open_at") or run.get("created_at"))
        return f"file not sent yet — waiting until {clock.fmt(due)}"
    if run.get("status") == "parsed" and not C.PAPER:
        return "already parsed today"
    if clock.past_json_deadline(date_str) and run.get("status") != "parsed":
        store.update_run(run["id"], status="expired", parse_error="past json deadline")
        return f"past {C.JSON_DEADLINE_CT} CT deadline — no trade today"

    # v1.5.1 guards. A forecast pasted after the show has started is not a forecast, and
    # a re-book after fills exist would delete those orders and re-create them from a
    # different quote. Both were ways a "later" quote could replace the real one.
    if clock.now_ct() > clock._at(date_str, C.SHOW_CANCEL_CT):
        store.log_activity("parse_reject", f"late paste after {C.SHOW_CANCEL_CT} CT ignored")
        return f"too late: past {C.SHOW_CANCEL_CT} CT the show is on. Not booking."
    if any(float(o.get("filled_contracts") or 0) > 0 for o in store.orders_for_date(date_str)):
        store.log_activity("parse_reject", "re-paste ignored: orders already have paper fills")
        return "orders already have paper fills — not re-booking (that would erase them)."

    words = run.get("word_list") or []
    if isinstance(words, str):
        import json as _json
        words = _json.loads(words)
    expected = [w["word"] for w in words]
    word_to_ticker = {w["word"]: w["market_ticker"] for w in words}

    try:
        parsed = parser.validate(raw, expected, event_date=date_str)
    except parser.ParseError as exc:
        store.update_run(
            run["id"],
            raw_response=raw[:20000],
            parse_error=str(exc),
            status="awaiting_json",
        )
        store.log_activity("parse_reject", str(exc))
        return f"rejected: {exc}"

    store.update_run(
        run["id"],
        raw_response=raw[:50000],
        parsed=parsed,
        parse_error=None,
        status="parsed",
        parsed_at=datetime.now(timezone.utc),
        submitted_at=datetime.now(timezone.utc),
    )

    forecast_rows = []
    for f in parsed["forecasts"]:
        forecast_rows.append({
            "word": f["word"],
            "market_ticker": word_to_ticker[f["word"]],
            "probability": f["probability"],
            "p_block_airs": f.get("p_block_airs"),
            "p_said_given_airs": f.get("p_said_given_airs"),
            "carrying_story": f.get("carrying_story"),
            "substitute_risk": f.get("substitute_risk"),
            "other_routes": f.get("other_routes"),
            "reasoning": f.get("reasoning"),
        })
    saved = store.replace_forecasts(
        run["id"], date_str, run["event_ticker"],
        C.HARNESS, C.PROMPT_VERSION, forecast_rows,
    )
    notes: list[str] = []
    booked = book_from_forecasts(run, saved, notes)
    return _book_summary(saved, booked, expected, notes)


def _i(x):
    if x is None or x == "":
        return None
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None


def order_row_from_decision(run: dict, spec: dict, rule: str, sig: dict, decision: dict) -> dict:
    """The one place an order row is assembled (booking and gap/rebook.py both use it)."""
    f = sig["forecast"]
    return {
        "forecast_id": f.get("id"),
        "run_id": run["id"],
        "event_date": str(run["event_date"])[:10],
        "market_ticker": f["market_ticker"],
        "word": f["word"],
        "side": decision["side"],
        "limit_price_cents": decision["yes_price_cents"],
        "our_price_cents": int(decision["our_price_cents"]),
        "contracts": decision["contracts"],
        "cost_cents": decision["cost_cents"],
        "gap_points": decision["gap_points"],
        "threshold": decision["threshold"],
        "cluster_key": sig["cluster_key"],
        "paper": True,
        "status": "paper_sweep",
        "variant_id": spec["id"],
        "exit_rule": spec["exit"],
        "notional_dollars": spec["notional"],
        "execution_model": C.EXECUTION_MODEL,
        "quote_bid_cents": sig["bid"],
        "quote_ask_cents": sig["ask"],
        "quote_captured_at": sig["captured_at"],
        "book_rule": rule,
    }


def book_from_forecasts(run: dict, forecasts: list[dict], notes: list[str] | None = None) -> list[dict]:
    """One decision set, independent books. No shared size or cash.

    v1.5.1: quotes are the FROZEN decision-time quotes (gap/quotes.py). This function
    never reads the live book. A BROKEN quote (bid<=1, ask<=1, bid>=99, bid>ask, or a
    missing side; wide spreads are traded since v1.5.9) means every rule that needs the market skips that
    word; Grok-10 books ignore the market and are not affected.
    What each book books for a word comes from strategy.order_for_rule -- the same
    function the weekly RULE AUDIT uses to check us.
    """
    frozen = quotes.ensure_frozen(run)
    signals = []
    for f in forecasts:
        q = frozen.get(f["market_ticker"]) or {}
        bid, ask = _i(q.get("yes_bid_cents")), _i(q.get("yes_ask_cents"))
        valid = bool(q.get("valid"))
        if not valid and notes is not None:
            notes.append(
                f"INVALID QUOTE {f['word']}: bid {bid} / ask {ask} "
                f"({q.get('invalid_reason') or 'no quote'}) - market-based books skip it"
            )
        signals.append({
            "forecast": f,
            "bid": bid,
            "ask": ask,
            "valid": valid,
            "captured_at": q.get("captured_at"),
            "cluster_key": strategy.cluster_key(f["word"], f.get("carrying_story")),
        })

    from sqlalchemy import text
    date_str = str(run.get("event_date") or "")[:10]
    with store.engine().begin() as conn:
        conn.execute(
            text(
                "delete from gap_settlements where order_id in "
                "(select id from gap_orders where run_id = :id "
                " or event_date = cast(:d as date))"
            ),
            {"id": run["id"], "d": date_str},
        )
        conn.execute(
            text(
                "delete from gap_orders where run_id = :id "
                "or event_date = cast(:d as date)"
            ),
            {"id": run["id"], "d": date_str},
        )

    kept_all: list[dict] = []
    for spec in C.VARIANTS:
        rule = spec.get("rule") or "fade15"
        candidates = []
        for sig in signals:
            f = sig["forecast"]
            decision = strategy.order_for_rule(
                rule, int(f["probability"]), sig["bid"], sig["ask"], sig["valid"], spec["notional"],
            )
            if not decision:
                continue
            candidates.append(order_row_from_decision(run, spec, rule, sig, decision))
        kept = strategy.apply_caps(candidates, notional=spec["notional"])
        for row in kept:
            store.insert_order(row)
        kept_all.extend(kept)
    return kept_all


def _book_summary(saved, booked, expected, notes: list[str] | None = None) -> str:
    store.log_activity(
        "parsed",
        f"{len(saved)} forecasts, {len(booked)} paper rows across {','.join(v['id'] for v in C.VARIANTS)}",
    )
    lines = [
        f"parsed {len(saved)}/{len(expected)}",
        f"|gap|>{C.GAP_THRESHOLD}¢ | take {C.LIMIT_OFFSET_CENTS}¢ from mid | cancel +{C.CANCEL_AFTER_MIN}m",
        f"{len(C.VARIANTS)} books, no shared size:",
    ]
    for spec in C.VARIANTS:
        rows = [o for o in booked if o.get("variant_id") == spec["id"]]
        spent = sum(o["cost_cents"] for o in rows) / 100.0
        n_yes = sum(1 for o in rows if o["side"] == "YES")
        n_no = sum(1 for o in rows if o["side"] == "NO")
        lines.append(
            f"  {spec['id']} ${spec['notional']:.0f} {spec['exit']}: "
            f"{len(rows)} ({n_no} NO / {n_yes} YES) booked ${spent:.2f}"
        )
    lines.append(f"live: {'on' if C.may_place_live() else 'off'}")
    if notes:
        lines.append("")
        lines.append(f"{len(notes)} INVALID QUOTE(S) — no market-based trade on these:")
        lines += notes[:12]
        if len(notes) > 12:
            lines.append(f"... and {len(notes) - 12} more")
    return "\n".join(lines)


def book_waiting_if_due() -> None:
    date_str = clock.today_ct()
    if clock.before_decision(date_str):
        return
    run = store.get_run_for_date(date_str)
    if not run or run.get("status") != "parsed_waiting_decision":
        return
    forecasts = store.forecasts_for_run(run["id"])
    notes: list[str] = []
    booked = book_from_forecasts(run, forecasts, notes)
    store.update_run(run["id"], status="parsed")
    notify.send(_book_summary(forecasts, booked, [f["word"] for f in forecasts], notes))


_FROZEN_SEEN: set[int] = set()


def freeze_if_due() -> None:
    """At the decision time, freeze today's quotes once. (Booking also freezes on demand
    if this never ran, using the same as-of-decision-time lookup, so the answer is the same.)"""
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    if not run or run["id"] in _FROZEN_SEEN:
        return
    dec = clock.parse_dt(run.get("decision_at"))
    if dec is None or clock.now_ct() < dec.astimezone(C.CT):
        return
    out = quotes.freeze_run_quotes(run)
    if out["added"]:
        store.log_activity(
            "quotes_frozen",
            f"{date_str} moment={clock.fmt(out['moment'])} valid={out['valid']} invalid={out['invalid']}",
        )
    _FROZEN_SEEN.add(run["id"])


def fills_tick() -> None:
    """Advance paper fills every poll tick while any order's window is open.
    Before v1.5.1 fills moved ONLY when someone opened the Streamlit page, and then
    only against whatever the book looked like at that instant."""
    if not C.BACKGROUND_FILLS:
        return
    date_str = clock.today_ct()
    orders = store.orders_for_date(date_str)
    if not orders:
        return
    live = [
        o for o in orders
        if fills.window_open(o)
        and float(o.get("filled_contracts") or 0) < float(o.get("contracts") or 0) - 1e-6
    ]
    if not live:
        return
    run = store.get_run_for_date(date_str)
    fills.apply_to_orders(orders, event_ticker=(run or {}).get("event_ticker"))
    settle.sync_gh_fills(date_str)  # H mirrors G, exactly as the board does on a page load


def expire_if_needed() -> None:
    date_str = clock.today_ct()
    if not clock.past_json_deadline(date_str):
        return
    run = store.get_run_for_date(date_str)
    if run and run.get("status") in ("awaiting_json", "detected"):
        store.update_run(run["id"], status="expired", parse_error="past json deadline")
        notify.send(f"{date_str}: JSON deadline passed — no gap trades today.")
        store.log_activity("expired", date_str)


def poll_once() -> dict:
    # v1.5.0: tape.track_tape() removed along with the scalp exit rule (it polled live
    # quotes, the pattern behind every bug in this repo's history).
    # v1.5.1: paper fills advance from fills_tick() below, so they no longer depend on
    # someone having the Streamlit page open. Settlement still runs from board.tonight().
    if clock.is_saturday_ct():
        try:
            from . import weekly
            msg = weekly.send_week_report(force=False)
            return {"ok": True, "reason": "saturday_weekly", "detail": msg}
        except Exception as exc:
            log.exception("weekly")
            return {"ok": False, "reason": "weekly_failed", "error": str(exc)}
    if not clock.weekday_ct():
        return {"ok": True, "reason": "weekend"}
    book_waiting_if_due()
    try:
        freeze_if_due()
    except Exception:
        log.exception("freeze_if_due")
    try:
        fills_tick()
    except Exception:
        log.exception("fills_tick")
    if not clock.in_poll_window() and store.get_run_for_date(clock.today_ct()):
        expire_if_needed()
        return {"ok": True, "reason": "outside_window"}
    if not clock.in_poll_window():
        return {"ok": True, "reason": "outside_window"}
    try:
        maybe_upgrade_event()
    except Exception:
        log.exception("upgrade")
    return send_prompt_for_today()


def register_commands() -> None:
    notify.on_json(ingest_json)

    def _status(_args, _msg):
        run = store.get_run_for_date(clock.today_ct())
        if not run:
            return f"no run today\n{C.summary()}"
        orders = store.orders_for_date(clock.today_ct())
        extra = ""
        if run.get("status") == "detected":
            due = clock.send_due_at(run.get("market_open_at") or run.get("created_at"))
            extra = f"\nsend file at {clock.fmt(due)}"
        return (
            f"{C.summary()}\n\n"
            f"run #{run['id']} {run['event_ticker']}\n"
            f"status={run['status']} markets={run.get('markets_n')}"
            f"{extra}\n"
            f"orders today={len(orders)} (all paper={C.PAPER})"
        )

    def _prep(_args, _msg):
        out = send_prompt_for_today(force=False)
        if not out.get("ok"):
            return f"prep failed: {out.get('reason')}"
        if out.get("reason") == "waiting_send":
            return f"event seen. file waits until {out.get('send_at')}. /gap_sendnow to skip."
        if out.get("reason") in ("already_sent", "already_detected"):
            return f"{out.get('reason')}. /gap_resend or /gap_sendnow to send the file."
        if out.get("reason") == "sent":
            return f"sent {out.get('n')} words — paste into a NEW Expert chat"
        return f"{out.get('reason')} n={out.get('n')}"

    def _resend(_args, _msg):
        out = dispatch_prompt(force=True)
        if not out.get("ok"):
            return f"resend failed: {out.get('reason')}"
        return f"sent {out.get('n')} words — paste into a NEW Expert chat"

    def _sendnow(_args, _msg):
        return _resend(_args, _msg)

    def _pnl(_args, _msg):
        orders = store.orders_for_date(clock.today_ct())
        if not orders:
            return "no paper orders today"
        lines = []
        spent = 0
        for o in orders:
            spent += o.get("cost_cents") or 0
            lines.append(
                f"{o.get('variant_id') or '?'} {o['word']}: {o['side']} "
                f"{o['contracts']} @ {o['limit_price_cents']}c "
                f"gap {o['gap_points']:+.1f}"
            )
        lines.append("")
        for spec in C.VARIANTS:
            rows = [x for x in orders if x.get("variant_id") == spec["id"]]
            s = sum(x.get("cost_cents") or 0 for x in rows)
            lines.append(f"{spec['id']} ${spec['notional']:.0f} {spec['exit']}: {len(rows)}  ${s/100:.2f}")
        return "\n".join(lines)

    notify.register("gap_status", _status)
    notify.register("status", _status)
    notify.register("gap_prep", _prep)
    notify.register("gap_resend", _resend)
    notify.register("gap_sendnow", _sendnow)
    def _settle(_args, _msg):
        start, end, week_id = clock.week_mon_fri()
        from . import settle as settle_mod
        out = settle_mod.settle_range(start, end)
        return f"{week_id} settle {out}"

    def _week(_args, _msg):
        from . import weekly
        return weekly.send_week_report(force=True)

    def _void(args, _msg):
        import re as _re
        if not args or not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[0]):
            return "usage: /gap_void YYYY-MM-DD reason words"
        v = dict(store.get_state("void_nights", {}) or {})
        v[args[0]] = " ".join(args[1:]) or "manual"
        store.set_state("void_nights", v)
        return f"{args[0]} marked VOID ({v[args[0]]}). Weekly dump will label it VOID and leave it out of the books."

    def _unvoid(args, _msg):
        v = dict(store.get_state("void_nights", {}) or {})
        if not args or args[0] not in v:
            return f"void nights: {', '.join(sorted(v)) or 'none'}. usage: /gap_unvoid YYYY-MM-DD"
        v.pop(args[0])
        store.set_state("void_nights", v)
        return f"{args[0]} is no longer void."

    def _quotes(_args, _msg):
        run = store.get_run_for_date(clock.today_ct())
        if not run:
            return "no run today"
        rows = store.frozen_quotes_for_run(run["id"])
        if not rows:
            return "no frozen quotes yet (they freeze at the decision time)"
        bad = [r for r in rows if not r.get("valid")]
        lines = [f"{len(rows)} frozen quotes, {len(rows) - len(bad)} valid, {len(bad)} INVALID"]
        for r in bad[:15]:
            lines.append(f"INVALID {r['market_ticker'].split('-')[-1]}: bid {r.get('yes_bid_cents')} / ask {r.get('yes_ask_cents')} ({r.get('invalid_reason')})")
        return "\n".join(lines)

    notify.register("gap_void", _void)
    notify.register("gap_unvoid", _unvoid)
    notify.register("gap_quotes", _quotes)
    notify.register("gap_pnl", _pnl)
    notify.register("gap_today", _pnl)
    notify.register("gap_settle", _settle)
    notify.register("gap_week", _week)
