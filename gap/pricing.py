"""THE ONLY PLACE A PRICE CONVERSION MAY HAPPEN.

Background
----------
gap_orders stores two different prices and they are easy to confuse:

    limit_price_cents   the YES-side limit sent to the Kalshi API. This is the
                        order-ticket number. On a short (side='NO') it is the
                        price we are SELLING YES at -- it is NOT what we risk.

    our_price_cents     the price of the side we actually hold. This is the
                        only number that belongs in a P&L calculation.

Before v1.4.8 only limit_price_cents existed, so seven different modules each
re-derived our price with their own `100 - limit` helper. Any one of them
forgetting the flip booked a short as a long. Helicopter (SELL YES @ 90, word
was said) printed +$8.93 instead of -$1.07 that way.

Rule from here on
-----------------
No module may write `100 - limit` ever again. Every read of a price goes
through entry_price_cents() below. The live bot (wnt-nofade-bot) has no such
helper at all because it stores no_price_cents directly; this module is the
equivalent single source of truth for this repo.
"""
from __future__ import annotations

from . import fees as _fees


def entry_price_cents(order: dict) -> int:
    """Price in cents of the side we hold. The ONE accessor for P&L.

    Order of trust:
      1. avg_fill_price_cents -- what the exchange really filled us at
      2. our_price_cents      -- written at booking time
      3. legacy derivation    -- old rows booked before the migration
    """
    avg = order.get("avg_fill_price_cents")
    if avg not in (None, ""):
        try:
            px = int(round(float(avg)))
            if 0 < px < 100:
                return px
        except (TypeError, ValueError):
            pass

    stored = order.get("our_price_cents")
    if stored not in (None, ""):
        try:
            px = int(round(float(stored)))
            if 0 < px < 100:
                return px
        except (TypeError, ValueError):
            pass

    return _legacy_from_limit(order)


def _legacy_from_limit(order: dict) -> int:
    """Only for rows booked before 004_our_price.sql. New code must not rely
    on this -- if it fires on a fresh row, the booking path is broken."""
    yes_limit = int(order.get("limit_price_cents") or 0)
    if (order.get("side") or "NO").upper() == "YES":
        return yes_limit
    return 100 - yes_limit


def yes_ticket_cents(order: dict) -> int:
    """The YES-side number you would type into Kalshi. Display only."""
    return int(order.get("limit_price_cents") or 0)


def filled_contracts(order: dict) -> float:
    sim = order.get("_fill") or {}
    if sim.get("filled_ct") is not None:
        return float(sim["filled_ct"])
    stored = order.get("filled_contracts")
    if stored is not None:
        return float(stored or 0)
    return 0.0


def entry_fee_cents(order: dict) -> int:
    """Fees the exchange charged, else the Kalshi formula on our price."""
    recorded = order.get("fees_cents")
    if recorded not in (None, ""):
        try:
            return int(round(float(recorded)))
        except (TypeError, ValueError):
            pass
    return _fees.fee_cents(filled_contracts(order), entry_price_cents(order))


def hold_pnl_cents(order: dict, outcome: str) -> int:
    """Settled P&L in cents. Pure function of stored fields + the outcome."""
    filled = filled_contracts(order)
    px = entry_price_cents(order)
    fee = entry_fee_cents(order)
    return _fees.hold_pnl_cents(order.get("side") or "NO", filled, px, outcome, fee)


def cost_cents(order: dict) -> int:
    """Cash actually put at risk."""
    filled = filled_contracts(order)
    if filled <= 0:
        return 0
    return int(round(filled * entry_price_cents(order)))


def mark_value_cents(order: dict, mark_yes: int | None) -> int | None:
    """Current value of the position given a YES mark. Open positions only."""
    if mark_yes is None:
        return None
    filled = filled_contracts(order)
    if (order.get("side") or "NO").upper() == "YES":
        return int(round(filled * mark_yes))
    return int(round(filled * (100 - mark_yes)))


def unrealized_cents(order: dict, mark_yes: int | None) -> int | None:
    """Open-position P&L against a YES mark. NEVER call this on a settled row."""
    if mark_yes is None:
        return None
    filled = filled_contracts(order)
    if filled <= 0:
        return 0
    yes_ticket = yes_ticket_cents(order)
    fee = entry_fee_cents(order)
    if (order.get("side") or "NO").upper() == "YES":
        gross = filled * (mark_yes - yes_ticket)
    else:
        gross = filled * (yes_ticket - mark_yes)
    return int(round(gross - fee))
