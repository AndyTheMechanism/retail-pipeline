-- Sales per store and day.
--
-- The grain is set by the spine, not by the data: there is a row for every
-- working day of every store, even if nothing arrived from the source that day.
-- Emptiness stays emptiness — a coalesce to zero sits only where the data did
-- arrive. Zero means "we counted, and it came to zero", null means "there was
-- nothing to count", and the second must not be replaced by the first: that is
-- exactly the mistake that makes a mart update silently and wrongly.
--
-- The main decision of this mart: a return belongs to the order date, not to
-- the arrival date. Otherwise yesterday's number would never move — and this
-- whole project is about that number moving and the move being visible. The
-- reprocessing window of this mart and the mart_store_daily_revisions model
-- both rest on that; attributing by return date would leave both with nothing
-- to do.
--
-- Alongside sits returns_arrived_amount on the second axis — returns that
-- arrived on this day against orders of any date. It takes no part in revenue
-- and exists for exactly one thing: to show where a change to a past number
-- came from.
--
-- Orders are counted from headers rather than from lines: an order with no
-- lines is still an order. Cancelled ones are not thrown away but taken out
-- into a column of their own — a day when half the orders were cancelled has
-- to look different from a day when there simply were none.
--
-- ON THE REPROCESSING WINDOW.
--
-- The mart is incremental, and the write strategy is the same as the raw
-- load's — delete-and-insert by key. The symmetry is not accidental: a repeat
-- run for the same date has to leave the same state in both places, not doubled
-- rows.
--
-- A daily run rebuilds a window backwards rather than a single day, because a
-- return arrives later than its purchase and changes the revenue of a past day.
-- The size of the window is set by the return_window_days variable in
-- dbt_project.yml, and the number is justified there. Counting the whole
-- history every day is expensive, counting only today is wrong; the window is a
-- price chosen deliberately, not a compromise that crept in unnoticed.
--
-- Without run_date there is no filter and the mart is built in full. Both paths
-- give the same result — and that is what the checksums verify.

{{ config(
    materialized = 'incremental',
    unique_key = ['store_id', 'order_date'],
    incremental_strategy = 'delete+insert'
) }}

{% set run_date = var('run_date', none) %}
{% set window_days = var('return_window_days') %}

{% if is_incremental() and run_date %}
    {% set window_start = "date '" ~ run_date ~ "' - " ~ window_days %}
    {% set window_end = "date '" ~ run_date ~ "'" %}
    {% set in_window = "between " ~ window_start ~ " and " ~ window_end %}
{% else %}
    {% set in_window = none %}
{% endif %}

with spine as (
    select store_id, calendar_date
    from {{ ref('int_store_day_spine') }}
    {% if in_window %} where calendar_date {{ in_window }} {% endif %}
),

orders as (
    select
        store_id,
        order_date,
        count(*) filter (where not is_cancelled) as orders_count,
        count(*) filter (where is_cancelled)     as orders_cancelled_count
    from {{ ref('stg_orders') }}
    {% if in_window %} where order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

lines as (
    select
        store_id,
        order_date,
        count(*)         as lines_count,
        sum(quantity)    as units_sold,
        sum(line_amount) as revenue_gross
    from {{ ref('int_order_lines') }}
    where not is_cancelled
    {% if in_window %} and order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

returns_by_order_date as (
    select
        store_id,
        order_date,
        sum(returned_amount) as returns_amount
    from {{ ref('int_returns_attributed') }}
    -- A return belongs to the order date, so the window is cut on it too: the
    -- arrival date of these returns can be anything, today included.
    {% if in_window %} where order_date {{ in_window }} {% endif %}
    group by store_id, order_date
),

returns_by_arrival as (
    select
        store_id,
        returned_date,
        sum(returned_amount) as returns_arrived_amount
    from {{ ref('int_returns_attributed') }}
    -- This column lives on the second axis, and its window is cut on the
    -- arrival date.
    {% if in_window %} where returned_date {{ in_window }} {% endif %}
    group by store_id, returned_date
),

joined as (
    select
        sp.store_id,
        sp.calendar_date as order_date,

        -- Whether any orders arrived for this day at all. For an open store
        -- that is the norm; false across the whole network on one day means a
        -- partition that did not arrive, and the freshness gate
        -- assert_source_is_fresh catches it.
        o.store_id is not null as has_orders,

        o.orders_count,
        o.orders_cancelled_count,
        l.lines_count,
        l.units_sold,
        l.revenue_gross,
        r.returns_amount,
        ra.returns_arrived_amount

    from spine sp
    left join orders o
        on o.store_id = sp.store_id and o.order_date = sp.calendar_date
    left join lines l
        on l.store_id = sp.store_id and l.order_date = sp.calendar_date
    left join returns_by_order_date r
        on r.store_id = sp.store_id and r.order_date = sp.calendar_date
    left join returns_by_arrival ra
        on ra.store_id = sp.store_id and ra.returned_date = sp.calendar_date
)

select
    store_id,
    order_date,
    has_orders,

    orders_count,
    orders_cancelled_count,

    case when has_orders then coalesce(lines_count, 0)    end as lines_count,
    case when has_orders then coalesce(units_sold, 0)     end as units_sold,
    case when has_orders then coalesce(revenue_gross, 0)  end as revenue_gross,
    case when has_orders then coalesce(returns_amount, 0) end as returns_amount,

    -- This column lives on an axis of its own: the returns partition for a day
    -- arrives regardless of whether the orders did. An empty partition means
    -- there were no returns that day — a zero, not an unknown.
    coalesce(returns_arrived_amount, 0) as returns_arrived_amount,

    case when has_orders
         then coalesce(revenue_gross, 0) - coalesce(returns_amount, 0)
    end as revenue_net

from joined
