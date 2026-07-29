"""Stage 10 — execute the approved plan, and reverse it.

Design constraints, in priority order:

  1. **Dry run is the default.** `--apply` is required to touch anything.
  2. **Journal before mutate.** Every write is recorded as an intent, then re-recorded
     as committed. A crash leaves an uncommitted entry — recoverable — rather than a
     mutation with no record.
  3. **Waves with checkpoints.** Work proceeds in batches of ~200 so the user can
     inspect Gmail mid-run and abort. A 7,000-message change that can only be
     evaluated after it finishes is not reviewable.
  4. **Labels first, trash last.** Ordering matters for undo: labels created early are
     deleted last, and a message is filed before it is removed from the inbox.

Only approved actions are executed. Approval comes from the TUI, or from
`approve_all()` for a user who has reviewed the dry-run output and wants the lot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import db
from .journal import Entry, Journal

Progress = Callable[[str], None]


@dataclass
class ApplyResult:
    labels_created: int = 0
    labelled: int = 0
    archived: int = 0
    trashed: int = 0
    failed: int = 0
    waves: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.labels_created} labels created",
            f"{self.labelled:,} messages labelled",
            f"{self.archived:,} archived",
            f"{self.trashed:,} moved to Trash",
        ]
        if self.failed:
            parts.append(f"{self.failed:,} failed")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approve_all(conn: sqlite3.Connection) -> int:
    """Approve every pending action. For a user who reviewed the dry run."""
    cur = conn.execute(
        "UPDATE plan_actions SET approved = 1 WHERE applied_at IS NULL"
    )
    return cur.rowcount


def approve_clusters(conn: sqlite3.Connection, cluster_keys: list[str]) -> int:
    if not cluster_keys:
        return 0
    placeholders = ",".join("?" * len(cluster_keys))
    cur = conn.execute(
        f"UPDATE plan_actions SET approved = 1 "
        f"WHERE applied_at IS NULL AND cluster_key IN ({placeholders})",
        cluster_keys,
    )
    return cur.rowcount


def approve_non_destructive(conn: sqlite3.Connection) -> int:
    """Approve only labelling and archiving — nothing gets deleted.

    A useful first move: it empties the inbox and files everything without any
    deletion at all, so the result can be judged before approving any trashing.
    """
    cur = conn.execute(
        "UPDATE plan_actions SET approved = 1 "
        "WHERE applied_at IS NULL AND action IN ('add_label', 'archive')"
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def dry_run(conn: sqlite3.Connection, *, only_approved: bool = False) -> dict[str, object]:
    """Describe what an apply would do, without touching Gmail."""
    where = "applied_at IS NULL" + (" AND approved = 1" if only_approved else "")

    by_action = {
        r["action"]: r["n"]
        for r in conn.execute(
            f"SELECT action, COUNT(*) AS n FROM plan_actions WHERE {where} GROUP BY action"
        )
    }
    labels = {
        r["label"]: r["n"]
        for r in conn.execute(
            f"SELECT label, COUNT(*) AS n FROM plan_actions "
            f"WHERE {where} AND label IS NOT NULL GROUP BY label ORDER BY n DESC"
        )
    }
    existing = {r["name"] for r in conn.execute("SELECT name FROM existing_labels")}
    to_create = sorted(set(labels) - existing)

    trash_by_cluster = [
        (r["display_name"], r["n"], r["reason"])
        for r in conn.execute(
            f"""
            SELECT c.display_name, COUNT(*) AS n, MIN(p.reason) AS reason
            FROM plan_actions p
            LEFT JOIN clusters c ON c.key = p.cluster_key
            WHERE {where} AND p.action = 'trash'
            GROUP BY p.cluster_key
            ORDER BY n DESC
            """
        )
    ]

    return {
        "by_action": by_action,
        "labels": labels,
        "labels_to_create": to_create,
        "trash_by_cluster": trash_by_cluster,
        "total_actions": sum(by_action.values()),
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_plan(
    *,
    limit: int | None = None,
    wave_size: int | None = None,
    checkpoint: Callable[[int, ApplyResult], bool] | None = None,
    progress: Progress | None = None,
) -> ApplyResult:
    """Execute approved actions against Gmail.

    `checkpoint` is called between waves with (wave number, running result); returning
    False aborts cleanly. `limit` caps the number of messages touched — used for the
    5-message smoke test that must pass before a full run.
    """
    from . import config
    from .gmail.auth import service
    from .gmail.mutate import batch_modify, ensure_labels, trash_messages

    emit = progress or (lambda _: None)
    result = ApplyResult()
    journal = Journal()
    svc = service()
    size = wave_size or config.TUNABLES.wave_size

    # --- Labels first, so message actions have somewhere to file to ------
    with db.session() as conn:
        wanted = [
            r["label"]
            for r in conn.execute(
                "SELECT DISTINCT label FROM plan_actions "
                "WHERE approved = 1 AND applied_at IS NULL AND label IS NOT NULL"
            )
        ]

    label_ids: dict[str, str] = {}
    if wanted:
        label_ids, created = ensure_labels(svc, wanted, progress=emit)
        result.labels_created = len(created)
        for name, label_id in created:
            entry = Entry(
                op="create_label", after={"name": name, "label_id": label_id}
            )
            journal.record(entry)
            journal.commit(entry)
        # Keep the local label snapshot current so a later run doesn't re-create.
        with db.session() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO existing_labels(id, name, type) "
                "VALUES(?,?,'user')",
                [(lid, name) for name, lid in created],
            )

    # --- Group the work: same (action, label) can go in one API call ------
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT id, message_id, action, label
            FROM plan_actions
            WHERE approved = 1 AND applied_at IS NULL
            ORDER BY CASE action
                        WHEN 'add_label' THEN 0
                        WHEN 'archive'   THEN 1
                        WHEN 'trash'     THEN 2
                     END,
                     label
            """
        ).fetchall()
        # Prior label state, needed so undo can restore exactly.
        prior = {
            r["id"]: r["label_ids"]
            for r in conn.execute("SELECT id, label_ids FROM messages")
        }

    if limit:
        # Cap by distinct messages, not actions, so a 5-message smoke test really
        # touches 5 messages.
        seen: set[str] = set()
        capped = []
        for row in rows:
            if row["message_id"] not in seen and len(seen) >= limit:
                continue
            seen.add(row["message_id"])
            capped.append(row)
        rows = capped

    if not rows:
        emit("nothing approved to apply")
        return result

    has_bar = hasattr(emit, "stage")
    if has_bar:
        emit.stage("applying actions", total=len(rows))
    emit(f"applying {len(rows):,} actions in waves of {size}")

    for wave_no, start in enumerate(range(0, len(rows), size), start=1):
        wave = rows[start : start + size]
        result.waves = wave_no

        # Bucket by the API call each action maps to.
        label_buckets: dict[str, list[sqlite3.Row]] = {}
        archive_rows: list[sqlite3.Row] = []
        trash_rows: list[sqlite3.Row] = []

        for row in wave:
            if row["action"] == "add_label":
                label_buckets.setdefault(row["label"], []).append(row)
            elif row["action"] == "archive":
                archive_rows.append(row)
            elif row["action"] == "trash":
                trash_rows.append(row)

        applied_ids: list[int] = []

        # --- Labelling ---------------------------------------------------
        for label_name, bucket in label_buckets.items():
            label_id = label_ids.get(label_name)
            if not label_id:
                result.errors.append(f"missing label id for {label_name}")
                result.failed += len(bucket)
                continue
            ids = [r["message_id"] for r in bucket]
            entries = [
                Entry(
                    op="modify",
                    wave=wave_no,
                    message_id=mid,
                    before={"labelIds": prior.get(mid)},
                    after={"addLabelIds": [label_id], "removeLabelIds": []},
                )
                for mid in ids
            ]
            journal.record_all(entries)
            try:
                batch_modify(svc, ids, add_label_ids=[label_id])
            except Exception as exc:
                result.failed += len(ids)
                result.errors.append(f"label {label_name}: {exc}")
                for entry in entries:
                    journal.commit(entry, error=str(exc))
                continue
            for entry in entries:
                journal.commit(entry)
            result.labelled += len(ids)
            applied_ids.extend(r["id"] for r in bucket)

        # --- Archiving ---------------------------------------------------
        if archive_rows:
            ids = [r["message_id"] for r in archive_rows]
            entries = [
                Entry(
                    op="modify",
                    wave=wave_no,
                    message_id=mid,
                    before={"labelIds": prior.get(mid)},
                    after={"addLabelIds": [], "removeLabelIds": ["INBOX"]},
                )
                for mid in ids
            ]
            journal.record_all(entries)
            try:
                batch_modify(svc, ids, remove_label_ids=["INBOX"])
            except Exception as exc:
                result.failed += len(ids)
                result.errors.append(f"archive: {exc}")
                for entry in entries:
                    journal.commit(entry, error=str(exc))
            else:
                for entry in entries:
                    journal.commit(entry)
                result.archived += len(ids)
                applied_ids.extend(r["id"] for r in archive_rows)

        # --- Trashing (last, and only via messages.trash) ----------------
        if trash_rows:
            ids = [r["message_id"] for r in trash_rows]
            entries = {
                mid: Entry(
                    op="trash",
                    wave=wave_no,
                    message_id=mid,
                    before={"labelIds": prior.get(mid)},
                )
                for mid in ids
            }
            journal.record_all(list(entries.values()))
            ok, errors = trash_messages(svc, ids, progress=emit)
            for mid in ok:
                journal.commit(entries[mid])
            for mid, err in errors.items():
                journal.commit(entries[mid], error=err)
            result.trashed += len(ok)
            result.failed += len(errors)
            if errors:
                result.errors.append(f"{len(errors)} trash failures")
            ok_set = set(ok)
            applied_ids.extend(
                r["id"] for r in trash_rows if r["message_id"] in ok_set
            )

        # --- Mark applied ------------------------------------------------
        now = datetime.now(UTC).isoformat()
        with db.session() as conn:
            conn.executemany(
                "UPDATE plan_actions SET applied_at = ?, wave = ? WHERE id = ?",
                [(now, wave_no, action_id) for action_id in applied_ids],
            )

        if has_bar:
            emit.advance(len(wave))
            emit.set("labelled", result.labelled)
            emit.set("archived", result.archived)
            emit.set("trashed", result.trashed)
            emit.set("failed", result.failed)
        else:
            emit(f"wave {wave_no}: {result.summary()}")

        if checkpoint is not None and start + size < len(rows):
            if not checkpoint(wave_no, result):
                emit("aborted at checkpoint")
                break

    return result


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def undo(
    *,
    since: str | None = None,
    wave: int | None = None,
    progress: Progress | None = None,
) -> dict[str, int]:
    """Reverse committed mutations, newest first.

    Note what this cannot reverse: an unsubscribe. Those are reported separately so
    the user knows precisely what was and wasn't undone.
    """
    from .gmail.auth import service
    from .gmail.mutate import batch_modify, delete_label, untrash_messages

    emit = progress or (lambda _: None)
    journal = Journal()
    steps = journal.undo_plan(since=since, wave=wave)

    if not steps:
        emit("nothing to undo")
        return {"restored": 0, "relabelled": 0, "labels_deleted": 0, "irreversible": 0}

    svc = service()
    counts = {"restored": 0, "relabelled": 0, "labels_deleted": 0, "irreversible": 0}

    # Group by operation so the reversal is also batched.
    untrash_ids = [s["message_id"] for s in steps if s["op"] == "untrash"]
    restore_labels = {
        s["message_id"]: s.get("restore_labels", [])
        for s in steps
        if s["op"] == "untrash"
    }
    modify_steps = [s for s in steps if s["op"] == "modify"]
    label_deletes = [s for s in steps if s["op"] == "delete_label"]
    counts["irreversible"] = sum(1 for s in steps if s["op"] == "noop")

    if untrash_ids:
        emit(f"restoring {len(untrash_ids):,} messages from Trash")
        ok, _errors = untrash_messages(svc, untrash_ids, progress=emit)
        counts["restored"] = len(ok)
        # Untrash returns a message to its prior labels, but INBOX can be dropped in
        # some cases — re-add it explicitly where it was there before.
        needs_inbox = [
            mid for mid in ok if "INBOX" in (restore_labels.get(mid) or [])
        ]
        if needs_inbox:
            batch_modify(svc, needs_inbox, add_label_ids=["INBOX"])

    # Invert label changes, grouped by the exact add/remove pair.
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for step in modify_steps:
        key = (
            tuple(step.get("addLabelIds", [])),
            tuple(step.get("removeLabelIds", [])),
        )
        grouped.setdefault(key, []).append(step["message_id"])

    for (add, remove), ids in grouped.items():
        if not add and not remove:
            continue
        batch_modify(svc, ids, add_label_ids=list(add), remove_label_ids=list(remove))
        counts["relabelled"] += len(ids)
        emit(f"reverted labels on {len(ids):,} messages")

    for step in label_deletes:
        if step.get("label_id"):
            delete_label(svc, step["label_id"])
            counts["labels_deleted"] += 1

    # Clear applied markers so the plan can be re-applied after adjustment.
    with db.session() as conn:
        if wave is not None:
            conn.execute(
                "UPDATE plan_actions SET applied_at = NULL WHERE wave = ?", (wave,)
            )
        else:
            conn.execute("UPDATE plan_actions SET applied_at = NULL")

    return counts
