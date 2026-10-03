"""One running worker per bot, enforced by the database.

Why: the same bot must never run in two places at once (Streamlit Cloud and the server). Two workers
would mean two Telegram listeners and a race on every order. A worker takes the lease at start and
renews it every RENEW_S seconds. Another place can take it only after the holder has been silent
for STALE_S seconds.

Three states matter to the rest of the bot:
  running()    loops and the Telegram listener may run here (we hold the lease, or no lease is in
               use at all: scripts and tests)
  may_trade()  a NEW order may be sent right now. False when the lease is not ours, and also when
               we could not renew it for SAFE_S seconds (database unreachable): by then another
               place may be about to take over, so we stop sending until a renew works again.
  lost         another place holds the lease. Final for this process.

Cancels are never blocked by the lease. Only new orders and loop ticks are.

Same rules as klsh-engine/engine/core/lease.py. This copy adds renew() (a renew can never take the
lease back from someone else), the gate, and a SQLite path for local tests. Works through
Supabase's pooler (plain rows, no session locks). No imports from this package on purpose.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:                    # nothing heavy at import time: kalshi.py imports this module,
    from sqlalchemy.engine import Engine   # and the cancel scripts must import with the bare minimum

log = logging.getLogger("gap.lease")

RENEW_S = 20
STALE_S = 90
SAFE_S = STALE_S - RENEW_S          # stop sending new orders this long after the last good renew
TABLE = "public.worker_leases"


class LeaseError(RuntimeError):
    """Raised when something tries to send a new order without the lease."""


# ---------------------------------------------------------------- the gate (one per process)
class _Gate:
    def __init__(self) -> None:
        self.mode = "off"            # off | dashboard | waiting | held | lost
        self.name = ""
        self.holder = ""
        self.other = ""              # who holds it when it is not us
        self.last_ok = 0.0           # time.monotonic() of the last successful acquire/renew
        self.since = 0.0             # time.time() when the mode last changed

    def set(self, mode: str, **kw) -> None:
        self.mode = mode
        self.since = time.time()
        for k, v in kw.items():
            setattr(self, k, v)

    def reset(self) -> None:         # tests only
        self.__init__()


GATE = _Gate()


def running() -> bool:
    return GATE.mode in ("off", "held")


def may_trade() -> bool:
    if GATE.mode == "off":
        return True
    return GATE.mode == "held" and (time.monotonic() - GATE.last_ok) < SAFE_S


def require(what: str = "send an order") -> None:
    if not may_trade():
        raise LeaseError("refused to %s: this process does not hold the worker lease (%s)" % (what, describe()))


def describe() -> str:
    g = GATE
    if g.mode == "off":
        return "no lease in use"
    if g.mode == "dashboard":
        return "dashboard only"
    if g.mode == "waiting":
        return "waiting, held by %s" % (g.other or "?")
    if g.mode == "lost":
        return "lost to %s" % (g.other or "?")
    age = time.monotonic() - g.last_ok
    return "held by %s, renewed %.0fs ago%s" % (g.holder, age, "" if age < SAFE_S else " (TOO OLD: not trading)")


# ---------------------------------------------------------------- SQL
def text(sql: str):
    from sqlalchemy import text as _text
    return _text(sql)


def _pg(db: Engine) -> bool:
    return db.dialect.name == "postgresql"


def _t(db: Engine, table: str) -> str:
    return table if _pg(db) else table.split(".")[-1]


def ensure_table(db: Engine, table: str = TABLE) -> None:
    t = _t(db, table)
    if _pg(db):
        with db.connect() as conn:
            exists = conn.execute(text("select to_regclass(:t)"), {"t": t}).scalar()
        if exists:
            return
        with db.begin() as conn:
            conn.execute(text(
                "create table if not exists %s (name text primary key, holder text not null, "
                "heartbeat timestamptz not null default now())" % t))
            conn.execute(text("alter table %s enable row level security" % t))
    else:
        with db.begin() as conn:
            conn.execute(text(
                "create table if not exists %s (name text primary key, holder text not null, "
                "heartbeat real not null)" % t))


def acquire(db: Engine, name: str, holder: str, table: str = TABLE, stale_s: int = STALE_S) -> bool:
    """True if we hold the lease now: new, already ours, or taken from a silent holder."""
    t = _t(db, table)
    with db.begin() as conn:
        if _pg(db):
            row = conn.execute(text(
                "insert into %(t)s (name, holder, heartbeat) values (:n, :h, now()) "
                "on conflict (name) do update set holder = excluded.holder, heartbeat = now() "
                "where %(t)s.holder = excluded.holder "
                "or %(t)s.heartbeat < now() - make_interval(secs => :stale) "
                "returning holder" % {"t": t}), {"n": name, "h": holder, "stale": stale_s}).first()
        else:
            now = time.time()
            row = conn.execute(text(
                "insert into %(t)s (name, holder, heartbeat) values (:n, :h, :now) "
                "on conflict (name) do update set holder = excluded.holder, heartbeat = excluded.heartbeat "
                "where %(t)s.holder = excluded.holder or %(t)s.heartbeat < :cutoff "
                "returning holder" % {"t": t}), {"n": name, "h": holder, "now": now, "cutoff": now - stale_s}).first()
    return row is not None


def renew(db: Engine, name: str, holder: str, table: str = TABLE) -> bool:
    """True if the row is still ours (and its heartbeat is now fresh). Never takes it from anyone."""
    t = _t(db, table)
    with db.begin() as conn:
        if _pg(db):
            row = conn.execute(text(
                "update %s set heartbeat = now() where name = :n and holder = :h returning holder" % t),
                {"n": name, "h": holder}).first()
        else:
            row = conn.execute(text(
                "update %s set heartbeat = :now where name = :n and holder = :h returning holder" % t),
                {"n": name, "h": holder, "now": time.time()}).first()
    return row is not None


def current(db: Engine, name: str, table: str = TABLE) -> dict | None:
    """{'holder': ..., 'age_s': seconds since its last heartbeat} or None when nobody holds it."""
    t = _t(db, table)
    with db.connect() as conn:
        if _pg(db):
            row = conn.execute(text(
                "select holder, extract(epoch from now() - heartbeat) as age_s from %s where name = :n" % t),
                {"n": name}).mappings().first()
        else:
            row = conn.execute(text("select holder, heartbeat from %s where name = :n" % t),
                               {"n": name}).mappings().first()
            if row:
                row = {"holder": row["holder"], "age_s": time.time() - float(row["heartbeat"])}
    if not row:
        return None
    return {"holder": row["holder"], "age_s": float(row["age_s"] or 0)}


def release(db: Engine, name: str, holder: str, table: str = TABLE) -> None:
    with db.begin() as conn:
        conn.execute(text("delete from %s where name = :n and holder = :h" % _t(db, table)),
                     {"n": name, "h": holder})


# ---------------------------------------------------------------- the keeper
class Keeper:
    """Takes the lease, then renews it in a background thread and keeps GATE up to date.

    on_lost(other_holder) is called once, from the renew thread, when another place holds it.
    A database hiccup is NOT a loss: may_trade() goes False after SAFE_S without a good renew and
    comes back by itself when a renew works again (unless someone else took it meanwhile).
    """

    def __init__(self, db: Engine, name: str, holder: str, on_lost=None, table: str = TABLE,
                 renew_s: float = RENEW_S, stale_s: int = STALE_S):
        self.db, self.name, self.holder, self.table = db, name, holder, table
        self.on_lost = on_lost or (lambda other: None)
        self.renew_s, self.stale_s = renew_s, stale_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._table_ok = False
        self.last_error = ""

    def _other(self) -> str:
        try:
            cur = current(self.db, self.name, self.table) or {}
            return "%s (%.0fs ago)" % (cur.get("holder"), cur.get("age_s") or 0) if cur else "nobody"
        except Exception:  # noqa: BLE001
            return "?"

    def try_acquire(self) -> bool:
        """One attempt. Never raises: a database error counts as 'not ours yet'."""
        t0 = time.monotonic()
        try:
            if not self._table_ok:
                ensure_table(self.db, self.table)
                self._table_ok = True
            ok = acquire(self.db, self.name, self.holder, self.table, self.stale_s)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:200]
            log.warning("lease '%s': could not reach the lease table: %s", self.name, self.last_error)
            GATE.set("waiting", name=self.name, holder=self.holder, other="? (database error)")
            return False
        self.last_error = ""
        if ok:
            GATE.set("held", name=self.name, holder=self.holder, other="", last_ok=t0)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="lease-%s" % self.name, daemon=True)
                self._thread.start()
        else:
            GATE.set("waiting", name=self.name, holder=self.holder, other=self._other())
        return ok

    def wait(self, every_s: float = 10, say=None, say_every_s: float = 30) -> bool:
        """Block until the lease is ours. False if stop() was called first."""
        last_said = None
        while not self._stop.is_set():
            if self.try_acquire():
                return True
            if say and (last_said is None or time.monotonic() - last_said >= say_every_s):
                last_said = time.monotonic()
                say("lease '%s' is held by %s; waiting" % (self.name, GATE.other))
            if self._stop.wait(every_s):
                break
        return False

    def _loop(self) -> None:
        while not self._stop.wait(self.renew_s):
            t0 = time.monotonic()
            try:
                ok = renew(self.db, self.name, self.holder, self.table)
                if not ok and current(self.db, self.name, self.table) is None:
                    ok = acquire(self.db, self.name, self.holder, self.table, self.stale_s)   # row was removed
            except Exception as exc:  # noqa: BLE001  hiccup: may_trade() closes by itself after SAFE_S
                self.last_error = str(exc)[:200]
                log.warning("lease '%s': renew failed (%s); not sending new orders once %ds pass without a renew",
                            self.name, self.last_error, SAFE_S)
                continue
            self.last_error = ""
            if ok:
                GATE.last_ok = t0
                continue
            other = self._other()
            GATE.set("lost", other=other)
            log.error("lease '%s' LOST: now held by %s", self.name, other)
            try:
                self.on_lost(other)
            except Exception:  # noqa: BLE001
                log.exception("on_lost")
            return

    def stop(self, give_up: bool = True) -> None:
        """Stop renewing. give_up=True also deletes our row so another place can start at once."""
        self._stop.set()
        if give_up and GATE.mode == "held":
            try:
                release(self.db, self.name, self.holder, self.table)
            except Exception:  # noqa: BLE001
                pass
            GATE.set("waiting", other="nobody")
