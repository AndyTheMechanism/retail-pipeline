[![English](https://img.shields.io/badge/lang-English-2563eb?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/%D1%8F%D0%B7%D1%8B%D0%BA-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-64748b?style=for-the-badge)](README.ru.md)

# Retail Funnel Pipeline

A daily pipeline that computes a retail chain's revenue per store, survives late
returns and broken sources, and **never changes yesterday's number silently.**

PostgreSQL · dbt · Airflow · Python · pandas. Eighteen months of synthetic data —
120 stores, 1.5M orders, 3.6M order lines — produced by a seeded generator that
lives in this repository. One command builds the whole thing from nothing.

![The revision log](images/revision-log.png)

Every recomputation that moved a published number leaves a row behind: which
store, which day, when it was recomputed, what it was, what it became, and why.
That table is the whole project in one screen.

## The three questions people ask first

**When a task fails,** the chain stops before the marts are built: the checks
that guard publication sit above them on the graph, so the earlier number is not
rewritten at all — neither correctly nor incorrectly. An alert fires; the mart
stays bit-for-bit as it was.

**Reprocessing an earlier period** is one command, and a repeat run yields the
same state: everything is written with delete-and-insert by key. Two runs for the
same date, five days reprocessed in a row, and a full rebuild from scratch all
leave identical checksums.

**A test breaks the chain rather than logging a warning** when reprocessing can
fix the problem — a doubled grain, a broken reference, a missing partition. When
it cannot, the affected store is flagged and the rest of the network is still
counted.

## Run it

```bash
make demo                                    # everything from scratch, about three minutes
```

That is three steps in a row: generate eighteen months of data, build the marts
behind the checks, reconcile them against the raw layer. Then, piece by piece:

```bash
make run DATE=2026-03-14                     # run the pipeline for one date
make backfill FROM=2026-03-01 TO=2026-03-05  # reprocess a period
make revisions                               # what changed, when and why
make reconcile                               # reconcile the marts against the raw layer
make                                         # every other target, with descriptions
```

You need a container engine, `make` and Python 3.10 or newer — nothing else, and
nothing to configure. `make up` works out whether you have docker or podman;
`make psql` opens a client inside the container so you do not have to install
one. Each step builds its own virtual environment. On Windows, run it under WSL2.

## The layers

![Everything the sales mart is built from](images/dbt-lineage.png)

Above is everything the sales mart is built from — the raw layer on the left,
the returns arriving on their own branch, the calendar spine feeding the grain.
That is a filtered view; `make docs` opens the whole graph, all 21 objects of
it.

Three layers, one directory each. `staging` fixes the shape and nothing else;
`intermediate` brings sources together; `marts` hands out the numbers. The
database schemas carry the same names.

The fact marts take their grain from a calendar rather than from the data —
store by day from the store's opening date, exactly 62,690 rows. A day when nothing
arrived has to appear as a row, otherwise "the store was closed", "the data did
not arrive" and "there were no sales" become indistinguishable, and telling them
apart is the entire job.

> **Emptiness is not replaced by zero.** Zero means "we counted, and it came to
> zero". Null means "there was nothing to count".

**Two decisions get asked about first.** A return belongs to the **order date**,
not the arrival date — it lands days later and reduces the revenue of the day the
purchase was made, which is exactly why yesterday's number moves. Attributing by
arrival date would give an immutable history and no project. And duplicates are
removed **by grain rather than by `distinct`**: `distinct` drops only exact
copies and silently leaves two rows on one line once the copies have diverged,
while a window function over `(order_id, line_no)` states the grain and counts
how many source rows there were.

## Stop and flag

A run is four tasks: fetch the raw data for a date, check it arrived, build the
marts behind the gate, announce what was published.

```
land_partition → check_freshness → build_marts → publish
                        │                │
                       stop             stop
```

![The chain stopped on freshness; the two tasks below it never ran](images/airflow-chain-stopped.png)

A pipeline that falls over at every defect is as useless as one that never falls
over. The line is not drawn by how bad the problem is, but by one question:
**can reprocessing fix it?**

A doubled grain can be fixed, so nothing may be counted on it and the chain
stops. A broken door counter at one store cannot be fixed by anything, and
stopping the whole network over it means having no numbers at all instead of one
store's worth.

| Check | What it does |
|---|---|
| Grain uniqueness, not-null on keys | **Stop** |
| Referential integrity of returns and order lines | **Stop** |
| Source freshness for the target run date | **Stop** |
| Order lines reconciled against the raw layer | **Stop** |
| Cancellations agreeing with order status | **Stop** |
| Conversion above the "orders > traffic × 0.95" threshold | Flag |
| A return arriving outside the reprocessing window | Flag |
| A missing partition left behind in history | Flag |

Stops are ordinary dbt tests, and where they sit matters. A dbt test runs
**after** its model is built, so a test on a mart only fails once the wrong mart
is already in the database. Everything that has to keep a bad number out of a
mart therefore lives higher up the graph — raw-layer reconciliation and
referential integrity are checked on the intermediate layer. What stays on the
marts are shape checks: grain uniqueness and not-null.

Flags are rows in a quality table — store, day, check name, reason in prose — and
one `warn`-severity test prints their summary on every run, so they are not data
somebody has to remember to look at.

Five defects are planted in the **raw layer, ahead of any pipeline**: late
returns, a missing partition, duplicated export rows, a broken traffic counter,
outliers. A defect fitted to a test only proves the test can catch itself.

The reconciliation in `make reconcile` is deliberately **not** written in SQL —
it recomputes everything from the raw layer in pandas, because a check built with
the same tool over the same models would repeat their mistake without noticing.

## Yesterday's number moves, and you can see why

![The late-return scenario, end to end](images/scenario-late-return.png)

Three scenarios reproduce from scratch, one command each, on the defects the
generator planted rather than on anything staged for a demo:

```bash
make scenario-late-return        # yesterday's number moved, and you can see why
make scenario-missing-partition  # the source did not arrive, the chain stopped
make scenario-broken-counter     # the device lies, the network still counts
```

Each of them **can fail**: if reality parts ways with the story, the exit code is
not zero. A demo that is always green proves nothing.

The late return is the substantial one. It winds time back — removes the returns
that "have not arrived yet" — then replays five days the way a scheduler would.
The replay restores what it deleted, so the raw layer ends exactly as it began.
The log fills with 891 revisions, every one of them reading `late return`.

### The window is 28 days, and the size was measured

A daily run rebuilds a window backwards rather than a single day. Across 136,116
returns the median delay is 2 days, the 95th percentile 19, the 99th 28.

| Window | Returns left outside | Amount |
|---|---|---|
| 14 days | 10.53% | 38.5M |
| 21 days | 3.47% | 12.6M |
| **28 days** | **0.78%** | **2.6M** |

Not 30, even though the largest delay in the data is exactly 30: taking the
observed maximum fits the window to the sample and pretends the tail is bounded.
It is not. What falls outside is not lost quietly — those days carry a
`return_outside_window` flag, currently on 952 store-days, and the window can be
widened on that evidence rather than on a quarterly reconciliation.

> **The window is a property of a particular mart, not a pipeline-wide setting.**
> The conversion mart has none at all: neither traffic nor orders are rewritten
> after the fact. One window everywhere means either computing too much or not
> computing enough.

## Performance, measured rather than asserted

![The same query without and with order_date in the window partition](images/query-plan.png)

Building one date of the intermediate layer took four and a half seconds for
5,834 rows, and almost three of those were a single query. The cause was not the
volume: the filter on the date sat **after** a window function, so deduplication
was computed across all 3.5M rows and then all but 5,834 were thrown away.

A predicate cannot be pushed below a window function — the result of
`row_number()` depends on which rows are in the window — **unless the filter
column is part of `partition by`**. Adding `order_date` there made the push legal.
Read it off the two plans above: `Rows Removed by Filter: 3560185` disappears, the
sequential scan becomes an index scan, the sort moves out of a 200 MB external
merge into 606 kB of memory, and the query drops from **2.8 seconds to under 6
milliseconds** — without changing a single number.

[`QUERY-PLAN.md`](QUERY-PLAN.md) has the full write-up, measured separately and
so differing in the third decimal, including the index that was measured and
turned out to cost more than it saved.

## Documentation that cannot go stale

![Column descriptions and their tests, side by side](images/dbt-columns.png)

Descriptions live in the same yml as the checks on the column they describe, and
[`DICTIONARY.md`](DICTIONARY.md) — 21 objects, 149 columns — is generated from
them. It cannot drift from the schema because it is built out of it, and the
build **fails** if a single column is left undescribed: a dictionary with holes
is worse than none, because people trust it to be complete.

The lineage graph is generated for the same reason. A picture drawn by hand parts
ways with the code inside a week.

## How it was built

| Stage | What arrived | Done when |
|---|---|---|
| 0. Skeleton | Database, Makefile, repository skeleton | `make up` brings the database up ✅ |
| 1. Data | Synthetic generator, `raw` schema, planted defects | `make seed` twice leaves the same state ✅ |
| 2. dbt layers | staging, intermediate, sales and conversion marts | `dbt run` builds the marts ✅ |
| 3. Test gates | Invariants that stop, checks that flag | Broken data is not published ✅ |
| 4. Airflow | DAG, idempotency, reprocessing window, backfill | Reprocessing a past day is one command ✅ |
| 5. Revisions | Revision log and three debugging scenarios | Every scenario reproduces from scratch ✅ |
| 6. Documentation | Dictionary, lineage, the query-plan chapter | All six completion criteria are met ✅ |

Every stage ended in a working state rather than a half-built one, so the commit
history shows not only what came out but what was revised along the way — the
decision **against** making the intermediate layer incremental, for one, was
reversed at the last stage on a measurement rather than on taste.

## What is deliberately not here

**No alerting.** A failure exits non-zero and writes a line to `alerts.log`.
Where email or a messenger would go, the interface is the same callback — but
requiring SMTP would mean demanding setup in a project that promises one command.

**No CI.** The gates exist and return non-zero; nothing calls them automatically.

**No orchestration theatre.** The DAG runs through `airflow dags test` — in full,
synchronously, with no scheduler up. Dependencies, retries and the schedule are
declared all the same, and `make airflow` brings up the web UI if you want it.

**No retries by default.** A retry helps against a transient connection error and
against nothing else; a failed data test will not turn green on the second
attempt. The one retry sits on the landing task, the only one reaching into an
external system.

## Where things are

| Document | What it answers |
|---|---|
| [`DICTIONARY.md`](DICTIONARY.md) | What every table and column means — generated, not written |
| [`QUERY-PLAN.md`](QUERY-PLAN.md) | One heavy query taken apart, with plans before and after |
| [`INCIDENTS.md`](INCIDENTS.md) | What broke, what it looked like from outside, and what to do |

And the code, in the order the data moves through it:

| Directory | What lives there |
|---|---|
| `generator/` | Makes the synthetic data and plants the five defects |
| `db/` | The raw schema, and the only DDL written by hand |
| `dbt/` | 14 models in three layers, 93 tests, macros, the snapshot |
| `airflow/` | The DAG and the alert callback — four tasks, no more |
| `checks/` | The reconciliation, in pandas rather than SQL on purpose |
| `scenarios/` | The three demos, each able to fail |
| `docs/` | Builds `DICTIONARY.md` out of the yml descriptions |

`INCIDENTS.md` has two halves. The first is the incidents the pipeline exists to
handle. The second is the ones the project hit while being built — a check that
passed green having compared nothing, a demo that explained something which had
not happened, a command that worked only in the shell of the person who wrote it.
None of them announced itself. That is the class of failure the whole project is
built against, and hiding them in the commit history would have been the wrong
kind of tidy.

## Licence

MIT, see [`LICENSE`](LICENSE).
