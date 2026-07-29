"""SQLite index for the mailbox and every stage's output.

One file holds the whole run: raw message metadata, deterministic clusters, each
model's verdicts, human decisions, and the resulting action plan. Keeping it all
in one place means any stage can be re-run without redoing the ones before it,
and the TUI has a single source of truth to render.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Raw inbox metadata. One row per message. No bodies stored here.
CREATE TABLE IF NOT EXISTS messages (
    id                      TEXT PRIMARY KEY,
    thread_id               TEXT NOT NULL,
    from_addr               TEXT,
    from_name               TEXT,
    from_domain             TEXT,
    to_addrs                TEXT,          -- json array
    subject                 TEXT,
    subject_norm            TEXT,          -- normalized signature for clustering
    date_ts                 INTEGER,       -- epoch seconds, from internalDate
    snippet                 TEXT,
    size_estimate           INTEGER,
    label_ids               TEXT,          -- json array, as-fetched
    list_id                 TEXT,
    list_unsubscribe        TEXT,
    list_unsubscribe_post   TEXT,
    has_attachment          INTEGER NOT NULL DEFAULT 0,
    has_calendar_invite     INTEGER NOT NULL DEFAULT 0,
    is_starred              INTEGER NOT NULL DEFAULT 0,
    is_important            INTEGER NOT NULL DEFAULT 0,
    is_unread               INTEGER NOT NULL DEFAULT 0,
    cluster_key             TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_cluster ON messages(cluster_key);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_ts);

-- Message bodies, fetched only for escalated messages. Separate table so the
-- main index stays small and a body fetch is obviously opt-in.
CREATE TABLE IF NOT EXISTS bodies (
    message_id  TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    text        TEXT,
    fetched_at  TEXT NOT NULL
);

-- Deterministic clusters. Produced by code, never by a model.
CREATE TABLE IF NOT EXISTS clusters (
    key             TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,     -- list_id | sender | domain_subject
    display_name    TEXT NOT NULL,
    sender_addr     TEXT,
    sender_domain   TEXT,
    list_id         TEXT,
    message_count   INTEGER NOT NULL,
    unread_count    INTEGER NOT NULL DEFAULT 0,
    first_ts        INTEGER,
    last_ts         INTEGER,
    has_unsub       INTEGER NOT NULL DEFAULT 0,
    unsub_method    TEXT,              -- one_click | http | mailto | none
    guard_flags     TEXT,              -- json array of triggered guard names
    never_trash     INTEGER NOT NULL DEFAULT 0,
    sample_subjects TEXT               -- json array
);

-- Addresses you've emailed. Derived read-only from the Sent folder; these are
-- real relationships and are never deletion candidates.
CREATE TABLE IF NOT EXISTS protected_senders (
    addr        TEXT PRIMARY KEY,
    sent_count  INTEGER NOT NULL DEFAULT 1
);

-- Threads you've replied to, harvested from the Sent folder. A reply is the
-- single strongest signal that a thread matters to you.
CREATE TABLE IF NOT EXISTS replied_threads (
    thread_id  TEXT PRIMARY KEY
);

-- Per-message guard results. Computed by code before any model runs; a message
-- flagged never_trash here cannot be deleted no matter what a model concludes.
CREATE TABLE IF NOT EXISTS message_guards (
    message_id   TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    never_trash  INTEGER NOT NULL DEFAULT 0,
    flags        TEXT NOT NULL,   -- json array of guard names that fired
    categories   TEXT NOT NULL    -- json array of keep-signal categories hit
);
CREATE INDEX IF NOT EXISTS idx_guards_never ON message_guards(never_trash);

-- Labels that already exist in the mailbox, so a new taxonomy doesn't collide.
CREATE TABLE IF NOT EXISTS existing_labels (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    type  TEXT
);

-- Stage 5: Haiku per-cluster classification.
CREATE TABLE IF NOT EXISTS triage_verdicts (
    cluster_key   TEXT PRIMARY KEY REFERENCES clusters(key) ON DELETE CASCADE,
    category      TEXT NOT NULL,
    disposition   TEXT NOT NULL,   -- keep | archive | trash | unsubscribe
    confidence    REAL NOT NULL,
    is_mixed      INTEGER NOT NULL DEFAULT 0,
    keep_signals  TEXT,            -- json array
    rationale     TEXT,
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Stage 6: Opus strategic output. One row per run, newest wins.
CREATE TABLE IF NOT EXISTS strategy_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taxonomy    TEXT NOT NULL,   -- json
    rules       TEXT NOT NULL,   -- json
    weak_signals TEXT NOT NULL,  -- json
    ambiguities TEXT NOT NULL,   -- json
    notes       TEXT,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Stage 7: Fable adversarial pass over proposed deletions.
CREATE TABLE IF NOT EXISTS challenges (
    cluster_key TEXT PRIMARY KEY REFERENCES clusters(key) ON DELETE CASCADE,
    refuted     INTEGER NOT NULL,   -- 1 = deletion successfully challenged
    argument    TEXT,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Stage 8: per-message escalation for mixed clusters.
CREATE TABLE IF NOT EXISTS escalations (
    message_id  TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    disposition TEXT NOT NULL,
    label_hint  TEXT,
    entities    TEXT,            -- json object (vendor, amount, date, ...)
    rationale   TEXT,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Human rulings from the TUI ambiguity queue. Outranks every model.
CREATE TABLE IF NOT EXISTS decisions (
    cluster_key TEXT PRIMARY KEY REFERENCES clusters(key) ON DELETE CASCADE,
    disposition TEXT NOT NULL,
    label       TEXT,
    unsubscribe INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    decided_at  TEXT NOT NULL
);

-- The merged, reviewable plan. One row per message per action.
CREATE TABLE IF NOT EXISTS plan_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    cluster_key TEXT,
    action      TEXT NOT NULL,   -- add_label | archive | trash
    label       TEXT,
    reason      TEXT NOT NULL,
    source      TEXT NOT NULL,   -- guard | triage | strategy | challenge | escalate | human
    approved    INTEGER NOT NULL DEFAULT 0,
    applied_at  TEXT,
    wave        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_plan_message ON plan_actions(message_id);
CREATE INDEX IF NOT EXISTS idx_plan_cluster ON plan_actions(cluster_key);
CREATE INDEX IF NOT EXISTS idx_plan_pending ON plan_actions(approved, applied_at);

-- Unsubscribe worklist, one row per cluster with a usable mechanism.
CREATE TABLE IF NOT EXISTS unsub_targets (
    cluster_key   TEXT PRIMARY KEY REFERENCES clusters(key) ON DELETE CASCADE,
    method        TEXT NOT NULL,   -- one_click | http | mailto
    endpoint      TEXT NOT NULL,
    approved      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
                  -- pending | done | failed | needs_manual | skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    evidence_path TEXT,
    error         TEXT,
    updated_at    TEXT
);

-- Resumability and misc run state: page tokens, batch ids, stage completion.
CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the index, creating and migrating it if needed."""
    config.ensure_dirs()
    target = path or config.DB_PATH
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection. Commits on clean exit, rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# kv helpers — used for page tokens, batch ids, and stage watermarks
# ---------------------------------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# Convenience accessors the later stages and the TUI share
# ---------------------------------------------------------------------------


def message_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]


def cluster_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM clusters").fetchone()["n"]


def protected_addrs(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT addr FROM protected_senders").fetchall()
    return {r["addr"] for r in rows}


def iter_clusters(
    conn: sqlite3.Connection, *, order_by: str = "message_count DESC"
) -> Iterator[sqlite3.Row]:
    # order_by is caller-controlled and never user input; kept as a literal so
    # callers can sort without a second query path.
    yield from conn.execute(f"SELECT * FROM clusters ORDER BY {order_by}")


def upsert_many(
    conn: sqlite3.Connection, table: str, columns: list[str], rows: Iterable[tuple]
) -> int:
    """Insert-or-replace a batch. Returns the number of rows written."""
    placeholders = ", ".join("?" for _ in columns)
    collist = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table}({collist}) VALUES({placeholders})"
    cur = conn.executemany(sql, rows)
    return cur.rowcount


def reset_stage(conn: sqlite3.Connection, stage: str) -> None:
    """Wipe one stage's output so it can be re-run cleanly."""
    tables = {
        "cluster": ["clusters"],
        "guards": ["message_guards"],
        "triage": ["triage_verdicts"],
        "strategy": ["strategy_runs"],
        "challenge": ["challenges"],
        "escalate": ["escalations"],
        "plan": ["plan_actions"],
        "unsub": ["unsub_targets"],
    }
    if stage not in tables:
        raise ValueError(f"unknown stage: {stage}")
    for table in tables[stage]:
        conn.execute(f"DELETE FROM {table}")
    if stage == "cluster":
        conn.execute("UPDATE messages SET cluster_key = NULL")
