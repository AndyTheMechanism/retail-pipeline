-- Order header.
--
-- Two boolean flags instead of comparing string values in every model
-- downstream. The values themselves are passed through exactly as the source
-- sends them: remapping them onto a vocabulary of our own would silently rename
-- a fact of the source, and every reconciliation of a mart against the raw
-- layer would then mean holding a mapping in your head. The column names are
-- ours to choose, the values are not.

with source as (
    select * from {{ source('raw', 'orders') }}
)

select
    order_id,
    store_id,
    order_ts,
    order_date,
    channel,
    status,
    customer_id,

    status = 'cancelled' as is_cancelled,

    -- Offline is singled out because the door counter sees nothing else. Web
    -- and app have nothing to do with a store's footfall, and mixing them in
    -- would give a conversion that grows as online grows.
    channel = 'offline' as is_offline

from source
