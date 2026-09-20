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


def gap_points_exact(probability: int, bid: int | None, ask: int | None,
                     market_prob: float | None = None) -> float | None:
    """Grok minus market mid, in points. Exact: with a bid and an ask it is done in
    integers (2p - (bid+ask)) / 2, so 'exactly 15.0' really is exactly 15.0. The old
    float version, (p/100 - mid) * 100, produced 15.000000000000002 for 100 different
    (p, mid) pairs and booked them even though 15.0 is not > 15."""
    if bid is not None and ask is not None:
        return (2 * int(probability) - (int(bid) + int(ask))) / 2.0
    if market_prob is None:
        return None
    return round(int(probability) - float(market_prob) * 100.0, 6)


def decide(
    probability: int,
    market_prob: float | None,
    yes_bid_cents: int | None,
    yes_ask_cents: int | None,
    threshold: int | None = None,
    notional: float | None = None,
) -> dict | None:
    threshold = C.GAP_THRESHOLD if threshold is None else threshold
    gap_points = gap_points_exact(probability, yes_bid_cents, yes_ask_cents, market_prob)
    if gap_points is None:
        return None
    if not abs(gap_points) > threshold:  # strictly greater. 15.0 is NOT a signal at 15.
        return None

    # Economic side. Kalshi only lists YES.
    side = "YES" if gap_points > 0 else "NO"
    kalshi_action = "buy_yes" if side == "YES" else "sell_yes"
    if yes_bid_cents is not None and yes_ask_cents is not None:
        mid_cents = int(round((yes_bid_cents + yes_ask_cents) / 2.0))
    else:
        mid_cents = int(round(float(market_prob) * 100.0))
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


def decide_exec(
    probability: int,
    yes_bid_cents: int | None,
    yes_ask_cents: int | None,
    threshold: int | None = None,
    notional: float | None = None,
) -> dict | None:
    """Book I. Edge against the price you would REALLY pay, not the mid.
      buy YES  costs the ask:            edge = Grok - ask
      buy NO   (sell YES) gets the bid:  edge = bid - Grok
    Trade only if edge is strictly greater than the threshold. Order is placed AT the
    touch (the ask, or the bid), so it is marketable immediately."""
    threshold = C.EDGE_EXEC_THRESHOLD if threshold is None else threshold
    if yes_bid_cents is None or yes_ask_cents is None:
        return None
    p, bid, ask = int(probability), int(yes_bid_cents), int(yes_ask_cents)
    edge_yes = p - ask
    edge_no = bid - p
    if edge_yes > threshold and edge_yes >= edge_no:
        side, yes_limit, edge = "YES", ask, edge_yes
    elif edge_no > threshold:
        side, yes_limit, edge = "NO", bid, edge_no
    else:
        return None
    our_px = our_price_cents(side, yes_limit)
    if yes_limit <= 0 or yes_limit >= 100 or our_px <= 0 or our_px >= 100:
        return None
    sized = notional is not None and notional > 0
    contracts = round(notional / (our_px / 100.0), 2) if sized else 0.0
    return {
        "side": side,
        "kalshi_action": "buy_yes" if side == "YES" else "sell_yes",
        "yes_price_cents": yes_limit,
        "our_price_cents": our_px,
        "limit_price_cents": yes_limit,
        "contracts": contracts,
        "cost_cents": int(round(contracts * our_px)) if sized else 0,
        "gap_points": float(edge),  # for book I this is the executable edge
        "threshold": threshold,
        "execution_model": C.EXECUTION_MODEL,
        "touch_cents": yes_limit,
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


def order_for_rule(rule: str, probability: int, bid: int | None, ask: int | None,
                   valid: bool, notional: float) -> dict | None:
    """What a book with this rule SHOULD book for one word. The booking code and the
    weekly RULE AUDIT both call this, so they can never drift apart.
    Returns None = no order."""
    p = int(probability)
    if rule == "grok10":  # ignores the market completely
        g = grok10_limit(p)
        if not g:
            return None
        our_px = int(g.get("our_price_cents") or our_price_cents(g["side"], g["yes_price_cents"]))
        contracts = round(notional / (our_px / 100.0), 2)
        return {
            "side": g["side"],
            "yes_price_cents": g["yes_price_cents"],
            "our_price_cents": our_px,
            "contracts": contracts,
            "cost_cents": int(round(contracts * our_px)),
            "gap_points": 0.0,
            "threshold": 0,
        }
    # Every other rule needs a real market. Invalid or one-sided quote = no order.
    if not valid or bid is None or ask is None:
        return None
    if rule in ("fade15", "fade15_gate50"):
        mid = ((int(bid) + int(ask)) / 2.0) / 100.0
        d = decide(p, mid, int(bid), int(ask), notional=notional)
        if not d:
            return None
        if rule == "fade15_gate50" and not fade_gate_ok(p, d["side"]):
            return None
        if not d.get("our_price_cents"):
            d["our_price_cents"] = our_price_cents(d["side"], d["yes_price_cents"])
        return d
    if rule == "edge_exec":
        return decide_exec(p, int(bid), int(ask), notional=notional)
    return None
