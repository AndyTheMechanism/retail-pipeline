"""Reconciling the marts against the raw layer.

The marts are computed by SQL inside dbt. Here the same things are computed a
second time — with a different tool and straight off the raw data. Had this
check been written in the same SQL on the same engine, it would have repeated
the model's mistake without noticing it; the point of a reconciliation is
exactly that two independent routes arrive at one number.

Money is read as whole kopecks rather than as roubles in floating point. The
reason is worth a paragraph, because people trip over it: numeric(12,2) arrives
in pandas as float64, and on a sum over three and a half million rows exact
equality falls apart somewhere around 1e-9 — so the very first run would have
produced a false failure and an hour spent chasing it. In kopecks the sums are
integers and match exactly. The one place where a tolerance is honest is the
moving average: the mean of seven numbers is fractional by nature.

What is checked here and what is not. This is a one-off reconciliation outside
the pipeline, and it produces numbers. Inside the gate the same numbers stand as
dbt tests — order lines are reconciled against the raw layer by
dbt/tests/assert_order_lines_match_raw.sql on every run. That is not duplicated
work but two roles of one check.

Run: make reconcile
"""

from __future__ import annotations

import io
import os
import sys

import pandas as pd
import psycopg

# Defect dates and edge cases, known in advance from the generator. The numbers
# here are not fitted to the result: they come from the defect map
# (make defects) and serve as an expectation rather than as a check against
# itself.
# We keep the dates as Timestamp objects rather than as strings. A string
# compares against a date differently depending on the operation: == parses it,
# while isin silently finds nothing and leaves the check with no rows — green
# and meaningless.
DUPLICATE_DATES = [pd.Timestamp(d) for d in
                   ("2025-07-23", "2025-11-25", "2026-01-27", "2026-03-19", "2026-05-09")]
ORDERS_MISSING_DATE = pd.Timestamp("2025-02-26")
TRAFFIC_MISSING_DATES = [pd.Timestamp(d) for d in ("2025-07-23", "2026-06-25")]

MONEY = 100  # kopecks in a rouble


def dsn() -> str:
    """Connection parameters, not logic, so the duplication is deliberate.

    Importing them from the generator would tie the check to the code it is
    checking — and a reconciliation has to be able to look at any database that
    holds these marts.
    """
    if url := os.environ.get("DATABASE_URL"):
        return url
    user = os.environ.get("POSTGRES_USER", "pipeline")
    password = os.environ.get("POSTGRES_PASSWORD", "pipeline")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DB", "warehouse")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def read(conn: psycopg.Connection, sql: str, **kwargs) -> pd.DataFrame:
    """Reads a query result through COPY rather than through fetchall.

    Fetchall on three and a half million rows would first build millions of
    Python tuples and only then hand them to pandas — gigabytes where hundreds
    of megabytes are enough. COPY delivers a stream that pandas parses itself.
    """
    buf = io.BytesIO()
    with conn.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT (FORMAT CSV, HEADER)") as copy:
            for chunk in copy:
                buf.write(chunk)
    buf.seek(0)
    return pd.read_csv(buf, **kwargs)


# ---------------------------------------------------------------------------
# Keeping score
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("  %s  %-48s %s" % ("OK  " if ok else "FAIL", name, detail))


def mismatch(expected: pd.Series, actual: pd.Series) -> pd.Series:
    """Where two columns diverged. Two nulls count as a match.

    This matters: a null in the mart means "there was nothing to count", and if
    the reconciliation expects a null in the same place, then the two agree
    rather than differ.
    """
    both_missing = expected.isna() & actual.isna()
    equal = (expected == actual).fillna(False).astype(bool)
    return ~(both_missing | equal)


def verdict(diff: pd.DataFrame, keys: list[str], total: int, limit: int = 3) -> str:
    """The outcome of a comparison — always with the number of rows compared.

    That number is not there for looks. A check that had nothing to compare
    looks exactly like a successful one and passes silently — the most dangerous
    kind of green there is. An empty comparison counts as a failure, and how
    many rows matched is visible in the output.
    """
    if not diff.empty:
        head = diff[keys].head(limit).to_dict("records")
        return "%d of %d rows differ, for example %s" % (len(diff), total, head)
    if total == 0:
        return "there was nothing to compare — this check proves nothing"
    return "matched on %d rows" % total


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load(conn: psycopg.Connection) -> dict[str, pd.DataFrame]:
    print("Reading the raw layer and the marts...")

    data = {}

    data["stores"] = read(
        conn,
        "select store_id, opened_on from raw.stores",
        parse_dates=["opened_on"],
    )

    data["orders"] = read(
        conn,
        """
        select order_id, store_id, order_date, channel, status
        from raw.orders
        """,
        parse_dates=["order_date"],
        dtype={"channel": "category", "status": "category"},
    )

    # Money as whole kopecks — see the module docstring.
    data["items"] = read(
        conn,
        """
        select
            order_id,
            order_date,
            line_no,
            sku,
            quantity,
            (unit_price * 100)::bigint  as unit_price_kop,
            (line_amount * 100)::bigint as line_amount_kop
        from raw.order_items
        """,
        parse_dates=["order_date"],
        dtype={"sku": "category"},
    )

    data["returns"] = read(
        conn,
        """
        select
            return_id, order_id, order_date, returned_date,
            (returned_amount * 100)::bigint as returned_amount_kop
        from raw.returns
        """,
        parse_dates=["order_date", "returned_date"],
    )

    data["traffic"] = read(
        conn,
        "select store_id, traffic_date, visitors from raw.store_traffic",
        parse_dates=["traffic_date"],
    )

    data["sales"] = read(
        conn,
        """
        select
            store_id, order_date, has_orders,
            orders_count, orders_cancelled_count, lines_count, units_sold,
            (revenue_gross * 100)::bigint          as revenue_gross_kop,
            (returns_amount * 100)::bigint         as returns_amount_kop,
            (returns_arrived_amount * 100)::bigint as returns_arrived_kop,
            (revenue_net * 100)::bigint            as revenue_net_kop
        from marts.mart_store_daily_sales
        """,
        parse_dates=["order_date"],
    )

    data["conversion"] = read(
        conn,
        """
        select store_id, traffic_date, has_traffic, has_orders,
               visitors, orders_offline, conversion
        from marts.mart_store_daily_conversion
        """,
        parse_dates=["traffic_date"],
    )

    data["trend"] = read(
        conn,
        """
        select store_id, order_date, revenue_net_avg_7d
        from marts.mart_store_daily_sales_trend
        """,
        parse_dates=["order_date"],
    )

    # COPY in CSV format prints booleans as t and f, and pandas faithfully
    # reads them as strings. Without this fix the first negation would have
    # blown up on a string.
    for frame, columns in (
        ("sales", ["has_orders"]),
        ("conversion", ["has_traffic", "has_orders"]),
    ):
        for column in columns:
            data[frame][column] = data[frame][column].map({"t": True, "f": False}).astype(bool)

    # pandas reads integer columns that contain nulls as float. We put them
    # back into an integer type that supports nulls, otherwise the comparison
    # would run in floating point — exactly what we are avoiding here.
    for frame, columns in (
        ("sales", ["orders_count", "orders_cancelled_count", "lines_count", "units_sold",
                   "revenue_gross_kop", "returns_amount_kop", "returns_arrived_kop",
                   "revenue_net_kop"]),
        ("conversion", ["visitors", "orders_offline"]),
    ):
        for column in columns:
            data[frame][column] = data[frame][column].astype("Int64")

    return data


def deduplicate(items: pd.DataFrame) -> pd.DataFrame:
    """The same deduplication as in staging, repeated independently.

    The rule is copied word for word, ordering included: the reconciliation has
    to check the result of the rule, not swap the rule for a convenient
    approximation of it.
    """
    ordered = items.sort_values(
        ["order_id", "line_no", "line_amount_kop", "quantity", "unit_price_kop", "sku"],
        kind="stable",
    )
    return ordered.drop_duplicates(subset=["order_id", "line_no"], keep="first")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_grain(data: dict[str, pd.DataFrame]) -> None:
    orders, traffic, stores = data["orders"], data["traffic"], data["stores"]

    first_day = min(orders["order_date"].min(), traffic["traffic_date"].min())
    last_day = max(orders["order_date"].max(), traffic["traffic_date"].max())
    horizon = pd.date_range(first_day, last_day, freq="D")

    # The expected row count is derived from the opening dates rather than
    # taken from the mart.
    expected_rows = int(sum((horizon >= opened).sum() for opened in stores["opened_on"]))

    for name, frame, key in (
        ("sales", data["sales"], "order_date"),
        ("conversion", data["conversion"], "traffic_date"),
        ("trend", data["trend"], "order_date"),
    ):
        check(
            "mart grain: %s" % name,
            len(frame) == expected_rows,
            "rows %d, expected %d" % (len(frame), expected_rows),
        )
        check(
            "grain is unique: %s" % name,
            not frame.duplicated(subset=["store_id", key]).any(),
            "grain duplicates %d" % int(frame.duplicated(subset=["store_id", key]).sum()),
        )

    # Stores that opened inside the horizon: no rows before the opening date.
    late = stores[stores["opened_on"] > first_day]
    first_seen = data["sales"].groupby("store_id")["order_date"].min().rename("first_row")
    joined = late.set_index("store_id").join(first_seen)
    wrong = joined[joined["first_row"] != joined["opened_on"]]
    check(
        "late stores start at their opening date",
        wrong.empty,
        "such stores %d, mismatched %d" % (len(late), len(wrong)),
    )


def check_sales(data: dict[str, pd.DataFrame]) -> None:
    orders, sales = data["orders"], data["sales"]
    deduped = deduplicate(data["items"])

    order_head = orders[["order_id", "store_id", "order_date", "status"]]
    live = order_head[order_head["status"] != "cancelled"]

    # Orders: counted off the headers, the same way the mart counts them.
    counted = (
        order_head.assign(cancelled=order_head["status"] == "cancelled")
        .groupby(["store_id", "order_date"], observed=True)
        .agg(
            orders_count=("cancelled", lambda s: int((~s).sum())),
            orders_cancelled_count=("cancelled", lambda s: int(s.sum())),
        )
        .reset_index()
    )
    merged = sales.merge(counted, on=["store_id", "order_date"], how="left",
                         suffixes=("_mart", "_expected"))
    for column in ("orders_count", "orders_cancelled_count"):
        diff = merged[mismatch(merged[column + "_expected"].astype("Int64"),
                               merged[column + "_mart"])]
        check("orders: %s" % column, diff.empty and len(merged) > 0,
              verdict(diff, ["store_id", "order_date"], len(merged)))

    # Revenue and lines off the deduplicated raw data.
    lines = deduped.merge(live[["order_id", "store_id"]], on="order_id", how="inner")
    aggregated = (
        lines.groupby(["store_id", "order_date"], observed=True)
        .agg(
            lines_count=("line_no", "size"),
            units_sold=("quantity", "sum"),
            revenue_gross_kop=("line_amount_kop", "sum"),
        )
        .reset_index()
    )
    merged = sales.merge(aggregated, on=["store_id", "order_date"], how="left",
                         suffixes=("_mart", "_expected"))

    # The mart writes a zero where the data arrived and the raw layer simply
    # yields no rows. We bring the expectation to the same rule, otherwise we
    # would diverge on empty days over a difference in conventions rather than
    # over a real error.
    for column in ("lines_count", "units_sold", "revenue_gross_kop"):
        expected = merged[column + "_expected"].astype("Int64")
        expected = expected.where(~merged["has_orders"], expected.fillna(0))
        expected = expected.where(merged["has_orders"], pd.NA)
        diff = merged[mismatch(expected, merged[column + "_mart"])]
        check("revenue: %s" % column, diff.empty and len(merged) > 0,
              verdict(diff, ["store_id", "order_date"], len(merged)))

    total = int(aggregated["revenue_gross_kop"].sum())
    check("gross revenue, total over the horizon", True,
          "%.2f RUB" % (total / MONEY))


def check_deduplication(data: dict[str, pd.DataFrame]) -> None:
    """A gap against the undeduplicated raw layer is proof, not a failure.

    There are two statements here, and they are opposites. The mart must agree
    with the raw layer reduced to the grain. And it must NOT agree with the raw
    layer as it stands — on exactly the dates where the duplicates are planted,
    and by exactly the copies removed. No gap anywhere means the deduplication
    did nothing; a gap somewhere else means it worked in the wrong place.
    """
    items, orders = data["items"], data["orders"]
    live = orders[orders["status"] != "cancelled"][["order_id", "store_id"]]

    naive = (
        items.merge(live, on="order_id", how="inner")
        .groupby(["store_id", "order_date"], observed=True)["line_amount_kop"]
        .sum()
        .rename("naive_kop")
        .reset_index()
    )
    merged = data["sales"].merge(naive, on=["store_id", "order_date"], how="left")
    merged["gap_kop"] = merged["naive_kop"].astype("Int64") - merged["revenue_gross_kop"]

    by_date = merged.groupby("order_date")["gap_kop"].sum()
    with_gap = sorted(by_date[by_date.fillna(0) != 0].index)

    check("gap against raw only on the duplicate dates",
          with_gap == sorted(DUPLICATE_DATES),
          "dates %d of %d: %s" % (len(with_gap), by_date.size,
                                  ", ".join(str(d.date()) for d in with_gap)))

    copies_removed = len(items) - len(deduplicate(items))
    check("the removed copies are counted", copies_removed > 0,
          "raw rows %d, after deduplication %d, removed %d"
          % (len(items), len(items) - copies_removed, copies_removed))


def check_returns(data: dict[str, pd.DataFrame]) -> None:
    returns, orders, sales = data["returns"], data["orders"], data["sales"]
    attributed = returns.merge(orders[["order_id", "store_id"]], on="order_id", how="left")

    check("returns attached to a store",
          attributed["store_id"].notna().all(),
          "returns with no order %d" % int(attributed["store_id"].isna().sum()))

    by_order_date = (
        attributed.groupby(["store_id", "order_date"], observed=True)["returned_amount_kop"]
        .sum().rename("expected").reset_index()
    )
    merged = sales.merge(by_order_date, on=["store_id", "order_date"], how="left")
    expected = merged["expected"].astype("Int64")
    expected = expected.where(~merged["has_orders"], expected.fillna(0))
    expected = expected.where(merged["has_orders"], pd.NA)
    diff = merged[mismatch(expected, merged["returns_amount_kop"])]
    check("returns on the order-date axis", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    by_arrival = (
        attributed.groupby(["store_id", "returned_date"], observed=True)["returned_amount_kop"]
        .sum().rename("expected").reset_index()
        .rename(columns={"returned_date": "order_date"})
    )
    merged = sales.merge(by_arrival, on=["store_id", "order_date"], how="left")
    expected = merged["expected"].astype("Int64").fillna(0)
    diff = merged[mismatch(expected, merged["returns_arrived_kop"])]
    check("returns on the arrival-date axis", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    # One and the same set, laid out on two axes, must give one total.
    left = int(sales["returns_amount_kop"].fillna(0).sum())
    right = int(sales["returns_arrived_kop"].fillna(0).sum())
    check("both return axes give the same total", left == right,
          "%.2f and %.2f RUB" % (left / MONEY, right / MONEY))

    net = sales["revenue_gross_kop"] - sales["returns_amount_kop"]
    diff = sales[mismatch(net, sales["revenue_net_kop"])]
    check("net = gross minus returns", diff.empty and len(sales) > 0,
          verdict(diff, ["store_id", "order_date"], len(sales)))


def check_conversion(data: dict[str, pd.DataFrame]) -> None:
    orders, traffic, conv = data["orders"], data["traffic"], data["conversion"]

    offline = orders[(orders["channel"] == "offline") & (orders["status"] != "cancelled")]
    counted = (
        offline.groupby(["store_id", "order_date"], observed=True)
        .size().rename("expected_offline").reset_index()
        .rename(columns={"order_date": "traffic_date"})
    )
    present = orders[["store_id", "order_date"]].drop_duplicates().assign(has_raw_orders=True)
    present = present.rename(columns={"order_date": "traffic_date"})

    merged = (
        conv.merge(counted, on=["store_id", "traffic_date"], how="left")
        .merge(present, on=["store_id", "traffic_date"], how="left")
        .merge(traffic.rename(columns={"visitors": "expected_visitors"}),
               on=["store_id", "traffic_date"], how="left")
    )
    # astype(bool) is required: after a left join the column becomes an object
    # column, and negating an object column yields integers, not booleans.
    merged["has_raw_orders"] = merged["has_raw_orders"].fillna(False).astype(bool)

    expected = merged["expected_offline"].astype("Int64")
    expected = expected.where(~merged["has_raw_orders"], expected.fillna(0))
    expected = expected.where(merged["has_raw_orders"], pd.NA)
    diff = merged[mismatch(expected, merged["orders_offline"])]
    check("conversion: orders in the numerator", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    diff = merged[mismatch(merged["expected_visitors"].astype("Int64"), merged["visitors"])]
    check("conversion: visitors in the denominator", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    ratio = merged["orders_offline"].astype("Float64") / merged["visitors"].replace(0, pd.NA)
    diff = merged[(ratio.round(9) != merged["conversion"].round(9))
                  & ~(ratio.isna() & merged["conversion"].isna())]
    check("conversion recomputed from the raw layer", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    # Breaking down the nulls: there must be exactly as many as we can explain.
    no_traffic = int((~conv["has_traffic"]).sum())
    zero_visitors = int((conv["visitors"] == 0).sum())
    no_orders = int((~conv["has_orders"]).sum())
    nulls = int(conv["conversion"].isna().sum())
    check("every conversion null is accounted for",
          nulls == no_traffic + zero_visitors + no_orders,
          "%d = %d without traffic + %d with zero visitors + %d without orders"
          % (nulls, no_traffic, zero_visitors, no_orders))


def check_defects(data: dict[str, pd.DataFrame]) -> None:
    sales, conv, items = data["sales"], data["conversion"], data["items"]

    day = sales[sales["order_date"] == ORDERS_MISSING_DATE]
    check("missing order partition: revenue null, not zero",
          len(day) > 0 and day["revenue_gross_kop"].isna().all() and not day["has_orders"].any(),
          "rows %d, all with null revenue" % len(day))
    arrived = int(day["returns_arrived_kop"].fillna(0).sum())
    check("returns still arrived on that same day", arrived > 0,
          "%.2f RUB against orders from earlier days" % (arrived / MONEY))

    days = conv[conv["traffic_date"].isin(TRAFFIC_MISSING_DATES)]
    check("missing traffic: conversion is null",
          len(days) > 0 and not days["has_traffic"].any() and days["conversion"].isna().all(),
          "rows %d over %d days" % (len(days), len(TRAFFIC_MISSING_DATES)))

    broken = conv[conv["conversion"] > 1]
    check("the broken counter gives conversion above 100%", not broken.empty,
          "stores %d, maximum %.0f%%"
          % (broken["store_id"].nunique(), broken["conversion"].max() * 100))

    # Outliers must reach the intermediate layer untouched.
    deduped = deduplicate(items)
    negative = int((deduped["line_amount_kop"] < 0).sum())
    zero_qty = int((deduped["quantity"] == 0).sum())
    check("outliers are not filtered out", negative > 0 and zero_qty > 0,
          "negative amounts %d, zero quantities %d" % (negative, zero_qty))


def check_trend(data: dict[str, pd.DataFrame]) -> None:
    """The moving average is the one place with a tolerance, and it is honest.

    The mean of seven numbers is fractional by nature and cannot be expressed in
    whole kopecks. Everything else is compared exactly.
    """
    sales, trend = data["sales"], data["trend"]
    base = sales[["store_id", "order_date", "revenue_net_kop"]].sort_values(
        ["store_id", "order_date"]
    )
    base["expected"] = (
        base.groupby("store_id")["revenue_net_kop"]
        .transform(lambda s: s.astype("Float64").rolling(7, min_periods=7).mean())
    )
    merged = trend.merge(base[["store_id", "order_date", "expected"]],
                         on=["store_id", "order_date"], how="left")
    actual = merged["revenue_net_avg_7d"].astype("Float64") * MONEY
    both_missing = merged["expected"].isna() & actual.isna()
    close = ((merged["expected"] - actual).abs() < 1e-6).fillna(False).astype(bool)
    diff = merged[~(both_missing | close)]
    check("moving average matches pandas", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    empty = int(merged["revenue_net_avg_7d"].isna().sum())
    check("incomplete windows are left null", empty > 0,
          "rows without an average %d" % empty)


def print_digests(conn: psycopg.Connection) -> None:
    """Mart checksums, by the same formula the generator uses.

    They are computed over sorted row hashes, so they do not depend on physical
    order. A second run of make models must give the same sums — that is the
    check that the ordering inside the deduplication is deterministic.
    """
    print()
    print("Mart checksums (a second run must give the same ones):")
    for table in ("marts.mart_store_daily_sales",
                  "marts.mart_store_daily_conversion",
                  "marts.mart_store_daily_sales_trend"):
        row = conn.execute(
            f"""
            select count(*), coalesce(md5(string_agg(h, '' order by h)), '-')
            from (select md5(t::text) as h from {table} t) s
            """
        ).fetchone()
        print("  %-40s %8d  %s" % (table, row[0], row[1]))


def main() -> int:
    with psycopg.connect(dsn()) as conn:
        data = load(conn)

        print()
        print("Reconciling the marts against the raw layer")
        print()

        check_grain(data)
        check_sales(data)
        check_deduplication(data)
        check_returns(data)
        check_conversion(data)
        check_defects(data)
        check_trend(data)

        print_digests(conn)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print("%d of %d checks failed:" % (len(failed), len(RESULTS)))
        for name in failed:
            print("  -", name)
        return 1

    print("All %d checks agree." % len(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
