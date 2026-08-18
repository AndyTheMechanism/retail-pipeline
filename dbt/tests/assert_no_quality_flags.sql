-- Quality flags are visible in a run, but they do not break the chain.
--
-- The only test in the project with severity warn, and it is here precisely so
-- that the difference between a stop and a flag is visible in the tool itself
-- rather than only in words. The mechanism is identical — a test that returned
-- rows; what differs is the consequence.
--
-- Without it the flags would be data that someone has to remember to look at,
-- and that always gets forgotten. With it every run prints how many store-days
-- are flagged, and no run comes back green in silence.

{{ config(severity = 'warn') }}

select
    check_name,
    count(*)       as flagged_store_days,
    min(flag_date) as first_seen,
    max(flag_date) as last_seen
from {{ ref('mart_store_daily_quality') }}
group by check_name
order by flagged_store_days desc
