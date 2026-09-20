#!/usr/bin/env python3
"""
resettle.py -- recompute settlements (gross / fees / net) for a range of nights.

Why: before v1.5.1 every paper order was settled with fees = 0.00 (the fee formula was
never applied). Settlements are recomputed from the official Kalshi result each time this
runs, so re-running it once fixes the old numbers. It changes NO orders, quotes or forecasts.

Usage (needs DATABASE_URL in the environment):
    python3 scripts/resettle.py --from 2026-09-14 --to 2026-09-18
    python3 scripts/resettle.py --from 2026-09-14 --to 2026-09-18 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gap import pricing, settle, store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True, help="first night, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="last night, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="only show what the fees would be")
    a = ap.parse_args()
    store.init_db()
    runs = store.runs_between(a.start, a.end)
    if not runs:
        sys.exit("no runs in that range")
    if a.dry_run:
        tot_fee = 0
        for run in runs:
            orders = store.orders_for_run(run["id"])
            fee = sum(pricing.entry_fee_cents(o) for o in orders)
            tot_fee += fee
            print(f"{str(run['event_date'])[:10]}: {len(orders)} orders, fees would be ${fee / 100:.2f}")
        print(f"total fees ${tot_fee / 100:.2f}   (nothing written)")
        return
    for run in runs:
        d = str(run["event_date"])[:10]
        settle.sync_gh_fills(d)
    out = settle.settle_range(a.start, a.end)
    print("re-settled:", out)
    for run in runs:
        s = store.settlements_for_order_ids([o["id"] for o in store.orders_for_run(run["id"])])
        gross = sum(v.get("gross_cents") or 0 for v in s.values())
        fees = sum(v.get("fees_cents") or 0 for v in s.values())
        print(f"{str(run['event_date'])[:10]}: gross ${gross / 100:+.2f}  fees ${fees / 100:.2f}  net ${(gross - fees) / 100:+.2f}")


if __name__ == "__main__":
    main()
