"""Append-only mutation log and undo planner.

Every write against the mailbox is journalled *before* it is attempted and marked
committed after. That ordering matters: a crash mid-wave leaves an uncommitted
entry, which is recoverable, rather than a silent mutation with no record.

This module is pure bookkeeping — it computes the inverse of what was done but
never touches Gmail. Execution lives in `apply.py` so there is exactly one place
in the codebase that mutates the mailbox.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import config

Op = Literal[
    "modify",  # add/remove labels on a message
    "trash",  # move a message to Trash
    "create_label",  # create a Gmail label
    "create_filter",  # create a Gmail filter
    "unsubscribe",  # an unsubscribe attempt (not reversible; recorded for audit)
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Entry:
    op: Op
    ts: str = field(default_factory=_now)
    wave: int | None = None
    message_id: str | None = None
    cluster_key: str | None = None
    # State captured before the mutation, so the inverse can be derived exactly.
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    committed: bool = False
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Entry:
        return cls(**json.loads(line))


class Journal:
    """A newline-delimited JSON log of mailbox mutations."""

    def __init__(self, path: Path | None = None) -> None:
        config.ensure_dirs()
        self.path = path or config.JOURNAL_PATH
        self.path.touch(mode=0o600, exist_ok=True)
        # touch() won't tighten an existing file's mode, so enforce it.
        os.chmod(self.path, 0o600)

    # -- writing ----------------------------------------------------------

    def record(self, entry: Entry) -> int:
        """Append an entry and return its line offset (used as its id)."""
        with self.path.open("a", encoding="utf-8") as fh:
            offset = fh.tell()
            fh.write(entry.to_json() + "\n")
        return offset

    def record_all(self, entries: list[Entry]) -> None:
        if not entries:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(entry.to_json() + "\n")

    def commit(self, entry: Entry, *, error: str | None = None) -> None:
        """Append a committed (or failed) copy of an already-recorded entry.

        Append-only means we don't rewrite the original line; the later entry for
        the same message wins when the log is replayed.
        """
        entry.committed = error is None
        entry.error = error
        entry.ts = _now()
        self.record(entry)

    # -- reading ----------------------------------------------------------

    def entries(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Entry.from_json(line)
                except (json.JSONDecodeError, TypeError):
                    # A torn final line from a hard kill. Skip it rather than
                    # refusing to read the rest of the log.
                    continue

    def committed_entries(self) -> list[Entry]:
        return [e for e in self.entries() if e.committed]

    def waves(self) -> list[int]:
        seen = {e.wave for e in self.committed_entries() if e.wave is not None}
        return sorted(seen)

    def last_wave(self) -> int | None:
        w = self.waves()
        return w[-1] if w else None

    # -- undo -------------------------------------------------------------

    def undo_plan(
        self, *, since: str | None = None, wave: int | None = None
    ) -> list[dict[str, Any]]:
        """Compute the inverse of committed mutations, newest first.

        Returns plain dicts describing what to do, so the caller (apply.py) owns
        all Gmail access. Filter by `since` (ISO timestamp) or a specific `wave`.
        """
        selected = [
            e
            for e in self.committed_entries()
            if (since is None or e.ts >= since) and (wave is None or e.wave == wave)
        ]

        inverse: list[dict[str, Any]] = []
        # Reverse order so nested effects unwind correctly (labels created early
        # are deleted last, after the messages using them are reverted).
        for entry in reversed(selected):
            match entry.op:
                case "modify":
                    added = entry.after.get("addLabelIds", [])
                    removed = entry.after.get("removeLabelIds", [])
                    if not added and not removed:
                        continue
                    inverse.append(
                        {
                            "op": "modify",
                            "message_id": entry.message_id,
                            # Swap the two: what we added, we now remove.
                            "addLabelIds": removed,
                            "removeLabelIds": added,
                        }
                    )
                case "trash":
                    inverse.append(
                        {
                            "op": "untrash",
                            "message_id": entry.message_id,
                            # Restore the exact label set the message had before.
                            "restore_labels": entry.before.get("labelIds", []),
                        }
                    )
                case "create_label":
                    inverse.append(
                        {
                            "op": "delete_label",
                            "label_id": entry.after.get("label_id"),
                            "name": entry.after.get("name"),
                        }
                    )
                case "create_filter":
                    inverse.append(
                        {
                            "op": "delete_filter",
                            "filter_id": entry.after.get("filter_id"),
                        }
                    )
                case "unsubscribe":
                    # Genuinely not reversible — you can't un-unsubscribe. Surfaced
                    # in the undo report so the user knows what wasn't undone.
                    inverse.append(
                        {
                            "op": "noop",
                            "reason": "unsubscribe cannot be reversed",
                            "cluster_key": entry.cluster_key,
                            "endpoint": entry.after.get("endpoint"),
                        }
                    )
        return inverse

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.committed_entries():
            counts[entry.op] = counts.get(entry.op, 0) + 1
        return counts
