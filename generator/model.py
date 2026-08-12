"""Как устроен один день сети.

Главное свойство всего файла: **генерация дня - чистая функция от (сид, дата).**
Никакой день не зависит от того, сгенерирован ли соседний. Из этого следует
то, ради чего это и сделано, - партицию за любую дату можно пересобрать
отдельно и получить ровно те же строки, что дал бы полный прогон.

Одно исключение из независимости честное и содержательное: возвраты. Возврат,
приехавший в день R, относится к заказу, сделанному раньше. Поэтому возврат
вычисляется **из самого заказа** - функцией от его идентификатора, - и
одинаково получается что при полном прогоне, что при пересборке партиции за R
с оглядкой на MAX_RETURN_DELAY_DAYS назад.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple

from . import defects
from .config import CANCELLATION_RATE, MAX_RETURN_DELAY_DAYS, RETURN_RATE, Config
from .rng import stream

CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань",
    "Нижний Новгород", "Челябинск", "Самара", "Уфа", "Ростов-на-Дону",
    "Омск", "Красноярск", "Воронеж", "Пермь", "Волгоград", "Краснодар",
    "Саратов", "Тюмень", "Тольятти", "Ижевск", "Барнаул", "Ульяновск",
    "Иркутск", "Хабаровск", "Ярославль", "Владивосток", "Махачкала",
    "Томск", "Оренбург", "Кемерово",
]

FORMATS = ["малый", "средний", "большой"]
FORMAT_WEIGHTS = [0.45, 0.38, 0.17]
FORMAT_BASE_ORDERS = {"малый": 12, "средний": 25, "большой": 45}

CHANNELS = ["офлайн", "сайт", "приложение"]
CHANNEL_WEIGHTS = [0.62, 0.22, 0.16]

# Понедельник = 0. Выходные заметно выше, понедельник ниже.
WEEKDAY_FACTOR = [0.85, 0.92, 0.95, 1.00, 1.15, 1.35, 1.25]

# Сезонность: декабрь - пик, январь и февраль - провал после него.
MONTH_FACTOR = {
    1: 0.82, 2: 0.86, 3: 0.98, 4: 1.00, 5: 1.05, 6: 1.02,
    7: 0.97, 8: 1.03, 9: 1.08, 10: 1.04, 11: 1.10, 12: 1.32,
}

SKU_POOL_SIZE = 4000
PRICE_BANDS = [(199, 899), (899, 2499), (2499, 6990), (6990, 19990)]
PRICE_BAND_WEIGHTS = [0.46, 0.31, 0.18, 0.05]

CANCEL_REASONS = ["отказ покупателя", "нет в наличии", "ошибка оформления", "не оплачен"]

# Больше тысячи заказов на магазин за день не бывает, и на этом держится
# кодирование order_id ниже. Если модель когда-нибудь вырастет - сломается
# assert, а не данные.
MAX_ORDERS_PER_STORE_DAY = 1000


class Store(NamedTuple):
    store_id: int
    store_code: str
    city: str
    store_format: str
    opened_on: date


class Order(NamedTuple):
    order_id: int
    store_id: int
    order_ts: datetime
    order_date: date
    channel: str
    status: str
    customer_id: int | None


class OrderItem(NamedTuple):
    order_id: int
    order_date: date
    line_no: int
    sku: str
    quantity: int
    unit_price: float
    line_amount: float


class Return(NamedTuple):
    return_id: int
    order_id: int
    order_date: date
    line_no: int
    returned_date: date
    quantity: int
    returned_amount: float


class Cancellation(NamedTuple):
    cancellation_id: int
    order_id: int
    order_date: date
    cancelled_date: date
    reason: str


class Traffic(NamedTuple):
    store_id: int
    traffic_date: date
    visitors: int


class DayData(NamedTuple):
    orders: list[Order]
    items: list[OrderItem]
    cancellations: list[Cancellation]
    traffic: list[Traffic]
    # Возвраты, порожденные заказами этого дня, разложенные по дате приезда.
    returns_by_arrival: dict[date, list[Return]]


def make_order_id(day: date, store_id: int, index: int) -> int:
    """Идентификатор заказа кодирует дату и магазин.

    Это не украшение: по идентификатору однозначно восстанавливается, к какой
    партиции он относится, и возврат можно связать с заказом, не поднимая
    таблицу заказов.
    """
    assert index < MAX_ORDERS_PER_STORE_DAY
    return (day.toordinal() * 100_000 + store_id) * MAX_ORDERS_PER_STORE_DAY + index


def build_stores(config: Config) -> list[Store]:
    rng = stream(config.seed, "stores")
    stores: list[Store] = []
    for store_id in range(1, config.store_count + 1):
        city = rng.choice(CITIES)
        fmt = rng.choices(FORMATS, weights=FORMAT_WEIGHTS, k=1)[0]
        # Часть сети открылась уже внутри горизонта - у таких магазинов до
        # даты открытия не должно быть ни заказов, ни трафика. Это дешевый
        # способ получить в данных честные пустоты, не являющиеся дефектом.
        if rng.random() < 0.12:
            offset = rng.randrange(30, 400)
            opened = config.start_date + timedelta(days=offset)
        else:
            opened = config.start_date - timedelta(days=rng.randrange(200, 3000))
        stores.append(
            Store(
                store_id=store_id,
                store_code="S%04d" % store_id,
                city=city,
                store_format=fmt,
                opened_on=opened,
            )
        )
    return stores


def _order_count(config: Config, store: Store, day: date) -> int:
    rng = stream(config.seed, "orders", day.toordinal(), store.store_id)
    base = FORMAT_BASE_ORDERS[store.store_format]
    factor = WEEKDAY_FACTOR[day.weekday()] * MONTH_FACTOR[day.month]
    noise = rng.gauss(1.0, 0.18)
    count = round(base * factor * max(0.25, noise))
    return max(0, min(count, MAX_ORDERS_PER_STORE_DAY - 1))


def _return_delay(rng) -> int:
    """Задержка возврата в днях.

    Смесь, а не одно распределение: большая часть возвращается почти сразу,
    но хвост тянется. Именно хвост определяет размер окна пересчета на этапе 4,
    поэтому его измеряют по данным, а не берут отсюда.
    """
    bucket = rng.choices([0, 1, 2, 3], weights=[0.52, 0.29, 0.15, 0.04], k=1)[0]
    if bucket == 0:
        return rng.randrange(0, 3)
    if bucket == 1:
        return rng.randrange(3, 8)
    if bucket == 2:
        return rng.randrange(8, 21)
    return rng.randrange(21, MAX_RETURN_DELAY_DAYS + 1)


def _returns_for_order(config: Config, order: Order, items: list[OrderItem]) -> list[Return]:
    """Возвраты по одному заказу - чистая функция от заказа.

    Ровно это свойство делает партицию возвратов пересобираемой.
    """
    if order.status == "отменен" or not items:
        return []
    rng = stream(config.seed, "returns", order.order_id)
    if rng.random() >= RETURN_RATE:
        return []

    line_count = 1 if rng.random() < 0.82 else 2
    chosen = rng.sample(items, min(line_count, len(items)))
    delay = _return_delay(rng)
    arrival = order.order_date + timedelta(days=delay)

    out: list[Return] = []
    for n, item in enumerate(chosen):
        qty = 1 if item.quantity == 1 else rng.randrange(1, item.quantity + 1)
        amount = round(item.unit_price * qty, 2)
        out.append(
            Return(
                return_id=order.order_id * 10 + n,
                order_id=order.order_id,
                order_date=order.order_date,
                line_no=item.line_no,
                returned_date=arrival,
                quantity=qty,
                returned_amount=amount,
            )
        )
    return out


def generate_day(config: Config, day: date, stores: list[Store]) -> DayData:
    """Полное содержимое одного дня. Чистая функция от (config, day)."""
    missing = defects.missing_partitions(config)
    orders_missing = day in missing.get("orders", set())
    traffic_missing = day in missing.get("store_traffic", set())
    broken = defects.broken_counter_windows(config)

    orders: list[Order] = []
    items: list[OrderItem] = []
    cancellations: list[Cancellation] = []
    traffic: list[Traffic] = []
    returns_by_arrival: dict[date, list[Return]] = {}

    for store in stores:
        if store.opened_on > day:
            continue

        offline_orders = 0
        if not orders_missing:
            count = _order_count(config, store, day)
            for index in range(count):
                order_id = make_order_id(day, store.store_id, index)
                o_rng = stream(config.seed, "order", order_id)

                channel = o_rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS, k=1)[0]
                if channel == "офлайн":
                    offline_orders += 1

                hour = o_rng.randrange(9, 22)
                order_ts = datetime(
                    day.year, day.month, day.day,
                    hour, o_rng.randrange(0, 60), o_rng.randrange(0, 60),
                )
                cancelled = o_rng.random() < CANCELLATION_RATE
                status = "отменен" if cancelled else "оформлен"
                customer_id = o_rng.randrange(1, 400_000) if o_rng.random() < 0.7 else None

                order = Order(
                    order_id=order_id,
                    store_id=store.store_id,
                    order_ts=order_ts,
                    order_date=day,
                    channel=channel,
                    status=status,
                    customer_id=customer_id,
                )
                orders.append(order)

                line_count = o_rng.choices(
                    [1, 2, 3, 4, 5, 6], weights=[0.34, 0.27, 0.18, 0.11, 0.06, 0.04], k=1
                )[0]
                order_items: list[OrderItem] = []
                for line_no in range(1, line_count + 1):
                    band = o_rng.choices(PRICE_BANDS, weights=PRICE_BAND_WEIGHTS, k=1)[0]
                    unit_price = round(o_rng.uniform(*band), 2)
                    quantity = o_rng.choices([1, 2, 3], weights=[0.74, 0.19, 0.07], k=1)[0]
                    order_items.append(
                        OrderItem(
                            order_id=order_id,
                            order_date=day,
                            line_no=line_no,
                            sku="SKU-%05d" % o_rng.randrange(1, SKU_POOL_SIZE + 1),
                            quantity=quantity,
                            unit_price=unit_price,
                            line_amount=round(unit_price * quantity, 2),
                        )
                    )
                items.extend(order_items)

                if cancelled:
                    cancelled_date = day + timedelta(days=o_rng.randrange(0, 3))
                    cancellations.append(
                        Cancellation(
                            cancellation_id=order_id,
                            order_id=order_id,
                            order_date=day,
                            cancelled_date=cancelled_date,
                            reason=o_rng.choice(CANCEL_REASONS),
                        )
                    )

                for ret in _returns_for_order(config, order, order_items):
                    returns_by_arrival.setdefault(ret.returned_date, []).append(ret)

        if not traffic_missing:
            traffic.append(_traffic_for(config, store, day, offline_orders, broken))

    if day in defects.duplicate_dates(config):
        items = _apply_duplicates(config, day, items)
    if day in defects.outlier_dates(config):
        items = _apply_outliers(config, day, items)

    return DayData(orders, items, cancellations, traffic, returns_by_arrival)


def _traffic_for(
    config: Config,
    store: Store,
    day: date,
    offline_orders: int,
    broken: list[defects.BrokenCounterWindow],
) -> Traffic:
    """Трафик считается ОТ заказов, а не независимо от них.

    Иначе конверсия получилась бы случайной величиной, и правило "чеки больше
    трафика" срабатывало бы само по себе, без всякого битого прибора.
    """
    rng = stream(config.seed, "traffic", day.toordinal(), store.store_id)
    conversion = rng.uniform(0.055, 0.115)
    real_visitors = max(1, round(offline_orders / conversion)) if offline_orders else rng.randrange(5, 40)

    visitors = real_visitors
    for window in broken:
        if window.covers(store.store_id, day):
            visitors = int(real_visitors * window.factor)
            break

    return Traffic(store_id=store.store_id, traffic_date=day, visitors=max(0, visitors))


def _apply_duplicates(config: Config, day: date, items: list[OrderItem]) -> list[OrderItem]:
    """Задваивает часть строк выгрузки, как это делает сбойный экспорт."""
    if not items:
        return items
    rng = stream(config.seed, "duplicates", day.toordinal())
    share = defects.duplicate_share()
    extra = [rng.choice(items) for _ in range(max(1, int(len(items) * share)))]
    return items + extra


def _apply_outliers(config: Config, day: date, items: list[OrderItem]) -> list[OrderItem]:
    """Отрицательная сумма и нулевое количество - то, чего не бывает."""
    if not items:
        return items
    rng = stream(config.seed, "outliers", day.toordinal())
    out = list(items)
    for _ in range(rng.randrange(2, 6)):
        pos = rng.randrange(len(out))
        item = out[pos]
        if rng.random() < 0.5:
            out[pos] = item._replace(line_amount=-abs(item.line_amount))
        else:
            out[pos] = item._replace(quantity=0, line_amount=0.0)
    return out
