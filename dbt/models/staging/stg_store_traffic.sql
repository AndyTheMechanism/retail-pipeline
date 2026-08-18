-- Daily footfall per store — the denominator of conversion.
--
-- Zero visitors is deliberately not turned into a null here. The raw layer
-- carries exactly what the device reported, and the judgement "the device was
-- not counting" is made in the mart, where the orders sit alongside: zero
-- visitors with live orders and zero visitors on a closed day are different
-- things, and they can only be told apart where both figures have been brought
-- together.

with source as (
    select * from {{ source('raw', 'store_traffic') }}
)

select
    store_id,
    traffic_date,
    visitors
from source
