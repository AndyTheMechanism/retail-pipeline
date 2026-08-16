"""Командный интерфейс сценариев.

    python -m scenarios late-return
    python -m scenarios missing-partition
    python -m scenarios broken-counter

Обычно зовется через make - см. цели scenario-*.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.load import connect  # noqa: E402  - после правки sys.path

ROOT = Path(__file__).resolve().parents[1]

# Даты сценариев. Взяты не с потолка: 26 июня - последняя неделя горизонта,
# поэтому удаленные возвраты восстанавливаются самим же проигрыванием и
# отдельного шага восстановления не нужно. 25 июня пропущено намеренно: за этот
# день не приехал трафик, и прогон встал бы на свежести - это другой сценарий.
LATE_RETURN_START = date(2026, 6, 26)
LATE_RETURN_END = date(2026, 6, 30)

# Дата, за которую генератор не отдает заказов. Дефект заложен на этапе 1.
MISSING_PARTITION_DATE = date(2025, 2, 26)
HEALTHY_DATE = date(2025, 2, 27)

# День внутри окна битого счетчика: магазин 74 в это время показывает ноль
# посетителей при живых чеках, магазин 17 занижает.
BROKEN_COUNTER_DATE = date(2026, 4, 15)


def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def step(text: str) -> None:
    print()
    print("--- %s" % text)


def run_pipeline(day: date, expect_failure: bool = False) -> bool:
    """Прогон пайплайна за дату - той же командой, что позвал бы человек."""
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
            print("    тест вернул строки, значит цепочка встает")
    print("    код возврата: %d%s" % (result.returncode, " (ожидаемо)" if expect_failure else ""))
    if ok == expect_failure:
        print("    НЕОЖИДАННО: ожидалось %s" % ("падение" if expect_failure else "успех"))
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
    return f"{value:,.2f}".replace(",", " ")


def mart_digest() -> str:
    return sql(
        """
        select coalesce(md5(string_agg(h, '' order by h)), '-')
        from (select md5(t::text) as h from marts.mart_store_daily_sales t) s
        """
    )[0][0]


# ---------------------------------------------------------------------------


def late_return() -> int:
    """Вчерашнее число изменилось, и в журнале видно, из-за чего."""
    banner("Сценарий: поздний возврат")
    print("""
Возврат приезжает через несколько дней после покупки и уменьшает выручку того
дня, когда покупка была. Значит опубликованное вчера число меняется - и весь
проект стоит на том, что оно меняется не молча.

Показать это на готовых данных нельзя: в сыром слое уже лежит вся история, и
витрина сразу считается с учетом всех возвратов. Поэтому сценарий отматывает
время назад - убирает возвраты, которые "еще не приехали", - и проигрывает
несколько дней подряд, как это делал бы планировщик.

Удаленные партиции возвращает само проигрывание: горизонт кончается 30 июня,
и к концу сценария сырой слой в точности такой же, каким был.""")

    step("Журнал ревизий очищается: сценарий показывает историю с нуля")
    execute("truncate table ops.snap_store_daily_sales")

    step("Возвраты, приехавшие позже %s, убираются - как будто их еще нет" % LATE_RETURN_START)
    removed = sql(
        "select count(*), coalesce(sum(returned_amount), 0) from raw.returns where returned_date > %s",
        (LATE_RETURN_START,),
    )[0]
    print("  убрано возвратов: %d на %s руб" % (removed[0], money(removed[1])))
    execute("delete from raw.returns where returned_date > %s", (LATE_RETURN_START,))

    step("Прогон за %s - это состояние мира на тот день" % LATE_RETURN_START)
    run_pipeline(LATE_RETURN_START)

    step("Проигрываю следующие дни: каждый привозит свою партию возвратов")
    day = LATE_RETURN_START + timedelta(days=1)
    while day <= LATE_RETURN_END:
        run_pipeline(day)
        day += timedelta(days=1)

    banner("Что попало в журнал ревизий")
    total = sql("select count(*) from marts.mart_store_daily_revisions")[0][0]
    print("\nСтрок в журнале: %d" % total)

    reasons = sql(
        """
        select reason, count(*), coalesce(sum(revenue_net_delta), 0)
        from marts.mart_store_daily_revisions
        group by reason order by 2 desc
        """
    )
    print("\n  %-26s %8s  %s" % ("причина", "строк", "суммарная дельта"))
    for reason, count, delta in reasons:
        print("  %-26s %8d  %s" % (reason, count, money(delta)))

    rows = sql(
        """
        select store_id, order_date, revised_at, revenue_net_was, revenue_net_became,
               revenue_net_delta, reason
        from marts.mart_store_daily_revisions
        order by revenue_net_delta limit 5
        """
    )
    print("\nПять самых крупных изменений:")
    print("  %-5s %-12s %-20s %14s %14s %14s" % ("маг.", "день", "пересчитано", "было", "стало", "дельта"))
    for store, day_, at, was, became, delta, reason in rows:
        print("  %-5d %-12s %-20s %14s %14s %14s"
              % (store, day_, at.strftime("%Y-%m-%d %H:%M:%S"), money(was), money(became), money(delta)))

    banner("Что осталось за окном пересчета")

    # Размер окна берется из данных, а не из константы: в таблице качества
    # лежит тот порог, с которым флаг реально сравнивал.
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
Окно пересчета - %d дней, и это не бесплатно. Возврат, приехавший позже, попадает
к заказу, чей день пересобирать уже не будут: число за тот день останется
прежним, то есть неверным.""" % window_days)
    print()
    print("  приехало в пределах окна и учтено:   %d возвратов" % tail[0])
    print("  приехало позже окна и НЕ учтено:     %d возвратов на %s руб" % (tail[1], money(tail[2])))

    flagged = sql(
        "select count(*) from marts.mart_store_daily_quality where check_name = 'return_outside_window'"
    )[0][0]
    print("  помечено флагом return_outside_window: %d магазино-дней" % flagged)

    print("""
Это и есть цена решения, и она названа числом, а не спрятана. Потерянное видно
в таблице качества по имени проверки; расширить окно - поменять одну переменную
return_window_days и пересчитать. Витрина остается в том состоянии, в каком ее
оставил пайплайн; вернуть эталон можно полной пересборкой - make models.

Читается так: витрина за прошлый день пересобралась, выручка нетто уменьшилась
ровно на сумму приехавших возвратов, и в журнале осталась строка с датой
пересчета, прежним значением, новым и причиной. Никто не искал, что изменилось,
- изменение само себя записало. А то, что в окно не влезло, не потерялось молча,
а помечено.""")
    return 0


def missing_partition() -> int:
    """Источник за день не приехал: цепочка встала, витрина не тронута."""
    banner("Сценарий: пропущенная партиция")
    print("""
За %s источник не отдал ни одной строки заказов. Дефект заложен генератором на
этапе 1, до всякого пайплайна, и воспроизводится по сиду - подстраивать ничего
не нужно.""" % MISSING_PARTITION_DATE)

    step("Контрольная сумма витрины до прогона")
    before = mart_digest()
    print("  %s" % before)

    alerts = ROOT / "airflow" / "alerts.log"
    alerts_before = alerts.read_text(encoding="utf-8").count("\n") if alerts.exists() else 0

    step("Прогон за дату, за которую данные не приехали")
    run_pipeline(MISSING_PARTITION_DATE, expect_failure=True)

    step("Что стало с витриной")
    after = mart_digest()
    print("  %s" % after)
    print("  витрина %s" % ("НЕ ИЗМЕНИЛАСЬ" if before == after else "изменилась - это ошибка"))

    step("Алерт")
    if alerts.exists():
        lines = alerts.read_text(encoding="utf-8").splitlines()
        added = lines[alerts_before:]
        for line in added or lines[-1:]:
            print("  %s" % line)
    else:
        print("  файла alerts.log нет - это ошибка")

    step("Флаги качества за эту дату")
    flags = sql(
        "select check_name, count(*) from marts.mart_store_daily_quality where flag_date = %s group by 1",
        (MISSING_PARTITION_DATE,),
    )
    for name, count in flags:
        print("  %-28s %d магазинов" % (name, count))

    step("Соседний день цел")
    row = sql(
        """
        select count(*), count(*) filter (where has_orders), coalesce(sum(revenue_net), 0)
        from marts.mart_store_daily_sales where order_date = %s
        """,
        (HEALTHY_DATE,),
    )[0]
    print("  %s: строк %d, с заказами %d, нетто %s" % (HEALTHY_DATE, row[0], row[1], money(row[2])))

    step("Восстановление после появления данных - та же самая команда")
    run_pipeline(HEALTHY_DATE)

    print("""
Читается так: свежесть не прошла, сборка не началась, витрина осталась той же
до бита, пришел алерт. Числа за этот день в витрине нет - и это правильнее, чем
ноль: ноль означал бы, что продаж не было, а их не посчитали. Когда источник
догонит, чинить руками нечего - тот же make run за ту же дату.""")
    return 0


def broken_counter() -> int:
    """Прибор врет в одной точке: сеть считается дальше, магазин помечен."""
    banner("Сценарий: битый счетчик")
    print("""
Дверной счетчик в нескольких магазинах занижает поток, а в одном не считает
вовсе. Конверсия там выходит выше ста процентов - физически невозможная. Это
дефект прибора, а не данных, и пересчетом он не чинится: останавливать из-за
него всю сеть значит остаться без цифр по всем магазинам вместо одного.""")

    step("Прогон за %s" % BROKEN_COUNTER_DATE)
    run_pipeline(BROKEN_COUNTER_DATE)

    step("Что помечено за этот день")
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
    print("  %-5s %10s %10s %10s  %s" % ("маг.", "чеков", "посетит.", "конверсия", "причина"))
    for store, reason, orders, visitors, conv in flags:
        print("  %-5d %10s %10s %10s  %s"
              % (store, orders, visitors, ("%s%%" % conv) if conv is not None else "нет", reason))

    step("Остальная сеть в этот день")
    row = sql(
        """
        select count(*), count(conversion),
               round(avg(conversion) filter (where conversion <= 1) * 100, 2),
               round(max(conversion) filter (where conversion <= 1) * 100, 2)
        from marts.mart_store_daily_conversion where traffic_date = %s
        """,
        (BROKEN_COUNTER_DATE,),
    )[0]
    print("  магазинов %d, конверсия посчитана у %d" % (row[0], row[1]))
    print("  средняя конверсия по здоровым точкам %s%%, максимум %s%%" % (row[2], row[3]))

    step("Продажи за этот день не пострадали")
    row = sql(
        """
        select count(*) filter (where has_orders), coalesce(sum(revenue_net), 0)
        from marts.mart_store_daily_sales where order_date = %s
        """,
        (BROKEN_COUNTER_DATE,),
    )[0]
    print("  магазинов с заказами %d, выручка нетто %s" % (row[0], money(row[1])))

    print("""
Читается так: цепочка прошла до конца, выручка посчитана по всей сети, а
конверсия помеченных точек отмечена строкой в таблице качества с причиной
человеческими словами. Число не подменено оценкой и не выброшено - сказано, что
ему нельзя верить.""")
    return 0


SCENARIOS = {
    "late-return": late_return,
    "missing-partition": missing_partition,
    "broken-counter": broken_counter,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scenarios", description="Сценарии отладки пайплайна")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    args = parser.parse_args(argv)
    return SCENARIOS[args.scenario]()


if __name__ == "__main__":
    sys.exit(main())
