#!/usr/bin/env python3
"""Manual: detect tonight's event and send the Telegram file."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gap import pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    store.init_db()
    pipeline.register_commands()
    out = pipeline.send_prompt_for_today(force="--force" in sys.argv)
    print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
