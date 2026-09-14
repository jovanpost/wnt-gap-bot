#!/usr/bin/env python3
"""Print the four-way fixture board (no Streamlit)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gap import backtest, config as C


def main() -> int:
    decisions, tape = backtest.build_fixture()
    batch = backtest.fixed_batch(decisions)
    four = backtest.run_four_way(batch, tape, cancel_min=C.CANCEL_AFTER_MIN)
    print(f"batch {len(batch)} / {len(decisions)}  cancel={four['cancel_min']}m  winner={four['winner']}")
    for row in four["summaries"]:
        print(
            f"{row['variant']}  fill-adj {row['fill_adjusted_edge']:+.4f}  "
            f"net ${row['net_dollars']:+.2f}  fill {row['avg_fill_pct']:.1f}%  "
            f"full {row['fully_filled_pct']:.1f}%"
        )
    print("\n$100 hold curve (fixed batch):")
    for row in backtest.cancel_window_curve(batch, tape, 100.0, "hold"):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
