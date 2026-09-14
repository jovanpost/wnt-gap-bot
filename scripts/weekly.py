#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gap import store, weekly


def main() -> int:
    store.init_db()
    force = "--force" in sys.argv
    print(weekly.send_week_report(force=force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
