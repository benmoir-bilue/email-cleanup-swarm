"""One retrying batch-get, used by everything that reads many messages.

This exists because the same bug was written three separate times: a
`BatchHttpRequest` returns HTTP 200 while individual sub-requests inside it fail, so
the outer `_with_backoff` never sees them, and a callback that only records successes
drops them silently. That cost 95 messages in the indexer, 10,297 in the trash path,
and 846 in `inbox-zero` — each time appearing as a clean success with a wrong total.

So: one helper that retries shed sub-requests, and nothing else re-implements it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from . import quota
from .index import _with_backoff

Progress = Callable[[str], None]

# Reads tolerate wide batches — 23,000 messages indexed 100-wide with no concurrency
# errors. Writes do not; see MUTATE_BATCH_CAP in mutate.py.
READ_BATCH_CAP = 100


def batch_get(
    svc,
    message_ids: list[str],
    *,
    fmt: str = "minimal",
    headers: list[str] | None = None,
    progress: Progress | None = None,
    attempts: int = 4,
    label: str = "fetched",
) -> tuple[dict[str, dict], list[str]]:
    """Fetch many messages by id.

    Returns ({id: message}, [ids that failed every attempt]). The caller gets an
    explicit failure list rather than a quietly short result, so a partial fetch can
    never be mistaken for a complete one.
    """
    emit = progress or (lambda _: None)
    results: dict[str, dict] = {}
    pending = list(message_ids)

    for attempt in range(1, attempts + 1):
        if not pending:
            break
        failed: list[str] = []

        for start in range(0, len(pending), READ_BATCH_CAP):
            chunk = pending[start : start + READ_BATCH_CAP]
            collected: list[tuple[str, dict]] = []

            def callback(request_id: str, response, exception) -> None:
                if exception is None and response:
                    collected.append((request_id, response))
                else:
                    failed.append(request_id)

            batch = svc.new_batch_http_request(callback=callback)
            for mid in chunk:
                kwargs = {"userId": "me", "id": mid, "format": fmt}
                if headers:
                    kwargs["metadataHeaders"] = headers
                batch.add(svc.users().messages().get(**kwargs), request_id=mid)

            quota.spend(len(chunk) * quota.COST_MESSAGES_GET)
            _with_backoff(batch.execute)

            results.update(dict(collected))
            emit(f"{label} {len(results):,}/{len(message_ids):,}")

        if failed and attempt < attempts:
            delay = min(2.0 * (2 ** (attempt - 1)), 20.0)
            emit(f"{len(failed):,} shed by the batch endpoint — retrying in {delay:.0f}s")
            time.sleep(delay + random.uniform(0, 1.0))
        pending = failed

    if pending:
        emit(f"[warn] {len(pending):,} could not be fetched after {attempts} attempts")

    return results, pending


def list_ids(
    svc,
    *,
    label_ids: list[str] | None = None,
    query: str | None = None,
    progress: Progress | None = None,
) -> list[str]:
    """Page through message ids for a label set or query."""
    emit = progress or (lambda _: None)
    ids: list[str] = []
    token = None
    while True:
        kwargs: dict = {"userId": "me", "maxResults": 500, "pageToken": token}
        if label_ids:
            kwargs["labelIds"] = label_ids
        if query:
            kwargs["q"] = query
        response = _with_backoff(
            lambda: svc.users().messages().list(**kwargs).execute(),
            units=quota.COST_MESSAGES_LIST,
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        token = response.get("nextPageToken")
        emit(f"listed {len(ids):,} ids")
        if not token:
            return ids
