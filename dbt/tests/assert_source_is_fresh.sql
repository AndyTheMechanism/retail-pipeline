-- Source freshness: the partition for the target date arrived and is not empty.
--
-- Measured against the TARGET RUN DATE, not against the wall clock. The data
-- horizon is fixed and ends in the past, so a "fresh as of today" check would
-- be red always and rightly so — and a disabled gate is worse than an absent
-- one.
--
-- In normal operation the scheduler passes the date in — the DAG substitutes
-- it into --vars, and then it is genuinely external. By hand it is set like
-- this:
--
--     make test VARS='{run_date: 2025-02-26}'
--
-- Two weak spots, named out loud because they are there.
--
-- First. If run_date is not passed, the date is taken from the data itself, and
-- a check like that will not notice that the whole tail failed to arrive: the
-- maximum will simply turn out to be yesterday's. One thing softens it: the
-- maximum is taken across two sources at once — if the orders for the last day
-- did not arrive but the traffic did, the date comes from traffic and the
-- check fires.
--
-- Second, and this one is more serious. What is checked is that the partition
-- IS THERE, not that it is complete: a day that arrived half full passes the
-- gate and gets published as an understated number. Reconciling the order lines
-- against the raw layer will not catch it either — it compares the model with
-- the same incomplete raw data and honestly agrees. The hole can be closed by
-- comparing the row count for a date against the median of the neighbouring
-- days; that is not done here, and it has to be known before anyone leans on
-- the gate.
--
-- The test sits on the source rather than on a model, deliberately: freshness
-- is a property of what arrived, and it has to fail before anything gets built.
-- The mechanics of the stop are not obvious, though, and are worth a sentence:
-- dbt build adds edges from a test to the children of the node it checks, so a
-- failed freshness check takes the spine, both marts and the snapshot down
-- with it. The staging layer still builds — harmless, those are views over the
-- same raw data.

with target as (
    select
        {% if var('run_date', none) %}
        date '{{ var("run_date") }}'
        {% else %}
        greatest(
            (select max(order_date) from {{ source('raw', 'orders') }}),
            (select max(traffic_date) from {{ source('raw', 'store_traffic') }})
        )
        {% endif %} as day
),

arrived as (
    select
        t.day,
        (select count(*) from {{ source('raw', 'orders') }} o
          where o.order_date = t.day) as order_rows,
        (select count(*) from {{ source('raw', 'store_traffic') }} s
          where s.traffic_date = t.day) as traffic_rows
    from target t
)

select 'raw.orders' as source_table, day as expected_date, order_rows as rows_found
from arrived
where order_rows = 0

union all

select 'raw.store_traffic', day, traffic_rows
from arrived
where traffic_rows = 0
