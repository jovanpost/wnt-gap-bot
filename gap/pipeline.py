"""Catch event → Telegram file → parse JSON → quote → paper book."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import clock, config as C, notify, parser, prompt, store, strategy
from .kalshi import (
    KalshiClient,
    event_open_at,
    market_mid_prob,
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
    store.log_activity("prompt_sent", f"{event['event_ticker']} n={len(words)} msg={msg_id}")
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
    booked = book_from_forecasts(run, saved)
    return _book_summary(saved, booked, expected)


def book_from_forecasts(run: dict, forecasts: list[dict]) -> list[dict]:
    """One decision set, eight independent books. No shared size or cash.

    Quote comes from no-fade's `depth` table (same shared Supabase project),
    not a fresh Kalshi call -- see store.latest_nofade_depth. Falls back to
    (None, None) if no-fade hasn't snapshotted this ticker yet, same as a
    failed Kalshi call used to.
    """
    date_str = str(run.get("event_date") or "")[:10]
    signals = []
    for f in forecasts:
        bid = ask = None
        try:
            snap = store.latest_nofade_depth(f["market_ticker"], date_str)
        except Exception as exc:
            log.warning("depth quote %s: %s", f["market_ticker"], exc)
            snap = None
        if snap:
            best_yes_bid = snap.get("best_yes_bid")
            best_no_bid = snap.get("best_no_bid")
            bid = best_yes_bid
            ask = (100 - best_no_bid) if best_no_bid is not None else None
        mid = market_mid_prob(bid, ask)
        store.insert_quote(run["id"], f.get("id"), f["market_ticker"], bid, ask, mid)
        signals.append({
            "forecast": f,
            "bid": bid,
            "ask": ask,
            "mid": mid,
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
        candidates = []
        for sig in signals:
            f = sig["forecast"]
            p = int(f["probability"])
            rule = spec.get("rule") or "fade15"
            if rule == "grok10":
                g = strategy.grok10_limit(p)
                if not g:
                    continue
                our_px = int(g.get("our_price_cents") or strategy.our_price_cents(g["side"], g["yes_price_cents"]))
                contracts = round(spec["notional"] / (our_px / 100.0), 2)
                decision = {
                    "side": g["side"],
                    "yes_price_cents": g["yes_price_cents"],
                    "our_price_cents": our_px,
                    "contracts": contracts,
                    "cost_cents": int(round(contracts * our_px)),
                    "gap_points": 0.0,
                    "threshold": 0,
                }
            else:
                decision = strategy.decide(
                    p, sig["mid"], sig["bid"], sig["ask"],
                    notional=spec["notional"],
                )
                if not decision:
                    continue
                if rule == "fade15_gate50" and not strategy.fade_gate_ok(p, decision["side"]):
                    continue
                if not decision.get("our_price_cents"):
                    decision["our_price_cents"] = strategy.our_price_cents(
                        decision["side"], decision["yes_price_cents"]
                    )
            candidates.append({
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
            })
        kept = strategy.apply_caps(candidates, notional=spec["notional"])
        for row in kept:
            store.insert_order(row)
        kept_all.extend(kept)
    return kept_all


def _book_summary(saved, booked, expected) -> str:
    store.log_activity(
        "parsed",
        f"{len(saved)} forecasts, {len(booked)} paper rows across A-H",
    )
    lines = [
        f"parsed {len(saved)}/{len(expected)}",
        f"|gap|>{C.GAP_THRESHOLD}¢ | take {C.LIMIT_OFFSET_CENTS}¢ from mid | cancel +{C.CANCEL_AFTER_MIN}m",
        "eight books, no shared size:",
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
    return "\n".join(lines)


def book_waiting_if_due() -> None:
    date_str = clock.today_ct()
    if clock.before_decision(date_str):
        return
    run = store.get_run_for_date(date_str)
    if not run or run.get("status") != "parsed_waiting_decision":
        return
    forecasts = store.forecasts_for_run(run["id"])
    booked = book_from_forecasts(run, forecasts)
    store.update_run(run["id"], status="parsed")
    notify.send(_book_summary(forecasts, booked, [f["word"] for f in forecasts]))


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
    # v1.5.0: tape.track_tape() removed along with the scalp exit rule.
    # It polled live quotes to decide whether an early exit had "hit" --
    # exactly the live-quote-dependent pattern that caused every bug found
    # in this repo's history. Nothing left needs a 60s tape poll; fills
    # (apply_to_orders) and settlement (settle.apply_official) run from
    # board.tonight() on each page load instead.
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

    notify.register("gap_pnl", _pnl)
    notify.register("gap_today", _pnl)
    notify.register("gap_settle", _settle)
    notify.register("gap_week", _week)
