"""Командный интерфейс генератора.

    python -m generator seed              полный прогон за весь горизонт
    python -m generator day 2026-03-14    пересобрать одну партицию
    python -m generator defects           карта заложенных дефектов
    python -m generator verify            контрольные суммы таблиц
    python -m generator measure-returns   распределение задержки возвратов
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

from . import defects as defects_mod
from . import load
from .config import CANCELLATION_MAX_DELAY_DAYS, MAX_RETURN_DELAY_DAYS, Config
from .model import build_stores, generate_day

# На сколько дней назад нужно оглянуться, чтобы собрать партицию за дату.
# Это максимум из всех задержек - возвраты сейчас глубже отмен.
LOOKBACK_DAYS = max(MAX_RETURN_DELAY_DAYS, CANCELLATION_MAX_DELAY_DAYS)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_seed(config: Config) -> int:
    started = time.monotonic()
    stores = build_stores(config)

    with load.connect() as conn:
        load.apply_schema(conn)
        load.load_stores(conn, stores)

        # Возвраты и отмены приезжают позже своего заказа, поэтому копятся,
        # пока их дата не наступит. Партиция за дату D полна ровно тогда,
        # когда сгенерирован день D: глубже задержки не бывает.
        pending_returns: dict[date, list] = {}
        pending_cancels: dict[date, list] = {}

        totals = {"orders": 0, "items": 0, "returns": 0, "cancellations": 0, "traffic": 0}
        all_dates = config.dates()

        for position, day in enumerate(all_dates, start=1):
            data = generate_day(config, day, stores)

            for arrival, rows in data.returns_by_arrival.items():
                pending_returns.setdefault(arrival, []).extend(rows)
            for cancellation in data.cancellations:
                pending_cancels.setdefault(cancellation.cancelled_date, []).append(cancellation)

            totals["orders"] += load.load_partition(conn, "raw.orders", day, load.ORDER_COLUMNS, data.orders)
            totals["items"] += load.load_partition(conn, "raw.order_items", day, load.ITEM_COLUMNS, data.items)
            totals["traffic"] += load.load_partition(conn, "raw.store_traffic", day, load.TRAFFIC_COLUMNS, data.traffic)
            totals["returns"] += load.load_partition(
                conn, "raw.returns", day, load.RETURN_COLUMNS, pending_returns.pop(day, [])
            )
            totals["cancellations"] += load.load_partition(
                conn, "raw.cancellations", day, load.CANCELLATION_COLUMNS, pending_cancels.pop(day, [])
            )
            conn.commit()

            if position % 30 == 0 or position == len(all_dates):
                done = position / len(all_dates)
                print(
                    "\r  %s  %3.0f%%  заказов %s" % (day, done * 100, f"{totals['orders']:,}".replace(",", " ")),
                    end="",
                    flush=True,
                )

        print()

    elapsed = time.monotonic() - started
    print()
    for name, value in totals.items():
        print("  %-14s %s" % (name, f"{value:,}".replace(",", " ")))
    print()
    print("  готово за %.0f с" % elapsed)
    print()
    print("  Хвост возвратов, приехавших после конца горизонта, отброшен")
    print("  намеренно: партиции для них нет. По той же причине в первые")
    print("  %d дней возвратов меньше - у набора нет предыстории." % LOOKBACK_DAYS)
    return 0


def cmd_day(config: Config, target: date) -> int:
    """Пересобрать одну партицию - этим работает задача land_partition.

    Возвраты и отмены за дату восстанавливаются оглядкой назад: их порождают
    заказы прошлых дней, а генерация дня детерминирована, поэтому те же заказы
    получаются заново без обращения к базе.
    """
    stores = build_stores(config)
    lookback_start = target - timedelta(days=LOOKBACK_DAYS)

    arriving_returns: list = []
    arriving_cancels: list = []
    own = None

    day = max(lookback_start, config.start_date)
    while day <= target:
        data = generate_day(config, day, stores)
        arriving_returns.extend(data.returns_by_arrival.get(target, []))
        arriving_cancels.extend(c for c in data.cancellations if c.cancelled_date == target)
        if day == target:
            own = data
        day += timedelta(days=1)

    assert own is not None

    with load.connect() as conn:
        load.apply_schema(conn)
        counts = {
            "orders": load.load_partition(conn, "raw.orders", target, load.ORDER_COLUMNS, own.orders),
            "order_items": load.load_partition(conn, "raw.order_items", target, load.ITEM_COLUMNS, own.items),
            "store_traffic": load.load_partition(conn, "raw.store_traffic", target, load.TRAFFIC_COLUMNS, own.traffic),
            "returns": load.load_partition(conn, "raw.returns", target, load.RETURN_COLUMNS, arriving_returns),
            "cancellations": load.load_partition(
                conn, "raw.cancellations", target, load.CANCELLATION_COLUMNS, arriving_cancels
            ),
        }
        conn.commit()

    print("Партиция за %s пересобрана, оглядка назад %d дней:" % (target, LOOKBACK_DAYS))
    for name, value in counts.items():
        print("  %-14s %d" % (name, value))
    return 0


def cmd_defects(config: Config) -> int:
    print(defects_mod.report(config))
    return 0


def cmd_verify(config: Config) -> int:
    with load.connect() as conn:
        digests = load.all_digests(conn)
    print("%-20s %12s  %s" % ("таблица", "строк", "контрольная сумма"))
    for table, (count, digest) in digests.items():
        print("%-20s %12s  %s" % (table, f"{count:,}".replace(",", " "), digest))
    return 0


def cmd_measure_returns(config: Config) -> int:
    """Замер, от которого зависит размер окна пересчета.

    Число берется из данных, а не из констант генератора. Так и должно быть:
    окно обосновано наблюдением, а не тем, что кто-то так задумал.
    """
    query = """
        WITH delays AS (
            SELECT (returned_date - order_date) AS days FROM raw.returns
        )
        SELECT
            count(*),
            percentile_cont(0.50) WITHIN GROUP (ORDER BY days),
            percentile_cont(0.90) WITHIN GROUP (ORDER BY days),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY days),
            percentile_cont(0.99) WITHIN GROUP (ORDER BY days),
            max(days),
            avg((days > 7)::int),
            avg((days > 14)::int)
        FROM delays
    """
    with load.connect() as conn:
        row = conn.execute(query).fetchone()

    if not row or not row[0]:
        print("Возвратов в базе нет - сначала make seed")
        return 1

    total, p50, p90, p95, p99, worst, over7, over14 = row
    print("Задержка возврата, замер по %s строкам" % f"{total:,}".replace(",", " "))
    print()
    print("  медиана        %5.1f дн" % p50)
    print("  90-й перцентиль %4.1f дн" % p90)
    print("  95-й перцентиль %4.1f дн" % p95)
    print("  99-й перцентиль %4.1f дн" % p99)
    print("  максимум       %5d дн" % worst)
    print()
    print("  приезжает позже 7 дней   %5.2f%%" % (over7 * 100))
    print("  приезжает позже 14 дней  %5.2f%%" % (over14 * 100))
    print()
    print("  По этим числам выбрано окно пересчета - 28 дней. Флаг")
    print("  return_outside_window ловит те возвраты, что в него не влезли.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generator", description="Генератор синтетики розничной воронки")
    parser.add_argument("--seed", type=int, default=Config.seed)
    parser.add_argument("--stores", type=int, default=Config.store_count)
    parser.add_argument("--start", type=_parse_date, default=Config.start_date)
    parser.add_argument("--end", type=_parse_date, default=Config.end_date)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    day_parser = sub.add_parser("day")
    day_parser.add_argument("date", type=_parse_date)
    sub.add_parser("defects")
    sub.add_parser("verify")
    sub.add_parser("measure-returns")

    args = parser.parse_args(argv)
    config = Config(seed=args.seed, start_date=args.start, end_date=args.end, store_count=args.stores)

    if args.command == "seed":
        return cmd_seed(config)
    if args.command == "day":
        return cmd_day(config, args.date)
    if args.command == "defects":
        return cmd_defects(config)
    if args.command == "verify":
        return cmd_verify(config)
    if args.command == "measure-returns":
        return cmd_measure_returns(config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
