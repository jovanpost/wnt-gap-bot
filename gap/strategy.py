"""Capped taker sweep on the YES contract. NO = sell YES."""
from __future__ import annotations

import re

from . import config as C


def cluster_key(word: str, carrying_story: str | None) -> str:
    story = (carrying_story or "").strip().lower()
    if story and story not in ("none", "none obvious", "n/a"):
        tokens = re.findall(r"[a-z0-9]+", story)
        if tokens:
            return " ".join(tokens[:2])
    base = re.sub(r"\s+\d+\+$", "", word.lower())
    base = re.sub(r"\s+\[\d+\]$", "", base)
    return base.strip() or word.lower()


def yes_limit_cents(
    economic_side: str,
    model_prob: int,
    mid_cents: int | None = None,
    take: int | None = None,
    threshold: int | None = None,
) -> int:
    """
    Walk TAKE cents from the quoted mid toward the model.
    Never cross the model. Old 'model − 15' is gone — that spent the
    whole filter on a minimum gap.
    """
    take = C.LIMIT_OFFSET_CENTS if take is None else take
    if mid_cents is None:
        # fallback only if we have no quote; keep a stub off the model
        if economic_side == "YES":
            return int(model_prob) - take
        return int(model_prob) + take
    mid = int(mid_cents)
    if economic_side == "YES":
        return min(mid + take, int(model_prob) - 1)
    return max(mid - take, int(model_prob) + 1)


def our_price_cents(economic_side: str, yes_limit: int) -> int:
    """Dollars-at-risk price: YES limit if we buy YES, 100−limit if we sell YES."""
    if economic_side == "YES":
        return yes_limit
    return 100 - yes_limit


def sweep_limit_cents(side: str, model_prob: int, threshold: int | None = None,
                     mid_cents: int | None = None) -> int:
    yes_px = yes_limit_cents(side, model_prob, mid_cents=mid_cents, threshold=threshold)
    return our_price_cents(side, yes_px)


def decide(
    probability: int,
    market_prob: float | None,
    yes_bid_cents: int | None,
    yes_ask_cents: int | None,
    threshold: int | None = None,
    notional: float | None = None,
) -> dict | None:
    threshold = C.GAP_THRESHOLD if threshold is None else threshold
    if market_prob is None:
        return None
    gap_points = (probability / 100.0 - market_prob) * 100.0
    if abs(gap_points) <= threshold:
        return None

    # Economic side. Kalshi only lists YES.
    side = "YES" if gap_points > 0 else "NO"
    kalshi_action = "buy_yes" if side == "YES" else "sell_yes"
    if yes_bid_cents is not None and yes_ask_cents is not None:
        mid_cents = int(round((yes_bid_cents + yes_ask_cents) / 2.0))
    else:
        mid_cents = int(round(market_prob * 100.0))
    yes_limit = yes_limit_cents(side, probability, mid_cents=mid_cents)
    our_px = our_price_cents(side, yes_limit)
    if yes_limit <= 0 or yes_limit >= 100 or our_px <= 0 or our_px >= 100:
        return None

    if side == "YES":
        touch = yes_ask_cents
        marketable = touch is not None and touch <= yes_limit
    else:
        # Selling YES: marketable when the YES bid is at or above our ask.
        touch = yes_bid_cents
        marketable = touch is not None and touch >= yes_limit

    sized = notional is not None and notional > 0
    contracts = round(notional / (our_px / 100.0), 2) if sized else 0.0
    cost_cents = int(round(contracts * our_px)) if sized else 0
    phone_notional_cents = int(round(contracts * yes_limit)) if sized else 0
    return {
        "side": side,
        "kalshi_action": kalshi_action,
        "yes_price_cents": yes_limit,
        "our_price_cents": our_px,
        "limit_price_cents": yes_limit,  # API / phone number
        "contracts": contracts,
        "cost_cents": cost_cents,
        "phone_notional_cents": phone_notional_cents,
        "gap_points": round(gap_points, 2),
        "threshold": threshold,
        "execution_model": C.EXECUTION_MODEL,
        "marketable_now": bool(marketable),
        "touch_cents": touch,
        "notional_dollars": notional,
    }


def apply_caps(candidates: list[dict], **_kwargs) -> list[dict]:
    """No caps. Every gap that passed decide() is booked on that book."""
    return list(candidates)


def grok_prefers(probability: int) -> str | None:
    """Side Grok puts at >= 50.01. Integer forecasts: YES if p>=51, NO if p<=49."""
    p = int(probability)
    if p >= 51:
        return "YES"
    if p <= 49:
        return "NO"
    return None


def fade_gate_ok(probability: int, side: str) -> bool:
    pref = grok_prefers(probability)
    return pref is not None and pref == side


def grok10_limit(probability: int) -> dict | None:
    """One rest: 10¢ cheap to Grok on Grok's side. YES@p-10 or NO@(100-p)-10."""
    pref = grok_prefers(probability)
    if pref is None:
        return None
    p = int(probability)
    if pref == "YES":
        yes_limit = p - 10
        side = "YES"
    else:
        no_cost = (100 - p) - 10
        yes_limit = 100 - no_cost  # == p + 10
        side = "NO"
    if yes_limit <= 0 or yes_limit >= 100:
        return None
    our_px = our_price_cents(side, yes_limit)
    if our_px <= 0 or our_px >= 100:
        return None
    return {
        "side": side,
        "yes_price_cents": yes_limit,
        "our_price_cents": our_px,
        "gap_points": None,
        "threshold": 0,
        "kalshi_action": "buy_yes" if side == "YES" else "sell_yes",
    }
