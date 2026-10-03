"""Start the bot's background work in exactly ONE place: Streamlit Cloud or the server.

Both entry points call start_workers():
  streamlit_app.py  -> start_workers(where="streamlit")            never blocks the page
  worker.py         -> start_workers(where="vps", block=True)      headless, run by systemd

RUN_WORKERS = "false" (a setting, default true) makes a place dashboard-only: the tables are
checked, nothing else starts. The worker lease (gap/lease.py) makes sure only one place runs the
loops even when both have RUN_WORKERS on: the second one waits.

The two loops live here (they used to live in streamlit_app.py) so both entry points share them.
The paper settlement that used to run only when someone opened the page (board.tonight) now also
runs from the poll loop, so it does not depend on a page being open.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, timedelta

from . import clock, config as C, lease, live, notify, pipeline, store

log = logging.getLogger("gap.runtime")

NAME = "wnt-gap-bot"
WAIT_S = 10                      # how often a waiting place looks at the lease again
POLL_S = 30                      # pipeline.poll_once every this many seconds (unchanged)
FAST_S = 1                       # live.fast_arm_watch_tick every this many seconds (unchanged)
SETTLE_EVERY_S = 300             # paper settlement check
SETTLE_BACK_DAYS = 4             # also look at this many earlier days that still have open paper orders

_lock = threading.Lock()
_started = False
_keeper: lease.Keeper | None = None
_exit_on_lost = False
_settle = {"last": None}
INFO = {"where": "", "host": "", "run_workers": True, "loops": []}


def host(where: str = "") -> str:
    """Who we are in the lease: the KLSH_HOST setting, else the entry point's own name."""
    return (C._secret("KLSH_HOST", "") or where or "unknown").strip()


def run_workers() -> bool:
    return C._flag("RUN_WORKERS", True)


# ---------------------------------------------------------------- the work
def paper_settle_tick(force: bool = False) -> list:
    """Mark paper orders against Kalshi's official results: today, plus recent days that still
    have open paper orders. Same call the page makes (settle.apply_official). Never sends orders."""
    from . import board, settle
    now = time.monotonic()
    if not force and _settle["last"] is not None and now - _settle["last"] < SETTLE_EVERY_S:
        return []
    _settle["last"] = now
    today = date.fromisoformat(clock.today_ct())
    done = []
    for back in range(SETTLE_BACK_DAYS + 1):
        d = (today - timedelta(days=back)).isoformat()
        try:
            if not any(str(o.get("status") or "") in board.OPEN for o in store.orders_for_date(d)):
                continue
            settle.apply_official(d)
            done.append(d)
        except Exception:
            log.exception("paper settle %s", d)
    return done


def _poll_loop() -> None:
    while True:
        if lease.running():          # False = another place holds the worker lease: do nothing here
            try:
                pipeline.poll_once()
            except Exception:
                logging.getLogger("gap.poll").exception("poll_once")
            try:
                paper_settle_tick()
            except Exception:
                logging.getLogger("gap.poll").exception("paper_settle_tick")
        time.sleep(POLL_S)


def _l_fast_watch_loop() -> None:
    # Book L's real-money fast path, its own thread. Mostly a no-op DB read; only starts hitting
    # Kalshi once tonight's real open_time is close (see live.fast_arm_watch_tick).
    while True:
        if lease.running():
            try:
                live.fast_arm_watch_tick()
            except Exception:
                logging.getLogger("gap.l_fast").exception("fast_arm_watch_tick")
        time.sleep(FAST_S)


def _tag_status_commands() -> None:
    """Every /..._status reply ends with the place it came from."""
    for name, fn in list(notify._handlers.items()):
        if name.endswith("status") and not getattr(fn, "_tagged", False):
            def tagged(args, msg, _fn=fn):
                return "%s\nhost: %s · %s" % (_fn(args, msg), INFO["host"], C.VERSION)
            tagged._tagged = True
            notify._handlers[name] = tagged


def _start_loops() -> None:
    notify.start_listener()
    threading.Thread(target=_poll_loop, name="gap-poll", daemon=True).start()
    threading.Thread(target=_l_fast_watch_loop, name="gap-l-fast-watch", daemon=True).start()
    INFO["loops"] = ["gap-poll", "gap-l-fast-watch"]
    msg = "🔑 %s workers started on %s" % (C.VERSION, INFO["host"])
    log.info(msg)
    store.log_activity("workers_start", "host=%s loops=%s" % (INFO["host"], ",".join(INFO["loops"])))
    notify.send(msg, quiet=True)


def _on_lost(other: str) -> None:
    msg = "⚠️ %s on %s LOST the worker lease to %s. Workers stopped here." % (C.VERSION, INFO["host"], other)
    log.error(msg)
    try:
        store.log_activity("workers_lost", "host=%s now=%s" % (INFO["host"], other))
        notify.send(msg)
    finally:
        if _exit_on_lost:
            os._exit(3)              # systemd starts it again; it then waits for the lease


def start_workers(where: str, block: bool = False, exit_on_lost: bool = False) -> dict:
    """Idempotent. Returns INFO. With block=True it returns once the loops are running."""
    global _started, _keeper, _exit_on_lost
    with _lock:
        if _started:
            return INFO
        _started = True
        _exit_on_lost = exit_on_lost
        INFO.update(where=where, host=host(where), run_workers=run_workers())
        store.init_db()
        if not INFO["run_workers"]:
            lease.GATE.set("dashboard", name=NAME, holder=INFO["host"])
            log.info("dashboard only (RUN_WORKERS=false): no listener, no loops")
            return INFO
        pipeline.register_commands()
        _tag_status_commands()
        _keeper = lease.Keeper(store.engine(), NAME, INFO["host"], on_lost=_on_lost)
        lease.GATE.set("waiting", name=NAME, holder=INFO["host"], other="?")

    def go() -> None:
        if _keeper.wait(every_s=WAIT_S, say=log.info):
            _start_loops()

    if block:
        go()
    else:
        threading.Thread(target=go, name="gap-lease-wait", daemon=True).start()
    return INFO


def interrupt() -> None:
    """Safe inside a signal handler: only wakes a worker that is still waiting for the lease."""
    if _keeper is not None:
        _keeper._stop.set()


def stop() -> None:
    """worker.py on SIGTERM: give the lease back so the other place can start at once."""
    if _keeper is not None:
        _keeper.stop(give_up=True)


def banner() -> tuple:
    """(level, text) for the top of the dashboard. level: info | warning | error | ok."""
    g = lease.GATE
    held_by = ""
    if g.mode in ("dashboard", "waiting", "lost"):
        try:
            cur = lease.current(store.engine(), NAME)
            held_by = "%s, last heartbeat %.0fs ago" % (cur["holder"], cur["age_s"]) if cur else "nobody"
        except Exception:
            held_by = "unknown (the lease table could not be read)"
    if g.mode == "dashboard":
        return ("info", "Dashboard only: the workers do not run here. Worker lease: %s." % held_by)
    if g.mode == "waiting":
        return ("warning", "Workers are WAITING here: the worker lease is held by %s." % held_by)
    if g.mode == "lost":
        return ("error", "Workers STOPPED here: the worker lease was taken by %s. Reboot the app to try again." % held_by)
    if g.mode == "held" and not lease.may_trade():
        return ("error", "Lease not renewed for over %ds (database unreachable?). No new orders until it renews." % lease.SAFE_S)
    return ("ok", "Workers run here (%s)." % INFO["host"])


def reset_for_tests() -> None:
    global _started, _keeper, _exit_on_lost
    if _keeper is not None:
        _keeper.stop(give_up=False)
    _started, _keeper, _exit_on_lost = False, None, False
    _settle["last"] = None
    INFO.update(where="", host="", run_workers=True, loops=[])
    lease.GATE.reset()
