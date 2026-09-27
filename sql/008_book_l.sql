-- Book L: LIVE real-money trading. Deliberately its own table, NOT gap_orders --
-- gap_orders is deleted-and-reinserted every time a night is (re)booked (see
-- gap/pipeline.py book_from_forecasts), which is safe for paper rows but would
-- risk orphaning a REAL resting Kalshi order (we'd lose the order_id needed to
-- cancel or reconcile it). gap_l_orders is only ever appended to.

create table if not exists gap_l_orders (
  id                    bigserial primary key,
  event_date            date not null,
  market_ticker         text not null,
  word                  text not null,
  forecast_id           bigint,
  run_id                bigint,
  side                  text not null,               -- always 'NO' for L, kept for clarity/audit
  limit_price_cents     int not null,                 -- YES limit we rest at (nofade/gap convention)
  our_price_cents       int not null,                 -- NO price we pay = 100 - limit_price_cents
  contracts             numeric not null,             -- intended size at $L_NOTIONAL_DOLLARS
  cost_cents            int not null,
  gap_points            numeric,
  quote_bid_cents       int,
  quote_ask_cents       int,
  quote_captured_at     timestamptz,
  client_order_id       text not null unique,
  kalshi_order_id       text,
  status                text not null default 'pending',
    -- pending -> resting -> (filled | partially_filled | cancelled | expired | rejected) -> settled
  reject_reason         text,
  placed_at             timestamptz not null default now(),
  cancel_deadline_at    timestamptz not null,          -- 5:29 CT show529, same as every paper book
  cancel_requested_at   timestamptz,
  cancel_confirmed_at   timestamptz,
  filled_contracts      numeric not null default 0,
  avg_fill_price_cents  int,
  first_fill_at         timestamptz,
  fees_cents            int not null default 0,
  result                text,                          -- 'yes' / 'no' / 'void' from Kalshi, once known
  realized_pnl_cents    int,
  settled_at            timestamptz,
  created_at            timestamptz not null default now()
);

create unique index if not exists gap_l_orders_one_per_night
  on gap_l_orders (event_date, market_ticker);

create index if not exists gap_l_orders_status on gap_l_orders (status);
create index if not exists gap_l_orders_event_date on gap_l_orders (event_date);

-- One row per ISO week, the circuit breaker's own bookkeeping. Updated as L's
-- orders settle; read before placing any new L order this week.
create table if not exists gap_l_weekly (
  iso_week      text primary key,          -- e.g. '2026-W40'
  net_cents     int not null default 0,
  trades        int not null default 0,
  paused        boolean not null default false,
  paused_at     timestamptz,
  paused_reason text,
  updated_at    timestamptz not null default now()
);

-- Arm-once-per-night and other small L flags reuse the existing gap_state
-- key/value table (get_state/set_state) under an "l_" key prefix -- no new
-- table needed for that.
