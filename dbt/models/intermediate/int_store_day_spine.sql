-- A dense "store by day" grid — the grain of both marts.
--
-- Why it is needed. Without a spine a mart is a group by over whatever is
-- there, and a day whose data did not arrive looks like a missing row. Then
-- "the store was closed", "the data did not arrive" and "there were no sales"
-- are indistinguishable, and telling them apart is what
-- mart_store_daily_quality is for. With a spine such a day shows up as a row
-- with null metrics, and those are different things: zero means "we counted,
-- and it came to zero", null means "there was nothing to count".
--
-- The horizon is taken from the observed data rather than from a constant in
-- config.py. A constant would drift away from the generator silently, and the
-- first sign of it would be a wrong number in a mart rather than an error.
--
-- Cutting on opened_on is mandatory: 12 stores out of 120 opened inside the
-- horizon, and the days before an opening are honest emptiness rather than
-- missing data. Without the cut the grid would give 65,520 rows instead of
-- 62,690 — that is 2,830 invented store-days.

with observed as (
    select min(order_date) as first_day, max(order_date) as last_day
    from {{ ref('stg_orders') }}

    union all

    select min(traffic_date), max(traffic_date)
    from {{ ref('stg_store_traffic') }}
),

bounds as (
    select min(first_day) as first_day, max(last_day) as last_day
    from observed
),

calendar as (
    select generate_series(first_day, last_day, interval '1 day')::date as calendar_date
    from bounds
),

stores as (
    select store_id, opened_on
    from {{ ref('stg_stores') }}
)

select
    s.store_id,
    c.calendar_date
from stores s
join calendar c on c.calendar_date >= s.opened_on
