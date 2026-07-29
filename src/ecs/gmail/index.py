"""Index inbox metadata into SQLite.

Three passes:
  1. `in:inbox` — full metadata for every message. This is the cleanup target.
  2. `in:sent` — senders only, read-only. Anyone you've emailed becomes a
     protected sender, which is the strongest available signal that a
     correspondent is a real relationship rather than a robot.
  3. Labels — so a newly designed taxonomy doesn't collide with existing labels.

No message bodies are fetched here. Bodies cost quota and tokens, and only the
small subset of messages that reach escalation ever need one.

Resumability matters at this size: a 7,000-message index is thousands of API calls.
The list page token and the set of already-fetched ids are both checkpointed, so an
interrupted run resumes instead of restarting.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable, Iterator
from email.utils import getaddresses, parseaddr

from googleapiclient.errors import HttpError

from .. import config, db
from ..cluster import normalize_subject, sender_domain
from . import quota

Progress = Callable[[str], None]

# Gmail's INBOX label counter, used to set an accurate progress total up front and to
# detect when a listing pass has silently under-collected.
INBOX_LABEL = "INBOX"

CHECKPOINT_INBOX_PAGE = "index.inbox.page_token"
CHECKPOINT_INBOX_DONE = "index.inbox.listing_complete"
CHECKPOINT_SCOPE = "index.inbox.scope"
CHECKPOINT_SENT_PAGE = "index.sent.page_token"
CHECKPOINT_SENT_DONE = "index.sent.complete"
PENDING_IDS = "index.inbox.pending_ids"

# Transient Gmail failures worth retrying rather than aborting a long index.
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_RETRYABLE_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
}


def _is_retryable(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    if status not in _RETRYABLE_STATUS:
        return False
    if status == 403:
        # 403 is overloaded: rate limiting is retryable, insufficient permission
        # never is. Distinguish on the reason string.
        body = (exc.content or b"").decode("utf-8", errors="replace")
        return any(reason in body for reason in _RETRYABLE_REASONS)
    return True


def _with_backoff(
    fn: Callable[[], object], *, attempts: int = 8, units: float = 0.0
) -> object:
    """Spend quota, then run a Gmail call, retrying transient failures.

    `units` is the call's quota cost. Paying it *before* the request keeps a long run
    under Gmail's 250 units/sec ceiling instead of discovering the limit by being
    rejected — which is what truncated the first index run.
    """
    if units:
        quota.spend(units)

    delay = 2.0
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except HttpError as exc:
            if not _is_retryable(exc):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            # Drain the bucket too: resuming at full rate straight after a rejection
            # just earns another one.
            quota.limiter().penalise(delay)
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay = min(delay * 2, 120.0)
    raise RuntimeError(
        f"Gmail call failed after {attempts} attempts: {last}"
    ) from last


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def label_counts(svc, label_id: str = INBOX_LABEL) -> dict[str, int]:
    """Gmail's own counters for a label — the authoritative message total.

    Worth trusting over anything derived from search: `messages.list` with a `q=` goes
    through the search index, which under-reports. Reading the label directly is how we
    know whether a listing pass actually saw everything.
    """
    response = _with_backoff(
        lambda: svc.users().labels().get(userId="me", id=label_id).execute(),
        units=quota.COST_LABELS_LIST,
    )
    return {
        "messages": response.get("messagesTotal", 0),
        "threads": response.get("threadsTotal", 0),
        "unread": response.get("messagesUnread", 0),
    }


def list_message_ids(
    svc,
    *,
    conn: sqlite3.Connection,
    page_checkpoint: str,
    done_checkpoint: str,
    label_ids: list[str] | None = None,
    query: str | None = None,
    limit: int | None = None,
    progress: Progress | None = None,
) -> list[str]:
    """Collect message ids, checkpointing the page token as we go.

    Prefer `label_ids` over `query`: filtering by label reads the label membership
    directly, while a `q=` string is resolved by the search index and can return a
    fraction of the real set. The first run of this indexer used `q="in:inbox"` and
    collected 5,933 of 23,361 messages, which is exactly that failure.
    """
    if not label_ids and not query:
        raise ValueError("pass either label_ids or query")

    collected: list[str] = list(db.kv_get(conn, PENDING_IDS, []) or [])
    if db.kv_get(conn, done_checkpoint, False) and collected:
        return collected[:limit] if limit else collected

    page_token = db.kv_get(conn, page_checkpoint)
    seen = set(collected)
    pages = 0

    while True:
        kwargs: dict = {"userId": "me", "maxResults": 500, "pageToken": page_token}
        if label_ids:
            kwargs["labelIds"] = label_ids
        if query:
            kwargs["q"] = query

        response = _with_backoff(
            lambda: svc.users().messages().list(**kwargs).execute(),
            units=quota.COST_MESSAGES_LIST,
        )
        for msg in response.get("messages", []):
            if msg["id"] not in seen:
                seen.add(msg["id"])
                collected.append(msg["id"])

        page_token = response.get("nextPageToken")
        pages += 1
        db.kv_set(conn, page_checkpoint, page_token)
        db.kv_set(conn, PENDING_IDS, collected)
        conn.commit()

        if progress:
            progress(f"listing: {len(collected):,} ids over {pages} pages")

        if limit and len(collected) >= limit:
            collected = collected[:limit]
            break
        if not page_token:
            db.kv_set(conn, done_checkpoint, True)
            conn.commit()
            break

    return collected


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _headers_to_dict(payload: dict) -> dict[str, str]:
    # Header names are case-insensitive per RFC 5322; Gmail preserves the sender's
    # casing, so normalise before lookup.
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }


def _walk_parts(payload: dict) -> Iterator[dict]:
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def _detect_attachment_and_invite(payload: dict, headers: dict[str, str]) -> tuple[bool, bool]:
    """Detect attachments and calendar invites from the MIME skeleton.

    A calendar invite is a strong never-trash signal: it means a real commitment
    was made, and the surrounding thread usually carries context worth keeping.
    """
    has_attachment = False
    has_invite = "text/calendar" in headers.get("content-type", "").lower()

    for part in _walk_parts(payload):
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        if "calendar" in mime or filename.lower().endswith(".ics"):
            has_invite = True
        if filename:
            has_attachment = True
        body = part.get("body") or {}
        if body.get("attachmentId"):
            has_attachment = True
    return has_attachment, has_invite


def parse_message(msg: dict) -> tuple:
    """Convert a Gmail metadata response into a `messages` row tuple."""
    payload = msg.get("payload", {}) or {}
    headers = _headers_to_dict(payload)

    from_name, from_addr = parseaddr(headers.get("from", ""))
    from_addr = (from_addr or "").strip().lower() or None

    to_pairs = getaddresses(
        [headers.get("to", ""), headers.get("cc", "")]
    )
    to_addrs = [a.strip().lower() for _, a in to_pairs if a and "@" in a]

    label_ids = msg.get("labelIds", []) or []
    subject = headers.get("subject", "")

    # internalDate is Gmail's own receipt timestamp in ms — more trustworthy than
    # the Date header, which senders routinely get wrong or forge.
    raw_ts = msg.get("internalDate")
    date_ts = int(raw_ts) // 1000 if raw_ts else None

    has_attachment, has_invite = _detect_attachment_and_invite(payload, headers)

    return (
        msg["id"],
        msg.get("threadId", ""),
        from_addr,
        (from_name or "").strip() or None,
        sender_domain(from_addr),
        json.dumps(to_addrs),
        subject,
        normalize_subject(subject),
        date_ts,
        msg.get("snippet", ""),
        msg.get("sizeEstimate", 0),
        json.dumps(label_ids),
        headers.get("list-id") or None,
        headers.get("list-unsubscribe") or None,
        headers.get("list-unsubscribe-post") or None,
        1 if has_attachment else 0,
        1 if has_invite else 0,
        1 if "STARRED" in label_ids else 0,
        1 if "IMPORTANT" in label_ids else 0,
        1 if "UNREAD" in label_ids else 0,
    )


MESSAGE_COLUMNS = [
    "id",
    "thread_id",
    "from_addr",
    "from_name",
    "from_domain",
    "to_addrs",
    "subject",
    "subject_norm",
    "date_ts",
    "snippet",
    "size_estimate",
    "label_ids",
    "list_id",
    "list_unsubscribe",
    "list_unsubscribe_post",
    "has_attachment",
    "has_calendar_invite",
    "is_starred",
    "is_important",
    "is_unread",
]


# ---------------------------------------------------------------------------
# Batched metadata fetch
# ---------------------------------------------------------------------------


def fetch_metadata_batch(svc, ids: list[str]) -> tuple[list[tuple], list[str]]:
    """Fetch metadata for up to 100 ids in one HTTP round trip.

    Returns (parsed rows, ids that failed). A failed id is retried by the caller
    rather than aborting the batch — one poisoned message shouldn't stop an index.
    """
    rows: list[tuple] = []
    failed: list[str] = []

    def callback(request_id: str, response: dict, exception: Exception | None) -> None:
        if exception is not None:
            failed.append(request_id)
            return
        try:
            rows.append(parse_message(response))
        except Exception:
            failed.append(request_id)

    batch = svc.new_batch_http_request(callback=callback)
    for mid in ids:
        batch.add(
            svc.users()
            .messages()
            .get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=config.METADATA_HEADERS,
            ),
            request_id=mid,
        )
    # One HTTP request, but Gmail bills every sub-request: 100 gets = 500 units.
    _with_backoff(batch.execute, units=len(ids) * quota.COST_MESSAGES_GET)
    return rows, failed


def scope_counts(svc, scope: str) -> dict[str, int]:
    """Count messages in a scope by listing ids.

    A tab's real size is the *intersection* of INBOX and its CATEGORY_ label, which no
    single label counter reports — CATEGORY_PROMOTIONS spans the whole mailbox, archived
    mail included. So this counts by listing, which is cheap (5 units per 500 ids).
    """
    label_ids = config.INBOX_SCOPES[scope]
    total, token = 0, None
    while True:
        response = _with_backoff(
            lambda: svc.users()
            .messages()
            .list(userId="me", labelIds=label_ids, maxResults=500, pageToken=token)
            .execute(),
            units=quota.COST_MESSAGES_LIST,
        )
        total += len(response.get("messages", []))
        token = response.get("nextPageToken")
        if not token:
            return {"messages": total}


def inbox_breakdown(svc, *, progress: Progress | None = None) -> dict[str, int]:
    """Messages per category tab, plus the uncategorised remainder.

    Worth running before committing to a scope: Gmail's UI count and the API's label
    counters disagree often enough that guessing the size of a job is unwise.
    """
    rep = progress or (lambda _: None)
    out: dict[str, int] = {}
    for scope in ("primary", "updates", "promotions", "social", "forums"):
        out[scope] = scope_counts(svc, scope)["messages"]
        rep(f"{scope}: {out[scope]:,}")
    inbox_total = label_counts(svc)["messages"]
    out["inbox_total"] = inbox_total
    out["uncategorised"] = inbox_total - sum(
        v for k, v in out.items() if k not in ("inbox_total", "uncategorised")
    )
    return out


def fetch_metadata_individually(
    svc, ids: list[str], *, progress: Progress | None = None
) -> tuple[list[tuple], list[str]]:
    """Fetch messages one at a time, as a last resort.

    Sub-requests inside a large `BatchHttpRequest` are sometimes shed under load, and
    re-batching the same ids reproduces the failure — the first index run lost 213
    messages that way despite two retry passes. Fetching them individually recovers
    them: it's slower per message, but at a few hundred stragglers the cost is seconds.
    """
    rep = progress or (lambda _: None)
    rows: list[tuple] = []
    failed: list[str] = []

    for index, mid in enumerate(ids, start=1):
        try:
            message = _with_backoff(
                lambda: svc.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=config.METADATA_HEADERS,
                )
                .execute(),
                units=quota.COST_MESSAGES_GET,
                attempts=4,
            )
            rows.append(parse_message(message))
        except Exception:
            # A genuinely unreachable message (deleted mid-run, or malformed) —
            # record it and move on rather than failing the whole pass.
            failed.append(mid)
        if index % 25 == 0:
            rep(f"individual fetch {index:,}/{len(ids):,}")

    return rows, failed


def index_inbox(
    *,
    limit: int | None = None,
    progress: Progress | None = None,
    label_ids: list[str] | None = None,
    query: str | None = None,
    scope: str = "all",
) -> dict[str, int]:
    """Index the inbox. Safe to interrupt and re-run.

    Defaults to filtering by the INBOX *label* rather than an `in:inbox` search, which
    is the difference between seeing the whole inbox and seeing whatever the search
    index feels like returning.
    """
    from .auth import service

    svc = service()
    rep = progress or (lambda _: None)

    if label_ids is None and not query:
        if scope not in config.INBOX_SCOPES:
            raise ValueError(
                f"unknown scope {scope!r}; choose from "
                f"{sorted(config.INBOX_SCOPES)}"
            )
        label_ids = config.INBOX_SCOPES[scope]

    # Structured progress if the caller passed a Reporter; a plain callable still works.
    has_bar = hasattr(rep, "stage")

    # For the whole inbox the label counter is exact and free. For a single tab it
    # isn't — CATEGORY_* counters span archived mail too — so fall back to counting
    # by listing, which the pass below has to do anyway.
    counts: dict[str, int] = {}
    if scope == "all" and not query:
        counts = label_counts(svc)
        rep(
            f"Gmail reports {counts['messages']:,} messages in INBOX "
            f"({counts['threads']:,} threads, {counts['unread']:,} unread)"
        )
    elif label_ids:
        rep(f"scope {scope!r}: labels {'+'.join(label_ids)}")

    # A Gmail page token is only valid for the exact query that produced it, so any
    # change to the listing parameters invalidates a resume point. Fingerprint the
    # listing rather than just the scope name — an index built by an older version
    # using `q="in:inbox"` leaves a page token that is meaningless to a labelIds
    # listing, and resuming from it silently yields the wrong set.
    fingerprint = f"labels:{'+'.join(label_ids)}" if label_ids else f"query:{query}"

    with db.session() as conn:
        previous = db.kv_get(conn, CHECKPOINT_SCOPE)
        stale_state = (
            db.kv_get(conn, CHECKPOINT_INBOX_PAGE) is not None
            or db.kv_get(conn, PENDING_IDS)
            or db.kv_get(conn, CHECKPOINT_INBOX_DONE, False)
        )
        if previous != fingerprint and stale_state:
            rep(
                f"listing method changed ({previous or 'unfingerprinted (older run)'} "
                f"-> {fingerprint}); discarding the stale resume point"
            )
            db.kv_delete(conn, CHECKPOINT_INBOX_PAGE)
            db.kv_delete(conn, CHECKPOINT_INBOX_DONE)
            db.kv_delete(conn, PENDING_IDS)
        db.kv_set(conn, CHECKPOINT_SCOPE, fingerprint)

    if has_bar:
        rep.stage("listing message ids", total=counts.get("messages") or None)

    with db.session() as conn:
        ids = list_message_ids(
            svc,
            conn=conn,
            page_checkpoint=CHECKPOINT_INBOX_PAGE,
            done_checkpoint=CHECKPOINT_INBOX_DONE,
            label_ids=label_ids or None,
            query=query,
            limit=limit,
            progress=rep,
        )
        existing = {
            r["id"] for r in conn.execute("SELECT id FROM messages").fetchall()
        }
        todo = [i for i in ids if i not in existing]

    # If the listing came back materially short of Gmail's own counter, say so rather
    # than quietly proceeding with a partial mailbox.
    expected = counts.get("messages")
    if expected and len(ids) < expected * 0.95 and limit is None:
        shortfall = expected - len(ids)
        warn = getattr(rep, "warn", rep)
        warn(
            f"listing found {len(ids):,} ids but Gmail reports {expected:,} "
            f"({shortfall:,} short) — re-run to pick up the remainder"
        )

    rep(
        f"{len(ids):,} in scope, {len(existing):,} already stored, "
        f"{len(todo):,} to fetch"
    )

    written = 0
    failures: list[str] = []
    size = config.METADATA_BATCH_SIZE

    if has_bar:
        rep.stage("fetching metadata", total=len(todo))
        rep.counter("indexed", "indexed", style="green")
        rep.counter("failed", "failed", style="yellow")
        rep.counter("quota", "quota units", style="cyan")

    for start in range(0, len(todo), size):
        chunk = todo[start : start + size]
        rows, failed = fetch_metadata_batch(svc, chunk)
        failures.extend(failed)

        with db.session() as conn:
            db.upsert_many(conn, "messages", MESSAGE_COLUMNS, rows)
        written += len(rows)

        if has_bar:
            rep.advance(len(chunk))
            rep.set("indexed", written)
            rep.set("failed", len(failures))
            rep.set("quota", quota.limiter().total_units)
            if failed:
                rep.warn(f"{len(failed)} messages failed in this batch, will retry")
        else:
            rep(f"indexed {written:,}/{len(todo):,}")

    # Retry transient per-message failures. Two passes over *all* failures, not just
    # the first batch — a large index can shed several hundred messages to transient
    # errors, and silently keeping only the first 100 retries would leave the rest
    # missing from the index with no indication why.
    for attempt in (1, 2):
        if not failures:
            break
        if has_bar:
            rep.stage(f"retry pass {attempt}", total=len(failures))
        rep(f"retry pass {attempt}: {len(failures):,} messages")
        still_failed: list[str] = []
        for start in range(0, len(failures), size):
            chunk = failures[start : start + size]
            rows, chunk_failed = fetch_metadata_batch(svc, chunk)
            still_failed.extend(chunk_failed)
            with db.session() as conn:
                db.upsert_many(conn, "messages", MESSAGE_COLUMNS, rows)
            written += len(rows)
            if has_bar:
                rep.advance(len(chunk))
                rep.set("indexed", written)
        recovered = len(failures) - len(still_failed)
        rep(f"retry pass {attempt}: recovered {recovered:,}")
        failures = still_failed
        if has_bar:
            rep.set("failed", len(failures))
        if failures and attempt == 1:
            # Brief pause before the second pass; most remaining failures are
            # rate-limit related and clear on their own.
            time.sleep(5)

    # Third and final pass, one message at a time. Anything still failing after two
    # batch passes is being shed by the batch endpoint rather than being genuinely
    # unavailable, and re-batching it a third time would fail identically.
    if failures:
        if has_bar:
            rep.stage("recovering stragglers individually", total=len(failures))
        rep(f"fetching {len(failures):,} stragglers one at a time")
        rows, still_failed = fetch_metadata_individually(
            svc, failures, progress=rep
        )
        with db.session() as conn:
            db.upsert_many(conn, "messages", MESSAGE_COLUMNS, rows)
        written += len(rows)
        rep(f"individual pass recovered {len(rows):,} of {len(failures):,}")
        failures = still_failed
        if has_bar:
            rep.set_completed(len(rows))
            rep.set("indexed", written)
            rep.set("failed", len(failures))

    if failures:
        # Persist the ids so a later run can retry them, and so the count in the
        # report is explainable rather than mysterious.
        with db.session() as conn:
            db.kv_set(conn, "index.inbox.failed_ids", failures)

    with db.session() as conn:
        # Listing is complete and everything is stored; clear the pending set so a
        # later run re-lists rather than trusting a stale id snapshot.
        if limit is None and db.kv_get(conn, CHECKPOINT_INBOX_DONE, False):
            db.kv_delete(conn, PENDING_IDS)
        total = db.message_count(conn)

    return {"in_scope": len(ids), "written": written, "failed": len(failures), "total": total}


# ---------------------------------------------------------------------------
# Sent-folder pass: protected senders
# ---------------------------------------------------------------------------


def index_sent_senders(
    *, max_messages: int = 5000, progress: Progress | None = None
) -> dict[str, int]:
    """Build the protected-sender set from the Sent folder.

    Read-only, headers only. Every address you've ever written to is treated as a
    real relationship and becomes permanently ineligible for deletion.
    """
    from .auth import service

    svc = service()
    rep = progress or (lambda _: None)
    has_bar = hasattr(rep, "stage")

    if has_bar:
        rep.stage("listing sent messages", total=max_messages)

    with db.session() as conn:
        ids = list_message_ids(
            svc,
            conn=conn,
            page_checkpoint=CHECKPOINT_SENT_PAGE,
            done_checkpoint=CHECKPOINT_SENT_DONE,
            label_ids=["SENT"],
            limit=max_messages,
            progress=rep,
        )
        db.kv_delete(conn, PENDING_IDS)

    counts: dict[str, int] = {}
    replied_threads: set[str] = set()
    size = config.METADATA_BATCH_SIZE

    if has_bar:
        rep.stage("scanning sent for correspondents", total=len(ids))
        rep.counter("senders", "protected senders", style="green")
        rep.counter("threads", "replied threads", style="green")

    for start in range(0, len(ids), size):
        chunk = ids[start : start + size]
        recipients: list[str] = []

        def callback(request_id: str, response: dict, exception: Exception | None) -> None:
            if exception is not None or not response:
                return
            # A sent message in a thread means you replied there — the strongest
            # available signal that the thread matters to you.
            if response.get("threadId"):
                replied_threads.add(response["threadId"])
            headers = _headers_to_dict(response.get("payload", {}) or {})
            for _, addr in getaddresses(
                [headers.get("to", ""), headers.get("cc", ""), headers.get("bcc", "")]
            ):
                if addr and "@" in addr:
                    recipients.append(addr.strip().lower())

        batch = svc.new_batch_http_request(callback=callback)
        for mid in chunk:
            batch.add(
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["To", "Cc", "Bcc"],
                ),
                request_id=mid,
            )
        _with_backoff(batch.execute, units=len(chunk) * quota.COST_MESSAGES_GET)

        for addr in recipients:
            counts[addr] = counts.get(addr, 0) + 1

        if has_bar:
            rep.advance(len(chunk))
            rep.set("senders", len(counts))
            rep.set("threads", len(replied_threads))
        else:
            rep(f"sent scanned {min(start + size, len(ids)):,}/{len(ids):,}")

    with db.session() as conn:
        conn.executemany(
            """
            INSERT INTO protected_senders(addr, sent_count) VALUES(?, ?)
            ON CONFLICT(addr) DO UPDATE SET sent_count = sent_count + excluded.sent_count
            """,
            list(counts.items()),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO replied_threads(thread_id) VALUES(?)",
            [(t,) for t in replied_threads],
        )
        total = conn.execute("SELECT COUNT(*) FROM protected_senders").fetchone()[0]

    return {
        "sent_scanned": len(ids),
        "protected_senders": total,
        "replied_threads": len(replied_threads),
    }


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def index_labels() -> int:
    """Snapshot existing labels so a new taxonomy avoids collisions."""
    from .auth import service

    svc = service()
    response = _with_backoff(
        lambda: svc.users().labels().list(userId="me").execute(),
        units=quota.COST_LABELS_LIST,
    )
    labels = response.get("labels", [])

    with db.session() as conn:
        conn.execute("DELETE FROM existing_labels")
        conn.executemany(
            "INSERT INTO existing_labels(id, name, type) VALUES(?,?,?)",
            [(l["id"], l["name"], l.get("type")) for l in labels],
        )
    return len(labels)
