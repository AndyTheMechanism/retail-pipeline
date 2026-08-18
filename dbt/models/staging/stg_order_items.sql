-- Order lines, one row per line.
--
-- The only place where the (order_id, line_no) grain is restored. The raw layer
-- holds duplicated export rows, and without this model they would reach the
-- mart as doubled revenue.
--
-- Why row_number and not distinct. Distinct removes only exact copies and
-- silently leaves two rows on one line once the copies have diverged — and
-- diverge they can: outliers are applied on top of a list that already contains
-- duplicates, and spoil one copy without touching the other. Row_number states
-- the grain instead of fighting the symptom. It also gives source_copies — how
-- many rows the source had for that line: deduplication has to be a checkable
-- fact rather than an assumed one.
--
-- Why the ordering inside the window is what it is. The choice between diverged
-- copies is arbitrary by nature, so it must at least be reproducible. Without
-- an order by, the row order inside the window is not defined at all, and two
-- dbt runs would give different numbers — reproducibility would break silently,
-- which is the most unpleasant way for it to break. Ctid is no good: it is
-- physical and changes after a partition is reloaded. The ordering deliberately
-- does not try to pick the "right" copy, the non-outlier one for instance: that
-- would be a quiet repair of the data, and repairing it here is not allowed —
-- the gate reconciling the assembled order against the raw layer rests on it,
-- tests/assert_order_lines_match_raw.sql.
--
-- An honest caveat: on the current seed the duplicate dates and the outlier
-- dates do not overlap, the copies are exact, and the ordering changes not a
-- single number. It is insurance against a change of seed, not a fix for
-- anything today.

with source as (
    select * from {{ source('raw', 'order_items') }}
),

numbered as (
    select
        order_id,
        order_date,
        line_no,
        sku,
        quantity,
        unit_price,
        line_amount,

        -- order_date sits in the partition by for the query plan rather than
        -- for meaning, and this is the only place in the project where the
        -- shape of an expression was chosen because of the optimiser. The
        -- reason: a predicate is not pushed down through a window function —
        -- the planner has to compute the window before it can apply the
        -- filter — unless the filtered column is part of the partition by.
        -- While it was not there, a run for a single date deduplicated all
        -- 3.5M rows only to throw 3,560,185 of them away: 2.7 seconds instead
        -- of nine milliseconds. Measurements and plans are in QUERY-PLAN.md.
        --
        -- It does not affect the result: order_date is functionally dependent
        -- on order_id, because the order identifier encodes the date. The added
        -- column does not split the groups, and if it ever did — the
        -- (order_id, line_no) grain is checked by a test of its own, and that
        -- test would go red.
        count(*) over (partition by order_date, order_id, line_no) as source_copies,

        row_number() over (
            partition by order_date, order_id, line_no
            order by line_amount, quantity, unit_price, sku
        ) as copy_no

    from source
)

select
    order_id,
    order_date,
    line_no,
    sku,
    quantity,
    unit_price,
    line_amount,
    source_copies
from numbered
where copy_no = 1
