-- The raw layer: what arrives from the source, exactly as it is.
--
-- There are deliberately NO primary keys and no uniqueness constraints here.
-- The raw layer reproduces the source export, defects and all, and among the
-- planted defects are doubled rows. A uniqueness constraint would mean a
-- duplicate could not even be loaded — and then the deduplication in staging
-- would be guarding against something that never happens. Grain uniqueness is
-- checked by the unique_grain test, and it sits on stg_order_items, right
-- after deduplication, not on the raw data.
--
-- Indexes sit only on the partition keys: the load works by delete-and-insert
-- for a date, and without an index every run would do a seq scan.

CREATE SCHEMA IF NOT EXISTS raw;

-- Store reference table. The one table without partitions: it is reloaded
-- whole, it is small and it changes rarely.
CREATE TABLE IF NOT EXISTS raw.stores (
    store_id      integer      NOT NULL,
    store_code    text         NOT NULL,
    city          text         NOT NULL,
    store_format  text         NOT NULL,   -- small | medium | large
    opened_on     date         NOT NULL
);

CREATE TABLE IF NOT EXISTS raw.orders (
    order_id      bigint       NOT NULL,
    store_id      integer      NOT NULL,
    order_ts      timestamp    NOT NULL,
    order_date    date         NOT NULL,   -- partition key
    channel       text         NOT NULL,   -- offline | web | app
    status        text         NOT NULL,   -- placed | cancelled
    customer_id   bigint
);
CREATE INDEX IF NOT EXISTS orders_order_date_idx ON raw.orders (order_date);

-- Order lines. order_date is denormalised on purpose: without it, deleting a
-- partition would take a join back to the orders, and this is exactly the case
-- where denormalising the raw layer costs less than a beautiful model.
--
-- About sku: it is an opaque item code and nothing more. There is no category
-- here, no brand and no cost price, and there will not be — that is category
-- management, a different profession, and the domain boundary is held from
-- the start.
CREATE TABLE IF NOT EXISTS raw.order_items (
    order_id      bigint       NOT NULL,
    order_date    date         NOT NULL,   -- partition key
    line_no       integer      NOT NULL,
    sku           text         NOT NULL,
    quantity      integer      NOT NULL,
    unit_price    numeric(10,2) NOT NULL,
    line_amount   numeric(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS order_items_order_date_idx ON raw.order_items (order_date);

-- Returns. The partition key is the return date, not the order date: a return
-- arrives on its own day and changes the revenue of an earlier one. The
-- reprocessing window on mart_store_daily_sales rests on this, and the shape
-- of the table allows it.
CREATE TABLE IF NOT EXISTS raw.returns (
    return_id       bigint       NOT NULL,
    order_id        bigint       NOT NULL,
    order_date      date         NOT NULL, -- date of the original order
    line_no         integer      NOT NULL,
    returned_date   date         NOT NULL, -- partition key
    quantity        integer      NOT NULL,
    returned_amount numeric(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS returns_returned_date_idx ON raw.returns (returned_date);

CREATE TABLE IF NOT EXISTS raw.cancellations (
    cancellation_id bigint      NOT NULL,
    order_id        bigint      NOT NULL,
    order_date      date        NOT NULL,
    cancelled_date  date        NOT NULL,  -- partition key
    reason          text        NOT NULL
);
CREATE INDEX IF NOT EXISTS cancellations_cancelled_date_idx ON raw.cancellations (cancelled_date);

CREATE TABLE IF NOT EXISTS raw.store_traffic (
    store_id      integer      NOT NULL,
    traffic_date  date         NOT NULL,   -- partition key
    visitors      integer      NOT NULL
);
CREATE INDEX IF NOT EXISTS store_traffic_traffic_date_idx ON raw.store_traffic (traffic_date);
