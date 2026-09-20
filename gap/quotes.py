"""Decision-time quotes: frozen once, validated, never refreshed.

Why this exists (W38 review): the quote used to be read from no-fade's `depth`
table at the moment the JSON was parsed, using the LATEST snapshot. If booking ran
again later (re-paste, re-run) it silently re-read a later, collapsed post-close
book (bid 99 / ask 1 on words that then said YES, bid 0 / ask 1 on words that
said NO) and re-created the orders from that.

Now:
  * ONE moment per run = the decision time (run.decision_at). If the JSON is booked
    even earlier (file force-sent), the moment is that earlier time.
  * The quote is the newest depth snapshot AT OR BEFORE that moment and no more
    than QUOTE_MAX_AGE_S older than it. Same answer no matter when you ask.
  * It is saved once (raw bid, raw ask, captured_at) in gap_quotes with frozen=true.
    A unique index makes overwriting impossible.
  * A quote that fails validate() is INVALID: no mid, and every rule that needs the
    market skips that word.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import clock, config as C, store

log = logging.getLogger("gap.quotes")

MAX_SPREAD_CENTS = 25
SOURCE = "depth_asof_decision"


def _int(x):
    if x is None or x == "":
        return None
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return None


def validate(bid, ask) -> tuple[bool, str | None]:
    """(is_valid, reason). Invalid: bid<=1, ask<=1, bid>=99, bid>ask, spread>25,
    or either side missing."""
    bid, ask = _int(bid), _int(ask)
    if bid is None and ask is None:
        return False, "no quote"
    if bid is None:
        return False, "no bid"
    if ask is None:
        return False, "no ask"
    reasons = []
    if bid <= 1:
        reasons.append("bid<=1")
    if ask <= 1:
        reasons.append("ask<=1")
    if bid >= 99:
        reasons.append("bid>=99")
    if bid > ask:
        reasons.append("bid>ask")
    if ask - bid > MAX_SPREAD_CENTS:
        reasons.append(f"spread>{MAX_SPREAD_CENTS}")
    if reasons:
        return False, ",".join(reasons)
    return True, None


def moment_for(run: dict, now: datetime | None = None) -> datetime:
    """The one moment a run's quotes are frozen at: the decision time, or earlier
    if we are booking before it (file was force-sent)."""
    now = now or clock.now_ct().astimezone(timezone.utc)
    dec = clock.parse_dt(run.get("decision_at"))
    if dec is None:
        return now
    return min(dec, now)


def build_row(run_id: int, ticker: str, snap: dict | None, moment: datetime,
              decision_at: datetime | None) -> dict:
    bid = ask = None
    captured = None
    age = None
    if snap:
        bid = _int(snap.get("best_yes_bid"))
        nb = _int(snap.get("best_no_bid"))
        ask = (100 - nb) if nb is not None else None
        captured = snap.get("ts")
        if captured is not None:
            try:
                age = int((moment - captured).total_seconds())
            except TypeError:
                age = None
    valid, reason = validate(bid, ask)
    if not snap:
        reason = f"no depth snapshot within {C.QUOTE_MAX_AGE_S}s before decision"
    return {
        "run_id": run_id,
        "market_ticker": ticker,
        "yes_bid_cents": bid,
        "yes_ask_cents": ask,
        # No fake mid: an invalid quote has NO market probability.
        "market_prob": ((bid + ask) / 2.0 / 100.0) if valid else None,
        "source": SOURCE,
        "captured_at": captured,
        "decision_at": decision_at,
        "valid": bool(valid),
        "invalid_reason": reason,
        "age_s": age,
    }


def freeze_run_quotes(run: dict, now: datetime | None = None) -> dict:
    """Freeze quotes for every market of the run that has none yet. Idempotent."""
    now = now or clock.now_ct().astimezone(timezone.utc)
    run_id = int(run["id"])
    date_str = str(run.get("event_date"))[:10]
    moment = moment_for(run, now)
    decision_at = clock.parse_dt(run.get("decision_at"))
    have = {q["market_ticker"] for q in store.frozen_quotes_for_run(run_id)}
    added = valid = invalid = 0
    for m in store.markets_for_run(run_id):
        t = m["market_ticker"]
        if t in have:
            continue
        try:
            snap = store.latest_nofade_depth(t, date_str, as_of=moment, max_age_s=C.QUOTE_MAX_AGE_S)
        except Exception as exc:
            log.warning("depth lookup %s: %s", t, exc)
            snap = None
        row = build_row(run_id, t, snap, moment, decision_at)
        store.insert_frozen_quote(row)
        try:  # keep the full book at the decision moment (the shared depth table gets pruned)
            store.insert_decision_book(run_id, t, decision_at or moment, snap, "freeze")
        except Exception as exc:
            log.warning("decision book %s: %s", t, exc)
        added += 1
        valid += 1 if row["valid"] else 0
        invalid += 0 if row["valid"] else 1
    return {"added": added, "valid": valid, "invalid": invalid, "moment": moment}


def ensure_frozen(run: dict, now: datetime | None = None) -> dict[str, dict]:
    """{market_ticker: frozen quote row}. Freezes first if anything is missing."""
    rows = store.frozen_quotes_for_run(int(run["id"]))
    have = {q["market_ticker"] for q in rows}
    markets = {m["market_ticker"] for m in store.markets_for_run(int(run["id"]))}
    if not markets.issubset(have):
        freeze_run_quotes(run, now)
        rows = store.frozen_quotes_for_run(int(run["id"]))
    return {q["market_ticker"]: q for q in rows}
