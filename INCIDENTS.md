# Incident log

What broke, what it looked like from the outside, and what to do about it. Two
kinds of entry: data incidents — what the pipeline is built for, each one
reproducible with a single command — and build incidents, the things the project
itself tripped over while it was being written.

The second section is not here as penance. What all seven entries in it have in
common is that not one of the errors announced itself: the install went through
cleanly, the check came up green, the explanation looked complete, the command
failed with a message about something that was not actually happening. This is
exactly the class of problem the whole project is built against — a silent error
in the checking tool is more dangerous than a loud one in the data — and burying
it in a commit history nobody reads would have been odd.

---

## Data incidents

### Yesterday's number changed

**Symptom.** Revenue for the previous day came out lower than it was yesterday.

**What the pipeline did.** Exactly what it is supposed to. A return arrives a
few days after the purchase and belongs to the purchase date, so the daily run
rebuilds a window backwards. Every change to a previously published number
leaves a row in `marts.mart_store_daily_revisions`: store, day, when it was
recomputed, before, after, delta and reason.

**What to do.** Look at the log: `make revisions`. If the reason is "late
return", that is ordinary life in this domain, not a breakage. If it is "raw
layer rebuilt", the source itself has moved, and that is worth looking into.

**Reproduce.** `make scenario-late-return`

**Known limitation.** The reprocessing window is 28 days, the 99th percentile of
the observed delay. A return arriving later belongs to an order whose day will
no longer be rebuilt: the number for that day stays as it was. The loss is not
silent — such days are marked with the `return_outside_window` flag in
`marts.mart_store_daily_quality`. The numbers that justify the size of the
window are in `dbt/dbt_project.yml`.

### The source for a day did not arrive

**Symptom.** The run failed and no mart appeared for the day.

**What the pipeline did.** The `check_freshness` task did not pass, the build
never started, the mart stayed bit-for-bit as it was, a line appeared in
`airflow/alerts.log`, and the command exited non-zero. There is no number for
that day in the mart — which is more correct than a zero: zero would say there
were no sales, when in fact nothing was counted.

**What to do.** Wait for the data and call the same command for the same date —
`make run DATE=...`. Loading goes through delete-and-insert by partition key, so
a repeat is safe and doubles nothing. There is no manual procedure.

**Reproduce.** `make scenario-missing-partition`

### Conversion above one hundred per cent

**Symptom.** At one store there are more orders than visitors.

**What the pipeline did.** Ran to completion. This is a defect in the device,
not in the data, and reprocessing does not fix it: stopping the whole network
over it means having no numbers for every store instead of one. The store is
marked by a row in `marts.mart_store_daily_quality` with the reason in plain
words; the conversion there is neither replaced by an estimate nor thrown away —
the row says outright that it cannot be trusted.

**What to do.** Fix the counter. In a report, exclude the flagged stores from
averages rather than recomputing them.

**Reproduce.** `make scenario-broken-counter`

**A special case — the dead device.** Zero visitors with orders still coming in.
The rule is written as a multiplication (`orders > visitors × 0.95`) rather than
a division for exactly this reason: with zero visitors the ratio does not exist,
and writing the rule in terms of conversion would have lost the worst case.

### The order-lines reconciliation went red while the data was fine

**Symptom.** The gate stopped the chain at `assert_order_lines_match_raw`: for
some date the intermediate layer has the wrong number of lines or the wrong
total, although the raw layer looks fine.

**What the pipeline did.** Exactly what it is supposed to, and this is not a
false alarm. The intermediate layer is incremental — it touches only the target
date — so if the raw layer for an earlier date was reloaded and no run was
called for that date, the layer kept its old contents. The reconciliation goes
to the source directly, bypassing staging, and sees the discrepancy first.

**What to do.** Call a run for the date whose raw data changed:
`make run DATE=...`. If it is not clear which date that was, or there are many,
rebuild everything: `make models` builds all the layers from scratch and costs
about a minute.

**Where this failure mode came from.** It was introduced by the optimisation
described in [`QUERY-PLAN.md`](QUERY-PLAN.md): before that, the layer was
rebuilt in full on every run and could not fall behind. The risk was named when
the decision was revisited, and it was taken on deliberately — precisely because
a gate that was already standing catches it, not because it is unlikely.

---

## Build incidents

### dbt 1.11 installs on Python 3.14 and fails on the very first command

**Symptom.** `pip install` goes through without a single error, `dbt --version`
fails with `UnserializableField`.

**Cause.** dbt-core 1.11 pins `mashumaro<3.15`, and that version cannot build an
unpacker on Python 3.14. Support arrived in mashumaro 3.17, and only dbt 1.12
allows it.

**What was done.** The version is pinned from below rather than from above:
`dbt-core>=1.12,<1.13`. The reasoning sits next to the pin in
`requirements-dbt.txt`, because a lower bound looks like a mistake and somebody
will ask about it.

**Lesson.** A successful install is no proof that the thing works. Check by
running it.

### A check came up green without checking anything

**Symptom.** In the reconciliation of the marts against the raw layer, the check
"conversion is empty on days with no traffic" reported success. The number of
rows it applied to was zero.

**Cause.** The dates were compared as strings through `isin` while the column
was `datetime64`. Nothing matched, the set came out empty, and "true for every
row" is true on an empty set.

**What was done.** Every comparison now prints the number of rows compared, and
an empty comparison counts as a failure. Dates are kept as date objects rather
than strings.

**Lesson.** A check that had nothing to check looks exactly like a check that
passed. Counting the rows compared is cheaper than trusting one empty green
result.

### The cancellations test failed at the edge of the horizon

**Symptom.** The gate went red on 56 orders dated 29 and 30 June, although the
data was fine.

**Cause.** A cancellation arrives a day or two after its order, so orders from
the last two days of the horizon have no cancellation row yet — not because it
was lost, but because its time has not come.

**What was done.** The check is bounded by dates to the depth of the delay. The
opposite direction — a cancellation row against a live order — has no bound:
that is a desync on any day.

**Lesson.** A check comparing two sources with different delays must be bounded
by dates to the depth of that delay. Otherwise it is red always, someone
switches it off, and there is no gate any more.

### The explanation of the discrepancy did not explain the discrepancy

**Symptom.** The late-return scenario removed 2.88M worth of returns while the
revision log showed movement of 1.63M. The difference was put down entirely to
the tail beyond the reprocessing window — six returns worth 26.5K. So the
explanation covered two per cent of the gap while reading as though it covered
all of it.

**Cause.** Two different quantities were being compared. 2.88M is the gross
amount of the rows removed. 1.63M is the net movement of published numbers, in
which 722 downward revisions totalling 2.33M are partly offset by 169 upward
revisions totalling 0.70M.

The upward revisions, in turn, follow from how the demo is built: the first
snapshot is taken over the whole mart while a run rebuilds only a window
backwards, so the days after it stay in the snapshot with values that still
account for the deleted returns. The baseline turned out not to be one moment in
time but two of them spliced together.

**What was done.** The scenario now prints the breakdown by sign and warns
outright that the log's net delta cannot be compared with the sum of the returns
removed. The price of the window is named on a line of its own rather than
passed off as the explanation of the whole gap.

**Lesson.** The only thing worse than an unexplained discrepancy is one
explained wrongly: the first makes somebody look into it, the second closes the
question. What has to be checked is not only whether the explanation makes
sense, but whether it adds up.

### Booleans came out of the export as strings

**Symptom.** The reconciliation crashed on the negation of a boolean column, and
somewhere else the same negation silently produced integers instead of booleans.

**Cause.** `COPY ... FORMAT CSV` prints booleans as `t` and `f`, and pandas
dutifully reads them as strings. After a left join the column becomes an object
column, and `~` over an object column returns something other than what it looks
like.

**What was done.** Types are cast explicitly, immediately after reading.

**Lesson.** The boundary between the database and the analysis tool is where
types get lost quietly. That is where they have to be checked — not where the
thing broke.

### The demo explained something that had not happened

**Symptom.** The late-return scenario printed a paragraph about where upward
revisions come from — on a run where the count of upward revisions was zero. The
line above it read "0.00 gross was removed": an interrupted earlier attempt had
already removed the returns in the tail, and there was nothing left to delete.

Every number on its own was correct. What was wrong was putting them side by
side: the text described a mechanism that had not happened on this run.

**Cause.** Both paragraphs were printed unconditionally. They had been written
against a run where both the deletion and the upward revisions did happen, and
the question "what if there are none" was never asked at all.

**What was done.** Both paragraphs are now printed against the facts: with zero
upward revisions the text explains why there are none; with nothing deleted, it
says that the raw layer was trimmed before the start, and how to see the whole
scenario.

**Lesson.** The project is built on the rule "do not substitute interpretation
for fact", and it was the project's own demo that broke it — in exactly the
place where the interpretation is written in advance and the fact arrives at run
time. Text that explains a result must depend on that result; otherwise it is
not an explanation but a caption under somebody else's picture.

**How it was found.** By running the project from scratch while working through
the study walkthrough — the same way as the next entry. Your own command, run in
the state you always run it in, does not show you things like this.

### The web UI would not come up: the command was installed but not found

**Symptom.** `make airflow` failed instantly, all four threads at once:

```
Exception in thread scheduler:
Exception in thread api-server:
Exception in thread dag-processor:
Exception in thread triggerer:
FileNotFoundError: [Errno 2] No such file or directory: 'airflow'
```

It reads as "Airflow is not installed". Yet two commands earlier `make run` had
just finished a job using that very binary.

**Cause.** `airflow standalone` does not run the components itself — it starts
them as subprocesses and calls them by their short name:
`subprocess.Popen(["airflow", ...])`, that is, it searches `PATH`. The Makefile
calls the binary by its path inside the venv. That is enough for the parent
process but not for its children: nobody activated the venv, and there is no
`airflow` anywhere on `PATH`.

**What was done.** The venv is added to `PATH` for the `airflow` target alone.
It is deliberately not changed globally: the generator, dbt and the
reconciliation have environments of their own, and Airflow's Python must not end
up ahead of somebody else's.

**Lesson.** Your own `PATH` is the least visible part of the environment: a
command works for the person who wrote it because their shell is already in the
right state. The only way to catch this is to run on a clean machine — here it
was caught by running the project from a separate clone.

**A second one turned up alongside.** The web UI was listening on `0.0.0.0` —
the Airflow default — while Postgres in this same project is deliberately nailed
to `127.0.0.1` with a comment about other people's Wi-Fi. A UI with an admin
password in plain sight has even less business being shown to the network: an
`AIRFLOW__API__HOST` variable was added.
