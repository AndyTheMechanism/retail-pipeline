"""Command-line interface to the scenarios.

    python -m scenarios late-return
    python -m scenarios missing-partition
    python -m scenarios broken-counter

Usually called through make — see the scenario-* targets.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator import defects as defect_map  # noqa: E402  - after the sys.path edit
from generator.config import Config  # noqa: E402
from generator.load import connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Scenario dates are computed from the defect map rather than written out as
# literals.
#
# Hardcoding them here would be exactly the silent lie the whole project is
# built against: change the seed or the horizon, and the scenario starts showing
# something other than what it promises without giving any sign of it. The
# generator knows where the defects are — so we ask it.
CONFIG = Config()


def _broken_days() -> set[date]:
    """Days where the source did not deliver something in full.

    A run for such a day stops on freshness, so scenarios that need a healthy
    day steer clear of them.
    """
    out: set[date] = set()
    for days in defect_map.missing_partitions(CONFIG).values():
        out |= set(days)
    return out


def _clean_tail(count: int) -> list[date]:
    """The last stretch of consecutive healthy days in the horizon.

    Taking the tail from the very end is deliberate: the replay itself puts back
    the returns the scenario removes, so no separate restore step is needed.
    """
    bad = _broken_days()
    days: list[date] = []
    day = CONFIG.end_date
    while len(days) < count and day >= CONFIG.start_date:
        days = [day] + days if day not in bad else []
        day -= timedelta(days=1)
    if len(days) < count:
        raise SystemExit("The horizon holds no %d healthy days in a row" % count)
    return days


def _counter_day() -> date:
    """A healthy day inside a broken-counter window."""
    bad = _broken_days()
    for window in defect_map.broken_counter_windows(CONFIG):
        day = window.start + (window.end - window.start) // 2
        if day not in bad:
            return day
    raise SystemExit("The data holds no broken-counter window")


_TAIL = _clean_tail(5)
LATE_RETURN_START, LATE_RETURN_END = _TAIL[0], _TAIL[-1]

MISSING_PARTITION_DATE = sorted(defect_map.missing_partitions(CONFIG)["orders"])[0]
HEALTHY_DATE = next(
    d for d in (MISSING_PARTITION_DATE + timedelta(days=i) for i in range(1, 15))
    if d not in _broken_days()
)

BROKEN_COUNTER_DATE = _counter_day()

# A scenario has to be able to fail. A demo that is always green proves
# nothing — that is exactly the complaint this project makes about other
# people's checks in INCIDENTS.md, and it holds just as well for its own.
FAILURES: list[str] = []


def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def step(text: str) -> None:
    print()
    print("--- %s" % text)


def run_pipeline(day: date, expect_failure: bool = False) -> bool:
    """Run the pipeline for a date — with the command a person would use."""
    print("  $ make run DATE=%s" % day)
    result = subprocess.run(
        ["make", "run", "DATE=%s" % day],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    for line in (result.stdout + result.stderr).splitlines():
        if "DagRun Finished" in line:
            print("    %s" % line.split("DagRun Finished:")[-1].split(", run_id")[0].strip())
        if "Got 1 result, configured to fail" in line:
            print("    the test returned rows, so the chain stops")
    print("    exit code: %d%s" % (result.returncode, " (expected)" if expect_failure else ""))
    if ok == expect_failure:
        note = "run for %s: expected %s, got the opposite" % (
            day, "failure" if expect_failure else "success")
        print("    UNEXPECTED: %s" % note)
        FAILURES.append(note)
    return ok


def sql(query: str, params: tuple = ()) -> list[tuple]:
    with connect() as conn:
        return conn.execute(query, params).fetchall()


def execute(query: str, params: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(query, params)
        conn.commit()


def money(value) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def mart_digest() -> str:
    return sql(
        """
        select coalesce(md5(string_agg(h, '' order by h)), '-')
        from (select md5(t::text) as h from marts.mart_store_daily_sales t) s
        """
    )[0][0]


# ---------------------------------------------------------------------------


def late_return() -> int:
    """Yesterday's number moved, and the log shows what moved it."""
    banner("Scenario: late return")
    print("""
A return arrives a few days after the purchase and reduces the revenue of the
day the purchase was made — so yesterday's published number moves, and the
whole project rests on it not moving silently.

This cannot be shown on finished data: the raw layer already holds the entire
history, so the mart is computed with every return from the start. So the
scenario winds time back — it removes the returns that "have not arrived yet" —
and replays several days in a row the way a scheduler would.

The replay itself restores the deleted partitions: the horizon ends on 30 June,
and by the end of the scenario the raw layer is exactly as it was.""")

    step("The revision log is cleared: the history fills from nothing")
    execute("truncate table ops.snap_store_daily_sales")

    step("Returns that arrived after %s are removed — as if they were not here yet" % LATE_RETURN_START)
    removed = sql(
        "select count(*), coalesce(sum(returned_amount), 0) from raw.returns where returned_date > %s",
        (LATE_RETURN_START,),
    )[0]
    print("  returns removed: %d worth %s RUB" % (removed[0], money(removed[1])))
    execute("delete from raw.returns where returned_date > %s", (LATE_RETURN_START,))

    step("Run for %s — the state of the world on that day" % LATE_RETURN_START)
    run_pipeline(LATE_RETURN_START)

    step("Replaying the days that follow: each brings its own batch of returns")
    day = LATE_RETURN_START + timedelta(days=1)
    while day <= LATE_RETURN_END:
        run_pipeline(day)
        day += timedelta(days=1)

    banner("What landed in the revision log")
    total = sql("select count(*) from marts.mart_store_daily_revisions")[0][0]
    print("\nRows in the log: %d" % total)

    reasons = sql(
        """
        select reason, count(*), coalesce(sum(revenue_net_delta), 0)
        from marts.mart_store_daily_revisions
        group by reason order by 2 desc
        """
    )
    print("\n  %-26s %8s  %s" % ("reason", "rows", "total delta"))
    for reason, count, delta in reasons:
        print("  %-26s %8d  %s" % (reason, count, money(delta)))

    # The split by sign is not decoration but a guard against a wrong reading.
    # The total delta invites comparison with the sum of the returns removed,
    # and it will not add up: those are different quantities, and the reason is
    # spelled out below.
    split = sql(
        """
        select
            count(*) filter (where revenue_net_delta < 0),
            coalesce(sum(revenue_net_delta) filter (where revenue_net_delta < 0), 0),
            count(*) filter (where revenue_net_delta > 0),
            coalesce(sum(revenue_net_delta) filter (where revenue_net_delta > 0), 0)
        from marts.mart_store_daily_revisions
        """
    )[0]
    print("\n  of these, downward: %d rows worth %s" % (split[0], money(split[1])))
    print("          and upward: %d rows worth %s" % (split[2], money(split[3])))

    # The explanation is printed when the facts call for it, not just in case.
    # Upward revisions do not always happen, and describing them when there are
    # none would explain something that did not occur — exactly the substitution
    # of interpretation for fact this project is built against.
    if split[2]:
        print("""
Upward revisions look odd and are explained by how the demo is built. The first
snapshot is taken over the whole mart, while the run for the first day rebuilds
only a window backwards — so the days AFTER it keep the values they had in the
snapshot, the ones that still account for the deleted returns. When such a day's
turn comes it is rebuilt as of then, its revenue goes up, and later runs bring
it back down as the returns arrive.""")
    else:
        print("""
There are no upward revisions, and that too has an explanation. They show up
when the mart goes into the scenario built with the full set of returns: the
days AFTER the first run sit understated in the snapshot, and the replay lifts
them first. Here the mart had already been built without those returns — so the
movement stayed one-way, downward, as they arrived.""")

    if removed[0]:
        print("""
A direct consequence worth remembering: the total delta of the log cannot be
compared with the sum of the returns removed — those are different quantities.
%s gross was removed; the log shows the movement of published numbers against a
baseline that was not itself a single moment in time.""" % money(removed[1]))
    else:
        print("""
There was nothing to remove: the returns past the tail were already gone before
the start. That happens on a repeat run of the scenario, or after an interrupted
one — the replay puts them back into the raw layer, but the mart started from a
different state. The numbers above are correct for this run; to see the scenario
whole, start from a full raw layer: make seed, then make models.""")

    rows = sql(
        """
        select store_id, order_date, revised_at, revenue_net_was, revenue_net_became,
               revenue_net_delta, reason
        from marts.mart_store_daily_revisions
        order by revenue_net_delta limit 5
        """
    )
    print("\nThe five largest changes:")
    print("  %-5s %-12s %-20s %14s %14s %14s" % ("store", "day", "recomputed at", "was", "became", "delta"))
    for store, day_, at, was, became, delta, reason in rows:
        print("  %-5d %-12s %-20s %14s %14s %14s"
              % (store, day_, at.strftime("%Y-%m-%d %H:%M:%S"), money(was), money(became), money(delta)))

    banner("What fell outside the reprocessing window")

    # The window size comes from the data rather than from a constant: the
    # quality table holds the very threshold the flag compared against.
    threshold = sql(
        """
        select distinct threshold_value from marts.mart_store_daily_quality
        where check_name = 'return_outside_window'
        """
    )
    window_days = int(threshold[0][0]) if threshold else 28

    tail = sql(
        """
        select
            count(*) filter (where returned_date - order_date <= %s),
            count(*) filter (where returned_date - order_date > %s),
            coalesce(sum(returned_amount) filter (where returned_date - order_date > %s), 0)
        from raw.returns
        where returned_date > %s
        """,
        (window_days, window_days, window_days, LATE_RETURN_START),
    )[0]

    print("""
The reprocessing window is %d days, and that is not free. A return that arrives
later belongs to an order whose day will not be rebuilt any more: the number for
that day stays as it was, which is to say wrong.""" % window_days)
    print()
    print("  arrived within the window and counted:    %d returns" % tail[0])
    print("  arrived after the window and NOT counted: %d returns worth %s RUB" % (tail[1], money(tail[2])))

    flagged = sql(
        "select count(*) from marts.mart_store_daily_quality where check_name = 'return_outside_window'"
    )[0][0]
    print("  flagged return_outside_window:            %d store-days" % flagged)

    print("""
This is the price of the decision, and it is named as a number rather than
hidden. What was lost is visible in the quality table under the check name;
widening the window means changing one variable, return_window_days, and
reprocessing. The mart stays in whatever state the pipeline left it in; a full
rebuild — make models — restores the reference state.

Read it like this: the mart for an earlier day was rebuilt, net revenue went
down by exactly the amount of the returns that arrived, and the log kept a row
with the date of the recomputation, the old value, the new one and the reason.
Nobody had to go looking for what changed — the change recorded itself. And what
did not fit inside the window was not lost silently — it was flagged.""")
    return 0


def missing_partition() -> int:
    """The day's source never arrived: the chain stopped, the mart untouched."""
    banner("Scenario: missing partition")
    print("""
For %s the source handed over not a single order row. The generator plants that
defect in the raw layer, ahead of any pipeline, and it reproduces under the
seed — nothing has to be staged, and the date is not even written out here as a
literal but taken from the defect map.""" % MISSING_PARTITION_DATE)

    step("Mart checksum before the run")
    before = mart_digest()
    print("  %s" % before)

    alerts = ROOT / "airflow" / "alerts.log"
    alerts_before = alerts.read_text(encoding="utf-8").count("\n") if alerts.exists() else 0

    step("Run for a date whose data never arrived")
    run_pipeline(MISSING_PARTITION_DATE, expect_failure=True)

    step("What happened to the mart")
    after = mart_digest()
    print("  %s" % after)
    if before == after:
        print("  the mart is UNCHANGED")
    else:
        print("  the mart changed — and it should not have")
        FAILURES.append("the mart changed even though freshness had failed")

    step("Alert")
    if alerts.exists():
        lines = alerts.read_text(encoding="utf-8").splitlines()
        added = lines[alerts_before:]
        for line in added or lines[-1:]:
            print("  %s" % line)
    else:
        print("  there is no alerts.log file — that is a bug")

    step("Quality flags for this date")
    flags = sql(
        "select check_name, count(*) from marts.mart_store_daily_quality where flag_date = %s group by 1",
        (MISSING_PARTITION_DATE,),
    )
    for name, count in flags:
        print("  %-28s %d stores" % (name, count))

    step("The neighbouring day is intact")
    row = sql(
        """
        select count(*), count(*) filter (where has_orders), coalesce(sum(revenue_net), 0)
        from marts.mart_store_daily_sales where order_date = %s
        """,
        (HEALTHY_DATE,),
    )[0]
    print("  %s: rows %d, with orders %d, net %s" % (HEALTHY_DATE, row[0], row[1], money(row[2])))

    step("Recovery once the data shows up — the very same command")
    run_pipeline(HEALTHY_DATE)

    print("""
Read it like this: freshness failed, the build never started, the mart stayed
bit-for-bit as it was, an alert arrived. There is no number for this day in the
mart — and that is more correct than a zero: a zero would say there were no
sales, when in fact nothing was counted. Once the source catches up there is
nothing to fix by hand — the same make run for the same date.""")
    return 0


def broken_counter() -> int:
    """One store's device lies: the network still counts, the store flagged."""
    banner("Scenario: broken counter")
    print("""
The door counter at several stores understates footfall, and at one of them it
does not count at all. Conversion there comes out above a hundred per cent —
physically impossible. This is a defect of the device, not of the data, and
reprocessing does not fix it: stopping the whole network over it means having no
numbers for every store instead of one.""")

    step("Run for %s" % BROKEN_COUNTER_DATE)
    run_pipeline(BROKEN_COUNTER_DATE)

    step("What is flagged for this day")
    flags = sql(
        """
        select q.store_id, q.reason, c.orders_offline, c.visitors,
               round(c.conversion * 100, 1)
        from marts.mart_store_daily_quality q
        join marts.mart_store_daily_conversion c
          on c.store_id = q.store_id and c.traffic_date = q.flag_date
        where q.flag_date = %s and q.check_name = 'conversion_above_threshold'
        order by q.store_id
        """,
        (BROKEN_COUNTER_DATE,),
    )
    print("  %-5s %10s %10s %10s  %s" % ("store", "orders", "visitors", "conversion", "reason"))
    for store, reason, orders, visitors, conv in flags:
        print("  %-5d %10s %10s %10s  %s"
              % (store, orders, visitors, ("%s%%" % conv) if conv is not None else "none", reason))

    step("The rest of the network on this day")
    row = sql(
        """
        select count(*), count(conversion),
               round(avg(conversion) filter (where conversion <= 1) * 100, 2),
               round(max(conversion) filter (where conversion <= 1) * 100, 2)
        from marts.mart_store_daily_conversion where traffic_date = %s
        """,
        (BROKEN_COUNTER_DATE,),
    )[0]
    print("  stores %d, conversion computed for %d" % (row[0], row[1]))
    print("  average conversion across healthy stores %s%%, maximum %s%%" % (row[2], row[3]))

    step("Sales for this day are unharmed")
    row = sql(
        """
        select count(*) filter (where has_orders), coalesce(sum(revenue_net), 0)
        from marts.mart_store_daily_sales where order_date = %s
        """,
        (BROKEN_COUNTER_DATE,),
    )[0]
    print("  stores with orders %d, net revenue %s" % (row[0], money(row[1])))

    print("""
Read it like this: the chain ran to the end, revenue was computed across the
whole network, and the conversion of the flagged stores carries a row in the
quality table with the reason in plain words. The number was neither replaced by
an estimate nor thrown away — the flag says outright that it cannot be trusted.""")
    return 0


SCENARIOS = {
    "late-return": late_return,
    "missing-partition": missing_partition,
    "broken-counter": broken_counter,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scenarios", description="Pipeline debugging scenarios")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    args = parser.parse_args(argv)
    SCENARIOS[args.scenario]()

    # The exit code is not a formality. A demo that cannot fail proves nothing,
    # and green by default is worse than red: it creates confidence where there
    # was no check at all.
    if FAILURES:
        print()
        print("The scenario did not add up — %d discrepancies:" % len(FAILURES))
        for note in FAILURES:
            print("  -", note)
        return 1

    print()
    print("The scenario ran as intended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
