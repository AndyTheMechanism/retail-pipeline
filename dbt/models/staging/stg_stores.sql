-- Store reference table.
--
-- The single source of the opening date, and the grain of both marts rests on
-- it: before it opens a store has neither orders nor footfall, and that is
-- honest emptiness rather than a missing partition. The two are told apart
-- further down the graph: int_store_day_spine cuts off the days before the
-- opening, and mart_store_daily_quality flags the emptiness left after the cut.

with source as (
    select * from {{ source('raw', 'stores') }}
)

select
    store_id,
    store_code,
    city,
    store_format,
    opened_on
from source
