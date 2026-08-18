"""Loading into Postgres.

The only way anything is written here is **delete-and-insert by partition
key.** Not `insert`, not `upsert`, not `truncate` of the whole table.

Why, in one sentence: a repeat run for the same date has to leave the same
state behind, not doubled rows and not missing neighbours. `insert` doubles,
`truncate` destroys the neighbouring partitions, `upsert` needs a key — and
the raw layer deliberately has none, because it holds the source's own
duplicates.

The same property is what makes reprocessing the window in a daily run safe: a
day can be rebuilt as many times as needed and the result does not drift.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import psycopg

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "schema" / "01_raw.sql"

ORDER_COLUMNS = ["order_id", "store_id", "order_ts", "order_date", "channel", "status", "customer_id"]
ITEM_COLUMNS = ["order_id", "order_date", "line_no", "sku", "quantity", "unit_price", "line_amount"]
RETURN_COLUMNS = ["return_id", "order_id", "order_date", "line_no", "returned_date", "quantity", "returned_amount"]
CANCELLATION_COLUMNS = ["cancellation_id", "order_id", "order_date", "cancelled_date", "reason"]
TRAFFIC_COLUMNS = ["store_id", "traffic_date", "visitors"]
STORE_COLUMNS = ["store_id", "store_code", "city", "store_format", "opened_on"]

# The table and the column the partition is cut on. One list, one source of
# truth: both the load and the integrity check come here for it.
PARTITIONED_TABLES = {
    "raw.orders": "order_date",
    "raw.order_items": "order_date",
    "raw.returns": "returned_date",
    "raw.cancellations": "cancelled_date",
    "raw.store_traffic": "traffic_date",
}


def dsn() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url
    user = os.environ.get("POSTGRES_USER", "pipeline")
    password = os.environ.get("POSTGRES_PASSWORD", "pipeline")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DB", "warehouse")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn())


def apply_schema(conn: psycopg.Connection) -> None:
    """DDL lives in .sql, not in Python strings: it is read and reviewed by eye."""
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def load_stores(conn: psycopg.Connection, stores) -> None:
    """The reference table is reloaded whole — it is small and not partitioned."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE raw.stores")
        with cur.copy("COPY raw.stores (%s) FROM STDIN" % ", ".join(STORE_COLUMNS)) as cp:
            for row in stores:
                cp.write_row(row)
    conn.commit()


def load_partition(conn: psycopg.Connection, table: str, day: date, columns: list[str], rows) -> int:
    """Replace the contents of one partition. Returns the number of rows written."""
    date_column = PARTITIONED_TABLES[table]
    written = 0
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE {date_column} = %s", (day,))
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
                written += 1
    return written


def table_digest(conn: psycopg.Connection, table: str) -> tuple[int, str]:
    """Row count and checksum of a table's contents.

    The checksum is computed over sorted row hashes, so it does not depend on
    physical order. That is exactly what has to be checked: two runs must give
    the same *contents*, not the same order of pages on disk.
    """
    row = conn.execute(
        f"""
        SELECT count(*), coalesce(md5(string_agg(h, '' ORDER BY h)), '-')
        FROM (SELECT md5(t::text) AS h FROM {table} t) s
        """
    ).fetchone()
    return int(row[0]), row[1]


def all_digests(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    tables = ["raw.stores", *PARTITIONED_TABLES.keys()]
    return {t: table_digest(conn, t) for t in tables}
