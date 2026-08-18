-- A return attached to a store through its order.
--
-- The model does not exist for the beauty of the layers: raw.returns has no
-- store_id at all. A return knows only the order identifier, and without this
-- attachment nothing ties it to a store.
--
-- The two date axes the mart chooses between are brought together here as well:
-- order_date — when the purchase was made, returned_date — when the return
-- arrived. The sales mart attributes a return to the first, which is why
-- yesterday's number moves; the second stays alongside so it is visible where
-- the change came from.
--
-- It is a left join for the same reason as in the assembled order: an orphaned
-- return has to stay visible with store_id = null rather than disappear before
-- the not_null test on store_id finds it. is_cancelled is pulled in here for
-- exactly one check — a return against a cancelled order is impossible by
-- definition, and that is the question asked by
-- tests/assert_returns_belong_to_live_orders.sql.

with returns as (
    select * from {{ ref('stg_returns') }}
),

orders as (
    select * from {{ ref('stg_orders') }}
)

select
    r.return_id,
    r.order_id,
    r.line_no,

    o.store_id,
    o.is_cancelled,

    r.order_date,
    r.returned_date,
    r.delay_days,

    r.quantity,
    r.returned_amount

from returns r
left join orders o on o.order_id = r.order_id
