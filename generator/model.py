"""How one day of the chain is put together.

The main property of this whole file: **generating a day is a pure function of
(seed, date).** No day depends on whether its neighbour has been generated.
And that is the point of it: the partition for any date can be rebuilt on its
own and comes out with exactly the rows a full run would have produced.

There is one honest and substantial exception to that independence: returns. A
return that arrives on day R belongs to an order made earlier. So a return is
computed **from the order itself** — as a function of its identifier — and
comes out the same either way: in a full run, or when the partition for R is
rebuilt looking MAX_RETURN_DELAY_DAYS days back.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple

from . import defects
from .config import CANCELLATION_RATE, MAX_RETURN_DELAY_DAYS, RETURN_RATE, Config
from .rng import stream

CITIES = [
    "Moscow", "Saint Petersburg", "Novosibirsk", "Yekaterinburg", "Kazan",
    "Nizhny Novgorod", "Chelyabinsk", "Samara", "Ufa", "Rostov-on-Don",
    "Omsk", "Krasnoyarsk", "Voronezh", "Perm", "Volgograd", "Krasnodar",
    "Saratov", "Tyumen", "Tolyatti", "Izhevsk", "Barnaul", "Ulyanovsk",
    "Irkutsk", "Khabarovsk", "Yaroslavl", "Vladivostok", "Makhachkala",
    "Tomsk", "Orenburg", "Kemerovo",
]

FORMATS = ["small", "medium", "large"]
FORMAT_WEIGHTS = [0.45, 0.38, 0.17]
FORMAT_BASE_ORDERS = {"small": 12, "medium": 25, "large": 45}

CHANNELS = ["offline", "web", "app"]
CHANNEL_WEIGHTS = [0.62, 0.22, 0.16]

# Monday = 0. Weekends are noticeably higher, Monday lower.
WEEKDAY_FACTOR = [0.85, 0.92, 0.95, 1.00, 1.15, 1.35, 1.25]

# Seasonality: December is the peak, January and February the slump after it.
MONTH_FACTOR = {
    1: 0.82, 2: 0.86, 3: 0.98, 4: 1.00, 5: 1.05, 6: 1.02,
    7: 0.97, 8: 1.03, 9: 1.08, 10: 1.04, 11: 1.10, 12: 1.32,
}

SKU_POOL_SIZE = 4000
PRICE_BANDS = [(199, 899), (899, 2499), (2499, 6990), (6990, 19990)]
PRICE_BAND_WEIGHTS = [0.46, 0.31, 0.18, 0.05]

CANCEL_REASONS = ["customer declined", "out of stock", "checkout error", "not paid"]

# A store never does more than a thousand orders in a day, and the encoding of
# order_id below rests on that. If the model ever outgrows it, the assert
# breaks rather than the data.
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
    # Returns produced by this day's orders, bucketed by arrival date.
    returns_by_arrival: dict[date, list[Return]]


def make_order_id(day: date, store_id: int, index: int) -> int:
    """The order identifier encodes the date and the store.

    That is not decoration: the identifier tells you exactly which partition
    an order belongs to, so a return can be tied to its order without touching
    the orders table.
    """
    assert index < MAX_ORDERS_PER_STORE_DAY
    return (day.toordinal() * 100_000 + store_id) * MAX_ORDERS_PER_STORE_DAY + index


def build_stores(config: Config) -> list[Store]:
    rng = stream(config.seed, "stores")
    stores: list[Store] = []
    for store_id in range(1, config.store_count + 1):
        city = rng.choice(CITIES)
        fmt = rng.choices(FORMATS, weights=FORMAT_WEIGHTS, k=1)[0]
        # Part of the chain opened inside the horizon — such stores must have
        # neither orders nor traffic before their opening date. That is a
        # cheap way to put honest emptiness into the data — emptiness that is
        # not a defect.
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
    """Delay of a return, in days.

    A mixture rather than one distribution: most returns come back almost at
    once, but the tail runs long. It is the tail that sets the size of the
    reprocessing window, which is why it is measured from the data
    (make measure-returns) rather than taken from here.
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
    """Returns for one order — a pure function of the order.

    That property is exactly what makes the returns partition rebuildable.
    """
    if order.status == "cancelled" or not items:
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
    """The full contents of one day. A pure function of (config, day)."""
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
                if channel == "offline":
                    offline_orders += 1

                hour = o_rng.randrange(9, 22)
                order_ts = datetime(
                    day.year, day.month, day.day,
                    hour, o_rng.randrange(0, 60), o_rng.randrange(0, 60),
                )
                cancelled = o_rng.random() < CANCELLATION_RATE
                status = "cancelled" if cancelled else "placed"
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
    """Traffic is computed FROM the orders, not independently of them.

    Otherwise conversion would be a random variable, and the rule "more orders
    than visitors" would fire on its own, with no broken counter involved.
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
    """Doubles some of the export rows, the way a faulty export does."""
    if not items:
        return items
    rng = stream(config.seed, "duplicates", day.toordinal())
    share = defects.duplicate_share()
    extra = [rng.choice(items) for _ in range(max(1, int(len(items) * share)))]
    return items + extra


def _apply_outliers(config: Config, day: date, items: list[OrderItem]) -> list[OrderItem]:
    """A negative amount and a zero quantity — things that do not happen."""
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
