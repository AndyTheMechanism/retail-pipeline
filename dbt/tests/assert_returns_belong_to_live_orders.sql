-- A return against a cancelled order makes no sense.
--
-- A cancelled order was never handed to the customer, so there is nothing to
-- return. If a row like that shows up, it is not a rare case from real life
-- but a desync between sources — and net revenue drops by an amount that never
-- existed.
--
-- Referential integrity of returns is checked separately, by the built-in
-- relationships test: this is not about a missing order but about an order
-- that exists and is the wrong one.

select
    return_id,
    order_id,
    store_id,
    order_date,
    returned_date,
    returned_amount
from {{ ref('int_returns_attributed') }}
where is_cancelled
