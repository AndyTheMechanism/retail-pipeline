"""Что опубликовано за дату.

Последний шаг цепочки. Витрина к этому моменту уже обновлена - обновилась она
ровно потому, что тесты прошли, - и задача печатает, что именно теперь лежит
за эту дату: строки, выручка, флаги качества.

Строчка с числами в логе прогона дороже, чем выглядит. Через неделю вопрос
"а что вчера опубликовалось" решается чтением лога, а не походом в базу с
восстановлением того, какой прогон когда был.

Запускается из DAG. Руками - тоже можно:

    .venv/bin/python airflow/publish.py 2026-03-14
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.load import connect  # noqa: E402  - после правки sys.path

SUMMARY = """
select
    count(*)                                       as strok,
    count(*) filter (where has_orders)              as s_zakazami,
    coalesce(sum(orders_count), 0)                  as zakazov,
    coalesce(sum(revenue_gross), 0)                 as vyruchka_brutto,
    coalesce(sum(returns_amount), 0)                as vozvraty,
    coalesce(sum(revenue_net), 0)                   as vyruchka_netto,
    coalesce(sum(returns_arrived_amount), 0)        as vozvraty_priehali
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

# Окно пересчета: за какие еще даты витрина могла измениться этим прогоном.
WINDOW = """
select count(*), coalesce(sum(revenue_net), 0)
from marts.mart_store_daily_sales
where order_date between %s and %s
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Нужна дата: publish.py 2026-03-14")
        return 1

    run_date = date.fromisoformat(argv[1])
    window_days = 28

    with connect() as conn:
        row = conn.execute(SUMMARY, (run_date,)).fetchone()
        flags = conn.execute(FLAGS, (run_date,)).fetchall()
        window = conn.execute(
            WINDOW, (run_date - timedelta(days=window_days), run_date)
        ).fetchone()

    strok, s_zakazami, zakazov, brutto, vozvraty, netto, priehali = row

    print("Опубликовано за %s" % run_date)
    print()
    print("  строк в витрине     %d, из них с заказами %d" % (strok, s_zakazami))
    print("  заказов             %s" % f"{zakazov:,}".replace(",", " "))
    print("  выручка брутто      %s" % f"{brutto:,.2f}".replace(",", " "))
    print("  возвраты к этой дате %s" % f"{vozvraty:,.2f}".replace(",", " "))
    print("  выручка нетто       %s" % f"{netto:,.2f}".replace(",", " "))
    print()
    print("  возвраты, приехавшие в этот день к заказам прошлых дат: %s"
          % f"{priehali:,.2f}".replace(",", " "))
    print("  окно пересчета %d дней: строк %d, нетто %s"
          % (window_days, window[0], f"{window[1]:,.2f}".replace(",", " ")))

    if flags:
        print()
        print("  флаги качества за эту дату:")
        for name, count in flags:
            print("    %-28s %d" % (name, count))
    else:
        print()
        print("  флагов качества за эту дату нет")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
