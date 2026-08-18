-- Quality flags: what cannot be counted for this store on this day.
--
-- A table of its own rather than columns on the marts, and that decision is
-- worth explaining. The marts hand out facts, and the judgement about whether a
-- fact can be trusted is a different kind of thing with a life of its own:
-- flags get added and refined, there can be several of them on one row, and
-- each has its own reason. As rows that grows naturally, as columns it does
-- not: every new flag would change the schema of both marts. The revision
-- log — mart_store_daily_revisions — is built in the same shape.
--
-- A flag does not break the chain and does not replace a number with an
-- estimate. It says exactly one thing: this number cannot be trusted, while the
-- rest of the network is counted as it always was. The difference from a stop
-- is not how bad the problem is, but whether reprocessing fixes it. A doubled
-- grain can be fixed, a broken door counter cannot, and stopping the whole
-- network over it means having no numbers for every store instead of one.
--
-- Grain: store, day, check name. The name is for machines — you filter and
-- join on it; the reason is for people — someone reads it in a report.

-- The model is rebuilt in full rather than by window. It reads both marts and
-- all returns, costs a fraction of a second, and a flag lost to a window
-- boundary would be worse than any saving.

{% set return_window_days = var('return_window_days') %}

with conversion_above_threshold as (
    -- The rule comes from a real door-counter incident: there cannot
    -- physically be more orders than visitors. The threshold is 0.95 rather
    -- than 1.0, because the device starts lying before it reaches equality.
    --
    -- The rule is written as a multiplication rather than a division, on
    -- purpose: with zero visitors the ratio does not exist, while the product
    -- gives zero, so live orders against a dead counter fall under the same
    -- flag instead of dropping out of the check as a null. A dead device is
    -- the worst case of a broken one, and losing it to the shape of an
    -- expression would be a shame.
    select
        store_id,
        traffic_date as flag_date,
        'conversion_above_threshold' as check_name,
        format(
            '%s orders against %s visitors — the counter cannot be trusted',
            orders_offline, visitors
        ) as reason,
        orders_offline::numeric  as measured_value,
        (visitors * 0.95)::numeric as threshold_value
    from {{ ref('mart_store_daily_conversion') }}
    where has_orders
      and has_traffic
      and orders_offline > visitors * 0.95
),

return_outside_window as (
    -- A return that arrived later than the reprocessing window changes the
    -- revenue of a day that will not be rebuilt again. The number for that day
    -- stays wrong, and the only way to find out is from here.
    --
    -- This is a flag, not a stop: the data is fine, it is the window that is
    -- too small. A test that breaks the chain because the window was chosen
    -- too narrow would stop work every day until somebody changed the
    -- constant.
    select
        store_id,
        order_date as flag_date,
        'return_outside_window' as check_name,
        format(
            '%s returns arrived after the window, the worst one %s days out against a window of %s',
            count(*), max(delay_days), {{ return_window_days }}
        ) as reason,
        max(delay_days)::numeric as measured_value,
        {{ return_window_days }}::numeric as threshold_value
    from {{ ref('int_returns_attributed') }}
    where delay_days > {{ return_window_days }}
    group by store_id, order_date
),

orders_partition_missing as (
    -- A day for which not a single order row arrived from the source.
    --
    -- This becomes a stop only for the target run date — that one is checked
    -- by assert_source_is_fresh, and there the chain halts. A hole left behind
    -- in history must not stop today's work, but it must not keep quiet about
    -- itself either: it is marked here and visible to a query.
    select
        store_id,
        order_date as flag_date,
        'orders_partition_missing' as check_name,
        'not a single order row arrived for this day' as reason,
        0::numeric    as measured_value,
        null::numeric as threshold_value
    from {{ ref('mart_store_daily_sales') }}
    where not has_orders
),

traffic_partition_missing as (
    select
        store_id,
        traffic_date as flag_date,
        'traffic_partition_missing' as check_name,
        'not a single traffic row arrived for this day' as reason,
        0::numeric    as measured_value,
        null::numeric as threshold_value
    from {{ ref('mart_store_daily_conversion') }}
    where not has_traffic
),

flagged as (
    select * from conversion_above_threshold
    union all
    select * from return_outside_window
    union all
    select * from orders_partition_missing
    union all
    select * from traffic_partition_missing
)

select
    store_id,
    flag_date,
    check_name,
    reason,
    measured_value,
    threshold_value
from flagged
