-- 007_v1510.sql  (wnt-gap v1.5.10)
-- Safe to run more than once. init_db() applies it at boot.

-- v1.5.10 B: when a resting order first goes from 0 to >0 filled contracts (used for the
-- decision-to-fill timing report). Never overwritten after the first fill.
alter table gap_orders add column if not exists first_fill_at timestamptz;

-- v1.5.10 A: SCALP book. Not a resting hold order like A-I -- a word can get several BUY
-- batches over the evening, each with its own paired SELL leg. One row per batch.
create table if not exists gap_scalp_batches (
  id                bigserial primary key,
  run_id            bigint not null,
  event_date        date not null,
  market_ticker     text not null,
  word              text not null,
  grok_probability  int,
  buy_at            timestamptz not null,
  buy_price_cents   int not null,
  buy_contracts     numeric not null,
  buy_cost_cents    int not null,
  buy_fee_cents     int not null default 0,
  status            text not null default 'resting_sell'
                     check (status in ('resting_sell', 'scalp_hit', 'fallback_sold')),
  sell_at           timestamptz,
  sell_price_cents  int,
  sell_contracts    numeric not null default 0,
  sell_proceeds_cents int not null default 0,
  sell_fee_cents    int not null default 0,
  net_cents         int,
  created_at        timestamptz not null default now()
);

create index if not exists gap_scalp_batches_run_ticker
  on gap_scalp_batches (run_id, market_ticker);
