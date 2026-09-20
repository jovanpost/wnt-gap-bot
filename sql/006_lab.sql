-- 006_lab.sql  (wnt-gap v1.5.6)
-- Safe to run more than once. init_db() applies it at boot.
-- The order book at the decision time, saved once per (run, market). The shared
-- `depth` table gets pruned by the archiver, so we keep our own copy of the one
-- snapshot the strategy lab needs (capacity, size sweeps, decision-time mid).
create table if not exists gap_decision_books (
  run_id        bigint not null,
  market_ticker text   not null,
  decision_at   timestamptz,
  captured_at   timestamptz,
  age_s         integer,
  yes_book      jsonb,
  no_book       jsonb,
  source        text,
  saved_at      timestamptz not null default now(),
  primary key (run_id, market_ticker)
);
