"""Postgres (Supabase) store. Writes only ever touch gap_* tables.

Read-only exception: latest_nofade_depth() below SELECTs from no-fade's
own `depth` table, which lives in this same shared Supabase project.
no-fade already polls the full orderbook for every market in the event
every 60s (11:00-18:15 CT) and stores it there -- re-polling Kalshi
ourselves for the same ticker during that window is pure duplication.
We never write to that table or any other no-fade table.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

from . import config as C

log = logging.getLogger("gap.store")

_engine: Engine | None = None
_lock = threading.Lock()

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_gap_schema.sql"
# Extra idempotent migrations applied at boot (safe to re-run).
MIGRATION_FILES = [
    Path(__file__).resolve().parent.parent / "sql" / "005_v151.sql",
    Path(__file__).resolve().parent.parent / "sql" / "006_lab.sql",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def using_postgres() -> bool:
    return bool(C.DATABASE_URL)


def engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        if C.DATABASE_URL:
            url = C.DATABASE_URL
            if url.startswith("postgres://"):
                url = "postgresql+psycopg2://" + url[len("postgres://"):]
            elif url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=280,
                pool_size=3,
                max_overflow=2,
                future=True,
            )
        else:
            _engine = create_engine(
                f"sqlite:///{C.SQLITE_PATH}",
                connect_args={"check_same_thread": False},
                future=True,
            )
        return _engine


def _split_statements(sql: str) -> list[str]:
    """Split on semicolons at end of line; skip comment-only lines."""
    statements = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";")
            if stmt:
                statements.append(stmt)
            buf = []
    return statements


def init_db() -> None:
    for path in [SCHEMA_FILE, *MIGRATION_FILES]:
        if not path.exists():
            log.warning("schema file missing: %s", path)
            continue
        statements = _split_statements(path.read_text(encoding="utf-8"))
        for stmt in statements:
            # One transaction per statement: in Postgres one failed statement would
            # otherwise poison every statement after it.
            try:
                with engine().begin() as conn:
                    conn.execute(text(stmt))
            except Exception as exc:
                # SQLite cannot do some PG-only bits; keep going for local paper.
                log.warning("init_db statement skipped: %s | %s", exc, stmt[:80])
    log.info("gap tables ready (%s)", "postgres" if using_postgres() else "sqlite")


def log_activity(kind: str, message: str) -> None:
    try:
        with engine().begin() as conn:
            conn.execute(
                text("insert into gap_activity (kind, message) values (:k, :m)"),
                {"k": kind, "m": message[:2000]},
            )
    except Exception as exc:
        log.warning("activity log failed: %s", exc)


def get_state(key: str, default: Any = None) -> Any:
    with engine().connect() as conn:
        row = conn.execute(
            text("select value from gap_state where key = :k"),
            {"k": key},
        ).mappings().first()
    if not row:
        return default
    val = row["value"]
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return val
    return val


def state_meta(key: str) -> tuple[Any, datetime | None]:
    """(value, updated_at) for a gap_state key, or (None, None)."""
    with engine().connect() as conn:
        row = conn.execute(
            text("select value, updated_at from gap_state where key = :k"),
            {"k": key},
        ).mappings().first()
    if not row:
        return None, None
    val = row["value"]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            pass
    return val, row["updated_at"]


def set_state(key: str, value: Any) -> None:
    payload = json.dumps(value)
    with engine().begin() as conn:
        if using_postgres():
            conn.execute(
                text("""
                    insert into gap_state (key, value, updated_at)
                    values (:k, cast(:v as jsonb), :t)
                    on conflict (key) do update
                      set value = excluded.value, updated_at = excluded.updated_at
                """),
                {"k": key, "v": payload, "t": _now()},
            )
        else:
            conn.execute(
                text("""
                    insert into gap_state (key, value, updated_at)
                    values (:k, :v, :t)
                    on conflict (key) do update
                      set value = excluded.value, updated_at = excluded.updated_at
                """),
                {"k": key, "v": payload, "t": _now()},
            )


def get_run_for_date(event_date: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(
            text("""
                select * from gap_runs
                where event_date = cast(:d as date)
                order by id desc
                limit 1
            """),
            {"d": event_date},
        ).mappings().first()
    return dict(row) if row else None


def insert_run(row: dict) -> dict:
    with engine().begin() as conn:
        existing = conn.execute(
            text("""
                select * from gap_runs
                where event_date = cast(:event_date as date)
                  and event_ticker = :event_ticker
            """),
            row,
        ).mappings().first()
        if existing:
            return dict(existing)
        wl = json.dumps(row["word_list"])
        if using_postgres():
            insert_sql = """
                insert into gap_runs (
                    event_date, event_ticker, status, prompt_version, harness,
                    word_list, prompt_text, markets_n
                ) values (
                    cast(:event_date as date), :event_ticker, :status, :prompt_version,
                    :harness, cast(:word_list as jsonb), :prompt_text, :markets_n
                )
            """
        else:
            insert_sql = """
                insert into gap_runs (
                    event_date, event_ticker, status, prompt_version, harness,
                    word_list, prompt_text, markets_n
                ) values (
                    :event_date, :event_ticker, :status, :prompt_version,
                    :harness, :word_list, :prompt_text, :markets_n
                )
            """
        conn.execute(text(insert_sql), {**row, "word_list": wl})
        saved = conn.execute(
            text("""
                select * from gap_runs
                where event_date = cast(:event_date as date)
                  and event_ticker = :event_ticker
            """),
            row,
        ).mappings().first()
    return dict(saved) if saved else row


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = []
    params: dict[str, Any] = {"id": run_id}
    for i, (k, v) in enumerate(fields.items()):
        key = f"p{i}"
        if k in ("word_list", "parsed") and not isinstance(v, str):
            v = json.dumps(v)
        if using_postgres() and k in ("word_list", "parsed"):
            assignments.append(f"{k} = cast(:{key} as jsonb)")
        else:
            assignments.append(f"{k} = :{key}")
        params[key] = v
    sql = f"update gap_runs set {', '.join(assignments)} where id = :id"
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def insert_markets(run_id: int, event_date: str, event_ticker: str, markets: list[dict]) -> None:
    with engine().begin() as conn:
        for m in markets:
            conn.execute(
                text("""
                    insert into gap_markets (
                        run_id, event_date, event_ticker, market_ticker, word, title
                    ) values (
                        :run_id, cast(:event_date as date), :event_ticker,
                        :market_ticker, :word, :title
                    )
                    on conflict (run_id, market_ticker) do update
                      set word = excluded.word, title = excluded.title
                """),
                {
                    "run_id": run_id,
                    "event_date": event_date,
                    "event_ticker": event_ticker,
                    "market_ticker": m["market_ticker"],
                    "word": m["word"],
                    "title": m.get("title"),
                },
            )


def markets_for_run(run_id: int) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_markets where run_id = :id order by id"),
            {"id": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def replace_forecasts(run_id: int, event_date: str, event_ticker: str,
                      harness: str, prompt_version: str, rows: list[dict]) -> list[dict]:
    saved: list[dict] = []
    with engine().begin() as conn:
        conn.execute(text("delete from gap_forecasts where run_id = :id"), {"id": run_id})
        for r in rows:
            conn.execute(
                text("""
                    insert into gap_forecasts (
                        run_id, event_date, event_ticker, market_ticker, word,
                        model, prompt_version, harness, probability,
                        p_block_airs, p_said_given_airs, carrying_story,
                        substitute_risk, other_routes, reasoning
                    ) values (
                        :run_id, cast(:event_date as date), :event_ticker, :market_ticker,
                        :word, :model, :prompt_version, :harness, :probability,
                        :p_block_airs, :p_said_given_airs, :carrying_story,
                        :substitute_risk, :other_routes, :reasoning
                    )
                """),
                {
                    "run_id": run_id,
                    "event_date": event_date,
                    "event_ticker": event_ticker,
                    "market_ticker": r["market_ticker"],
                    "word": r["word"],
                    "model": C.MODEL_LABEL,
                    "prompt_version": prompt_version,
                    "harness": harness,
                    "probability": r["probability"],
                    "p_block_airs": r.get("p_block_airs"),
                    "p_said_given_airs": r.get("p_said_given_airs"),
                    "carrying_story": r.get("carrying_story"),
                    "substitute_risk": r.get("substitute_risk"),
                    "other_routes": r.get("other_routes"),
                    "reasoning": r.get("reasoning"),
                },
            )
        fetched = conn.execute(
            text("select * from gap_forecasts where run_id = :id order by id"),
            {"id": run_id},
        ).mappings().all()
        saved = [dict(x) for x in fetched]
    return saved


def insert_quote(run_id: int, forecast_id: int | None, market_ticker: str,
                 yes_bid: int | None, yes_ask: int | None, market_prob: float | None) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("""
                insert into gap_quotes (
                    forecast_id, run_id, market_ticker, yes_bid_cents, yes_ask_cents, market_prob
                ) values (
                    :forecast_id, :run_id, :market_ticker, :yes_bid, :yes_ask, :market_prob
                )
            """),
            {
                "forecast_id": forecast_id,
                "run_id": run_id,
                "market_ticker": market_ticker,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "market_prob": market_prob,
            },
        )


def insert_order(row: dict) -> None:
    payload = {
        "forecast_id": row.get("forecast_id"),
        "run_id": row["run_id"],
        "event_date": row["event_date"],
        "market_ticker": row["market_ticker"],
        "word": row["word"],
        "side": row["side"],
        "limit_price_cents": row["limit_price_cents"],
        # v1.4.8: persist the price of the side WE hold. strategy.decide()
        # already computes this; it used to be dropped on the floor here,
        # forcing every downstream consumer to re-derive it (and some got it
        # wrong). Derive once as a fallback for callers that omit it.
        "our_price_cents": row.get("our_price_cents") or (
            row["limit_price_cents"]
            if str(row.get("side") or "NO").upper() == "YES"
            else 100 - int(row["limit_price_cents"])
        ),
        "avg_fill_price_cents": row.get("avg_fill_price_cents"),
        "fees_cents": row.get("fees_cents", 0),
        "contracts": row["contracts"],
        "cost_cents": row["cost_cents"],
        "gap_points": row["gap_points"],
        "threshold": row["threshold"],
        "cluster_key": row.get("cluster_key"),
        "paper": row.get("paper", True),
        "status": row.get("status", "paper_sweep"),
        "variant_id": row.get("variant_id"),
        "exit_rule": row.get("exit_rule"),
        "notional_dollars": row.get("notional_dollars"),
        "execution_model": row.get("execution_model", C.EXECUTION_MODEL),
        "quote_bid_cents": row.get("quote_bid_cents"),
        "quote_ask_cents": row.get("quote_ask_cents"),
        "quote_captured_at": row.get("quote_captured_at"),
        "book_rule": row.get("book_rule"),
    }
    with engine().begin() as conn:
        conn.execute(
            text("""
                insert into gap_orders (
                    forecast_id, run_id, event_date, market_ticker, word, side,
                    limit_price_cents, our_price_cents, avg_fill_price_cents, fees_cents,
                    contracts, cost_cents, gap_points, threshold,
                    cluster_key, paper, status,
                    variant_id, exit_rule, notional_dollars, execution_model,
                    quote_bid_cents, quote_ask_cents, quote_captured_at, book_rule
                ) values (
                    :forecast_id, :run_id, cast(:event_date as date), :market_ticker, :word,
                    :side, :limit_price_cents, :our_price_cents, :avg_fill_price_cents,
                    :fees_cents, :contracts, :cost_cents, :gap_points,
                    :threshold, :cluster_key, :paper, :status,
                    :variant_id, :exit_rule, :notional_dollars, :execution_model,
                    :quote_bid_cents, :quote_ask_cents, :quote_captured_at, :book_rule
                )
                -- gap_orders_one_per_book is a PARTIAL unique index
                -- (excludes rejected/cancelled rows, so a word can be
                -- retried after a rejection instead of being permanently
                -- blocked). Postgres only accepts a partial index as the
                -- ON CONFLICT arbiter if the same predicate is repeated
                -- here -- omitting it is what caused 'no unique or exclusion
                -- constraint matching the ON CONFLICT specification'.
                on conflict (event_date, market_ticker, variant_id)
                    where status not in ('rejected', 'cancelled')
                    do update set
                    forecast_id = excluded.forecast_id,
                    run_id = excluded.run_id,
                    word = excluded.word,
                    side = excluded.side,
                    limit_price_cents = excluded.limit_price_cents,
                    our_price_cents = excluded.our_price_cents,
                    contracts = excluded.contracts,
                    cost_cents = excluded.cost_cents,
                    gap_points = excluded.gap_points,
                    status = excluded.status,
                    exit_rule = excluded.exit_rule,
                    notional_dollars = excluded.notional_dollars,
                    quote_bid_cents = excluded.quote_bid_cents,
                    quote_ask_cents = excluded.quote_ask_cents,
                    quote_captured_at = excluded.quote_captured_at,
                    book_rule = excluded.book_rule
            """),
            payload,
        )


def orders_for_date(event_date: str) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("""
                select * from gap_orders
                where event_date = cast(:d as date)
                order by id
            """),
            {"d": event_date},
        ).mappings().all()
    return [dict(r) for r in rows]


def forecasts_for_run(run_id: int) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_forecasts where run_id = :id order by id"),
            {"id": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def recent_activity(limit: int = 40) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_activity order by id desc limit :n"),
            {"n": limit},
        ).mappings().all()
    return [dict(r) for r in rows]


def runs_between(start_date: str, end_date: str) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("""
                select * from gap_runs
                where event_date >= cast(:a as date)
                  and event_date <= cast(:b as date)
                order by event_date, id
            """),
            {"a": start_date, "b": end_date},
        ).mappings().all()
    return [dict(r) for r in rows]


def all_paper_orders() -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_orders where paper is true order by event_date, variant_id, id")
        ).mappings().all()
    return [dict(r) for r in rows]


def orders_for_run(run_id: int) -> list[dict]:

    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_orders where run_id = :id order by variant_id, id"),
            {"id": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def quotes_for_run(run_id: int) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_quotes where run_id = :id order by id"),
            {"id": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def settlements_for_order_ids(order_ids: list[int]) -> dict[int, dict]:
    if not order_ids:
        return {}
    out: dict[int, dict] = {}
    with engine().connect() as conn:
        for oid in order_ids:
            row = conn.execute(
                text("select * from gap_settlements where order_id = :id"),
                {"id": oid},
            ).mappings().first()
            if row:
                out[int(oid)] = dict(row)
    return out


def update_order(order_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = []
    params: dict[str, Any] = {"id": order_id}
    for i, (k, v) in enumerate(fields.items()):
        key = f"p{i}"
        assignments.append(f"{k} = :{key}")
        params[key] = v
    sql = f"update gap_orders set {', '.join(assignments)} where id = :id"
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def upsert_settlement(row: dict) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("delete from gap_settlements where order_id = :order_id"),
            {"order_id": row["order_id"]},
        )
        conn.execute(
            text("""
                insert into gap_settlements (
                    forecast_id, order_id, settled_at, outcome,
                    gross_cents, fees_cents, net_cents, fill_model
                ) values (
                    :forecast_id, :order_id, :settled_at, :outcome,
                    :gross_cents, :fees_cents, :net_cents, :fill_model
                )
            """),
            row,
        )


def latest_nofade_depth(market_ticker: str, event_date: str,
                        as_of: datetime | None = None,
                        max_age_s: int | None = None) -> dict | None:
    """Read-only lookup into no-fade's `depth` table (same Supabase project,
    different app). Returns the most recent orderbook snapshot no-fade has
    already taken for this market, or None if there is none.

    as_of:     only consider snapshots taken AT OR BEFORE this moment. This is what
               makes a quote reproducible: asking "what did the book look like at the
               decision time" gives the same answer no matter when you ask.
    max_age_s: with as_of, ignore snapshots older than as_of - max_age_s (stale).

    Callers must handle None -- it is not an error, just "no data".
    """
    sql = (
        "SELECT ts, best_yes_bid, best_no_bid, yes_book, no_book "
        "FROM depth "
        "WHERE market_ticker = :ticker AND event_date = :event_date"
    )
    params: dict[str, Any] = {"ticker": market_ticker, "event_date": str(event_date)[:10]}
    if as_of is not None:
        sql += " AND ts <= :as_of"
        params["as_of"] = as_of
        if max_age_s:
            sql += " AND ts >= :oldest"
            params["oldest"] = as_of - timedelta(seconds=int(max_age_s))
    sql += " ORDER BY ts DESC LIMIT 1"
    with engine().connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# v1.5.1: frozen decision-time quotes, results cache, history helpers
# ---------------------------------------------------------------------------
def insert_frozen_quote(row: dict) -> None:
    """One frozen quote per (run, market). A second call for the same market is ignored."""
    with engine().begin() as conn:
        conn.execute(
            text("""
                insert into gap_quotes (
                    run_id, market_ticker, yes_bid_cents, yes_ask_cents, market_prob,
                    source, captured_at, decision_at, valid, invalid_reason, age_s, frozen
                ) values (
                    :run_id, :market_ticker, :yes_bid_cents, :yes_ask_cents, :market_prob,
                    :source, :captured_at, :decision_at, :valid, :invalid_reason, :age_s, true
                )
                on conflict (run_id, market_ticker) where frozen is true do nothing
            """),
            row,
        )


def frozen_quotes_for_run(run_id: int) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select * from gap_quotes where run_id = :id and frozen is true order by id"),
            {"id": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def delete_frozen_quotes(run_id: int) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("delete from gap_quotes where run_id = :id and frozen is true"),
            {"id": run_id},
        )


def results_cached(tickers) -> dict[str, str]:
    tickers = [t for t in tickers if t]
    if not tickers:
        return {}
    stmt = text(
        "select market_ticker, result from gap_results where market_ticker in :t"
    ).bindparams(bindparam("t", expanding=True))
    out: dict[str, str] = {}
    with engine().connect() as conn:
        for i in range(0, len(tickers), 500):
            for r in conn.execute(stmt, {"t": tickers[i:i + 500]}).mappings().all():
                out[r["market_ticker"]] = r["result"]
    return out


def results_save(rows: dict[str, str]) -> None:
    rows = {t: r for t, r in rows.items() if t and r in ("yes", "no", "void")}
    if not rows:
        return
    with engine().begin() as conn:
        for t, r in rows.items():
            conn.execute(
                text("""
                    insert into gap_results (market_ticker, result) values (:t, :r)
                    on conflict (market_ticker) do update
                      set result = excluded.result, fetched_at = now()
                """),
                {"t": t, "r": r},
            )


def markets_history(before_date: str, n_dates: int = 10, span_days: int = 45) -> list[dict]:
    """Markets from the last n_dates event dates STRICTLY BEFORE before_date
    (newest date first). One row per (date, word); a later run wins."""
    with engine().connect() as conn:
        rows = conn.execute(
            text("""
                select r.event_date as event_date, m.word as word,
                       m.market_ticker as market_ticker, r.id as run_id
                from gap_markets m
                join gap_runs r on r.id = m.run_id
                where r.event_date < cast(:d as date)
                  and r.event_date >= cast(:d as date) - cast(:span as integer)
                order by r.event_date desc, r.id desc
                limit 8000
            """),
            {"d": str(before_date)[:10], "span": int(span_days)},
        ).mappings().all()
    seen = set()
    dates: list[str] = []
    out: list[dict] = []
    for r in rows:
        d = str(r["event_date"])[:10]
        key = (d, str(r["word"]).strip().lower())
        if key in seen:
            continue
        if d not in dates:
            if len(dates) >= n_dates:
                continue
            dates.append(d)
        seen.add(key)
        out.append({"event_date": d, "word": r["word"], "market_ticker": r["market_ticker"]})
    return out


def activity_between(start: datetime, end: datetime) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("select at, kind, message from gap_activity where at >= :a and at < :b order by at"),
            {"a": start, "b": end},
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# v1.5.6: decision-time order books (strategy lab)
# ---------------------------------------------------------------------------
def _book_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return [[int(p), float(c)] for p, c in v] if v else []


def depth_near(market_ticker: str, event_date: str, target: datetime, window_s: int = 900) -> dict | None:
    """The depth snapshot closest in time to `target` (within +/- window_s), or None."""
    with engine().connect() as conn:
        row = conn.execute(
            text("""
                select ts, best_yes_bid, best_no_bid, yes_book, no_book
                from depth
                where market_ticker = :t and event_date = :d
                  and ts between :lo and :hi
                order by abs(extract(epoch from (ts - :target))) asc
                limit 1
            """),
            {"t": market_ticker, "d": str(event_date)[:10], "target": target,
             "lo": target - timedelta(seconds=window_s), "hi": target + timedelta(seconds=window_s)},
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["yes_book"] = _book_list(out.get("yes_book"))
    out["no_book"] = _book_list(out.get("no_book"))
    return out


def depth_series(market_ticker: str, event_date: str, start: datetime, end: datetime) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text("""
                select ts, best_yes_bid, best_no_bid, yes_book, no_book
                from depth
                where market_ticker = :t and event_date = :d and ts >= :a and ts <= :b
                order by ts asc
            """),
            {"t": market_ticker, "d": str(event_date)[:10], "a": start, "b": end},
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["yes_book"] = _book_list(d.get("yes_book"))
        d["no_book"] = _book_list(d.get("no_book"))
        out.append(d)
    return out


def insert_decision_book(run_id: int, ticker: str, decision_at, snap: dict | None, source: str) -> None:
    if not snap:
        return
    cap = snap.get("ts")
    age = None
    try:
        if cap is not None and decision_at is not None:
            age = int(abs((decision_at - cap).total_seconds()))
    except TypeError:
        age = None
    with engine().begin() as conn:
        conn.execute(
            text("""
                insert into gap_decision_books
                    (run_id, market_ticker, decision_at, captured_at, age_s, yes_book, no_book, source)
                values (:r, :t, :d, :c, :a, cast(:yb as jsonb), cast(:nb as jsonb), :s)
                on conflict (run_id, market_ticker) do nothing
            """),
            {"r": run_id, "t": ticker, "d": decision_at, "c": cap, "a": age,
             "yb": json.dumps(snap.get("yes_book") or []), "nb": json.dumps(snap.get("no_book") or []), "s": source},
        )


def decision_books_for_runs(run_ids: list[int]) -> dict[tuple[int, str], dict]:
    run_ids = [int(r) for r in run_ids if r is not None]
    if not run_ids:
        return {}
    stmt = text("select * from gap_decision_books where run_id in :r").bindparams(bindparam("r", expanding=True))
    with engine().connect() as conn:
        rows = conn.execute(stmt, {"r": run_ids}).mappings().all()
    out = {}
    for r in rows:
        d = dict(r)
        d["yes_book"] = _book_list(d.get("yes_book"))
        d["no_book"] = _book_list(d.get("no_book"))
        out[(int(d["run_id"]), d["market_ticker"])] = d
    return out
