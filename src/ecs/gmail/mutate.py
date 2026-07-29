"""The only module that writes to the mailbox.

Concentrating every mutation here means there is exactly one place to audit, and the
journal wrapper in `apply.py` can be certain it isn't missing a write path.

Two mechanisms, for a reason:

  * **Labels and archiving** go through `batchModify`, which takes up to 1000 ids in a
    single call — the efficient path for the bulk of the work.
  * **Trashing** goes through `messages.trash()`, batched as HTTP sub-requests.
    `batchModify` is documented as not supporting the TRASH label, and relying on
    undocumented behaviour for the one irreversible-ish operation would be a poor
    trade. `trash()` also puts the message in Trash properly, so Google's 30-day
    purge timer starts — which is what makes the undo window real.
"""

from __future__ import annotations

from collections.abc import Callable

from googleapiclient.errors import HttpError

from . import quota
from .index import _with_backoff

Progress = Callable[[str], None]

# batchModify accepts 1000 ids in a single operation — one concurrent request, so it
# scales fine.
MODIFY_ID_CAP = 1000

# Trash/untrash are one sub-request each, and Gmail fans a BatchHttpRequest out
# server-side into one *concurrent* operation per sub-request. Its per-user concurrency
# ceiling for writes is far below 100, so a 100-wide batch earns
# "Too many concurrent requests for user" on most of its members — 10,297 failures out
# of 13,000 on the first real run. Quota pacing does not help: units-per-second and
# concurrency are separate limits.
MUTATE_BATCH_CAP = 10

# Reads tolerate wide batches happily; 23,000 messages indexed at 100-wide with no
# concurrency errors at all.
HTTP_BATCH_CAP = 100

# Per-sub-request failures inside a batch surface as individual errors while the batch
# HTTP call itself returns 200, so the outer _with_backoff never sees them. They have
# to be retried here or they look like permanent failures.
_SUBREQUEST_RETRY_REASONS = (
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "Too many concurrent requests",
    "backendError",
    "The service is currently unavailable",
    "internalError",
)


def _is_retryable_suberror(message: str) -> bool:
    return any(reason in message for reason in _SUBREQUEST_RETRY_REASONS)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def list_labels(svc) -> dict[str, str]:
    """Return {label name: label id}."""
    response = _with_backoff(lambda: svc.users().labels().list(userId="me").execute())
    return {label["name"]: label["id"] for label in response.get("labels", [])}


def _expand_parents(names: list[str]) -> list[str]:
    """Include intermediate labels for nested names.

    Gmail will display "Finance/Receipts" nested even without a "Finance" label, but
    creating the parents keeps the label list coherent if the user later reorganises
    by hand.
    """
    expanded: set[str] = set()
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            expanded.add("/".join(parts[:depth]))
    # Shallowest first, so parents exist before children.
    return sorted(expanded, key=lambda n: (n.count("/"), n))


def ensure_labels(
    svc, names: list[str], *, progress: Progress | None = None
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Create any missing labels.

    Returns ({name: id}, [(created name, created id)]) — the second element feeds the
    journal so `undo` can delete exactly the labels this run introduced and no others.
    """
    emit = progress or (lambda _: None)
    existing = list_labels(svc)
    created: list[tuple[str, str]] = []

    for name in _expand_parents([n for n in names if n]):
        if name in existing:
            continue
        try:
            label = _with_backoff(
                lambda: svc.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
        except HttpError as exc:
            # 409 means it was created concurrently (or differs only by case).
            if getattr(exc.resp, "status", None) == 409:
                existing = list_labels(svc)
                continue
            raise
        existing[name] = label["id"]
        created.append((name, label["id"]))
        emit(f"created label {name}")

    return existing, created


def delete_label(svc, label_id: str) -> None:
    try:
        _with_backoff(
            lambda: svc.users().labels().delete(userId="me", id=label_id).execute()
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            return  # already gone; undo is idempotent
        raise


# ---------------------------------------------------------------------------
# Label / archive mutation
# ---------------------------------------------------------------------------


def batch_modify(
    svc,
    message_ids: list[str],
    *,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> int:
    """Apply the same label change to many messages. Returns the count touched."""
    if not message_ids:
        return 0
    body = {
        "ids": [],
        "addLabelIds": add_label_ids or [],
        "removeLabelIds": remove_label_ids or [],
    }
    if not body["addLabelIds"] and not body["removeLabelIds"]:
        return 0

    touched = 0
    for start in range(0, len(message_ids), MODIFY_ID_CAP):
        chunk = message_ids[start : start + MODIFY_ID_CAP]
        payload = {**body, "ids": chunk}
        _with_backoff(
            lambda: svc.users().messages().batchModify(userId="me", body=payload).execute(),
            units=quota.COST_MESSAGES_BATCH_MODIFY,
        )
        touched += len(chunk)
    return touched


def list_unread(
    svc, *, inbox_only: bool = True, labelled_only: bool = False,
    progress: Progress | None = None,
) -> list[dict]:
    """Live query for unread messages, with their current label sets.

    Queried from Gmail rather than the local index: the index is a snapshot from before
    the apply ran, so its UNREAD flags are stale. Label sets come back too, because the
    journal needs the prior state to make the change reversible.

    `labelled_only` restricts the result to messages carrying a user label — useful once
    the inbox is empty and what remains unread is filed mail.
    """
    from .batch import batch_get, list_ids

    emit = progress or (lambda _: None)
    label_ids = ["UNREAD", "INBOX"] if inbox_only else ["UNREAD"]
    ids = list_ids(svc, label_ids=label_ids, progress=emit)
    emit(f"{len(ids):,} unread")

    names = {
        l["id"]: l["name"]
        for l in _with_backoff(
            lambda: svc.users().labels().list(userId="me").execute(),
            units=quota.COST_LABELS_LIST,
        ).get("labels", [])
    }

    fetched, failed = batch_get(svc, ids, fmt="minimal", progress=emit, label="inspected")
    if failed:
        emit(f"[warn] {len(failed):,} skipped — could not read their labels")

    out: list[dict] = []
    for mid, message in fetched.items():
        label_list = message.get("labelIds", [])
        user_labels = [names.get(l, l) for l in label_list if not _is_system_label(l)]
        if labelled_only and not user_labels:
            continue
        out.append({"id": mid, "labelIds": label_list, "user_labels": user_labels})
    return out


def mark_read(svc, message_ids: list[str]) -> int:
    """Remove the UNREAD label. Uses batchModify, so concurrency is not a concern."""
    return batch_modify(svc, message_ids, remove_label_ids=["UNREAD"])


SYSTEM_LABELS = frozenset(
    {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "TRASH", "SPAM", "CHAT"}
)


def _is_system_label(label_id: str) -> bool:
    return label_id in SYSTEM_LABELS or label_id.startswith("CATEGORY_")


def partition_inbox_by_label(
    svc, *, progress: Progress | None = None
) -> tuple[list[dict], list[dict]]:
    """Split inbox messages into (has a user label, has none).

    Archiving a message with no label makes it effectively unfindable — out of the
    inbox and filed nowhere. So the groups are returned separately: the labelled ones
    are safe to archive, the unlabelled ones need a decision first.
    """
    from .batch import batch_get, list_ids

    emit = progress or (lambda _: None)
    ids = list_ids(svc, label_ids=["INBOX"], progress=emit)
    emit(f"{len(ids):,} messages in the inbox")

    names = {
        l["id"]: l["name"]
        for l in _with_backoff(
            lambda: svc.users().labels().list(userId="me").execute(),
            units=quota.COST_LABELS_LIST,
        ).get("labels", [])
    }

    # `metadata` not `minimal`: the unlabelled ones need From/Subject so a human can
    # decide what to do with them.
    fetched, failed = batch_get(
        svc, ids, fmt="metadata", headers=["From", "Subject", "Date"],
        progress=emit, label="inspected",
    )
    if failed:
        emit(f"[warn] {len(failed):,} could not be inspected and are left alone")

    labelled: list[dict] = []
    unlabelled: list[dict] = []
    for mid, response in fetched.items():
        label_list = response.get("labelIds", [])
        user_labels = [names.get(l, l) for l in label_list if not _is_system_label(l)]
        hdrs = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (response.get("payload", {}) or {}).get("headers", [])
        }
        record = {
            "id": mid, "labelIds": label_list, "user_labels": user_labels,
            "from": hdrs.get("from", ""), "subject": hdrs.get("subject", ""),
        }
        (labelled if user_labels else unlabelled).append(record)

    return labelled, unlabelled


def archive_messages(svc, message_ids: list[str]) -> int:
    """Remove INBOX. Reversible by adding it back."""
    return batch_modify(svc, message_ids, remove_label_ids=["INBOX"])


# ---------------------------------------------------------------------------
# Trash / untrash
# ---------------------------------------------------------------------------


def _http_batch(svc, ids: list[str], make_request) -> tuple[list[str], dict[str, str]]:
    """Run one HTTP sub-request per id, returning (succeeded ids, {id: error})."""
    ok: list[str] = []
    errors: dict[str, str] = {}

    def callback(request_id: str, response, exception: Exception | None) -> None:
        if exception is not None:
            errors[request_id] = str(exception)
        else:
            ok.append(request_id)

    batch = svc.new_batch_http_request(callback=callback)
    for mid in ids:
        batch.add(make_request(mid), request_id=mid)
    # Callers pay the quota for their own sub-requests before calling in.
    _with_backoff(batch.execute)
    return ok, errors


def _mutate_many(
    svc,
    message_ids: list[str],
    make_request,
    *,
    verb: str,
    units_each: int,
    progress: Progress | None = None,
    attempts: int = 5,
) -> tuple[list[str], dict[str, str]]:
    """Apply a per-message mutation to many ids, narrow-batched and retried.

    Two things this gets right that the first version didn't: batches are kept narrow
    enough to stay under Gmail's write concurrency ceiling, and per-sub-request
    rate-limit failures are retried with backoff instead of being reported as permanent.
    """
    import random
    import time

    emit = progress or (lambda _: None)
    all_ok: list[str] = []
    pending = list(message_ids)
    errors: dict[str, str] = {}

    for attempt in range(1, attempts + 1):
        if not pending:
            break
        retry: list[str] = []

        for start in range(0, len(pending), MUTATE_BATCH_CAP):
            chunk = pending[start : start + MUTATE_BATCH_CAP]
            quota.spend(len(chunk) * units_each)
            ok, chunk_errors = _http_batch(svc, chunk, make_request)
            all_ok.extend(ok)

            for mid, message in chunk_errors.items():
                if _is_retryable_suberror(message):
                    retry.append(mid)
                    errors.pop(mid, None)
                else:
                    errors[mid] = message

            emit(f"{verb} {len(all_ok):,}/{len(message_ids):,}")

        if retry:
            # Back off before the next sweep; concurrency pressure needs time, not a
            # faster retry.
            delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
            emit(
                f"{len(retry):,} hit Gmail's concurrency limit — retrying in "
                f"{delay:.0f}s (attempt {attempt + 1}/{attempts})"
            )
            time.sleep(delay + random.uniform(0, 1.0))
        pending = retry

    # Anything still pending after every attempt is a genuine failure.
    for mid in pending:
        errors.setdefault(mid, "still rate-limited after all retries")

    return all_ok, errors


def trash_messages(
    svc, message_ids: list[str], *, progress: Progress | None = None
) -> tuple[list[str], dict[str, str]]:
    """Move messages to Trash. Reversible for 30 days by Gmail."""
    return _mutate_many(
        svc,
        message_ids,
        lambda mid: svc.users().messages().trash(userId="me", id=mid),
        verb="trashed",
        units_each=quota.COST_MESSAGES_TRASH,
        progress=progress,
    )


def untrash_messages(
    svc, message_ids: list[str], *, progress: Progress | None = None
) -> tuple[list[str], dict[str, str]]:
    """Restore messages from Trash. The core of `ecs undo`."""
    return _mutate_many(
        svc,
        message_ids,
        lambda mid: svc.users().messages().untrash(userId="me", id=mid),
        verb="restored",
        units_each=quota.COST_MESSAGES_UNTRASH,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# Filters (gmail.settings.basic)
# ---------------------------------------------------------------------------


def list_filters(svc) -> list[dict]:
    response = _with_backoff(
        lambda: svc.users().settings().filters().list(userId="me").execute()
    )
    return response.get("filter", [])


def create_filter(svc, criteria: dict, action: dict) -> str:
    """Create a Gmail filter. Returns its id for the journal."""
    result = _with_backoff(
        lambda: svc.users()
        .settings()
        .filters()
        .create(userId="me", body={"criteria": criteria, "action": action})
        .execute()
    )
    return result["id"]


def delete_filter(svc, filter_id: str) -> None:
    try:
        _with_backoff(
            lambda: svc.users()
            .settings()
            .filters()
            .delete(userId="me", id=filter_id)
            .execute()
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            return
        raise


# ---------------------------------------------------------------------------
# Send (used only by the mailto unsubscribe path)
# ---------------------------------------------------------------------------


def send_plain_message(svc, *, to: str, subject: str, body: str) -> str:
    """Send a minimal plaintext email. Returns the sent message id."""
    import base64
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = _with_backoff(
        lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    )
    return result["id"]
