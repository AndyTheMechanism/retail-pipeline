-- Reconciling the assembled order lines against the raw layer for a period —
-- a direct descendant of a production reconciliation against a reference.
--
-- The test goes to the raw layer directly through source rather than through
-- staging, and redoes the deduplication itself. That is the whole point: a
-- check built on the same models it checks would repeat their mistake and
-- never notice it. Two independent paths to one number, and they are obliged
-- to agree.
--
-- It sits on the intermediate layer rather than on a mart, and that is a
-- decision too. A dbt test runs after its model has been built — so a test on a
-- mart fails once the wrong mart is already in the database. To keep a broken
-- number from getting that far, the reconciliation has to sit higher up the
-- graph, and then a failed test simply does not let the marts be built.
--
-- A period rather than the whole horizon: the reconciliation is part of every
-- daily run, and there is no point going over three and a half million rows
-- every time. The size of the window is set by the reconcile_days variable.

{% set reconcile_days = var('reconcile_days', 30) %}

with target as (
    select
        {% if var('run_date', none) %}
        date '{{ var("run_date") }}'
        {% else %}
        (select max(order_date) from {{ source('raw', 'orders') }})
        {% endif %} as day
),

period as (
    select
        day - {{ reconcile_days }} as first_day,
        day                        as last_day
    from target
),

raw_lines as (
    select
        i.order_id,
        i.line_no,
        i.order_date,
        i.line_amount,
        row_number() over (
            partition by i.order_id, i.line_no
            order by i.line_amount, i.quantity, i.unit_price, i.sku
        ) as copy_no
    from {{ source('raw', 'order_items') }} i, period p
    where i.order_date between p.first_day and p.last_day
),

raw_expected as (
    select
        o.store_id,
        l.order_date,
        count(*)            as lines_count,
        sum(l.line_amount)  as revenue_gross
    from raw_lines l
    join {{ source('raw', 'orders') }} o on o.order_id = l.order_id
    where l.copy_no = 1
      and o.status <> 'cancelled'
    group by o.store_id, l.order_date
),

model_actual as (
    select
        store_id,
        order_date,
        count(*)         as lines_count,
        sum(line_amount) as revenue_gross
    from {{ ref('int_order_lines') }}, period p
    where order_date between p.first_day and p.last_day
      and not is_cancelled
    group by store_id, order_date
)

select
    coalesce(e.store_id, a.store_id)     as store_id,
    coalesce(e.order_date, a.order_date) as order_date,
    e.revenue_gross                      as expected_revenue,
    a.revenue_gross                      as actual_revenue,
    e.lines_count                        as expected_lines,
    a.lines_count                        as actual_lines
from raw_expected e
full outer join model_actual a
    on a.store_id = e.store_id
   and a.order_date = e.order_date
where e.revenue_gross is distinct from a.revenue_gross
   or e.lines_count is distinct from a.lines_count
