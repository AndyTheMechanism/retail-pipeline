"""Загрузка в Postgres.

Единственный способ записи здесь - **delete-and-insert по ключу партиции.**
Не `insert`, не `upsert`, не `truncate` всей таблицы.

Почему именно так, в одной фразе: повторный прогон за ту же дату должен давать
то же состояние, а не задвоение и не потерю соседних дат. `insert` задваивает,
`truncate` уничтожает соседние партиции, `upsert` требует ключа - а ключа в
сыром слое нет намеренно, потому что там лежат дубли источника.

Это же свойство делает безопасным пересчет окна в ежедневном прогоне:
пересобрать день можно столько раз, сколько нужно, и результат не поедет.
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

# Таблица и колонка, по которой режется партиция. Один список - один источник
# правды: и загрузка, и проверка целостности ходят сюда.
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
    """DDL живет в .sql, а не в строках Python: его читают и ревьюят глазами."""
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def load_stores(conn: psycopg.Connection, stores) -> None:
    """Справочник перегружается целиком - он маленький и не партиционирован."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE raw.stores")
        with cur.copy("COPY raw.stores (%s) FROM STDIN" % ", ".join(STORE_COLUMNS)) as cp:
            for row in stores:
                cp.write_row(row)
    conn.commit()


def load_partition(conn: psycopg.Connection, table: str, day: date, columns: list[str], rows) -> int:
    """Заменить содержимое одной партиции. Возвращает число записанных строк."""
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
    """Число строк и контрольная сумма содержимого таблицы.

    Сумма считается по отсортированным хешам строк, то есть не зависит от
    физического порядка. Именно это и надо проверять: два прогона обязаны дать
    одинаковое *содержимое*, а не одинаковый порядок страниц на диске.
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
