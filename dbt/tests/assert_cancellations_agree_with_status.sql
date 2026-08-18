-- Cancellations and order statuses agree in both directions.
--
-- The source reports a cancellation twice: as a status on the order itself, and
-- as a separate row in the cancellations table that arrives a day or two later.
-- One fact represented twice is always a place where the two will diverge, and
-- they can diverge in either direction:
--
--   order cancelled, no cancellation row  — it was lost on the way;
--   cancellation row, order still live    — it arrived, the status never moved.
--
-- The second case is the dangerous one: an order whose status went missing
-- keeps being counted as revenue.
--
-- ABOUT THE EDGE OF THE HORIZON, and that is the main thing in this file.
--
-- The first check is bounded by dates, the second is not, and the difference is
-- not arbitrary. A cancellation arrives 0-2 days after its order, so orders
-- from the last two days of the horizon may not have a cancellation row yet —
-- not because it was lost, but because its time has not come. On this data that
-- is exactly the case: 56 orders on 29 and 30 June are cancelled by status and
-- carry no cancellation row, because that row's date fell past the end of the
-- horizon and the generator dropped it.
--
-- The first version of this test did not account for that and went red on data
-- that was perfectly fine. The lesson generalises and is worth keeping: a check
-- that compares two sources with different delays must be bounded by dates to
-- the depth of that delay. Otherwise it is red always, someone switches it off,
-- and there is no gate any more.
--
-- The other direction needs no bound: a cancellation row against a live order
-- is a desync on any day, the last one included.

{% set cancellation_delay_days = var('cancellation_delay_days', 2) %}

with settled as (
    -- The date up to which the arrival window for cancellations has closed.
    select max(order_date) - {{ cancellation_delay_days }} as last_settled_date
    from {{ ref('stg_orders') }}
),

cancelled_without_row as (
    select
        o.order_id,
        o.order_date,
        true  as status_says_cancelled,
        false as has_cancellation_row
    from {{ ref('stg_orders') }} o, settled s
    where o.is_cancelled
      and o.order_date <= s.last_settled_date
      and not exists (
          select 1 from {{ ref('stg_cancellations') }} c
          where c.order_id = o.order_id
      )
),

row_without_cancelled_status as (
    select
        c.order_id,
        c.order_date,
        false as status_says_cancelled,
        true  as has_cancellation_row
    from {{ ref('stg_cancellations') }} c
    where exists (
        select 1 from {{ ref('stg_orders') }} o
        where o.order_id = c.order_id
          and not o.is_cancelled
    )
)

select * from cancelled_without_row
union all
select * from row_without_cancelled_status
