#!/usr/bin/env python3
"""
wipe_and_verify.py -- clear the corrupted days, then prove the new path works.

The Sep 15 / Sep 16 rows were written before our_price_cents existed and were
then marked with a mix of post-close mid=0 marks, a hand-maintained outcome
table, and $1-fill-percentages cloned onto $100 books. None of that is
recoverable into honest numbers, so it gets deleted rather than patched.

Usage:
    python3 scripts/wipe_and_verify.py --check          # show what exists
    python3 scripts/wipe_and_verify.py --wipe           # delete the bad days
    python3 scripts/wipe_and_verify.py --verify         # sanity-check schema

Needs DATABASE_URL in the environment.
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("pip install sqlalchemy psycopg2-binary")

DEFAULT_BAD_DAYS = ["2026-09-15", "2026-09-16"]


def engine():
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit("Set DATABASE_URL first.")
    return create_engine(url)


def check(eng):
    with eng.connect() as cx:
        print("Rows currently stored per day:\n")
        rows = cx.execute(text("""
            select event_date,
                   count(*)                                  as orders,
                   count(distinct variant_id)                as books,
                   count(our_price_cents)                    as have_our_price,
                   count(*) filter (where result is not null) as settled
            from gap_orders
            group by event_date
            order by event_date
        """)).mappings().all()
        if not rows:
            print("  (no orders at all)")
            return
        print(f"  {'date':<12} {'orders':>7} {'books':>6} {'our_px':>7} {'settled':>8}")
        for r in rows:
            print(f"  {str(r['event_date']):<12} {r['orders']:>7} {r['books']:>6} "
                  f"{r['have_our_price']:>7} {r['settled']:>8}")

        print("\nRows missing our_price_cents (booked before the migration):")
        n = cx.execute(text(
            "select count(*) from gap_orders where our_price_cents is null"
        )).scalar()
        print(f"  {n}")


def wipe(eng, days):
    with eng.begin() as cx:
        total = 0
        for day in days:
            # settlements first -- they reference orders
            s = cx.execute(text("""
                delete from gap_settlements
                where order_id in (
                    select id from gap_orders where event_date = cast(:d as date)
                )
            """), {"d": day}).rowcount or 0
            o = cx.execute(text(
                "delete from gap_orders where event_date = cast(:d as date)"
            ), {"d": day}).rowcount or 0
            q = cx.execute(text(
                "delete from gap_quotes where event_date = cast(:d as date)"
            ), {"d": day}).rowcount or 0 if _has(cx, "gap_quotes", "event_date") else 0
            print(f"  {day}: {o} orders, {s} settlements, {q} quotes deleted")
            total += o
        # forecasts and runs are kept: the Grok JSON is the scarce, honest data.
        print(f"\nDeleted {total} order rows. gap_forecasts and gap_runs untouched")
        print("(the Grok probabilities are real data and worth keeping).")


def _has(cx, table: str, col: str) -> bool:
    return bool(cx.execute(text("""
        select 1 from information_schema.columns
        where table_name = :t and column_name = :c
    """), {"t": table, "c": col}).first())


def verify(eng):
    with eng.connect() as cx:
        print("Schema check on gap_orders:\n")
        cols = cx.execute(text("""
            select column_name, data_type
            from information_schema.columns
            where table_name = 'gap_orders'
              and column_name in
                  ('limit_price_cents','our_price_cents',
                   'avg_fill_price_cents','fees_cents')
            order by column_name
        """)).mappings().all()
        found = {c["column_name"] for c in cols}
        for name in ("limit_price_cents", "our_price_cents",
                     "avg_fill_price_cents", "fees_cents"):
            mark = "OK " if name in found else "MISSING"
            print(f"  [{mark}] {name}")
        if "our_price_cents" not in found:
            print("\n  -> run sql/004_our_price.sql first.")
            return

        bad = cx.execute(text("""
            select count(*) from gap_orders
            where our_price_cents is not null
              and upper(coalesce(side,'NO')) = 'NO'
              and our_price_cents <> 100 - limit_price_cents
        """)).scalar()
        print(f"\nNO-side rows where our_price != 100 - limit: {bad}  (must be 0)")

        bad2 = cx.execute(text("""
            select count(*) from gap_orders
            where our_price_cents is not null
              and upper(coalesce(side,'NO')) = 'YES'
              and our_price_cents <> limit_price_cents
        """)).scalar()
        print(f"YES-side rows where our_price != limit:      {bad2}  (must be 0)")

        if bad == 0 and bad2 == 0:
            print("\nPrice convention is consistent.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--date", action="append", default=None,
                     help="event_date to wipe, YYYY-MM-DD. Repeatable. "
                          "Defaults to the original two corrupted days if omitted.")
    a = ap.parse_args()
    if not (a.check or a.wipe or a.verify):
        ap.print_help()
        return
    eng = engine()
    if a.check:
        check(eng)
    if a.wipe:
        days = a.date or DEFAULT_BAD_DAYS
        confirm = input(f"Delete ALL order rows for {days}? type YES: ")
        if confirm.strip() == "YES":
            wipe(eng, days)
        else:
            print("aborted")
    if a.verify:
        verify(eng)


if __name__ == "__main__":
    main()
