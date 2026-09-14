# WNT Gap Trader — Addendum v1.1 (execution & backtest spec)

The original plan is still the architecture, system prompt, schema, and
guardrails. This addendum replaces only the naive "take the current price"
assumption.

## Execution — capped taker sweep

Not a maker rest waiting for a pullback.

1. Decision at **60 minutes after market open**.
2. `limit = model_probability − 15` (YES cents). Example: model 82, market 60 →
   limit 67. Symmetric NO: `limit_no = (100 − model) − 15`.
3. Limit order for full intended notional at that price. Because the limit sits
   through the market, it is immediately marketable and takes size ≤ limit.
4. Unfilled remainder rests at the **same** limit for the cancel window.
5. Cancel remainder **60 minutes after the order was sent**.
6. Partial fills are expected. Size PnL on what filled.

## Timing — two clocks

- Signal: wait 60 min after open so the market has an opinion (median forward
  move settles ~5–7¢).
- Execution: fill quality keeps improving out to 60 min after send, especially
  at $100. 90/120 min still an open question; the board exposes those windows
  on the same fixed batch.

## Four-way backtest

| | Hold to settlement | Scalp at model |
|---|---|---|
| **$1** | A | C |
| **$100** | B | D |

Each variant is a pure function of `(ticker, decision_time, notional, exit_rule)`
against a **read-only** tape. They do not share a remaining-volume counter.

Fixed-batch rule: sample decision points once; walk each order forward through
1 / 5 / 15 / 30 / 60 / 90 / 120 minute cancel windows.

Phase 1 paper uses whichever variant wins on fill-adjusted net per intended
dollar. The other three stay on the board.
