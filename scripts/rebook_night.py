#!/usr/bin/env python3
"""
rebook_night.py -- add the orders a night would have had if today's rules had been in place at booking time.

    python3 scripts/rebook_night.py --date 2026-09-21            # dry run: shows what it would add
    python3 scripts/rebook_night.py --date 2026-09-21 --apply    # really adds them

It only ADDS missing orders (never edits or deletes one), uses the frozen decision-time quotes, stamps the
original booking time, and fills the new orders by replaying the stored order-book history from that moment.
Needs DATABASE_URL in the environment (same as resettle.py).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gap import rebook, store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="night, YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="really add the orders (default is a dry run)")
    a = ap.parse_args()
    store.init_db()
    print(rebook.format_report(rebook.rebook_missing(a.date, apply=a.apply)))


if __name__ == "__main__":
    main()
