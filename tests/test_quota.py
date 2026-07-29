"""Quota pacing.

Gmail's ceiling is 250 units/sec. A metadata batch of 100 messages costs 500 units, so
the limiter must cope with single requests larger than its own bucket — the first
implementation looped forever on exactly that case, which would have hung the indexer
on its first batch.
"""

from __future__ import annotations

import time

import pytest

from ecs.gmail.quota import DEFAULT_UNITS_PER_SECOND, QuotaLimiter


def test_single_request_larger_than_capacity_terminates():
    """The regression that hung the indexer: units (500) > capacity (250)."""
    lim = QuotaLimiter(units_per_second=10_000.0, capacity=250.0)
    start = time.monotonic()
    lim.acquire(500)
    # Must return, and quickly at this rate.
    assert time.monotonic() - start < 1.0


def test_zero_and_negative_units_are_free():
    lim = QuotaLimiter()
    assert lim.acquire(0) == 0.0
    assert lim.acquire(-5) == 0.0


def test_initial_burst_is_not_throttled():
    """A cold bucket should let the first request through immediately."""
    lim = QuotaLimiter(units_per_second=180.0, capacity=250.0)
    assert lim.acquire(200) == pytest.approx(0.0, abs=0.01)


def test_sustained_rate_stays_under_the_ceiling():
    """The property that matters: averaged spend must not exceed the configured rate."""
    rate = 5000.0  # fast so the test is quick; the maths is rate-independent
    lim = QuotaLimiter(units_per_second=rate, capacity=250.0)
    total = 0
    start = time.monotonic()
    for _ in range(8):
        lim.acquire(500)
        total += 500
    elapsed = time.monotonic() - start

    observed = total / elapsed
    # Allow generous slack for the initial free burst and scheduler jitter.
    assert observed < rate * 2.5


def test_waiting_is_reported():
    lim = QuotaLimiter(units_per_second=1000.0, capacity=100.0)
    lim.acquire(100)  # drains the bucket
    waited = lim.acquire(500)  # must wait ~0.5s at 1000/sec
    assert waited > 0
    assert lim.total_waited >= waited


def test_totals_accumulate():
    lim = QuotaLimiter(units_per_second=10_000.0, capacity=10_000.0)
    for _ in range(3):
        lim.acquire(100)
    assert lim.total_units == 300


def test_penalise_drains_the_bucket():
    """After a 429 we must not resume at full rate."""
    lim = QuotaLimiter(units_per_second=10_000.0, capacity=1000.0)
    lim.penalise(seconds=0.05)
    waited = lim.acquire(500)
    assert waited > 0


def test_default_rate_is_below_gmails_ceiling():
    assert DEFAULT_UNITS_PER_SECOND < 250.0


def test_realistic_index_pacing_is_bounded():
    """A 100-batch at the real default rate should pace to roughly 2.8s per batch."""
    lim = QuotaLimiter()  # 180 units/sec
    lim.acquire(250)  # consume the initial burst
    waited = lim.acquire(500)
    expected = 500 / DEFAULT_UNITS_PER_SECOND
    assert waited == pytest.approx(expected, rel=0.15)


class TestWriteConcurrency:
    """Gmail fans a batch out into one concurrent op per sub-request.

    A 100-wide trash batch asks for 100 concurrent writes and gets
    "Too many concurrent requests for user" on most of them — 10,297 real failures
    out of 13,000 before this was fixed. These pin the two properties that matter.
    """

    def test_mutating_batches_are_far_narrower_than_read_batches(self):
        from ecs.gmail.mutate import HTTP_BATCH_CAP, MUTATE_BATCH_CAP

        assert MUTATE_BATCH_CAP <= 10
        assert MUTATE_BATCH_CAP < HTTP_BATCH_CAP

    def test_concurrency_errors_are_classified_retryable(self):
        from ecs.gmail.mutate import _is_retryable_suberror

        assert _is_retryable_suberror(
            'returned "Too many concurrent requests for user." reason: rateLimitExceeded'
        )
        assert _is_retryable_suberror('"The service is currently unavailable."')
        assert _is_retryable_suberror("reason: userRateLimitExceeded")

    def test_permanent_errors_are_not_retried(self):
        from ecs.gmail.mutate import _is_retryable_suberror

        assert not _is_retryable_suberror('returned "Not Found" reason: notFound')
        assert not _is_retryable_suberror('"Insufficient Permission" reason: forbidden')
