-- The revision log: which published number changed, when and why.
--
-- The core of the project. The employer's complaint is "the mart updated
-- silently and wrongly"; the project's answer is "the mart updated, and here is
-- the row saying what it was, what it became and why". Everything else in the
-- repository serves this table.
--
-- A row appears only for the store-and-day pair whose published number actually
-- moved. The first version does not count as a revision: it is not a change but
-- the first appearance of a number.
--
-- The reason is not guessed but derived from which quantities diverged. Returns
-- changed while gross stayed put — a late return arrived, which is ordinary
-- life in this domain. Gross changed — the raw layer was rebuilt, and that one
-- is worth looking into.
--
-- A view, not a table: this is a report over the snapshot, not a mart. There is
-- no point computing it in advance, and a needless materialisation would create
-- a second place where the log can fall behind the history.

{{ config(materialized = 'view') }}

with versions as (
    select
        store_id,
        order_date,

        orders_count,
        revenue_gross,
        returns_amount,
        revenue_net,

        dbt_valid_from as revised_at,

        row_number()           over w as version_no,
        lag(orders_count)      over w as orders_count_before,
        lag(revenue_gross)     over w as revenue_gross_before,
        lag(returns_amount)    over w as returns_amount_before,
        lag(revenue_net)       over w as revenue_net_before

    from {{ ref('snap_store_daily_sales') }}

    window w as (partition by store_id, order_date order by dbt_valid_from)
)

select
    store_id,
    order_date,
    revised_at,
    version_no,

    revenue_net_before                   as revenue_net_was,
    revenue_net                          as revenue_net_became,
    revenue_net - revenue_net_before     as revenue_net_delta,

    returns_amount_before                as returns_amount_was,
    returns_amount                       as returns_amount_became,

    case
        when returns_amount is distinct from returns_amount_before
         and revenue_gross  is not distinct from revenue_gross_before
            then 'late return'
        when revenue_gross is distinct from revenue_gross_before
            then 'raw layer rebuilt'
        when orders_count is distinct from orders_count_before
            then 'order mix changed'
        else 'other'
    end as reason

from versions
where version_no > 1
