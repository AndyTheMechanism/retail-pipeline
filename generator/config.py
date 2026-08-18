"""Generation parameters.

The horizon is a pair of **fixed dates rather than an offset from today.**
That matters: `end_date = date.today()` would mean yesterday's run and today's
produce different data, and the criterion "a repeat run gives the same thing"
would stop meaning anything while keeping the appearance of a check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Maximum delay of a return. The bound is not decoration: to assemble the
# returns partition for date R you have to regenerate the orders for
# R-MAX_RETURN_DELAY_DAYS..R. Without an upper bound that means the whole
# history, every time. The same number bounds the depth of the reprocessing
# window — but the window itself, 28 days, was chosen from the measured
# distribution of the delay, not from this ceiling.
MAX_RETURN_DELAY_DAYS = 30

# Share of orders that get at least one return.
RETURN_RATE = 0.085

# Share of cancelled orders.
CANCELLATION_RATE = 0.03

# A cancellation also arrives on its own date, only the window is short. Its
# partition for a date is assembled by looking back the same way the returns
# partition is — only the depth differs.
CANCELLATION_MAX_DELAY_DAYS = 2


@dataclass(frozen=True)
class Config:
    seed: int = 20260812
    start_date: date = date(2025, 1, 1)
    end_date: date = date(2026, 6, 30)
    store_count: int = 120

    def dates(self) -> list[date]:
        span = (self.end_date - self.start_date).days
        return [date.fromordinal(self.start_date.toordinal() + i) for i in range(span + 1)]
