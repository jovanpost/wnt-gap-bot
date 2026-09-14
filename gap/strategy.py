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


def yes_limit_cents(economic_side: str, model_prob: int, threshold: int | None = None) -> int:
    """
    Price we send to Kalshi on the YES contract.

    Buy YES:  model − 15   (82 → 67)
    Sell YES: model + 15   (20 → 35)  == 100 − ((100 − model) − 15)
    """
    thr = C.GAP_THRESHOLD if threshold is None else threshold
    if economic_side == "YES":
        return int(model_prob) - thr
    return int(model_prob) + thr


def our_price_cents(economic_side: str, yes_limit: int) -> int:
    """Dollars-at-risk price: YES limit if we buy YES, 100−limit if we sell YES."""
    if economic_side == "YES":
        return yes_limit
    return 100 - yes_limit


def sweep_limit_cents(side: str, model_prob: int, threshold: int | None = None) -> int:
    """Back-compat: economic-side limit (NO cents when side=NO)."""
    yes_px = yes_limit_cents(side, model_prob, threshold)
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
    notional = C.NOTIONAL_DOLLARS if notional is None else notional
    if market_prob is None:
        return None
    gap_points = (probability / 100.0 - market_prob) * 100.0
    if abs(gap_points) <= threshold:
        return None

    # Economic side. Kalshi only lists YES.
    side = "YES" if gap_points > 0 else "NO"
    kalshi_action = "buy_yes" if side == "YES" else "sell_yes"
    yes_limit = yes_limit_cents(side, probability, threshold)
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

    contracts = round(notional / (our_px / 100.0), 2)
    cost_cents = int(round(contracts * our_px))  # risk / collateral
    phone_notional_cents = int(round(contracts * yes_limit))
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


def apply_caps(candidates: list[dict]) -> list[dict]:
    by_cluster: dict[str, int] = {}
    kept: list[dict] = []
    ordered = sorted(candidates, key=lambda r: abs(r["gap_points"]), reverse=True)
    night_cap_cents = int(C.BANKROLL_DOLLARS * C.NIGHT_CAP_FRACTION * 100)
    spent = 0
    for row in ordered:
        ck = row.get("cluster_key") or row["word"]
        if by_cluster.get(ck, 0) >= C.CLUSTER_CAP:
            continue
        if spent + row["cost_cents"] > night_cap_cents:
            continue
        by_cluster[ck] = by_cluster.get(ck, 0) + 1
        spent += row["cost_cents"]
        kept.append(row)
    return kept
