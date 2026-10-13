"""The write-rate guard.

Daikin's specification caps setting changes at **7,000 per year per point**.
That is nineteen a day. An automation with a bad trigger will spend a year's
budget in an afternoon, and the failure is a worn-out gateway rather than an
error message — so this is a requirement, not a nicety.

Four things, all of them cheap:

- a floor on how often one point may be written;
- coalescing, so a burst sends only its last value;
- a rolling 24-hour count per point, to warn before the budget is gone;
- a hard daily cap, on by default.

Nothing here talks to a gateway. It decides whether a write is allowed and
records that it happened, which makes the policy testable without a building
attached.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Final

_LOGGER = logging.getLogger(__name__)

#: Daikin's documented annual limit per point.
CHANGES_PER_YEAR: Final = 7000

#: What that works out to in a day, rounded down.
CHANGES_PER_DAY: Final = CHANGES_PER_YEAR // 365  # 19

#: Warn once the day's spend passes this share of the cap.
WARN_AT: Final = 0.5

DAY_SECONDS: Final = 86400


class WriteRefused(Exception):
    """A write was not sent, and why."""


class WriteRateExceeded(WriteRefused):
    """The point has had its allowance for the day."""


@dataclass(slots=True)
class PointBudget:
    """One point's recent write history."""

    sent: deque[float] = field(default_factory=deque)
    last_at: float | None = None
    warned: bool = False

    def prune(self, now: float) -> None:
        while self.sent and now - self.sent[0] > DAY_SECONDS:
            self.sent.popleft()

    def count(self, now: float) -> int:
        self.prune(now)
        return len(self.sent)


@dataclass(slots=True)
class WriteGuard:
    """Decides whether a write may be sent, and remembers that it was.

    `now` is passed in rather than read from the clock so that a test can
    cover a day's worth of behaviour without waiting one.
    """

    min_interval: float = 60.0
    daily_cap: int = CHANGES_PER_DAY
    enforce_cap: bool = True
    budgets: dict[str, PointBudget] = field(default_factory=dict)

    def budget(self, key: str) -> PointBudget:
        return self.budgets.setdefault(key, PointBudget())

    def check(self, key: str, now: float) -> None:
        """Raise if this write must not be sent."""
        budget = self.budget(key)
        spent = budget.count(now)

        if self.enforce_cap and spent >= self.daily_cap:
            oldest = budget.sent[0] if budget.sent else now
            retry_in = int(DAY_SECONDS - (now - oldest))
            raise WriteRateExceeded(
                f"{key} has been written {spent} times in the last 24 hours, "
                f"which is this point's daily allowance. Daikin caps changes "
                f"at {CHANGES_PER_YEAR} a year per point. The next write will "
                f"be allowed in about {max(retry_in, 0) // 60} minutes."
            )

        if budget.last_at is not None and now - budget.last_at < self.min_interval:
            wait = self.min_interval - (now - budget.last_at)
            raise WriteRefused(
                f"{key} was written {int(now - budget.last_at)}s ago; writes "
                f"to one point are spaced at least {int(self.min_interval)}s "
                f"apart. Try again in {int(wait) + 1}s."
            )

    def allows_within_cap(self, key: str, now: float) -> bool:
        """Whether the daily allowance still has room, ignoring spacing."""
        if not self.enforce_cap:
            return True
        return self.budget(key).count(now) < self.daily_cap

    def allows(self, key: str, now: float) -> bool:
        """Whether a write would be accepted, without raising."""
        try:
            self.check(key, now)
        except WriteRefused:
            return False
        return True

    def record(self, key: str, now: float) -> None:
        """Note that a write was sent."""
        budget = self.budget(key)
        budget.sent.append(now)
        budget.last_at = now
        spent = budget.count(now)

        if not budget.warned and spent >= self.daily_cap * WARN_AT:
            budget.warned = True
            _LOGGER.warning(
                "%s has been written %d times in the last 24 hours, over half "
                "its daily allowance. Daikin caps setting changes at %d a year "
                "per point; something is writing more often than a person "
                "would.",
                key,
                spent,
                CHANGES_PER_YEAR,
            )
        elif budget.warned and spent < self.daily_cap * WARN_AT:
            budget.warned = False

    def spent_today(self, key: str, now: float) -> int:
        return self.budget(key).count(now)

    def summary(self, now: float) -> dict[str, int]:
        """What has been written in the last day, busiest first."""
        counts = {
            key: budget.count(now)
            for key, budget in self.budgets.items()
            if budget.count(now)
        }
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    @property
    def over_half(self) -> list[str]:
        return [key for key, b in self.budgets.items() if b.warned]
