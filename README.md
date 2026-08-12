[![English](https://img.shields.io/badge/lang-English-2563eb?style=for-the-badge)](README.md) [![Русский](https://img.shields.io/badge/%D1%8F%D0%B7%D1%8B%D0%BA-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-64748b?style=for-the-badge)](README.ru.md)

# Retail Funnel Pipeline

A daily pipeline that computes a retail chain's revenue per store, survives
late returns and broken sources, and **never changes yesterday's number
silently.**

Stack: PostgreSQL, dbt, Airflow, Python. The data is synthetic; the generator
lives in this repository and is deterministic under a seed.

> **Status: stage 1 of 6.** The database and the raw layer with data are in
> place. Models, tests and orchestration are not. What is missing is listed
> below and printed by `make`.

## Run it

```bash
make seed    # start the database, build the environment, generate the data
make psql    # open a shell on the database
make down    # stop; the data survives
```

`make seed` does everything itself: starts Postgres, creates the virtual
environment, applies the schema and loads an eighteen-month horizon. On a fresh
clone it is the only command you need. It takes about a minute.

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

`make defects` prints where they are. The checks that catch them arrive at
stage 3.

### Loading is delete-and-insert by date, and nothing else

Not `insert`, not `truncate`, not `upsert`. Re-running a given date must leave
the same state behind — not doubled rows, and not missing neighbours. The same
property is what will make reprocessing safe at stage 4: `make seed-day` can be
called on any date as many times as you like.

The returns partition is keyed on the **return date**, not the order date —
a return arrives on its own day and changes the revenue of an earlier one. So
rebuilding one partition looks 30 days back and reconstructs exactly the
returns a full run would have produced. Verified by comparing checksums.

## How it is put together

One Postgres container holding two databases: `warehouse` for the raw layer and
the marts, `airflow_meta` for scheduler metadata. Airflow itself will be
installed into a local venv at stage 4 rather than containerised — reading logs
and debugging is markedly easier that way, and debugging is precisely what this
project is meant to show.

The official Airflow compose file is deliberately not used. It carries eight
services and over two hundred lines; they cannot be explained in a minute, and
being explainable is a requirement here.

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
| 2. dbt layers | staging, intermediate, sales and conversion marts | `dbt run` builds the marts |
| 3. Test gates | Invariants that stop, checks that flag | Broken data is not published |
| 4. Airflow | DAG, idempotency, reprocessing window, backfill | Reprocessing a past day is one command |
| 5. Revisions | Revision log and three debugging scenarios | Every scenario reproduces from scratch |
| 6. Documentation | Dictionary, lineage, the query-plan chapter | All six completion criteria are met |

## Licence

MIT, see [`LICENSE`](LICENSE).
