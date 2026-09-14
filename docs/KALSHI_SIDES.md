# Kalshi has one contract: YES

There is no separate NO instrument on the trade API we use.
Buying NO is **selling YES** at the complementary price. Same mapping
the 26¢ no-fade bot already lives on: Buy NO @ 26¢ = Sell YES @ 74¢.

## Gap → order

`gap = model_yes − market_yes` (points).

| | Model hotter (gap > +15) | Model colder (gap < −15) |
|---|---|---|
| Economic | buy YES | buy NO |
| Kalshi API | `action=buy`, `side=yes` | `action=sell`, `side=yes` |
| Limit on YES | `model − 15` | `model + 15` |
| Equivalent NO price | `100 − that` | `100 − that` |
| Example | model 82, mkt 60 → **buy YES @ 67** | model 20, mkt 50 → **sell YES @ 35** (= buy NO @ 65) |

Why `model + 15` when selling YES: it is the complement of
`limit_no = (100 − model) − 15`.

```
sell_yes_limit = 100 − ((100 − model) − 15) = model + 15
```

Worked NO example (addendum inverted):

- Model says 20% YES. Market is 50¢ YES (50¢ NO).
- We will not pay more than 65¢ for NO → we will not sell YES below 35¢.
- Current YES bid ~50 ≥ 35, so the sell is immediately marketable.
- It takes the YES bid and any deeper bids ≥ 35¢, then rests as an **ask
  on YES at 35¢** for 60 minutes.

## Sizing

Notional is **dollars at risk** (collateral), not the phone’s YES notional.

- Buy YES @ 67¢, $100 risk → `count = 100 / 0.67`
- Sell YES @ 35¢, $100 risk → risk per contract is 65¢ NO → `count = 100 / 0.65`
  Phone will show `count × $0.35`. Ignore that number for bankroll.

## Fills

- Buy YES fills when YES ask ≤ our YES limit.
- Sell YES fills when YES bid ≥ our YES limit.
  (Same as NO ask ≤ our NO limit.)

Last trade ≠ fill. Score `filled_contracts > 0`.
