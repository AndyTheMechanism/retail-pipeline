# Table and column dictionary

Built from the descriptions in yml by `make dictionary`. Not edited by hand:
an edit is lost on the next build. Edit the description next to the column
instead — in the same place as its checks.

Objects: 21, columns: 149. Columns with no description: 0 — building the
dictionary fails if that is more than zero.

Lineage is `make docs`: dbt opens a browser showing what each model is built
from. That picture cannot be drawn by hand — it would part ways with the code
within the first week.

---

## Raw layer — schema `raw`

The source export as it is, defects included. There are deliberately no primary keys and no uniqueness constraints here.

### `raw.cancellations`

*table*

Cancellations. Partitioned by cancelled_date, which is 0-2 days after the order date.

| Column | Type | What it is |
|---|---|---|
| `cancellation_id` | bigint | Cancellation identifier. It matches the order identifier. |
| `order_id` | bigint | The cancelled order. |
| `order_date` | date | Date of the original order. |
| `cancelled_date` | date | Cancellation date. The partition key. Orders from the last two days of the horizon may have no cancellation row yet — its time has not come, and the checks account for that. |
| `reason` | text | Cancellation reason from the source — one of four. |

### `raw.order_items`

*table*

Order lines. Partitioned by order_date. This is where the export duplicates and the outliers sit.

| Column | Type | What it is |
|---|---|---|
| `order_id` | bigint | The order the line belongs to. |
| `order_date` | date | Order date, denormalised on purpose: without it, dropping a partition would take a join to the orders table. |
| `line_no` | integer | Line number within the order. Together with order_id it forms the grain. |
| `sku` | text | An opaque item code. There is no category, no brand and no cost price here, and there will not be — that is category management, a different profession. |
| `quantity` | integer | Quantity. Zero does occur — that is a planted outlier. |
| `unit_price` | numeric(10,2) | Price per unit, from 199 to 19990. |
| `line_amount` | numeric(12,2) | Line amount. It can be negative — that is another planted outlier, and it deliberately makes it all the way to the mart. |

### `raw.orders`

*table*

Orders. Partitioned by order_date. The status is already set in the source.

| Column | Type | What it is |
|---|---|---|
| `order_id` | bigint | Order identifier. It encodes the date and the store, so the partition can be recovered from it unambiguously, and a return can be tied to its order without touching the orders table. |
| `store_id` | integer | The store that placed the order. |
| `order_ts` | timestamp without time zone | Order timestamp, hour between 9 and 21. The marts do not use it — the grain is daily. |
| `order_date` | date | Order date. The partition key. |
| `channel` | text | Channel — offline, web or app. The door counter sees only offline, and that is why the channel takes part in the conversion calculation. |
| `status` | text | Placed or cancelled. The status is already final in the source: a cancellation never changes yesterday's number. |
| `customer_id` | bigint | Customer identifier, filled in on 70% of orders. Left as groundwork for cohorts; no current mart uses it. |

### `raw.returns`

*table*

Returns. Partitioned by returned_date rather than by the order date: a return arrives on its own day and changes the revenue of an earlier one.

| Column | Type | What it is |
|---|---|---|
| `return_id` | bigint | Return identifier, derived from the order identifier. |
| `order_id` | bigint | The order the return belongs to. |
| `order_date` | date | Date of the original order. This is the date the sales mart attributes the return to. |
| `line_no` | integer | The order line that was returned. |
| `returned_date` | date | The date the return arrived. The partition key. It lags the order date by 0-30 days, median 2 days. |
| `quantity` | integer | The quantity returned, never more than was bought. |
| `returned_amount` | numeric(12,2) | Return amount — unit price times the quantity returned. |

### `raw.store_traffic`

*table*

Daily footfall per store. Partitioned by traffic_date. The denominator of conversion.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. |
| `traffic_date` | date | The date. The partition key. |
| `visitors` | integer | Visitors as counted by the door counter. A zero means a dead device rather than an absence of people, and that judgement is passed by a quality flag. |

### `raw.stores`

*table*

Store reference table. The only table without partitions, reloaded in full.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | Store identifier. The same one throughout the project. |
| `store_code` | text | Store code in the form S0001. The marts do not use it; it exists for readability. |
| `city` | text | City. One of thirty, a reference attribute. |
| `store_format` | text | Store format — small, medium or large. It sets the store's baseline order volume. |
| `opened_on` | date | Opening date. Before it the store has neither orders nor footfall, and the grain of both marts rests on it: 12 stores out of 120 opened inside the horizon, and their empty days are not missing data. |

---

## Staging layer — schema `staging`

Fixes the shape: types, names, grain. Counts nothing and repairs nothing. Materialised as views.

### `staging.stg_cancellations`

*view*

Cancellations. Deliberately unused by the marts: the status is already on the order. It is here to check referential integrity and agreement with orders.status.

| Column | Type | What it is |
|---|---|---|
| `cancellation_id` | bigint | Cancellation identifier. It matches the order identifier. |
| `order_id` | bigint | The cancelled order. |
| `order_date` | date | Date of the original order. |
| `cancelled_date` | date | Cancellation date, 0-2 days after the order date. |
| `reason` | text | Cancellation reason from the source — one of four. |

### `staging.stg_order_items`

*view*

Order lines, one row per line. The single place where the (order_id, line_no) grain is restored after the export duplicates.

| Column | Type | What it is |
|---|---|---|
| `order_id` | bigint | The order the line belongs to. |
| `order_date` | date | Order date, denormalised onto the line as the partition key. The window functions partition by it for the sake of the query plan — see QUERY-PLAN.md. |
| `line_no` | integer | Line number within the order. Together with order_id it forms the grain. |
| `sku` | text | An opaque item code. There is no category, no brand and no cost price here, and there will not be — that is category management, a different profession. |
| `quantity` | integer | Quantity. Zero does occur — that is a planted outlier. |
| `unit_price` | numeric(10,2) | Price per unit, from 199 to 19990. |
| `line_amount` | numeric(12,2) | Line amount. It can be negative: the outliers are deliberately left unfiltered, and the sign is not checked here — they are caught by a flag, not by a gate. |
| `source_copies` | bigint | How many rows the source held for this line. One is normal, more than one means an export duplicate. The column turns deduplication into a checkable fact rather than an assumption. |

### `staging.stg_orders`

*view*

Order header. Grain — order_id.

| Column | Type | What it is |
|---|---|---|
| `order_id` | bigint | Order identifier. It encodes the date and the store. |
| `store_id` | integer | The store that placed the order. |
| `order_ts` | timestamp without time zone | Order timestamp. The marts do not use it — the grain is daily. |
| `order_date` | date | Order date, the partition key in the raw layer. |
| `channel` | text | Source value, "offline", "web" or "app". |
| `status` | text | Source value, "placed" or "cancelled". |
| `customer_id` | bigint | Customer identifier, filled in on 70% of orders. Groundwork for cohorts; no current mart uses it. |
| `is_cancelled` | boolean | Cancellation flag. A cancelled order yields neither revenue nor returns. |
| `is_offline` | boolean | Offline-order flag. The numerator of conversion is counted on it alone — the door counter sees neither the website nor the app. |

### `staging.stg_returns`

*view*

Returns. Grain — return_id.

| Column | Type | What it is |
|---|---|---|
| `return_id` | bigint | Return identifier, derived from the order identifier. |
| `order_id` | bigint | The order the return belongs to. |
| `order_date` | date | Date of the original order. This is the date the sales mart attributes the return to. |
| `line_no` | integer | The order line that was returned. |
| `returned_date` | date | The date the return arrived. The raw layer is partitioned on it. |
| `quantity` | integer | The quantity returned, never more than was bought. |
| `returned_amount` | numeric(12,2) | Return amount. This is what gets subtracted from the revenue of the purchase day. |
| `delay_days` | integer | Return delay in days. Median 2, 95th percentile 19, 99th 28, maximum 30. The size of the reprocessing window rests on this number. |

### `staging.stg_store_traffic`

*view*

Daily footfall per store. Grain — (store_id, traffic_date).

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. |
| `traffic_date` | date | The date the footfall was observed. |
| `visitors` | integer | Visitors as counted by the door counter. A zero is not coerced to null here and is not treated as an error: whether the device was counting at all is judged by a quality flag, which shows the order count right beside it. |

### `staging.stg_stores`

*view*

Store reference table. Grain — store_id.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | Store identifier. The same one throughout the project. |
| `store_code` | text | Store code in the form S0001. The marts do not use it; it exists for readability. |
| `city` | text | City. One of thirty, a reference attribute. |
| `store_format` | text | Store format — small, medium or large. It sets the store's baseline order volume. |
| `opened_on` | date | Opening date. Before it the store has neither orders nor footfall, and the grain of the marts rests on that: 12 stores out of 120 opened inside the horizon, and their empty days are not missing data. |

---

## Intermediate layer — schema `intermediate`

Brings the sources together. Leaves everything visible, orphans included, so the gate has something to catch.

### `intermediate.int_order_lines`

*table*

An order line together with its header. Grain — (order_id, line_no). Incremental: a run touches exactly the target date, and that decision rests on a measurement of the query plan — see QUERY-PLAN.md.

| Column | Type | What it is |
|---|---|---|
| `order_id` | bigint | The order the line belongs to. |
| `line_no` | integer | Line number within the order. Together with order_id it forms the grain. |
| `order_date` | date | The date from the line, not from the header: in the raw layer it is the partition key, and even an orphaned row carries it. Agreeing with the header date is an invariant, and it is checked by a test rather than forced by a join. The reprocessing window is cut on this same column, and the index sits on it. |
| `store_id` | integer | The store from the order header. The join is deliberately a left one so that a line without an order does not vanish before it is checked — and this is that check. A null here means an orphaned line, and no revenue may be counted on it. |
| `channel` | text | The order's channel from the header. The sales mart does not use it; it is there for investigations. |
| `is_cancelled` | boolean | Cancellation flag from the header. Cancelled orders yield no revenue. |
| `is_offline` | boolean | Offline-order flag from the header. |
| `sku` | text | An opaque item code. It does not travel beyond this model. |
| `quantity` | integer | Quantity. Zero does occur — a planted outlier. |
| `unit_price` | numeric(10,2) | Price per unit. |
| `line_amount` | numeric(12,2) | Line amount. Negative values are deliberately left unfiltered. |
| `source_copies` | bigint | How many rows the source held for this line before deduplication. |

### `intermediate.int_returns_attributed`

*table*

A return tied to a store through its order. Grain — return_id. The model is required rather than decorative: returns in the raw layer carry no store_id.

| Column | Type | What it is |
|---|---|---|
| `return_id` | bigint | Return identifier. |
| `order_id` | bigint | The order the return belongs to. |
| `line_no` | integer | The order line that was returned. |
| `store_id` | integer | The store taken from the order. A null means an orphaned return: the amount would be subtracted from the revenue of no store in particular. |
| `is_cancelled` | boolean | Cancellation flag of the original order. A return against a cancelled order makes no sense — a test of its own, assert_returns_belong_to_live_orders, checks that. |
| `order_date` | date | Date of the original order. This is the date the sales mart attributes the return to. |
| `returned_date` | date | The date the return arrived. It shows where a change to yesterday's number came from. |
| `delay_days` | integer | Return delay in days. The input for the size of the reprocessing window and for the flag. |
| `quantity` | integer | The quantity returned. |
| `returned_amount` | numeric(12,2) | Return amount. |

### `intermediate.int_store_day_spine`

*table*

A dense store-by-day grid starting from each store's opening date. Grain — (store_id, calendar_date), exactly 62,690 rows. The grain of both marts.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. All 120 of them are present in the grid. |
| `calendar_date` | date | A day of the horizon. The horizon is computed from the observed data rather than taken as a constant: a constant would drift away from the generator silently. |

---

## Marts — schema `marts`

What a human and a dashboard read.

### `marts.mart_store_daily_conversion`

*table*

Conversion per store and day. Grain — (store_id, traffic_date), the same 62,690 rows. This mart has no reprocessing window: neither footfall nor orders are rewritten after the fact.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. Part of the grain. |
| `traffic_date` | date | The day of observation. Part of the grain. |
| `has_traffic` | boolean | Whether footfall arrived for this day. False means a partition that did not arrive. |
| `has_orders` | boolean | Whether orders arrived for this day. |
| `visitors` | integer | Visitors as counted by the door counter. Null if no footfall arrived for the day. |
| `orders_offline` | bigint | The numerator of conversion: offline orders excluding cancelled ones. Web and app are left out — the door counter does not see them. |
| `conversion` | numeric | Orders per visitor. Null when there is no footfall, no orders, or the visitor count is zero: zero visitors alongside real orders is a message about a broken device, not a value. The quality threshold is not applied here — that judgement is passed by a flag. |

### `marts.mart_store_daily_quality`

*table*

Quality flags: what cannot be counted for this store on this day. Grain — (store_id, flag_date, check_name). A table of its own rather than columns on the marts: a row can carry several flags, and more get added over time — as rows that grows naturally, as columns it does not.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store the flag belongs to. |
| `flag_date` | date | The day the flag belongs to. Part of the grain. |
| `check_name` | text | The name of the check. Machine-readable, because this is the column people filter and join on. The list is closed: a new flag is added deliberately, together with an edit to this test, rather than arriving as a typo. |
| `reason` | text | The reason in prose — a human reads it in a report, not a machine. |
| `measured_value` | numeric | The number that made the flag fire. |
| `threshold_value` | numeric | The threshold it was compared against. Empty where there was no comparison. |

### `marts.mart_store_daily_revisions`

*view*

The revision log: which published number changed, when and why. The core of the project. A row appears only for the store-and-day pair whose number actually moved; the first version does not count as a revision. A view over the ops.snap_store_daily_sales snapshot.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store whose number was recomputed. |
| `order_date` | date | The day whose number changed, not the day of the recomputation. |
| `revised_at` | timestamp without time zone | When the recomputation made that change. |
| `version_no` | bigint | The version number of the value. Always greater than one: the first version is not a change but the first appearance of the number. |
| `revenue_net_was` | numeric | What was published before the recomputation. |
| `revenue_net_became` | numeric | What was published after it. |
| `revenue_net_delta` | numeric | How far it moved. Negative means the number went down. |
| `returns_amount_was` | numeric | Returns attributed to this day, before the recomputation. |
| `returns_amount_became` | numeric | The same after it. The difference from the previous column is usually the whole revision. |
| `reason` | text | The reason is not guessed but derived from which quantities diverged. Returns changed while gross stayed put — a late return, ordinary life in this domain. Gross changed — the raw layer was rebuilt, and that one is worth looking into. |

### `marts.mart_store_daily_sales`

*table*

Sales per store and day. Grain — (store_id, order_date), 62,690 rows along the spine. A return is attributed to the order date, so yesterday's number can change — and that is the subject of the project.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. Part of the grain. |
| `order_date` | date | The day of purchase. Part of the grain. Returns are attributed to it rather than to the day they arrive, which is why the row can be rewritten later. |
| `has_orders` | boolean | Whether orders arrived for this day. For an open store that is the norm; false across the whole network on one day means a partition that did not arrive. |
| `orders_count` | bigint | Orders excluding cancelled ones, counted by header rather than by line. |
| `orders_cancelled_count` | bigint | Cancelled orders. Kept apart rather than thrown away: a day where half the orders were cancelled has to look different from a day with no orders at all. |
| `lines_count` | bigint | Lines in non-cancelled orders, already deduplicated. |
| `units_sold` | bigint | Units sold in non-cancelled orders. |
| `revenue_gross` | numeric | Revenue over non-cancelled orders. Null if the data for the day did not arrive — a zero is written only where there was something to count. |
| `returns_amount` | numeric | Returns attributed to the order date. These are what move yesterday's number, and the reprocessing window exists for them. |
| `returns_arrived_amount` | numeric | Returns that arrived on this day against orders of any date. It takes no part in revenue; it is here so you can see where a change to the past came from. |
| `revenue_net` | numeric | Revenue less the returns attributed to the order date. |

### `marts.mart_store_daily_sales_trend`

*table*

A weekly revenue baseline on top of the sales mart. A model of its own, because a rolling window pulls in neighbouring rows while the sales mart is incremental.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. Part of the grain. |
| `order_date` | date | The day of purchase. Part of the grain. |
| `revenue_net` | numeric | Net revenue for the day, carried over from the sales mart for ease of reading. |
| `revenue_net_avg_7d` | numeric | A 7-day rolling average of net revenue. Empty if the window contains a day without data: avg would skip the null and divide by fewer days, which is to say it would replace a gap with an estimate. |

---

## Internals — schema `ops`

The machinery the revision log rests on. Not meant to be read by hand.

### `ops.snap_store_daily_sales`

*table*

The history of published values of the sales mart. A service table living in the ops schema; there is no need to read it by hand — that is what marts.mart_store_daily_revisions is for.

| Column | Type | What it is |
|---|---|---|
| `store_id` | integer | The store. Part of the snapshot key. |
| `order_date` | date | The day of purchase. Part of the snapshot key. |
| `has_orders` | boolean | Whether orders arrived for this day. Changes to it are not tracked. |
| `orders_count` | bigint | Orders excluding cancelled ones. One of the four tracked quantities. |
| `orders_cancelled_count` | bigint | Cancelled orders. Changes to it are not tracked. |
| `lines_count` | bigint | Order lines after deduplication. Changes to it are not tracked. |
| `units_sold` | bigint | Units sold. Changes to it are not tracked. |
| `revenue_gross` | numeric | Gross revenue. One of the four tracked quantities. |
| `returns_amount` | numeric | Returns attributed to the order date. One of the four tracked quantities, and usually the only one that moves. |
| `returns_arrived_amount` | numeric | Returns by the date they arrived. Changes to it are not tracked. |
| `revenue_net` | numeric | Net revenue. One of the four tracked quantities. |
| `dbt_scd_id` | text | A service identifier for the version, set by dbt. |
| `dbt_updated_at` | timestamp without time zone | A service timestamp for the version, set by dbt. |
| `dbt_valid_from` | timestamp without time zone | The moment from which the version is in force. This is what becomes the recomputation date in the revision log. |
| `dbt_valid_to` | timestamp without time zone | The moment up to which the version was in force. Empty for the current version — the one now sitting in the mart. |

