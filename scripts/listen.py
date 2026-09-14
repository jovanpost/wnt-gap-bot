#!/usr/bin/env python3
"""Local Telegram listener without Streamlit. Ctrl-C to stop."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gap import notify, pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    store.init_db()
    pipeline.register_commands()
    notify.start_listener()
    print("listening for /gap_prep and JSON replies…")
    while True:
        time.sleep(30)
        try:
            pipeline.poll_once()
        except Exception:
            logging.exception("poll")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
