"""Сверка витрин с сырым слоем.

Витрины считает SQL внутри dbt. Здесь то же самое считается второй раз - другим
инструментом и по сырью напрямую. Если бы проверка была написана тем же SQL и
тем же движком, она повторила бы ошибку модели и не заметила бы ее; смысл
сверки ровно в том, чтобы два независимых способа сошлись в одном числе.

Деньги читаются в копейках целым числом, а не в рублях с плавающей точкой.
Причина стоит абзаца, потому что на ней спотыкаются: numeric(12,2) приезжает в
pandas как float64, и на сумме по трем с половиной миллионам строк точное
равенство разваливается в районе 1e-9 - то есть первый же прогон дал бы ложный
провал и час на его разбор. В копейках суммы целочисленные и сходятся ровно.
Единственное место, где допуск честен, - скользящее среднее: среднее семи чисел
дробно по своей природе.

Что здесь проверяется и чего не проверяется. Это разовая сверка снаружи
пайплайна, и она порождает числа. Внутри гейта те же числа стоят тестами dbt -
сверку чека с сырьем делает dbt/tests/assert_order_lines_match_raw.sql в каждом
прогоне. Это не дублирование работы, а две роли одной проверки.

Запуск: make reconcile
"""

from __future__ import annotations

import io
import os
import sys

import pandas as pd
import psycopg

# Даты дефектов и краевые случаи, известные заранее из генератора. Числа здесь
# не подгоняются под результат: они получены из карты дефектов (make defects) и
# служат ожиданием, а не сверкой с самой собой.
# Даты держим объектами Timestamp, а не строками. Строка сравнивается с датой
# по-разному в зависимости от операции: == разберет ее, а isin молча не найдет
# ничего и оставит проверку без строк - то есть зеленой и бессмысленной.
DUPLICATE_DATES = [pd.Timestamp(d) for d in
                   ("2025-07-23", "2025-11-25", "2026-01-27", "2026-03-19", "2026-05-09")]
ORDERS_MISSING_DATE = pd.Timestamp("2025-02-26")
TRAFFIC_MISSING_DATES = [pd.Timestamp(d) for d in ("2025-07-23", "2026-06-25")]

MONEY = 100  # копеек в рубле


def dsn() -> str:
    """Параметры соединения, а не логика, поэтому продублированы намеренно.

    Импортировать их из генератора значило бы связать проверку с кодом, который
    она проверяет, - а сверка должна уметь смотреть на любую базу, где лежат
    эти витрины.
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
    """Читает результат запроса через COPY, а не через fetchall.

    Fetchall на трех с половиной миллионах строк сначала построил бы миллионы
    кортежей Python и только потом отдал их pandas - это гигабайты там, где
    достаточно сотен мегабайт. COPY отдает поток, который pandas разбирает сам.
    """
    buf = io.BytesIO()
    with conn.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT (FORMAT CSV, HEADER)") as copy:
            for chunk in copy:
                buf.write(chunk)
    buf.seek(0)
    return pd.read_csv(buf, **kwargs)


# ---------------------------------------------------------------------------
# Учет результатов
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("  %s  %-46s %s" % ("OK  " if ok else "ПРОВАЛ", name, detail))


def mismatch(expected: pd.Series, actual: pd.Series) -> pd.Series:
    """Где два столбца разошлись. Два пропуска считаются совпадением.

    Это важно: null в витрине означает "считать не по чему", и если сверка
    ждет там же null, то они сошлись, а не разошлись.
    """
    both_missing = expected.isna() & actual.isna()
    equal = (expected == actual).fillna(False).astype(bool)
    return ~(both_missing | equal)


def verdict(diff: pd.DataFrame, keys: list[str], total: int, limit: int = 3) -> str:
    """Итог сравнения - всегда с числом сравненных строк.

    Число здесь не для красоты. Проверка, которой нечего было сравнивать,
    выглядит точно так же, как успешная, и молча проходит - это и есть самый
    опасный вид зеленого. Пустое сравнение считается провалом, а сколько строк
    сошлось, видно в выводе.
    """
    if not diff.empty:
        head = diff[keys].head(limit).to_dict("records")
        return "расхождений %d из %d, например %s" % (len(diff), total, head)
    if total == 0:
        return "сравнивать было нечего - проверка ничего не доказывает"
    return "совпало на %d строках" % total


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


def load(conn: psycopg.Connection) -> dict[str, pd.DataFrame]:
    print("Читаю сырой слой и витрины...")

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

    # Деньги в копейках целым числом - см. докстроку модуля.
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

    # COPY в формате CSV печатает булево как t и f, и pandas честно читает их
    # строками. Без этой правки первое же отрицание упало бы на строке.
    for frame, columns in (
        ("sales", ["has_orders"]),
        ("conversion", ["has_traffic", "has_orders"]),
    ):
        for column in columns:
            data[frame][column] = data[frame][column].map({"t": True, "f": False}).astype(bool)

    # Целые колонки с пропусками pandas читает как float. Возвращаем их в целый
    # тип с поддержкой пропуска, иначе сравнение пойдет по плавающей точке -
    # ровно того, чего мы избегаем.
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
    """Та же дедупликация, что в staging, повторенная независимо.

    Правило повторяется дословно, включая сортировку: сверка должна проверять
    результат правила, а не подменять правило удобным приближением.
    """
    ordered = items.sort_values(
        ["order_id", "line_no", "line_amount_kop", "quantity", "unit_price_kop", "sku"],
        kind="stable",
    )
    return ordered.drop_duplicates(subset=["order_id", "line_no"], keep="first")


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


def check_grain(data: dict[str, pd.DataFrame]) -> None:
    orders, traffic, stores = data["orders"], data["traffic"], data["stores"]

    first_day = min(orders["order_date"].min(), traffic["traffic_date"].min())
    last_day = max(orders["order_date"].max(), traffic["traffic_date"].max())
    horizon = pd.date_range(first_day, last_day, freq="D")

    # Ожидаемое число строк считается от дат открытия, а не берется из витрины.
    expected_rows = int(sum((horizon >= opened).sum() for opened in stores["opened_on"]))

    for name, frame, key in (
        ("продажи", data["sales"], "order_date"),
        ("конверсия", data["conversion"], "traffic_date"),
        ("тренд", data["trend"], "order_date"),
    ):
        check(
            "зерно витрины: %s" % name,
            len(frame) == expected_rows,
            "строк %d, ожидалось %d" % (len(frame), expected_rows),
        )
        check(
            "зерно уникально: %s" % name,
            not frame.duplicated(subset=["store_id", key]).any(),
            "дублей зерна %d" % int(frame.duplicated(subset=["store_id", key]).sum()),
        )

    # Магазины, открывшиеся внутри горизонта: до открытия строк быть не должно.
    late = stores[stores["opened_on"] > first_day]
    first_seen = data["sales"].groupby("store_id")["order_date"].min().rename("first_row")
    joined = late.set_index("store_id").join(first_seen)
    wrong = joined[joined["first_row"] != joined["opened_on"]]
    check(
        "поздние магазины начинаются с даты открытия",
        wrong.empty,
        "таких магазинов %d, расходится %d" % (len(late), len(wrong)),
    )


def check_sales(data: dict[str, pd.DataFrame]) -> None:
    orders, sales = data["orders"], data["sales"]
    deduped = deduplicate(data["items"])

    order_head = orders[["order_id", "store_id", "order_date", "status"]]
    live = order_head[order_head["status"] != "отменен"]

    # Заказы: считаем по шапкам, как и витрина.
    counted = (
        order_head.assign(cancelled=order_head["status"] == "отменен")
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
        check("заказы: %s" % column, diff.empty and len(merged) > 0,
              verdict(diff, ["store_id", "order_date"], len(merged)))

    # Выручка и позиции по дедуплицированному сырью.
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

    # Витрина ставит ноль там, где данные приехали, а сырье просто не дает
    # строки. Приводим ожидание к тому же правилу, иначе разойдемся на пустых
    # днях по разнице соглашений, а не по ошибке.
    for column in ("lines_count", "units_sold", "revenue_gross_kop"):
        expected = merged[column + "_expected"].astype("Int64")
        expected = expected.where(~merged["has_orders"], expected.fillna(0))
        expected = expected.where(merged["has_orders"], pd.NA)
        diff = merged[mismatch(expected, merged[column + "_mart"])]
        check("выручка: %s" % column, diff.empty and len(merged) > 0,
              verdict(diff, ["store_id", "order_date"], len(merged)))

    total = int(aggregated["revenue_gross_kop"].sum())
    check("выручка брутто, итог по горизонту", True,
          "%.2f руб" % (total / MONEY))


def check_deduplication(data: dict[str, pd.DataFrame]) -> None:
    """Расхождение с недедуплицированным сырьем - доказательство, а не провал.

    Утверждений два, и они противоположные по смыслу. Витрина обязана сходиться
    с сырьем, приведенным к зерну. И она обязана НЕ сходиться с сырьем как есть,
    причем ровно на тех датах, где заложены дубли, и ровно на снятые копии. Если
    расхождения нет нигде - дедупликация не сработала; если оно есть где-то
    еще - сработала не там.
    """
    items, orders = data["items"], data["orders"]
    live = orders[orders["status"] != "отменен"][["order_id", "store_id"]]

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

    check("расхождение с сырьем как есть - ровно на датах дублей",
          with_gap == sorted(DUPLICATE_DATES),
          "дат %d из %d: %s" % (len(with_gap), by_date.size,
                                ", ".join(str(d.date()) for d in with_gap)))

    copies_removed = len(items) - len(deduplicate(items))
    check("снятые копии посчитаны", copies_removed > 0,
          "строк сырья %d, после дедупликации %d, снято %d"
          % (len(items), len(items) - copies_removed, copies_removed))


def check_returns(data: dict[str, pd.DataFrame]) -> None:
    returns, orders, sales = data["returns"], data["orders"], data["sales"]
    attributed = returns.merge(orders[["order_id", "store_id"]], on="order_id", how="left")

    check("возвраты привязались к магазину",
          attributed["store_id"].notna().all(),
          "возвратов без заказа %d" % int(attributed["store_id"].isna().sum()))

    by_order_date = (
        attributed.groupby(["store_id", "order_date"], observed=True)["returned_amount_kop"]
        .sum().rename("expected").reset_index()
    )
    merged = sales.merge(by_order_date, on=["store_id", "order_date"], how="left")
    expected = merged["expected"].astype("Int64")
    expected = expected.where(~merged["has_orders"], expected.fillna(0))
    expected = expected.where(merged["has_orders"], pd.NA)
    diff = merged[mismatch(expected, merged["returns_amount_kop"])]
    check("возвраты по оси даты заказа", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    by_arrival = (
        attributed.groupby(["store_id", "returned_date"], observed=True)["returned_amount_kop"]
        .sum().rename("expected").reset_index()
        .rename(columns={"returned_date": "order_date"})
    )
    merged = sales.merge(by_arrival, on=["store_id", "order_date"], how="left")
    expected = merged["expected"].astype("Int64").fillna(0)
    diff = merged[mismatch(expected, merged["returns_arrived_kop"])]
    check("возвраты по оси даты приезда", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    # Один и тот же набор, разложенный по двум осям, обязан дать один итог.
    left = int(sales["returns_amount_kop"].fillna(0).sum())
    right = int(sales["returns_arrived_kop"].fillna(0).sum())
    check("итоги по двум осям возвратов равны", left == right,
          "%.2f и %.2f руб" % (left / MONEY, right / MONEY))

    net = sales["revenue_gross_kop"] - sales["returns_amount_kop"]
    diff = sales[mismatch(net, sales["revenue_net_kop"])]
    check("нетто = брутто минус возвраты", diff.empty and len(sales) > 0,
          verdict(diff, ["store_id", "order_date"], len(sales)))


def check_conversion(data: dict[str, pd.DataFrame]) -> None:
    orders, traffic, conv = data["orders"], data["traffic"], data["conversion"]

    offline = orders[(orders["channel"] == "офлайн") & (orders["status"] != "отменен")]
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
    # astype(bool) обязателен: после левого джойна колонка становится объектной,
    # и отрицание объектного столбца дает целые числа, а не булево.
    merged["has_raw_orders"] = merged["has_raw_orders"].fillna(False).astype(bool)

    expected = merged["expected_offline"].astype("Int64")
    expected = expected.where(~merged["has_raw_orders"], expected.fillna(0))
    expected = expected.where(merged["has_raw_orders"], pd.NA)
    diff = merged[mismatch(expected, merged["orders_offline"])]
    check("конверсия: чеки в числителе", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    diff = merged[mismatch(merged["expected_visitors"].astype("Int64"), merged["visitors"])]
    check("конверсия: посетители в знаменателе", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    ratio = merged["orders_offline"].astype("Float64") / merged["visitors"].replace(0, pd.NA)
    diff = merged[(ratio.round(9) != merged["conversion"].round(9))
                  & ~(ratio.isna() & merged["conversion"].isna())]
    check("конверсия пересчитана из сырья", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "traffic_date"], len(merged)))

    # Раскладка пропусков: их должно быть ровно столько, сколько объяснимо.
    no_traffic = int((~conv["has_traffic"]).sum())
    zero_visitors = int((conv["visitors"] == 0).sum())
    no_orders = int((~conv["has_orders"]).sum())
    nulls = int(conv["conversion"].isna().sum())
    check("пропуски конверсии раскладываются без остатка",
          nulls == no_traffic + zero_visitors + no_orders,
          "%d = %d без трафика + %d с нулем посетителей + %d без заказов"
          % (nulls, no_traffic, zero_visitors, no_orders))


def check_defects(data: dict[str, pd.DataFrame]) -> None:
    sales, conv, items = data["sales"], data["conversion"], data["items"]

    day = sales[sales["order_date"] == ORDERS_MISSING_DATE]
    check("пропущенная партиция заказов: выручка пуста, а не ноль",
          len(day) > 0 and day["revenue_gross_kop"].isna().all() and not day["has_orders"].any(),
          "строк %d, все с пустой выручкой" % len(day))
    arrived = int(day["returns_arrived_kop"].fillna(0).sum())
    check("в тот же день возвраты все равно приехали", arrived > 0,
          "%.2f руб к заказам прошлых дней" % (arrived / MONEY))

    days = conv[conv["traffic_date"].isin(TRAFFIC_MISSING_DATES)]
    check("пропущенный трафик: конверсия пуста",
          len(days) > 0 and not days["has_traffic"].any() and days["conversion"].isna().all(),
          "строк %d за %d дня" % (len(days), len(TRAFFIC_MISSING_DATES)))

    broken = conv[conv["conversion"] > 1]
    check("битый счетчик дает конверсию выше 100%", not broken.empty,
          "магазинов %d, максимум %.0f%%"
          % (broken["store_id"].nunique(), broken["conversion"].max() * 100))

    # Выбросы обязаны дойти до промежуточного слоя нетронутыми.
    deduped = deduplicate(items)
    negative = int((deduped["line_amount_kop"] < 0).sum())
    zero_qty = int((deduped["quantity"] == 0).sum())
    check("выбросы не отфильтрованы", negative > 0 and zero_qty > 0,
          "отрицательных сумм %d, нулевых количеств %d" % (negative, zero_qty))


def check_trend(data: dict[str, pd.DataFrame]) -> None:
    """Скользящее среднее - единственное место с допуском, и он честен.

    Среднее семи чисел дробно по своей природе, целыми копейками его не
    выразить. Все остальное сравнивается точно.
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
    check("скользящее среднее совпадает с pandas", diff.empty and len(merged) > 0,
          verdict(diff, ["store_id", "order_date"], len(merged)))

    empty = int(merged["revenue_net_avg_7d"].isna().sum())
    check("неполные окна оставлены пустыми", empty > 0,
          "строк без среднего %d" % empty)


def print_digests(conn: psycopg.Connection) -> None:
    """Контрольные суммы витрин той же формулой, что у генератора.

    Считаются по отсортированным хешам строк, то есть не зависят от физического
    порядка. Двойной прогон make models обязан дать те же суммы - это и есть
    проверка того, что сортировка в дедупликации детерминирована.
    """
    print()
    print("Контрольные суммы витрин (двойной прогон обязан дать те же):")
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
        print("Сверка витрин с сырым слоем")
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
        print("Провалено %d проверок из %d:" % (len(failed), len(RESULTS)))
        for name in failed:
            print("  -", name)
        return 1

    print("Все %d проверок сошлись." % len(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
