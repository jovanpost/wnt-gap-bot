-- 004_our_price.sql
-- Permanent fix for the long/short inversion class of bug.
--
-- Until now gap_orders stored ONLY limit_price_cents, which is the YES price
-- even on orders where we are economically short (side = 'NO'). Every consumer
-- had to remember to flip it with 100 - limit. Any consumer that forgot booked
-- a short as a long. That is what printed Helicopter (SELL YES @ 90, word was
-- said) as +$8.93 instead of -$1.07.
--
-- wnt-nofade-bot -- the bot that is trusted and running live -- never has this
-- problem because it stores the price of the side it actually bought
-- (no_price_cents) plus what the exchange really filled and charged. This
-- migration brings gap_orders to the same shape.
--
-- Safe to run more than once.

alter table gap_orders
    add column if not exists our_price_cents      integer,
    add column if not exists avg_fill_price_cents double precision,
    add column if not exists fees_cents           double precision default 0;

comment on column gap_orders.our_price_cents is
    'Price in cents of the side WE hold. side=YES -> the YES limit. '
    'side=NO -> 100 - YES limit. Written at booking time. This is the only '
    'price any P&L calculation may use. Never derive it again downstream.';

comment on column gap_orders.avg_fill_price_cents is
    'What the exchange actually filled us at, when there is a real fill. '
    'NULL in paper, where the fill is assumed at our_price_cents.';

comment on column gap_orders.fees_cents is
    'Fees the exchange actually charged. 0 for maker fills.';

comment on column gap_orders.limit_price_cents is
    'YES-side limit as sent to the Kalshi API. This is the ORDER TICKET price, '
    'not the price we risk. Do NOT use it for P&L -- use our_price_cents.';

-- Backfill existing rows from the YES limit + side. This is exactly the
-- conversion the scattered helpers were doing, applied once, centrally.
update gap_orders
set our_price_cents = case
        when upper(coalesce(side, 'NO')) = 'YES' then limit_price_cents
        else 100 - limit_price_cents
    end
where our_price_cents is null
  and limit_price_cents is not null;

-- Guard rail: a price outside 1..99 is not a tradeable contract.
alter table gap_orders drop constraint if exists gap_orders_our_price_sane;
alter table gap_orders add constraint gap_orders_our_price_sane
    check (our_price_cents is null or (our_price_cents between 1 and 99));
