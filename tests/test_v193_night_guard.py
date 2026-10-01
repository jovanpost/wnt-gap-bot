"""v1.9.3 tests: once a night has Grok's numbers it can never go back to 'waiting for the JSON'
or 'expired' (Oct 1: a /gap_resend did exactly that, and at 4:30 PM the night was marked expired).
Needs TEST_DATABASE_URL (a THROWAWAY local Postgres).

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from gap import clock, config as C, notify, pipeline, store

DATE = "2026-10-01"
WORDS = [{"word": "Pardon", "market_ticker": "KX-PARD"}, {"word": "SNAP / Food Stamp", "market_ticker": "KX-SNAP"}]
FILE = "SYSTEM PROMPT\n---\nDate: 2026-10-01\n1. Pardon\n2. SNAP / Food Stamp\n"


@pytest.fixture()
def night(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        for t in ("gap_l_orders", "gap_forecasts", "gap_markets", "gap_orders", "gap_runs"):
            try:
                conn.execute(store.text(f"delete from {t}"))
            except Exception:  # noqa: BLE001
                pass
    run = store.insert_run({"event_date": DATE, "event_ticker": "KXWNM-26OCT01", "status": "detected",
                            "prompt_version": "gap-test", "harness": C.HARNESS, "word_list": WORDS,
                            "prompt_text": FILE, "markets_n": 2})
    sent = {"docs": [], "msgs": []}
    monkeypatch.setattr(notify, "send_document", lambda name, body, caption: sent["docs"].append((name, caption)) or 7)
    monkeypatch.setattr(notify, "send", lambda text, **kw: sent["msgs"].append(text))
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    yield run, sent
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


def _parse(run, probs=(5, 6)):
    store.replace_forecasts(run["id"], DATE, run["event_ticker"], C.HARNESS, "gap-test", [
        {"word": w["word"], "market_ticker": w["market_ticker"], "probability": p} for w, p in zip(WORDS, probs)])
    store.update_run(run["id"], status="parsed", parsed_at=datetime.now(timezone.utc))


def test_resend_after_parse_is_read_only(night):
    run, sent = night
    _parse(run)
    out = pipeline.dispatch_prompt(force=True)
    assert out["reason"] == "sent_readonly"
    after = store.get_run_for_date(DATE)
    assert after["status"] == "parsed" and after["prompt_text"] == FILE
    assert sent["docs"][0][0] == "gap-2026-10-01-READ-ONLY.txt" and "Do NOT paste" in sent["docs"][0][1]


def test_todays_exact_accident_cannot_expire_the_night(night, monkeypatch):
    """Oct 1: parsed in the morning, an old-code resend set 'awaiting_json', 4:30 PM expired it."""
    run, sent = night
    _parse(run)
    store.update_run(run["id"], status="awaiting_json")          # what the old resend did
    monkeypatch.setattr(clock, "past_json_deadline", lambda d: True)
    pipeline.expire_if_needed()
    assert store.get_run_for_date(DATE)["status"] == "awaiting_json"   # not 'expired'
    assert not any("deadline passed" in m for m in sent["msgs"])
    out = pipeline.dispatch_prompt(force=True)                     # and a resend stays read-only
    assert out["reason"] == "sent_readonly"


def test_night_without_forecasts_still_expires(night, monkeypatch):
    run, sent = night
    store.update_run(run["id"], status="awaiting_json")
    monkeypatch.setattr(clock, "past_json_deadline", lambda d: True)
    pipeline.expire_if_needed()
    assert store.get_run_for_date(DATE)["status"] == "expired"
    assert any("deadline passed" in m for m in sent["msgs"])


def test_broken_repaste_keeps_the_parsed_night(night, monkeypatch):
    run, _ = night
    _parse(run)
    monkeypatch.setattr(clock, "past_json_deadline", lambda d: False)
    monkeypatch.setattr(clock, "now_ct", lambda: datetime(2026, 10, 1, 14, 0, tzinfo=C.CT))
    monkeypatch.setattr(C, "PAPER", True)
    reply = pipeline.ingest_json("{ this is not json")
    assert "nothing changed" in reply
    after = store.get_run_for_date(DATE)
    assert after["status"] == "parsed"
    assert [f["probability"] for f in store.forecasts_for_run(run["id"])] == [5, 6]


def test_repaste_refused_when_book_l_has_real_orders(night, monkeypatch):
    run, _ = night
    _parse(run)
    with store.engine().begin() as conn:
        conn.execute(store.text("""
            insert into gap_l_orders (event_date, market_ticker, word, side, limit_price_cents, our_price_cents,
                                      contracts, cost_cents, client_order_id, cancel_deadline_at)
            values (cast(:d as date), 'KX-PARD', 'Pardon', 'NO', 20, 80, 12.5, 1000, 'uuid-test', now())"""),
            {"d": DATE})
    monkeypatch.setattr(C, "PAPER", True)
    good = json.dumps({"date": DATE, "forecasts": [{"word": w["word"], "probability": 60, "reasoning": "Blind: x"}
                                                   for w in WORDS]})
    reply = pipeline.ingest_json(good)
    assert "Book L already placed real orders" in reply
    assert [f["probability"] for f in store.forecasts_for_run(run["id"])] == [5, 6]   # untouched


def test_resend_command_reply(night):
    run, _ = night
    _parse(run)
    handlers = {}
    orig = notify.register
    notify.register = lambda name, fn: handlers.__setitem__(name, fn)
    try:
        pipeline.register_commands()
    finally:
        notify.register = orig
    reply = handlers["gap_resend"]([], {})
    assert reply.startswith("read-only copy sent") and "status stays parsed" in reply
