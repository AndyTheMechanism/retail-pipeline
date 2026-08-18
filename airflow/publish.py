"""What was published for a date.

The last step of the chain. By this point the mart is already updated — it
updated precisely because the tests passed — and the task prints what now sits
there for that date: rows, revenue, quality flags.

A line of numbers in the run log is worth more than it looks. A week later
the question "what did yesterday's run publish" is answered by reading the log
rather than by going into the database and reconstructing which run happened
when.

Called from the DAG. By hand works too:

    .venv/bin/python airflow/publish.py 2026-03-14
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.load import connect  # noqa: E402  - sys.path is patched above

SUMMARY = """
select
    count(*)                                       as mart_rows,
    count(*) filter (where has_orders)              as rows_with_orders,
    coalesce(sum(orders_count), 0)                  as orders,
    coalesce(sum(revenue_gross), 0)                 as gross,
    coalesce(sum(returns_amount), 0)                as returns_for_date,
    coalesce(sum(revenue_net), 0)                   as net,
    coalesce(sum(returns_arrived_amount), 0)        as returns_arrived
from marts.mart_store_daily_sales
where order_date = %s
"""

FLAGS = """
select check_name, count(*)
from marts.mart_store_daily_quality
where flag_date = %s
group by check_name
order by 2 desc
"""

# The reprocessing window: which other dates this run could have changed the
# mart for.
WINDOW = """
select count(*), coalesce(sum(revenue_net), 0)
from marts.mart_store_daily_sales
where order_date between %s and %s
"""

# The window size is deliberately not a constant here. It lives in the vars of
# dbt/dbt_project.yml and reaches this file through the data: the quality table
# carries the very threshold the "return outside the window" flag compared
# against. A second definition of the number next to the first would part ways
# with it at the first edit — which is exactly what once happened to a constant
# in the DAG.
WINDOW_DAYS = """
select distinct threshold_value::int
from marts.mart_store_daily_quality
where check_name = 'return_outside_window'
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("A date is required: publish.py 2026-03-14")
        return 1

    run_date = date.fromisoformat(argv[1])

    with connect() as conn:
        row = conn.execute(SUMMARY, (run_date,)).fetchone()
        flags = conn.execute(FLAGS, (run_date,)).fetchall()

        measured = conn.execute(WINDOW_DAYS).fetchall()
        window_days = int(measured[0][0]) if len(measured) == 1 else None
        window = (
            conn.execute(WINDOW, (run_date - timedelta(days=window_days), run_date)).fetchone()
            if window_days is not None
            else None
        )

    mart_rows, rows_with_orders, orders, gross, returns_for_date, net, arrived = row

    print("Published for %s" % run_date)
    print()
    print("  rows in the mart      %d, of which %d have orders" % (mart_rows, rows_with_orders))
    print("  orders                %s" % f"{orders:,}")
    print("  gross revenue         %s" % f"{gross:,.2f}")
    print("  returns for this date %s" % f"{returns_for_date:,.2f}")
    print("  net revenue           %s" % f"{net:,.2f}")
    print()
    print("  returns that arrived on this day against orders from earlier dates: %s"
          % f"{arrived:,.2f}")
    if window is not None:
        print("  reprocessing window, %d days: rows %d, net %s"
              % (window_days, window[0], f"{window[1]:,.2f}"))
    else:
        # The "return outside the window" flag has never fired, so the window
        # size cannot be recovered from the data. That is not a publication
        # error but an absence of observations.
        print("  reprocessing window: nothing has ever fallen outside it, "
              "so its size cannot be read off the data")

    if flags:
        print()
        print("  quality flags for this date:")
        for name, count in flags:
            print("    %-28s %d" % (name, count))
    else:
        print()
        print("  no quality flags for this date")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
