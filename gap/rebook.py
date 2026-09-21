"""Re-book a night as if today's rules had been in place at booking time.

Use case (v1.5.9): a night was booked while a rule (for example the old spread>25 rule) skipped
some words. After the rule changes, this adds ONLY the orders that are missing, judged on the
FROZEN decision-time quotes, stamped with the ORIGINAL booking time, and fills them by replaying
the stored order-book history from that moment (same no-double-counting rule as the live fill model).

Safe by design:
  * never edits, deletes or re-fills an order that already exists
  * never touches forecasts, results, or a frozen quote's bid/ask/captured_at (only its valid flag and mid)
  * dry-run unless apply=True
  * running it twice adds nothing the second time
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import clock, config as C, fills, lab, pipeline, quotes, store, strategy

log = logging.getLogger("gap.rebook")


def _i(x):
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def _until(spec: dict, placed: datetime, date_str: str, now: datetime) -> datetime:
    if spec.get("cancel") == "show529":
        y, m, d = [int(x) for x in date_str.split("-")]
        end = datetime(y, m, d, 17, 29, tzinfo=C.CT).astimezone(timezone.utc)
    else:
        end = placed + timedelta(minutes=C.CANCEL_AFTER_MIN)
    return min(end, now)


def rebook_missing(date_str: str, apply: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    run = store.get_run_for_date(date_str)
    if not run:
        return {"error": f"no run for {date_str}"}
    frozen = {q["market_ticker"]: q for q in store.frozen_quotes_for_run(run["id"])}
    if not frozen:
        return {"error": "this night has no frozen quotes (nights before v1.5.1 cannot be re-booked)"}
    forecasts = store.forecasts_for_run(run["id"])
    orders = store.orders_for_run(run["id"])
    if not orders:
        return {"error": "no orders on this night: use the normal booking, not a re-book"}
    have = {(o["variant_id"], o["market_ticker"]) for o in orders}
    booked_at = min(o["placed_at"] for o in orders if o.get("placed_at") is not None)

    # 1) re-judge frozen quotes under today's rule
    rejudged = []
    for t, q in list(frozen.items()):
        bid, ask = _i(q.get("yes_bid_cents")), _i(q.get("yes_ask_cents"))
        if bid is None or ask is None:
            continue
        valid, reason = quotes.validate(bid, ask)
        if bool(q.get("valid")) != bool(valid):
            mid = (bid + ask) / 2.0 / 100.0 if valid else None
            rejudged.append({"ticker": t, "bid": bid, "ask": ask, "was": q.get("invalid_reason"), "valid": bool(valid), "reason": reason})
            if apply:
                store.update_frozen_quote(run["id"], t, valid, reason, mid)
            frozen[t] = {**q, "valid": bool(valid), "invalid_reason": reason}

    # 2) which orders would exist now, that do not exist yet
    rows = []
    for spec in C.VARIANTS:
        rule = spec.get("rule") or "fade15"
        for f in forecasts:
            q = frozen.get(f["market_ticker"]) or {}
            sig = {"forecast": f, "bid": _i(q.get("yes_bid_cents")), "ask": _i(q.get("yes_ask_cents")),
                   "valid": bool(q.get("valid")), "captured_at": q.get("captured_at"),
                   "cluster_key": strategy.cluster_key(f["word"], f.get("carrying_story"))}
            decision = strategy.order_for_rule(rule, int(f["probability"]), sig["bid"], sig["ask"], sig["valid"], spec["notional"])
            if not decision or (spec["id"], f["market_ticker"]) in have:
                continue
            rows.append((spec, pipeline.order_row_from_decision(run, spec, rule, sig, decision)))

    report = {"date": date_str, "run_id": run["id"], "booked_at": booked_at, "rejudged": rejudged, "added": [], "applied": apply}
    for spec, row in rows:
        until = _until(spec, booked_at, date_str, now)
        item = {"book": spec["id"], "word": row["word"], "side": row["side"], "limit_yes": row["limit_price_cents"],
                "our_px": row["our_price_cents"], "contracts": row["contracts"], "gap": row["gap_points"],
                "bid": row["quote_bid_cents"], "ask": row["quote_ask_cents"], "filled": None, "note": ""}
        replay_order = {"placed_at": booked_at, "ticker": row["market_ticker"], "date": date_str,
                        "yes_ticket": row["limit_price_cents"], "intended": row["contracts"], "side": row["side"], "outcome": None}
        r = lab._replay_one(replay_order, until)
        if r is None:
            item["note"] = "could not replay"
        elif r.get("note"):
            item["note"] = r["note"]
        else:
            item["filled"] = r["filled"]
        if apply:
            store.insert_order(row)
            new = [o for o in store.orders_for_run(run["id"])
                   if o["variant_id"] == spec["id"] and o["market_ticker"] == row["market_ticker"]]
            if new:
                oid = new[-1]["id"]
                store.set_order_placed_at(oid, booked_at)
                if r and not r.get("note"):
                    store.update_order(oid, filled_contracts=round(r["filled"], 4))
                    store.set_state(fills.CREDIT_KEY.format(oid=oid), {"last": r["last"], "credit": r["credit"]})
                item["order_id"] = oid
        report["added"].append(item)
    if apply and report["added"]:
        store.log_activity("rebook", f"{date_str}: added {len(report['added'])} orders, re-judged {len(rejudged)} quotes")
    return report


def format_report(rep: dict) -> str:
    if rep.get("error"):
        return "ERROR: " + rep["error"]
    lines = [f"{rep['date']}  original booking time {clock.fmt(rep['booked_at'])}   [{'APPLIED' if rep['applied'] else 'DRY RUN - nothing changed'}]"]
    lines.append(f"quotes re-judged under today's rule: {len(rep['rejudged'])}")
    for r in rep["rejudged"]:
        lines.append(f"   {r['ticker'].split('-')[-1]:<8} {r['bid']}/{r['ask']}  was: {r['was']}  now: valid")
    lines.append(f"orders that would exist now but did not: {len(rep['added'])}")
    for a in rep["added"]:
        fl = "n/a" if a["filled"] is None else f"{a['filled']:.2f}"
        lines.append(f"   [{a['book']}] {a['word']:<28} {a['side']:<3} limit YES {a['limit_yes']}c  our px {a['our_px']}c  want {a['contracts']:.2f}  "
                     f"quote {a['bid']}/{a['ask']}  gap {a['gap']:+.1f}  replayed fill {fl} {a['note']}")
    return "\n".join(lines)
