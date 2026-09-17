"""Postgres (Supabase) store. gap_* tables only — never touch no-fade tables."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import config as C

log = logging.getLogger("gap.store")

_engine: Engine | None = None
_lock = threading.Lock()

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_gap_schema.sql"


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


def init_db() -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    # Split on semicolons but keep it simple — skip comments-only chunks.
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
    with engine().begin() as conn:
        for stmt in statements:
            try:
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
    }
    with engine().begin() as conn:
        conn.execute(
            text("""
                insert into gap_orders (
                    forecast_id, run_id, event_date, market_ticker, word, side,
                    limit_price_cents, our_price_cents, avg_fill_price_cents, fees_cents,
                    contracts, cost_cents, gap_points, threshold,
                    cluster_key, paper, status,
                    variant_id, exit_rule, notional_dollars, execution_model
                ) values (
                    :forecast_id, :run_id, cast(:event_date as date), :market_ticker, :word,
                    :side, :limit_price_cents, :our_price_cents, :avg_fill_price_cents,
                    :fees_cents, :contracts, :cost_cents, :gap_points,
                    :threshold, :cluster_key, :paper, :status,
                    :variant_id, :exit_rule, :notional_dollars, :execution_model
                )
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
