[![English](https://img.shields.io/badge/lang-English-2563eb?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/%D1%8F%D0%B7%D1%8B%D0%BA-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-64748b?style=for-the-badge)](README.ru.md)

# Retail Funnel Pipeline

A daily pipeline that computes a retail chain's revenue per store, survives
late returns and broken sources, and **never changes yesterday's number
silently.**

Stack: PostgreSQL, dbt, Airflow, Python. The data is synthetic; the generator
lives in this repository and is deterministic under a seed.

> **Status: stage 5 of 6.** The pipeline runs and explains itself: a DAG
> computes a day, broken data never reaches the marts, reprocessing a past day
> is one command, and every change to a previously published number leaves a row
> in the revision log. What is left is the documentation — dictionary, lineage
> and the query-plan chapter. What is missing is listed below and printed by
> `make`.

## Run it

```bash
make seed                                    # start the database, generate the data
make run DATE=2026-03-14                     # run the pipeline for one date
make backfill FROM=2026-03-01 TO=2026-03-05  # reprocess a period
make revisions                               # what changed, when and why
make reconcile                               # reconcile the marts against the raw layer
make psql                                    # open a shell on the database
make down                                    # stop; the data survives
```

The marts can also be built without the orchestrator, in full: `make models`
builds them behind the same checks, `make test` runs the checks alone.

`make seed` does everything itself: starts Postgres, creates the virtual
environment, applies the schema and loads an eighteen-month horizon. On a fresh
clone it is the only command you need. It takes about a minute.

`make models` is just as self-contained: dbt has a virtual environment of its
own and the target builds it. A full run of the twelve models takes about ten
seconds.

You need a container engine and `make`. Which engine does not matter: `make up`
detects whether you have docker or podman and calls the right compose. There is
no need to install a `psql` client on the host — `make psql` opens one inside
the container. On Windows, run this under WSL2: the entry point here is a
Makefile, and native Windows has none.

Nothing needs configuring before the first run — every variable has a default.
To override one, say a port that is already taken, use `.env`; a template sits
in `.env.example`.

```bash
make defects          # where the defects are planted
make measure-returns  # distribution of return delay
make verify           # table checksums
make seed-day DATE=2026-03-14   # rebuild a single partition
```

For the full list of targets, run `make` with no arguments.

## The data

Eighteen months, 120 stores, roughly 1.5M orders and 3.6M order lines. The
volume is not decoration: at this size a query plan starts to mean something,
and at a toy size it does not.

The generator is deterministic under a seed, so the data is not committed —
what produces it is. Two consecutive runs leave the database in the same state,
and `make verify` demonstrates that rather than promising it.

The horizon is a pair of fixed dates rather than an offset from today.
Otherwise yesterday's run and today's would differ, and the repeatability check
would keep nothing but its appearance.

### The defects are planted on purpose

Five of them, and they sit in the **raw layer, ahead of any pipeline**: late
returns, a missing partition, duplicated export rows, a broken traffic counter,
and outliers. A defect fitted to a test only proves that the test can catch
itself.

`make defects` prints where they are. What the checks do with them is in the
stop-and-flag section: some break the chain, some mark the store and let the
rest of the chain through.

### Loading is delete-and-insert by date, and nothing else

Not `insert`, not `truncate`, not `upsert`. Re-running a given date must leave
the same state behind — not doubled rows, and not missing neighbours. The same
property is what makes reprocessing safe: `make seed-day` can be called on any
date as many times as you like, and the sales mart is written the very same way.

The returns partition is keyed on the **return date**, not the order date —
a return arrives on its own day and changes the revenue of an earlier one. So
rebuilding one partition looks 30 days back and reconstructs exactly the
returns a full run would have produced. Verified by comparing checksums.

## The layers

Three layers, one directory each: `staging` fixes the shape, `intermediate`
brings sources together, `marts` hands out the marts. The schemas in the
database carry the same names — `staging`, `intermediate`, `marts`.

There are two marts — sales per store and day, conversion per store and day —
plus a weekly revenue baseline as a separate model on top of sales. All three
share one grain: store by day from the store's opening date, exactly 62,690
rows. A calendar sets that grain, not the data, and deliberately so. A day
where nothing arrived from the source has to show up as a row rather than as a
missing one; otherwise "the store was closed", "the data did not arrive" and
"there were no sales" become indistinguishable — and telling them apart is what
the gate is for.

Hence a rule kept everywhere here: **emptiness is not replaced by zero.** Zero
means "we counted, and it came to zero"; a null means "there was nothing to
count".

The sales mart is incremental: a daily run rebuilds a window backwards rather
than the whole history. The window — and why the conversion mart does not have
one — has a section of its own below, along with the checks.

### The two decisions people ask about first

**A return belongs to the order date, not to the arrival date.** It arrives
days later and reduces the revenue of the day the purchase was made — so
yesterday's number moves. That is not a side effect but the subject of the
project: the reprocessing window and the revision log both rest on it.
Attributing by arrival date would give an immutable history. The second axis
stays alongside as its own column, so it is visible where the change came from.

**Duplicates are removed by grain, not by `distinct`.** `distinct` only removes
exact copies and silently leaves two rows on one line when the copies have
diverged. A window function over `(order_id, line_no)` states the grain, and
counts how many source rows there were for that line while it is at it. The
ordering inside the window is explicit: without it the order is undefined, two
runs would produce different numbers, and reproducibility would break silently.

## Stop and flag

Not all defects are equal, and a pipeline that falls over at every one of them
is as useless as a pipeline that never falls over. The line is drawn not by how
bad the problem is, but by a single question: **can reprocessing fix it?**

A doubled grain can be fixed — so nothing may be counted on it and the chain
stops. A broken door counter at one store cannot be fixed by anything, and
stopping the whole chain over it means having no numbers for every store
instead of one.

| Check | What it does |
|---|---|
| Grain uniqueness, not-null on keys | Stop |
| Referential integrity of returns and order lines | Stop |
| Source freshness for the target run date | Stop |
| Order lines reconciled against the raw layer for a period | Stop |
| Cancellations agreeing with order status | Stop |
| Conversion above the "checks > traffic × 0.95" threshold | Flag |
| A return arriving outside the reprocessing window | Flag |
| A missing partition left behind in history | Flag |

Stops are ordinary dbt tests. Flags are rows in
`marts.mart_store_daily_quality`: store, day, check name, reason in Russian. A
table of its own rather than columns on the marts, because a single row can
carry several flags and there will be more of them over time: as rows that
grows naturally, as columns it does not. On top of that, one test with `warn`
severity prints their summary on every run — otherwise flags would be data that
someone has to remember to look at.

Freshness is measured against the **target run date**, not against the wall
clock: the data horizon is fixed and ends in the past, so a "fresh as of today"
check would be red always and rightly so — and a disabled gate is worse than an
absent one. The date can be set by hand:
`make test VARS='{run_date: 2025-02-26}'`.

### What "not published" actually is

`make models` calls `dbt build`, not `dbt run`: tests are interleaved with
models along the graph, and a failing test on a lower layer simply does not let
the marts be built. The previous mart stays where it was, untouched —
yesterday's number is not rewritten, rather than rewritten wrongly.

Hence something worth knowing about the tool itself: a dbt test runs after its
model has been built, so a test on a mart fails once the wrong mart is already
in the database. Everything that must keep a bad number out of a mart sits
higher up the graph — the raw-layer reconciliation and referential integrity
live on the intermediate layer. What is left on the marts are shape checks.

It takes a minute to verify by hand: slip a duplicate order into the raw layer,
call `make models`, and watch uniqueness fail, the marts get skipped, and their
checksums stay exactly as they were. One command puts it back —
`make seed-day DATE=...`.

### A check bounded by dates

A cancellation arrives a day or two after its order, so orders from the last
two days of the horizon may not have a cancellation row yet — not because it was
lost, but because its time has not come. The first version of that test did not
account for this and failed on 56 orders.

The lesson generalises, and it is an expensive one: a check comparing two
sources with different delays must be bounded by dates to the depth of that
delay. Otherwise it is red always, someone switches it off, and there is no
gate any more.

### Reconciliation

`make reconcile` computes the same things a second time — from the raw layer,
in pandas — and compares them with the marts. It is deliberately not written in
SQL: a check made with the same tool over the same models would repeat their
mistake without noticing it.

Acceptance rests on two opposite statements. The mart must agree with the raw
layer reduced to the grain, and it must **disagree** with the raw layer as it
stands — on exactly the five dates where duplicates are planted, and by exactly
the copies removed. No discrepancy anywhere would mean the deduplication did
nothing.

## The revision log

The mart is never rewritten silently. Every recomputation that changed a
previously published number leaves a row behind: store, day, when it was
recomputed, before, after, delta and reason.

```bash
make revisions
```

It rests on a dbt snapshot: after every build the state of the sales mart is
captured, and a new version appears only when one of four values has actually
moved. The log itself is a view over the snapshot that puts adjacent versions
side by side with a window function.

The reason is not guessed but derived from which quantities diverged. Returns
changed while gross stayed put — a late return arrived, which is ordinary life
in this domain. Gross changed — the raw layer was rebuilt, and that is worth
looking into.

This is the answer to the pain the whole project is built around. "The mart
updated silently and wrongly" is the employer's complaint; "the mart updated,
and here is the row saying what it was, what it became and why" is the answer.

## Three scenarios

Each reproduces from scratch with one command and rests on defects planted into
the raw layer by the generator rather than staged for the demo.

```bash
make scenario-late-return        # yesterday's number moved, and you can see why
make scenario-missing-partition  # the source did not arrive, the chain stopped
make scenario-broken-counter     # the device lies, the network still counts
```

**The late return** is the substantial one. It cannot be shown on finished data:
the raw layer already holds the whole history, so the mart is computed with
every return from the start. So the scenario winds time back — removes the
returns that "have not arrived yet" — and replays five days in a row the way a
scheduler would. The replay itself restores the deleted partitions, so by the
end the raw layer is exactly as it was. The log fills with 891 revisions
totalling −1.63M, every one of them reading "late return".

The same output names the price of the window as a number: of the 1,089 returns
removed, 1,083 arrived within 28 days and were accounted for, while 6 worth
26.5K arrived later — their days were no longer rebuilt, so the number for them
stayed as it was. This is not hidden: those days carry the
`return_outside_window` flag.

What broke and how it was fixed, including the build's own incidents, is in
[`INCIDENTS.md`](INCIDENTS.md).

## The daily run

A DAG of four tasks — exactly the chain from the blueprint:

```
land_partition -> check_freshness -> build_marts -> publish
                        |                 |
                       stop              stop
```

Fetch the raw data for a date, check freshness, build the marts behind the gate,
announce what was published. The tasks call the tools as subprocesses rather
than importing them: the generator, dbt and Airflow have incompatible pins and
each lives in its own environment. What ties them together is an exit code, and
that does not break because somebody upgraded a library.

The run date is handed to the tools explicitly rather than read by them off the
wall clock. Everything rests on it: freshness and the reprocessing window alike.
A run for a past day must produce exactly what it would have produced then —
otherwise it is not reprocessing but a new history.

### Reprocessing a past day

```bash
make run DATE=2026-03-14                     # one day
make backfill FROM=2026-03-01 TO=2026-03-05  # a period
```

Verified by checksums rather than by promise: two runs for the same date, five
consecutive days reprocessed, and a full build from scratch all leave the marts
bit-for-bit identical. Everything is written with delete-and-insert by key — the
same technique as the raw load, and for the same reason.

The run goes through `airflow dags test`: the DAG executes in full and
synchronously, without a scheduler running. For a project that has to start with
one command that matters more than a demonstration of daemons — dependencies,
retries and the schedule are declared in the DAG, and it can be run without
bringing anything up. The web UI, if you want it, is `make airflow`.

### The window is 28 days

A return arrives later than the purchase and changes the revenue of an earlier
day, so a daily run rebuilds a window backwards rather than a single day. The
size was measured, not assigned: across 136,116 returns the median is 2 days,
the 95th percentile 19, the 99th 28.

| Window | Returns left outside | Amount |
|---|---|---|
| 14 days | 10.5% | 38.5M |
| 21 days | 3.47% | 12.6M |
| **28 days** | **0.78%** | **2.6M** |

Why not 30, when the maximum in the data is exactly 30. Taking the observed
maximum means fitting the window to the sample and pretending the tail is
bounded. It is not: the next return may arrive on day 31, and a window equal to
the maximum will say nothing about it. At 28 days the remainder is caught by the
`return_outside_window` flag — it currently marks 952 store-days — and the
window can be widened on its evidence rather than on a quarterly reconciliation.

Worth stating separately, because it is not obvious: **the window is a property
of a particular mart, not a pipeline-wide setting.** The conversion mart has no
window at all — neither traffic nor orders are rewritten after the fact, they
have no second date on which they could arrive later — so a run touches exactly
the target date. Applying one window everywhere means either computing too much
or not computing enough.

### Retries and the alert

There are no retries by default. A retry helps against a transient connection
error and against nothing else: a failed data test will not turn green on the
second attempt, and three tries only triple the time before a human hears about
it. The retry sits precisely on the landing task — the only one that reaches
into an external system.

On failure a callback fires: a line in `airflow/alerts.log` and in the task log.
In production this is where email or a messenger goes, but the interface is the
same — a callback function, and only it would change. Requiring SMTP in a
project that promises to start with one command would mean demanding setup
exactly where none was promised.

It takes a minute to check: `make run DATE=2025-02-26` — no order partition ever
arrived for that date. Freshness fails, the build never starts, the marts do not
move, a line appears in `alerts.log`, and the command exits non-zero.

## How it is put together

One Postgres container holding two databases: `warehouse` for the raw layer and
the marts, `airflow_meta` for scheduler metadata. Airflow itself sits in a local
venv rather than a container — reading logs and debugging is markedly easier
that way, and debugging is precisely what this project is meant to show. It is
installed against the official constraints file: it carries around two hundred
transitive dependencies, and without pins the resolver assembles a combination
nobody has ever tested.

Airflow's own config is not committed: it is generated, it runs to over a
hundred kilobytes of somebody else's defaults, and those cannot be explained.
Everything this project needs is five environment variables in the Makefile.

The official Airflow compose file is deliberately not used. It carries eight
services and over two hundred lines; they cannot be explained in a minute, and
being explainable is a requirement here.

There are four virtual environments, one per tool: the generator, dbt, the
reconciliation and Airflow. Their versions are not obliged to agree, and that is
not theory — Airflow pins around two hundred packages, and any attempt to fold
it together with dbt ends in a night with the resolver. What ties them together
is an exit code rather than a shared set of packages: the DAG calls the tools as
subprocesses instead of importing them.

## Three questions this file has to answer

The section needs answers, not headings: by stage 6 it dissolves into the text
above, and the README should make all three plain without a single question
mark —

1. **what happens when a task fails** — which checks stop the chain, what is
   left unpublished as a result, and how anyone finds out;
2. **how to reprocess an earlier period** — one command, same result, no
   six-step manual procedure;
3. **why a test breaks the chain instead of logging a warning** — and where the
   line between "stop" and "flag" runs.

Empty for now: there is nothing to answer with, the machinery does not exist
yet. The section exists so that by the end this is neither forgotten nor
reduced to three headings full of generalities.

## What is not there yet

| Stage | What arrives | Done when |
|---|---|---|
| 0. Skeleton | Database, Makefile, repository skeleton | `make up` brings the database up ✅ |
| 1. Data | Synthetic generator, `raw` schema, planted defects | `make seed` twice leaves the same state ✅ |
| 2. dbt layers | staging, intermediate, sales and conversion marts | `dbt run` builds the marts ✅ |
| 3. Test gates | Invariants that stop, checks that flag | Broken data is not published ✅ |
| 4. Airflow | DAG, idempotency, reprocessing window, backfill | Reprocessing a past day is one command ✅ |
| 5. Revisions | Revision log and three debugging scenarios | Every scenario reproduces from scratch ✅ |
| 6. Documentation | Dictionary, lineage, the query-plan chapter | All six completion criteria are met |

## Licence

MIT, see [`LICENSE`](LICENSE).
