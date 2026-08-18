-- A line's date matches the date of its order.
--
-- In the raw layer order_date is denormalised onto the line as the partition
-- key, so the same date sits in two places. Assembling the order lines
-- deliberately does NOT join on both dates: joining on fields that disagree
-- would hide the discrepancy as a missing row, and the line would just vanish
-- from revenue. The invariant is checked here, where it is visible, rather
-- than masked by a join condition.

select
    l.order_id,
    l.line_no,
    l.order_date as line_date,
    o.order_date as header_date
from {{ ref('stg_order_items') }} l
join {{ ref('stg_orders') }} o on o.order_id = l.order_id
where l.order_date <> o.order_date
