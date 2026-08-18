-- Returns.
--
-- delay_days is computed here rather than by a one-off command, because two
-- decisions rest on that number: the size of the reprocessing window — 28 days,
-- return_window_days in dbt_project.yml — and the return_outside_window flag,
-- which catches returns that arrived beyond the window. A measurement that
-- something depends on has to be a column you can write a query against, not a
-- line in the output of a script.

with source as (
    select * from {{ source('raw', 'returns') }}
)

select
    return_id,
    order_id,
    order_date,
    line_no,
    returned_date,
    quantity,
    returned_amount,

    returned_date - order_date as delay_days

from source
