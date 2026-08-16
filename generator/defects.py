"""Пять дефектов, заложенных намеренно.

Они закладываются **здесь, в сырье, до всякого пайплайна.** Разница
существенная: дефект, подогнанный под тест, доказывает только то, что тест
умеет ловить сам себя. Дефект, лежащий в источнике, ловится или не ловится
честно.

Где именно они лежат - вычисляется от сида, то есть воспроизводимо и заранее
известно. Список печатается командой `make defects`: тесты в dbt/tests написаны
против него, а на собеседовании по нему показывают, что сломалось и почему это
поймалось.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .rng import stream


@dataclass(frozen=True)
class BrokenCounterWindow:
    """Окно, в котором счетчик посетителей врет.

    `factor` - доля от настоящего трафика. Он маленький намеренно. При обычной
    конверсии около 8% мягкое занижение на 20-30% ничего не ломает и ничем не
    отличается от плохой погоды; чтобы конверсия физически перевалила за 100%,
    прибор должен считать единицы вместо сотен. Ровно так это и выглядело в
    жизни, откуда и взялась цифра 109%.
    """

    store_id: int
    start: date
    end: date
    factor: float           # 0.0 = прибор мертв

    def covers(self, store_id: int, day: date) -> bool:
        return store_id == self.store_id and self.start <= day <= self.end


def _safe_dates(config: Config) -> list[date]:
    """Даты, пригодные для порчи.

    Первые MAX_RETURN_DELAY дней исключены: там и без дефектов мало возвратов,
    потому что у набора данных просто нет предыстории. Накладывать дефект на
    и без того куцый край - значит потом не понять, что именно сломалось.
    """
    from .config import MAX_RETURN_DELAY_DAYS

    all_dates = config.dates()
    return all_dates[MAX_RETURN_DELAY_DAYS + 1 : -1]


def missing_partitions(config: Config) -> dict[str, set[date]]:
    """Дефект 2: источник за день не приехал."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "missing")
    picked = rng.sample(pool, 3)
    return {
        # Заказы не приехали - день пустой целиком.
        "orders": {picked[0]},
        # Трафик не приехал, а заказы приехали. Более коварный случай:
        # конверсия за этот день не считается, а не считается неправильно.
        "store_traffic": {picked[1], picked[2]},
    }


def duplicate_dates(config: Config) -> set[date]:
    """Дефект 3: выгрузка задвоила часть строк."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "duplicates")
    return set(rng.sample(pool, 5))


def duplicate_share() -> float:
    """Какая доля строк задваивается в испорченный день."""
    return 0.012


def broken_counter_windows(config: Config) -> list[BrokenCounterWindow]:
    """Дефект 4: битый счетчик трафика, конверсия выше 100%."""
    rng = stream(config.seed, "defect", "counter")
    pool = _safe_dates(config)
    windows: list[BrokenCounterWindow] = []

    # Три окна с занижением до единиц процентов и одно с мертвым прибором.
    for i, factor in enumerate([0.05, 0.03, 0.06, 0.0]):
        store_id = rng.randrange(1, config.store_count + 1)
        start = rng.choice(pool[: len(pool) - 90])
        length = rng.randrange(25, 75)
        windows.append(
            BrokenCounterWindow(
                store_id=store_id,
                start=start,
                end=start + timedelta(days=length),
                factor=factor,
            )
        )
    return windows


def outlier_dates(config: Config) -> set[date]:
    """Дефект 5: выбросы - отрицательная сумма, нулевое количество."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "outliers")
    return set(rng.sample(pool, 8))


def report(config: Config) -> str:
    """Человекочитаемая карта дефектов."""
    lines: list[str] = []
    lines.append("Заложенные дефекты (воспроизводимы по сиду %d)" % config.seed)
    lines.append("")

    lines.append("1. Поздний возврат - не точечный дефект, а свойство домена.")
    lines.append("   Возвраты приезжают своей датой, хвост распределения уходит")
    lines.append("   до %d дней. Замер по факту: make measure-returns" % _max_delay())
    lines.append("")

    missing = missing_partitions(config)
    lines.append("2. Пропущенная партиция:")
    for table, days in sorted(missing.items()):
        for day in sorted(days):
            lines.append("   %-14s %s" % (table, day))
    lines.append("")

    lines.append("3. Дубли строк в выгрузке, доля %.1f%% от позиций:" % (duplicate_share() * 100))
    for day in sorted(duplicate_dates(config)):
        lines.append("   %s" % day)
    lines.append("")

    lines.append("4. Битый счетчик трафика:")
    for w in broken_counter_windows(config):
        state = "прибор мертв" if w.factor == 0.0 else "считает %.0f%% реального" % (w.factor * 100)
        lines.append("   магазин %-4d %s .. %s  %s" % (w.store_id, w.start, w.end, state))
    lines.append("")

    lines.append("5. Выбросы - отрицательная сумма и нулевое количество:")
    for day in sorted(outlier_dates(config)):
        lines.append("   %s" % day)

    return "\n".join(lines)


def _max_delay() -> int:
    from .config import MAX_RETURN_DELAY_DAYS

    return MAX_RETURN_DELAY_DAYS
