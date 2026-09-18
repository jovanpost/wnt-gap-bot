"""Kalshi fee: 0.07 × contracts × P × (1−P), rounded up to a cent."""
from __future__ import annotations

import math


def fee_cents(contracts: float, price_cents: int) -> int:
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    dollars = 0.07 * float(contracts) * p * (1.0 - p)
    return int(math.ceil(dollars * 100.0 - 1e-12))


def hold_pnl_cents(side: str, filled: float, avg_fill_cents: int,
                   outcome: str, fees: int) -> int:
    """Payout $1 if the side is correct, else $0. Fee already in cents."""
    if filled <= 0:
        return -fees
    won = (side == "YES" and outcome == "yes") or (side == "NO" and outcome == "no")
    if won:
        gross = filled * (100 - avg_fill_cents)
    else:
        gross = filled * (-avg_fill_cents)
    return int(round(gross - fees))


def scalp_pnl_cents(side: str, filled: float, entry_cents: int, exit_cents: int,
                    entry_fee: int, exit_fee: int) -> int:
    """Used ONLY by gap/backtest.py's historical fixture curve (the old
    128-night comparison, unrelated to live paper trading). Removing this
    when scalp was dropped from the live bot was a mistake -- backtest.py
    still runs its own internal hold-vs-scalp comparison on static
    historical data for the Cancel-window curve tab. Restored verbatim from
    the pre-v1.5.0 file so that comparison keeps working unchanged."""
    if filled <= 0:
        return -(entry_fee + exit_fee)
    # Both prices are the price of OUR side.
    gross = filled * (exit_cents - entry_cents)
    return int(round(gross - entry_fee - exit_fee))
