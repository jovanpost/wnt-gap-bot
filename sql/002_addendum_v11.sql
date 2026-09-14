-- Re-run on an existing gap_* database that already applied 001.
alter table gap_orders add column if not exists execution_model text default 'capped_sweep';
alter table gap_runs add column if not exists market_open_at timestamptz;
alter table gap_runs add column if not exists decision_at timestamptz;
