-- The weekly revenue baseline: a 7-day moving average.
--
-- It lives as a model of its own rather than as a column on the sales mart, and
-- the reason is practical. The sales mart is incremental and is rebuilt by a
-- window of dates; a seven-day moving window pulls in neighbouring rows, and
-- recomputing one day would start changing seven. Keeping them apart costs one
-- join in a dashboard and removes the need to explain any of that: the trend
-- model itself is built in full, and on 62 thousand rows a full rebuild is
-- cheap.

with sales as (
    select
        store_id,
        order_date,
        revenue_net
    from {{ ref('mart_store_daily_sales') }}
)

select
    store_id,
    order_date,
    revenue_net,

    -- No average comes out if the window contains a day with no data. avg
    -- would silently skip the null and divide by fewer days — substituting an
    -- estimate for a gap, which is exactly what this project does not do. An
    -- incomplete window is more honest left empty.
    case when count(revenue_net) over w = 7
         then avg(revenue_net) over w
    end as revenue_net_avg_7d

from sales

window w as (
    -- rows, not range: we count exactly seven consecutive rows, and that is
    -- correct only because the axis is dense — the spine makes it so. On a
    -- sparse axis rows would take seven rows that could have months between
    -- them.
    partition by store_id
    order by order_date
    rows between 6 preceding and current row
)
