"""v1.16.0 tests: the bot runs in ONE place only (Streamlit Cloud or the server).

  - only one place holds the worker lease; a second one waits and starts nothing
  - RUN_WORKERS=false starts nothing at all (dashboard only)
  - without the lease: no loop tick, no Telegram listening, no real order, no claimed L row,
    no "armed" flag, and the page writes nothing
  - a database hiccup pauses new orders but is not a loss; a real loss stops this place
  - the headless worker starts the same loops as the Streamlit app
  - paper settlement runs from the poll loop, not only when someone opens the page

Needs TEST_DATABASE_URL (a THROWAWAY local Postgres). No network, no Telegram, no Kalshi key.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from gap import board, clock, config as C, kalshi, lease, live, notify, pipeline, runtime, settle, store

ROOT = Path(__file__).resolve().parents[1]
NAME = runtime.NAME
DATE = "2026-10-05"


def wait_for(cond, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def _pg_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture()
def db(monkeypatch):
    """The bot's own store pointed at the throwaway Postgres, lease row cleared, runtime reset."""
    url = _pg_url()
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    monkeypatch.delenv("RUN_WORKERS", raising=False)
    monkeypatch.delenv("KLSH_HOST", raising=False)
    store.init_db()
    runtime.reset_for_tests()
    eng = store.engine()
    lease.ensure_table(eng)
    with eng.begin() as conn:
        conn.execute(text("delete from public.worker_leases"))
        for stmt in ("delete from gap_l_orders", "delete from gap_forecasts", "delete from gap_markets",
                     "delete from gap_orders", "delete from gap_runs",
                     "delete from gap_state where key like 'l_armed:%'"):
            try:
                with conn.begin_nested():
                    conn.execute(text(stmt))
            except Exception:  # noqa: BLE001
                pass
    sent: list = []
    monkeypatch.setattr(notify, "send", lambda text_, **kw: sent.append(text_))
    yield eng, sent
    runtime.reset_for_tests()
    eng.dispose()
    monkeypatch.setattr(store, "_engine", None)


@pytest.fixture()
def engines(tmp_path):
    out = {"sqlite": create_engine(f"sqlite:///{tmp_path / 'lease.db'}", future=True)}
    if os.environ.get("TEST_DATABASE_URL"):
        out["postgres"] = create_engine(
            os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+psycopg2://", 1), future=True)
    yield out
    for e in out.values():
        e.dispose()


# ------------------------------------------------------------------ 1) the lease table itself
def test_lease_rules_on_sqlite_and_postgres(engines):
    t = "public.worker_leases_test"
    for label, eng in engines.items():
        with eng.begin() as conn:
            conn.execute(text("drop table if exists %s" % lease._t(eng, t)))
        lease.ensure_table(eng, t)
        lease.ensure_table(eng, t)
        assert lease.acquire(eng, "bot", "streamlit", t), label
        assert lease.acquire(eng, "bot", "streamlit", t), label          # same place, restart
        assert not lease.acquire(eng, "bot", "vps", t), label            # second place refused
        assert lease.renew(eng, "bot", "streamlit", t), label
        assert not lease.renew(eng, "bot", "vps", t), label              # a renew never takes it
        cur = lease.current(eng, "bot", t)
        assert cur["holder"] == "streamlit" and cur["age_s"] < 5, (label, cur)
        assert lease.acquire(eng, "other-bot", "vps", t), label          # leases are per bot
        assert lease.acquire(eng, "bot", "vps", t, stale_s=0), label     # silent holder replaced
        assert not lease.renew(eng, "bot", "streamlit", t), label
        assert not lease.acquire(eng, "bot", "streamlit", t), label
        lease.release(eng, "bot", "streamlit", t)                        # not the holder: no effect
        assert lease.current(eng, "bot", t)["holder"] == "vps", label
        lease.release(eng, "bot", "vps", t)
        assert lease.current(eng, "bot", t) is None, label
        if lease._pg(eng):
            with eng.connect() as conn:
                assert conn.execute(text("select relrowsecurity from pg_class where oid = to_regclass(:t)"),
                                    {"t": t}).scalar() is True
        with eng.begin() as conn:
            conn.execute(text("drop table if exists %s" % lease._t(eng, t)))


# ------------------------------------------------------------------ 2) keeper and gate
def test_no_lease_in_use_means_scripts_and_tests_work():
    runtime.reset_for_tests()
    assert lease.running() and lease.may_trade()
    lease.require("send a real order")


def test_database_hiccup_pauses_orders_but_is_not_a_loss(db, monkeypatch):
    eng, _ = db
    lost: list = []
    k = lease.Keeper(eng, NAME, "streamlit", on_lost=lost.append, renew_s=0.05)
    assert k.try_acquire() and lease.GATE.mode == "held" and lease.may_trade()
    first = lease.GATE.last_ok
    assert wait_for(lambda: lease.GATE.last_ok > first)                  # renew thread works

    real = lease.renew
    monkeypatch.setattr(lease, "renew", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database down")))
    time.sleep(0.2)
    lease.GATE.last_ok = time.monotonic() - lease.SAFE_S - 1             # the outage lasted SAFE_S
    assert not lease.may_trade()
    assert lease.running() and lease.GATE.mode == "held" and not lost    # NOT a loss
    with pytest.raises(lease.LeaseError):
        lease.require("send a real order")
    monkeypatch.setattr(lease, "renew", real)
    assert wait_for(lease.may_trade)                                     # comes back by itself
    k.stop(give_up=True)
    assert lease.current(eng, NAME) is None


def test_another_holder_is_a_loss_and_is_final(db):
    eng, _ = db
    lost: list = []
    k = lease.Keeper(eng, NAME, "streamlit", on_lost=lost.append, renew_s=0.05)
    assert k.try_acquire()
    with eng.begin() as conn:
        conn.execute(text("update public.worker_leases set holder = 'vps', heartbeat = now() where name = :n"), {"n": NAME})
    assert wait_for(lambda: len(lost) == 1)
    assert "vps" in lost[0]
    assert lease.GATE.mode == "lost" and not lease.running() and not lease.may_trade()
    time.sleep(0.2)
    assert lease.current(eng, NAME)["holder"] == "vps" and len(lost) == 1   # never takes it back
    k.stop(give_up=True)
    assert lease.current(eng, NAME)["holder"] == "vps"                      # stop() leaves vps alone


def test_keeper_survives_a_database_error_while_waiting(db, monkeypatch):
    eng, _ = db
    k = lease.Keeper(eng, NAME, "vps", renew_s=0.05)
    real = lease.acquire
    monkeypatch.setattr(lease, "acquire", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no route")))
    assert k.try_acquire() is False and lease.GATE.mode == "waiting" and "no route" in k.last_error
    monkeypatch.setattr(lease, "acquire", real)
    assert k.try_acquire() is True
    k.stop(give_up=True)


# ------------------------------------------------------------------ 3) without the lease nothing acts
@pytest.mark.parametrize("mode", ["dashboard", "waiting", "lost"])
def test_real_order_refused_before_any_request(mode):
    runtime.reset_for_tests()
    sent: list = []
    client = kalshi.KalshiClient.__new__(kalshi.KalshiClient)
    client.request = lambda *a, **kw: sent.append(a) or {"order": {"order_id": "o1"}}
    lease.GATE.set(mode)
    with pytest.raises(lease.LeaseError):
        client.create_no_order("T-1", 80, 1, "coid-1")
    assert sent == []
    lease.GATE.reset()
    client.create_no_order("T-1", 80, 1, "coid-1")                      # scripts keep working
    assert len(sent) == 1


def test_l_orders_not_claimed_and_not_armed_without_the_lease(db, monkeypatch):
    eng, _ = db
    run = store.insert_run({"event_date": DATE, "event_ticker": "KXWNM-26OCT05", "status": "parsed",
                            "prompt_version": "gap-test", "harness": C.HARNESS,
                            "word_list": [{"word": "Pardon", "market_ticker": "KX-PARD"}],
                            "prompt_text": "x", "markets_n": 1})
    take = [{"forecast": {"id": None, "word": "Pardon", "market_ticker": "KX-PARD", "probability": 5},
             "decision": {"side": "NO", "yes_price_cents": 10, "our_price_cents": 90, "contracts": 5.0,
                          "cost_cents": 450, "gap_points": None}}]

    class Client:
        calls: list = []

        def create_no_order(self, **kw):
            self.calls.append(kw)
            return {"order_id": "o1"}

    lease.GATE.set("lost", other="vps")
    out = live._fire_l_orders(Client(), run, DATE, take, 5, 1)
    assert out == {"ok": False, "reason": "no_lease"}
    assert Client.calls == []
    assert not store.l_order_exists(DATE, "KX-PARD")                    # no row claimed
    assert store.get_state(live._armed_key(DATE)) is None               # night not marked armed

    lease.GATE.reset()                                                  # the rightful place: all normal
    out = live._fire_l_orders(Client(), run, DATE, take, 5, 1)
    assert out["ok"] and out["placed"] == ["Pardon"] and len(Client.calls) == 1
    assert store.l_order_exists(DATE, "KX-PARD")


class _Done(Exception):
    pass


def _three_sleeps(seen: list):
    def sleep(s):
        seen.append(s)
        if len(seen) >= 3:
            raise _Done()
    return sleep


def test_poll_loops_do_nothing_without_the_lease(monkeypatch):
    runtime.reset_for_tests()
    polls, fast, settles, slept = [], [], [], []
    monkeypatch.setattr(pipeline, "poll_once", lambda: polls.append(1))
    monkeypatch.setattr(live, "fast_arm_watch_tick", lambda: fast.append(1))
    monkeypatch.setattr(runtime, "paper_settle_tick", lambda: settles.append(1))
    monkeypatch.setattr(runtime.time, "sleep", _three_sleeps(slept))
    lease.GATE.set("lost")
    with pytest.raises(_Done):
        runtime._poll_loop()
    slept.clear()
    with pytest.raises(_Done):
        runtime._l_fast_watch_loop()
    assert polls == [] and fast == [] and settles == []
    lease.GATE.reset()
    slept.clear()
    with pytest.raises(_Done):
        runtime._poll_loop()
    slept.clear()
    with pytest.raises(_Done):
        runtime._l_fast_watch_loop()
    assert len(polls) == 3 and len(settles) == 3 and len(fast) == 3
    assert slept == [runtime.FAST_S] * 3 and runtime.POLL_S == 30 and runtime.FAST_S == 1   # same pace as before


def test_telegram_listener_is_silent_without_the_lease(db, monkeypatch):
    calls, slept = [], []
    monkeypatch.setattr(notify.requests, "get", lambda *a, **kw: calls.append(a) or (_ for _ in ()).throw(_Done()))
    monkeypatch.setattr(notify.time, "sleep", _three_sleeps(slept))
    lease.GATE.set("lost")
    with pytest.raises(_Done):
        notify._listen()
    assert calls == [] and slept == [5, 5, 5]


def test_page_writes_nothing_when_dashboard_only(db, monkeypatch):
    applied, settled = [], []
    monkeypatch.setattr(board.fills, "apply_to_orders", lambda orders, **kw: applied.append(1))
    monkeypatch.setattr(settle, "apply_official", lambda d: settled.append(d))
    monkeypatch.setattr(board, "enrich_orders", lambda orders, results=None: [])
    for mode in ("dashboard", "waiting", "lost"):
        lease.GATE.set(mode)
        board.tonight(DATE)
    assert applied == [] and settled == []
    lease.GATE.reset()
    board.tonight(DATE)                                                 # where the workers run: as before
    assert applied == [1] and settled == [DATE]


def test_paper_settlement_runs_from_the_poll_loop(db, monkeypatch):
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    open_days = {DATE: [{"status": "paper_booked"}], "2026-10-02": [{"status": "working"}],
                 "2026-10-03": [{"status": "settled"}], "2026-09-25": [{"status": "paper_booked"}]}
    monkeypatch.setattr(store, "orders_for_date", lambda d: open_days.get(d, []))
    settled: list = []
    monkeypatch.setattr(settle, "apply_official", lambda d: settled.append(d))
    assert runtime.paper_settle_tick() == [DATE, "2026-10-02"]           # today + a recent open day
    assert settled == [DATE, "2026-10-02"]                               # settled-only and old days skipped
    assert runtime.paper_settle_tick() == []                             # not again within SETTLE_EVERY_S
    assert runtime.paper_settle_tick(force=True) == [DATE, "2026-10-02"]
    monkeypatch.setattr(settle, "apply_official", lambda d: (_ for _ in ()).throw(RuntimeError("kalshi down")))
    assert runtime.paper_settle_tick(force=True) == []                   # an error never escapes the tick


def test_unknown_telegram_command_gets_an_answer(monkeypatch):
    monkeypatch.setattr(notify, "_handlers", {"gap_status": lambda a, m: "ok"})
    assert notify._dispatch_command("/gap_statsu", {}) == "unknown command /gap_statsu, send /help"
    assert notify._dispatch_command("/gap_status", {}) == "ok"
    assert "gap bot commands" in notify._dispatch_command("/help", {})


# ------------------------------------------------------------------ 4) the entry points
@pytest.fixture()
def stubbed(db, monkeypatch):
    eng, sent = db
    started, listener = [], []
    monkeypatch.setattr(runtime, "_poll_loop", lambda: started.append("poll"))
    monkeypatch.setattr(runtime, "_l_fast_watch_loop", lambda: started.append("fast"))
    monkeypatch.setattr(notify, "start_listener", lambda: listener.append(1))
    monkeypatch.setattr(runtime, "WAIT_S", 0.05)
    monkeypatch.setattr(notify, "_handlers", {})
    return eng, sent, started, listener


def test_run_workers_false_starts_nothing(stubbed, monkeypatch):
    eng, sent, started, listener = stubbed
    monkeypatch.setenv("RUN_WORKERS", "false")
    info = runtime.start_workers(where="streamlit")
    time.sleep(0.2)
    assert info["run_workers"] is False
    assert started == [] and listener == [] and notify._handlers == {}
    assert lease.GATE.mode == "dashboard" and lease.current(eng, NAME) is None
    level, txt = runtime.banner()
    assert level == "info" and "Dashboard only" in txt and "nobody" in txt


def test_second_place_waits_then_takes_over(stubbed, monkeypatch):
    eng, sent, started, listener = stubbed
    lease.acquire(eng, NAME, "streamlit")                                # Streamlit runs the bot
    monkeypatch.setenv("KLSH_HOST", "vps")
    runtime.start_workers(where="vps")
    time.sleep(0.3)
    assert started == [] and listener == [] and lease.GATE.mode == "waiting"
    level, txt = runtime.banner()
    assert level == "warning" and "streamlit" in txt
    assert lease.current(eng, NAME)["holder"] == "streamlit"
    lease.release(eng, NAME, "streamlit")                                # Streamlit goes dashboard-only
    assert wait_for(lambda: sorted(started) == ["fast", "poll"])
    assert listener == [1] and lease.current(eng, NAME)["holder"] == "vps"
    assert any("vps" in t and C.VERSION in t for t in sent)
    runtime.start_workers(where="vps")                                   # idempotent
    assert sorted(started) == ["fast", "poll"]
    monkeypatch.setattr(clock, "today_ct", lambda: "2031-01-01")
    reply = notify._handlers["gap_status"]([], {})
    assert reply.endswith("host: vps · %s" % C.VERSION)
    runtime.stop()
    assert lease.current(eng, NAME) is None


def test_streamlit_default_name_and_blocking_start(stubbed):
    eng, sent, started, listener = stubbed
    info = runtime.start_workers(where="streamlit", block=True)
    assert wait_for(lambda: sorted(started) == ["fast", "poll"])
    assert info["host"] == "streamlit" and lease.current(eng, NAME)["holder"] == "streamlit"
    assert runtime.banner()[0] == "ok"


def test_lost_lease_tells_telegram(stubbed):
    eng, sent, started, listener = stubbed
    runtime.INFO["host"] = "streamlit"
    runtime._on_lost("vps (3s ago)")
    assert any("LOST the worker lease to vps" in t for t in sent)


# ------------------------------------------------------------------ 5) worker.py and the page, in real processes
WORKER = """
import sys, time
sys.argv = ["worker.py"]
from gap import config as C, notify, runtime
assert C.DATABASE_URL == sys.stdin.readline().strip(), "refusing: not the throwaway test database"
runtime._poll_loop = lambda: time.sleep(3600)
runtime._l_fast_watch_loop = lambda: time.sleep(3600)
notify.start_listener = lambda: None
runtime.WAIT_S = 0.5
import worker
sys.exit(worker.main())
"""


def _worker(env, url):
    p = subprocess.Popen([sys.executable, "-c", WORKER], cwd=ROOT, env=env, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdin.write(url + "\n")
    p.stdin.flush()
    return p


def test_worker_py_waits_takes_over_and_stops_cleanly(db):
    eng, _ = db
    url = C.DATABASE_URL
    env = dict(os.environ, DATABASE_URL=url, TELEGRAM_TOKEN="", PYTHONUNBUFFERED="1")
    env.pop("RUN_WORKERS", None)
    p1 = _worker(dict(env, KLSH_HOST="box1"), url)
    assert wait_for(lambda: (lease.current(eng, NAME) or {}).get("holder") == "box1", 30)
    p2 = _worker(dict(env, KLSH_HOST="vps"), url)
    time.sleep(3)
    assert lease.current(eng, NAME)["holder"] == "box1"                  # p2 is waiting
    p1.terminate()
    out1 = p1.communicate(timeout=30)[0]
    assert p1.returncode == 0, out1[-600:]
    assert f"{C.VERSION} worker starting on box1" in out1 and "workers started on box1" in out1
    assert wait_for(lambda: (lease.current(eng, NAME) or {}).get("holder") == "vps", 30)
    p2.terminate()
    out2 = p2.communicate(timeout=30)[0]
    assert p2.returncode == 0, out2[-600:]
    assert "is held by box1" in out2 and "workers started on vps" in out2
    assert out2.index("is held by box1") < out2.index("workers started on vps")
    assert lease.current(eng, NAME) is None                              # SIGTERM gave the lease back

    for extra, want in ((dict(RUN_WORKERS="false"), "RUN_WORKERS is false"),
                        (dict(KLSH_HOST="streamlit"), "belongs to the Streamlit app")):
        p = _worker(dict(env, **extra), url)
        out = p.communicate(timeout=60)[0]
        assert p.returncode == 2 and want in out, out[-400:]
    assert lease.current(eng, NAME) is None


RENDER = """
import sys, time
from gap import config as C, notify, runtime
assert C.DATABASE_URL == sys.stdin.readline().strip(), "refusing: not the throwaway test database"
runtime._poll_loop = lambda: time.sleep(3600)
runtime._l_fast_watch_loop = lambda: time.sleep(3600)
notify.start_listener = lambda: None
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("streamlit_app.py", default_timeout=180)
at.run()
if at.exception:
    print("EXC", [e.value for e in at.exception]); sys.exit(1)
print("INFO", [i.value for i in at.info])
"""


@pytest.mark.parametrize("mode", ["false", "true"])
def test_dashboard_renders_in_both_modes(db, mode):
    pytest.importorskip("streamlit")
    url = C.DATABASE_URL
    env = dict(os.environ, DATABASE_URL=url, TELEGRAM_TOKEN="", RUN_WORKERS=mode, KLSH_HOST="streamlit")
    r = subprocess.run([sys.executable, "-c", RENDER], cwd=ROOT, env=env, input=url + "\n",
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]
    assert ("Dashboard only" in r.stdout) == (mode == "false"), r.stdout[-400:]
