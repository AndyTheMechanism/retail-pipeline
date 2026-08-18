-- The assembled order: an order line together with its header.
--
-- The heavy join, 3.57M rows against 1.49M. Materialised so that the
-- deduplication sort and this join run once, rather than again on every query
-- against a mart and on every check.
--
-- It is a left join on purpose. An inner one would silently drop a line that
-- has no order, and the not_null test on store_id would have nothing to catch.
-- Here such a line stays with store_id = null and is visible to a query; what
-- to do with it is decided by the mart, not by the intermediate layer.
--
-- order_date is taken from the line rather than from the header: in the raw
-- layer it is denormalised as the partition key and is present even on an
-- orphaned row. That the two dates agree is an invariant, and it is checked by
-- a test rather than enforced by the join: joining on both dates would hide a
-- discrepancy as a missing row.
--
-- ON INCREMENTALITY AND THE INDEX — a decision reversed on a measurement.
--
-- At first this model was built in full on every run: eight seconds, and it was
-- assumed that incrementality buys nothing here except risk. The query plan
-- said otherwise, and the details are in QUERY-PLAN.md. The short version: the
-- mart takes a 28-day window from here, and without an index that is a seq scan
-- over 3.5M rows for the sake of 195 thousand. The index makes the query twice
-- as fast — but it takes 883 ms to build and saves 120, so as long as the table
-- was rebuilt in full it was a straight loss.
--
-- Incrementality removes both costs at once: the rebuild and the index build.
-- Order lines do not change after the fact, so a run touches exactly the target
-- date.
--
-- unique_key here is not a uniqueness key but a partition key. delete+insert on
-- it replaces the contents of a day wholesale, exactly as the raw load does,
-- rather than adding rows on top of what is already there. The grain stays
-- (order_id, line_no) and is checked by a test of its own.
--
-- The risk this adds is named out loud: if the raw layer for a past date is
-- reloaded and no run is called for that date, this layer falls behind. That is
-- caught by the reconciliation of the assembled order against the raw layer —
-- it compares a 30-day window on every run, and it does not know how to go red
-- quietly.

{{ config(
    materialized = 'incremental',
    unique_key = 'order_date',
    incremental_strategy = 'delete+insert',
    indexes = [{'columns': ['order_date'], 'type': 'btree'}]
) }}

{% set run_date = var('run_date', none) %}

with lines as (
    select * from {{ ref('stg_order_items') }}
    {% if is_incremental() and run_date %}
    where order_date = date '{{ run_date }}'
    {% endif %}
),

orders as (
    select * from {{ ref('stg_orders') }}
)

select
    l.order_id,
    l.line_no,
    l.order_date,

    o.store_id,
    o.channel,
    o.is_cancelled,
    o.is_offline,

    l.sku,
    l.quantity,
    l.unit_price,
    l.line_amount,
    l.source_copies

from lines l
left join orders o on o.order_id = l.order_id
