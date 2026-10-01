-- v1.8.0: challenger forecasts (paper only, NEVER traded). One row per night + model + word.
-- model = 'gemini:<model id>' or 'baseline-v1'. Scored against gap_results next to Grok every Saturday.
create table if not exists gap_shadow_forecasts (
  id              bigserial primary key,
  event_date      date not null,
  event_ticker    text,
  market_ticker   text,
  word            text not null,
  model           text not null,
  probability     int not null,
  reasoning       text,
  raw             text,
  made_at         timestamptz not null default now(),
  seconds         numeric,
  unique (event_date, model, word)
);
create index if not exists gap_shadow_forecasts_date on gap_shadow_forecasts (event_date);
alter table gap_shadow_forecasts enable row level security;
