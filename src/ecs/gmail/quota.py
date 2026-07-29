"""Proactive Gmail quota pacing.

Gmail bills API calls in *quota units*, capped at 250 units per user per second
(15,000/minute). Reactive backoff alone isn't enough at this scale: firing batches as
fast as they complete means every request storms into the limit, eats a 403, and waits
out an exponential backoff. Pacing to just under the limit is both faster overall and
far less likely to leave a long index truncated.

The cost that matters most here: a `BatchHttpRequest` carrying 100 `messages.get`
sub-requests is one HTTP call but bills 100 x 5 = 500 units. So the limiter has to be
told the batch size, not the request count.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Per-call unit costs, from Gmail's published quota table.
COST_MESSAGES_LIST = 5
COST_MESSAGES_GET = 5
COST_MESSAGES_MODIFY = 5
COST_MESSAGES_BATCH_MODIFY = 50
COST_MESSAGES_TRASH = 5
COST_MESSAGES_UNTRASH = 5
COST_MESSAGES_SEND = 100
COST_LABELS_LIST = 1
COST_LABELS_CREATE = 5
COST_LABELS_DELETE = 5
COST_FILTERS_LIST = 1
COST_FILTERS_CREATE = 5
COST_GET_PROFILE = 1

# Gmail's ceiling is 250 units/sec. Sit meaningfully below it: the accounting is
# approximate on Google's side, and a sustained run at 100% of a quota tends to
# trip it anyway.
DEFAULT_UNITS_PER_SECOND = 180.0


@dataclass
class QuotaLimiter:
    """Token bucket over Gmail quota units.

    Single-threaded by design — the whole indexer is sequential, and batch HTTP means
    one in-flight request at a time, so there's no need for locking.
    """

    units_per_second: float = DEFAULT_UNITS_PER_SECOND
    capacity: float = 250.0
    _tokens: float = field(default=250.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    total_units: float = field(default=0.0, init=False)
    total_waited: float = field(default=0.0, init=False)

    def acquire(self, units: float) -> float:
        """Block until `units` have been paid for. Returns seconds spent waiting.

        Debt model rather than a wait-and-retry loop, because a single request can
        legitimately cost more than the bucket can ever hold: a 100-message metadata
        batch is 500 units against a 250-unit capacity. A loop that waits for
        `tokens >= units` never terminates in that case — it refills to the cap,
        finds it still insufficient, and sleeps again forever.

        Instead: compute the shortfall once, sleep exactly long enough to earn it at
        the sustained rate, and consume everything. Averaged over a run this holds the
        real rate at `units_per_second` regardless of individual request size.
        """
        if units <= 0:
            return 0.0

        now = time.monotonic()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._last) * self.units_per_second
        )
        self._last = now

        waited = 0.0
        if units > self._tokens:
            deficit = units - self._tokens
            waited = deficit / self.units_per_second
            time.sleep(waited)
            # We slept precisely long enough to earn `deficit`, so the balance after
            # spending `units` is zero.
            self._tokens = 0.0
            self._last = time.monotonic()
        else:
            self._tokens -= units

        self.total_units += units
        self.total_waited += waited
        return waited

    def penalise(self, seconds: float = 5.0) -> None:
        """Drain the bucket after a 429/403 so we back off rather than resume at rate."""
        self._tokens = 0.0
        self._last = time.monotonic() + seconds

    @property
    def effective_rate(self) -> float:
        """Units per second actually achieved, for reporting."""
        elapsed = time.monotonic() - (self._last - self.total_waited)
        return self.total_units / elapsed if elapsed > 0 else 0.0


# One shared limiter per process: the quota is per user, not per call site.
_limiter: QuotaLimiter | None = None


def limiter() -> QuotaLimiter:
    global _limiter
    if _limiter is None:
        _limiter = QuotaLimiter()
    return _limiter


def spend(units: float) -> float:
    """Convenience wrapper over the shared limiter."""
    return limiter().acquire(units)
