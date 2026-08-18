-- Conversion per store and day: orders per visitor.
--
-- The numerator is offline orders, cancelled ones excluded. Offline, because
-- the door counter sees neither the web nor the app, and mixing them in would
-- give a conversion that grows as online grows. Cancelled ones excluded,
-- because a cancelled order is not a sale — and revenue in the neighbouring
-- mart is counted over the same set of orders: one mart, one set of orders.
--
-- A measurement worth knowing when reading these numbers: the synthetic data
-- builds footfall from offline orders including the cancelled ones, so the
-- measured conversion sits systematically below the intended one by roughly the
-- cancellation share, about 3%. It has no effect at all on whether the "orders
-- above traffic × 0.95" threshold fires, but a range check would have to be
-- counted from the shifted number rather than from the intended one.
--
-- Zero visitors with live orders is not a zero but a message from the source
-- that the device was not counting. Hence the nullif and a null in the result,
-- rather than a division by zero or an infinity. The quality flag itself is not
-- raised here: the mart hands out the ingredients, the judgement is made by the
-- gate.
--
-- THIS MART HAS NO REPROCESSING WINDOW, and that is the main thing to know
-- about it.
--
-- The sales mart rebuilds 28 days backwards, because a return arrives later
-- than the purchase and changes the revenue of a past day. Here there is
-- nothing to change after the fact: neither footfall nor orders for a past day
-- are rewritten — they have no second date on which they could arrive later. So
-- a run touches exactly the target date.
--
-- Hence a rule worth more than the setting itself: the reprocessing window is
-- not a pipeline-wide setting but a property of a particular mart. Setting it
-- the same everywhere means either computing too much or not computing enough.

{{ config(
    materialized = 'incremental',
    unique_key = ['store_id', 'traffic_date'],
    incremental_strategy = 'delete+insert'
) }}

{% set run_date = var('run_date', none) %}

{% if is_incremental() and run_date %}
    {% set on_run_date = "= date '" ~ run_date ~ "'" %}
{% else %}
    {% set on_run_date = none %}
{% endif %}

with spine as (
    select store_id, calendar_date
    from {{ ref('int_store_day_spine') }}
    {% if on_run_date %} where calendar_date {{ on_run_date }} {% endif %}
),

traffic as (
    select store_id, traffic_date, visitors
    from {{ ref('stg_store_traffic') }}
    {% if on_run_date %} where traffic_date {{ on_run_date }} {% endif %}
),

offline_orders as (
    select
        store_id,
        order_date,
        count(*) as orders_offline
    from {{ ref('stg_orders') }}
    where is_offline and not is_cancelled
    {% if on_run_date %} and order_date {{ on_run_date }} {% endif %}
    group by store_id, order_date
),

orders_present as (
    select distinct store_id, order_date
    from {{ ref('stg_orders') }}
    {% if on_run_date %} where order_date {{ on_run_date }} {% endif %}
),

joined as (
    select
        sp.store_id,
        sp.calendar_date as traffic_date,

        t.store_id is not null  as has_traffic,
        op.store_id is not null as has_orders,

        t.visitors,
        oo.orders_offline

    from spine sp
    left join traffic t
        on t.store_id = sp.store_id and t.traffic_date = sp.calendar_date
    left join offline_orders oo
        on oo.store_id = sp.store_id and oo.order_date = sp.calendar_date
    left join orders_present op
        on op.store_id = sp.store_id and op.order_date = sp.calendar_date
)

select
    store_id,
    traffic_date,
    has_traffic,
    has_orders,

    visitors,

    case when has_orders then coalesce(orders_offline, 0) end as orders_offline,

    case when has_orders and has_traffic
         then coalesce(orders_offline, 0)::numeric / nullif(visitors, 0)
    end as conversion

from joined
