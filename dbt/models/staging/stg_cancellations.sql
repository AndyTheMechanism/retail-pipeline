-- Cancellations.
--
-- Not used by any mart, and this is not a forgotten model. The order status
-- already sits on the order itself, and all this adds is the cancellation date
-- and the reason — no mart rests on either.
--
-- The model exists because referential integrity and agreement with
-- orders.status are checked through it: every cancelled order must have exactly
-- one cancellation row, and the other way round. Both directions are reconciled
-- by tests/assert_cancellations_agree_with_status.sql.
--
-- It also shows why the revision log rests on returns. The generator fixes the
-- status at the moment the order is created and sets cancelled_date 0-2 days
-- later. So a cancellation in this data never changes yesterday's number:
-- rebuilding the orders partition gives the same status back. The only source
-- of revisions is returns, and the revision-log scenario is built on them
-- rather than on cancellations.

with source as (
    select * from {{ source('raw', 'cancellations') }}
)

select
    cancellation_id,
    order_id,
    order_date,
    cancelled_date,
    reason
from source
