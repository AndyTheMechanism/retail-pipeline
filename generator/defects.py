"""Five defects, planted on purpose.

They are planted **here, in the raw data, ahead of any pipeline.** The
difference matters: a defect fitted to a test only proves that the test can
catch itself. A defect sitting in the source is caught, or not caught, honestly.

Where exactly they sit is derived from the seed, so it is reproducible and
known in advance. The list is printed by `make defects`: the tests in dbt/tests
are written against it, and at an interview it is what you point to when
showing what broke and why it was caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .rng import stream


@dataclass(frozen=True)
class BrokenCounterWindow:
    """A window in which the visitor counter lies.

    `factor` is the share of the real traffic it reports. It is small on
    purpose. At an ordinary conversion of around 8% a mild 20-30% undercount
    breaks nothing and looks no different from bad weather; for conversion to
    physically pass 100% the counter has to report units where there are
    hundreds. That is exactly how it looked in real life, and where the figure
    of 109% came from.
    """

    store_id: int
    start: date
    end: date
    factor: float           # 0.0 = the counter is dead

    def covers(self, store_id: int, day: date) -> bool:
        return store_id == self.store_id and self.start <= day <= self.end


def _safe_dates(config: Config) -> list[date]:
    """Dates that are safe to corrupt.

    The first MAX_RETURN_DELAY_DAYS days are excluded: even without defects
    they hold few returns, because the data set has no past. Planting a
    defect on an edge that is already thin means not being able to tell
    afterwards what actually broke.
    """
    from .config import MAX_RETURN_DELAY_DAYS

    all_dates = config.dates()
    return all_dates[MAX_RETURN_DELAY_DAYS + 1 : -1]


def missing_partitions(config: Config) -> dict[str, set[date]]:
    """Defect 2: the source for a day did not arrive."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "missing")
    picked = rng.sample(pool, 3)
    return {
        # Orders did not arrive — the whole day is empty.
        "orders": {picked[0]},
        # Traffic did not arrive but orders did. The more insidious case:
        # conversion for that day is not computed, rather than computed wrong.
        "store_traffic": {picked[1], picked[2]},
    }


def duplicate_dates(config: Config) -> set[date]:
    """Defect 3: the export doubled some of its rows."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "duplicates")
    return set(rng.sample(pool, 5))


def duplicate_share() -> float:
    """What share of the rows gets doubled on a corrupted day."""
    return 0.012


def broken_counter_windows(config: Config) -> list[BrokenCounterWindow]:
    """Defect 4: a broken traffic counter, conversion above 100%."""
    rng = stream(config.seed, "defect", "counter")
    pool = _safe_dates(config)
    windows: list[BrokenCounterWindow] = []

    # Three windows undercounting down to single-digit percent, and one with
    # a dead counter.
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
    """Defect 5: outliers — a negative amount, a zero quantity."""
    pool = _safe_dates(config)
    rng = stream(config.seed, "defect", "outliers")
    return set(rng.sample(pool, 8))


def report(config: Config) -> str:
    """A human-readable map of the defects."""
    lines: list[str] = []
    lines.append("Planted defects (reproducible from seed %d)" % config.seed)
    lines.append("")

    lines.append("1. Late return — not a point defect but a property of the domain.")
    lines.append("   Returns arrive on their own date, and the tail of the")
    lines.append("   distribution runs out to %d days. Measured: make measure-returns" % _max_delay())
    lines.append("")

    missing = missing_partitions(config)
    lines.append("2. Missing partition:")
    for table, days in sorted(missing.items()):
        for day in sorted(days):
            lines.append("   %-14s %s" % (table, day))
    lines.append("")

    lines.append("3. Doubled rows in the export, %.1f%% of the order lines:" % (duplicate_share() * 100))
    for day in sorted(duplicate_dates(config)):
        lines.append("   %s" % day)
    lines.append("")

    lines.append("4. Broken traffic counter:")
    for w in broken_counter_windows(config):
        state = "counter dead" if w.factor == 0.0 else "counts %.0f%% of the real traffic" % (w.factor * 100)
        lines.append("   store %-4d %s .. %s  %s" % (w.store_id, w.start, w.end, state))
    lines.append("")

    lines.append("5. Outliers — a negative amount and a zero quantity:")
    for day in sorted(outlier_dates(config)):
        lines.append("   %s" % day)

    return "\n".join(lines)


def _max_delay() -> int:
    from .config import MAX_RETURN_DELAY_DAYS

    return MAX_RETURN_DELAY_DAYS
