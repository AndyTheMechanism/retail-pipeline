# SQL depth: a query plan before and after

One heavy query, taken apart all the way down. Not one invented for the demo —
the one the pipeline executes on every run.

Every measurement was taken on this data: 3.57M order lines, 1.49M orders,
PostgreSQL 16 in a container on a single machine. The numbers reproduce — the
generator is deterministic under a seed.

The result up front, so nobody has to wait for it: a run for one date went from
38.4 to 29.0 seconds, of which building the marts went from 22.5 to 11.4. Two
changes, and the one that matters is not the index.

---

## Where to look for the heavy part

The place to look is not where a query looks frightening but where it repeats.
The daily run builds the sales mart over a window of 28 days backwards, and at
that step it reads the intermediate order-lines layer — a table of 3.5M rows and
370 MB.

```sql
select store_id, order_date, count(*), sum(quantity), sum(line_amount)
from intermediate.int_order_lines
where not is_cancelled
  and order_date between date '2026-06-30' - 28 and date '2026-06-30'
group by store_id, order_date
```

`explain (analyze, buffers)` before any changes:

```
Finalize GroupAggregate (actual time=103.475..119.851 rows=3480)
  Buffers: shared hit=3009 read=44415, temp read=695 written=697
  ->  Sort (actual time=100.520..104.383 rows=64856)
        Sort Method: external merge  Disk: 1672kB
        ->  Parallel Seq Scan on int_order_lines (actual time=1.954..81.300 rows=64856)
              Filter: ((NOT is_cancelled) AND (order_date >= '2026-06-02') AND (order_date <= '2026-06-30'))
              Rows Removed by Filter: 1123817
Execution Time: 120.424 ms
```

Here is how to read it. `Rows Removed by Filter: 1123817` for each of three
workers — the table was read end to end, 3.37M rows thrown away to get at the
195 thousand that were wanted. `read=44415` — forty-four thousand blocks lifted
off disk, which is those 350 MB. And the line
`Sort Method: external merge Disk: 1672kB` means the sort did not fit in memory
and went to disk.

Not one of the tables dbt created had an index at all — dbt does not create them
until you ask.

## Change one: an index on the partition key

```sql
create index on intermediate.int_order_lines (order_date)
```

```
HashAggregate (actual time=51.904..52.611 rows=3480)
  Buffers: shared hit=2746 read=206 written=162
  ->  Bitmap Heap Scan on int_order_lines (actual time=4.299..20.710 rows=194568)
        Filter: (NOT is_cancelled)
        Rows Removed by Filter: 5961
        ->  Bitmap Index Scan on int_order_lines_order_date_idx (rows=200529)
Execution Time: 52.759 ms
```

| | before | after |
|---|---|---|
| time | 120.4 ms | 52.8 ms |
| blocks read | 47,424 | 2,952 |
| rows removed by filter | 3,371,451 | 5,961 |
| sort | external, on disk | not needed at all |

More interesting than the speed-up is that **the shape of the plan changed**.
While the input set ran to millions of rows, the planner chose a sort with
grouping, and the sort did not fit in memory. As soon as the set became twenty
times smaller it switched to `HashAggregate` — and the sort vanished from the
plan entirely. The index removed not only the wasted reading but the work that
reading created.

The index weighs 24 MB against a 370 MB table — at the moment it is built. After
a month of running it weighs 121 MB, and the reason has a section of its own
below.

## And why that was not enough

Here begins the part this chapter was written for.

The intermediate layer was rebuilt in full by every run — the model was
materialised as a table. So the index, too, was built again every time.
Measured:

```
create index ... on int_order_lines (order_date);
Time: 882.827 ms
```

Eight hundred and eighty-three milliseconds to build an index that saves a
hundred and twenty. **The index was net negative**, and the whole change above
was a loss.

Hence a rule worth remembering in full: an index on a table that gets recreated
has to be counted together with the cost of building it. Measuring only the
query means measuring half of it and being pleased with the result.

## Change two: the layer stopped being rebuilt

If an index only pays for itself when the table survives the run, then the table
has to survive the run. The intermediate layer became incremental.

This reverses a decision taken earlier. What was written then: making the
intermediate layer incremental "buys nothing but risk", and eight seconds of
rebuilding is an acceptable price for having no subtle bugs. The measurement
showed that it does buy something: eight seconds of building plus a second for
the index, every run.

The risk has not gone away — it is covered. If the raw layer for an earlier
date is reloaded and no run is called for that date, the layer falls behind the
source. That is caught by the reconciliation of assembled order lines against
the raw layer: it compares a window of 30 days on every run and goes to the
source directly, bypassing staging. A gate put in two stages earlier turned out
to be the insurance for an optimisation nobody was planning at the time.

`unique_key` in this model is not a uniqueness key but a partition key:
`delete+insert` on it replaces a day's contents wholesale, exactly as the raw
load does. The grain `(order_id, line_no)` stays where it was and is checked by
a test.

A run for one date: 22.5 seconds of building became 18.1.

## Change three, the main one: predicates do not fall through window functions

Eighteen seconds is still a lot for a single day. The profile showed that the
most expensive thing in the run is building that same intermediate layer — 4.46
seconds for 5,834 inserted rows. That is out of all proportion, and taking it
apart turned up the real cause.

The model reads `stg_order_items` — the view where the window-function
deduplication lives. The plan for building one date:

```
Hash Right Join (actual time=2689.986..2889.301 rows=5834)
  ->  Seq Scan on orders (rows=1485638)
  ->  Hash (actual time=2674.287..2674.290 rows=5834)
        ->  Subquery Scan on numbered (actual time=2671.121..2673.779 rows=5834)
              Rows Removed by Filter: 3560185
              ->  Seq Scan on order_items (rows=3566399)
Execution Time: 2934.907 ms
```

`Rows Removed by Filter: 3560185`. The single-date filter sits **after** the
window: the deduplication was computed over all 3.5 million rows, and only then
was everything but 5,834 thrown out of the result.

This is not an oversight by the planner but its duty. A window function sees the
whole window, and the result of `row_number()` depends on which rows landed in
it. Pushing the filter below the window would change the meaning of the query —
the planner does not do that.

**With one exception.** If the filtered column is part of `partition by`, then
no discarded row could have affected the ones that remain: windows do not cross
that column's boundary. Then the predicate falls through legitimately.

It was `partition by order_id, line_no`. It became `partition by order_date,
order_id, line_no`:

```
Subquery Scan on numbered (actual time=3.569..8.730 rows=5834)
  ->  WindowAgg (actual time=3.568..8.273 rows=5834)
        ->  WindowAgg (actual time=3.563..5.669 rows=5834)
              ->  Bitmap Heap Scan on order_items (rows=5834)
                    ->  Bitmap Index Scan on order_items_order_date_idx
Execution Time: 9.051 ms
```

**2,934 ms turned into 9.** One word in `partition by`.

The added column does not change the result: `order_date` is functionally
dependent on `order_id`, because the order identifier encodes the date — that is
how the generator works. It does not split the groups. And if it ever did, the
grain `(order_id, line_no)` is checked by a test of its own, and that test would
go red.

This is the only place in the project where the form of an expression was chosen
for the optimiser rather than for the meaning — and that is why a paragraph of
comment sits above it in the model.

## The result

| | before | after |
|---|---|---|
| `make run DATE=...` end to end | 38.4 s | 29.0 s |
| of which building the marts | 22.5 s | 11.4 s |
| building the intermediate layer | 8.4 s | 0.3 s |

The numbers agree: the checksum of the sales mart after all the changes is the
same as it was before them, and the reconciliation against the raw layer passes
in full — 31 checks out of 31. An optimisation that changed the numbers would
not have been an optimisation.

## What is most expensive now, and why it was left that way

After the changes the run profile looks like this:

```
assert_cancellations_agree_with_status                3.52 s
assert_line_dates_match_order_dates                   3.15 s
relationships_stg_order_items_order_id__stg_orders    2.93 s
not_null_stg_order_items_source_copies                2.83 s
unique_grain_stg_order_items_order_id__line_no        2.70 s
```

The most expensive thing in the daily run is no longer building the data but
checking it. Each of these tests reads the whole history rather than a window.

Narrowing them to the window is not hard, and it would halve the run again. That
was deliberately not done: a gate that looks only at today catches only today's
breakages. Corruption that arrived in an earlier partition would go straight
past, and that is precisely what the gate is there for. Eleven seconds to check
three and a half million rows in full is a fair price for having the claim
"broken data is not published" apply to all the data rather than to the last
twenty-four hours.

If the volume grows to the point where the price becomes unacceptable, the right
move is not to narrow the checks but to split them by frequency: grain
invariants on every run, the full reconciliation against the raw layer once a
day in a DAG of its own. But that is a decision made on a measurement at
production volume, not in advance.

## The cost of incrementality that was not counted at first

Every change has a side effect, and the side effect of this one turned up later
— while the project was being run from a separate clone.

A freshly built index on the intermediate layer weighs 24 MB. The one standing
on the live table weighs 121 MB — **five times more**. This can be checked
without breaking anything: `create index` in Postgres is transactional, and it
can be rolled back.

```sql
begin;
create index tmp_freshidx on intermediate.int_order_lines (order_date);
select pg_size_pretty(pg_relation_size('intermediate.tmp_freshidx'));  -- 24 MB
rollback;
```

**The cause is the write strategy itself.** `delete+insert` does not recreate
the table, it deletes and inserts rows. A deleted entry in a btree frees space
inside its page, but the page is neither handed back nor compacted. Over many
runs the index bloats.

The main contribution comes not from the daily run but from a full build.
Without `run_date` there is no filter in the model, so the temporary table holds
all 546 dates; the `delete` then clears the table entirely and the `insert`
fills it again — all of that without recreating anything. That is,
**`make models` is not a full rebuild but a full delete+insert**, and the
difference between the two is invisible in dbt's output while being very visible
in the size of the index.

**What to do about it.** `reindex index concurrently` costs the same 890 ms as
the initial build, and it makes sense to call it once a week rather than on
every run.

**Why it is not in the project.** Because at this volume the price is small: the
bitmap scan from the first section reads 298 index blocks out of fifteen
thousand, so the bloat costs single-digit milliseconds and a hundred megabytes
of disk. At production volume neither of those would be trivial any more, and
then `reindex` would go into the DAG as a task of its own — but on a
measurement, not in advance.

Worth saying separately why this is written down here rather than fixed
silently. The measurement above refutes a line in this very document: "the index
weighs 24 MB" is true exactly at the moment it is built and false after a month
of running. Keeping the one figure and not writing the other would have made the
document look more precise and be more dishonest.

---

## The models are built on CTEs

The second half of the SQL-depth requirement is how the models themselves are
written.

Every model is a sequence of named CTEs rather than nested subqueries. The
difference is not about beauty: in `explain` each CTE shows up as its own line
with its own time and row count, and taking the plan apart starts with reading
it rather than with untangling brackets. A named step also explains itself —
`returns_by_arrival` says more than a third level of nesting.

Postgres has not materialised CTEs by default since version 12, so readability
costs nothing here: the planner inlines them exactly as it would inline a
subquery. Before 12 this would have been a trade-off, and that is worth
remembering if the project ever moves to an older version.

## Window functions: where and why

Window functions appear in three models and once in a check, and every one of
those places can be defended.

**Deduplicating order lines** — `row_number()` over the grain with an explicit
ordering. The alternative, `distinct`, removes only exact copies and silently
leaves two rows on one line when the copies have diverged. The window states the
grain instead of fighting the symptom, and counts `count(*) over (...)` while it
is at it — how many source rows there were for that line.

**A 7-day moving average of revenue** — `avg() over (rows between 6 preceding
and current row)`. `rows`, not `range`: exactly seven consecutive rows are
counted, and that is only correct because the axis is dense — the spine makes it
so. No average is produced if the window holds a day without data:
`count() over w = 7` guards against `avg` silently passing over the gap and
dividing by fewer days.

**The revision log** — `lag()` over snapshot versions. It puts adjacent versions
of the same store-day side by side, so "before" and "after" end up on one row.

The fourth place is in a check rather than in a model: the reconciliation of
assembled order lines against the raw layer repeats the deduplication with a
`row_number()` of its own, word for word, ordering included. That is not
duplication by oversight but the condition on which it works: a check has to
reproduce the rule rather than refer to it — otherwise it confirms the model
with the model.

There is no fifth place, and that is a decision too: a window function for the
sake of demonstrating a window function is the worst kind of ornament, because
it costs a lot and does nothing.
