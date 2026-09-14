#!/usr/bin/env python3
"""Phase-1 stub. Settlement join comes after paper fills exist."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gap import clock, store


def main() -> int:
    store.init_db()
    print(f"{clock.today_ct()}: settle not wired in paper-only v0.1")
    print("orders today:", len(store.orders_for_date(clock.today_ct())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
