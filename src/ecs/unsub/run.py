"""Orchestrate the unsubscribe worklist across all three tiers.

Order of preference per sender: RFC 8058 one-click, then a browser page, then mailto.
A tier that reports `needs_manual` escalates to the next one rather than giving up —
several senders advertise one-click and then don't implement POST, and the browser
recovers those.

Everything runs behind explicit approval, rate-limited, and journalled. Unsubscribes
are the one genuinely irreversible action in this system, which is why they're the
only stage that never happens as a side effect of anything else.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import config, db
from ..journal import Entry, Journal
from .oneclick import UnsubResult, post_one_click
from .parse import parse_targets


@dataclass
class UnsubRun:
    attempted: int = 0
    done: int = 0
    needs_manual: int = 0
    failed: int = 0
    skipped: int = 0
    manual_list: list[tuple[str, str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.done} unsubscribed"]
        if self.needs_manual:
            parts.append(f"{self.needs_manual} need manual follow-up")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)


def approve_all(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE unsub_targets SET approved = 1 WHERE status = 'pending'"
    )
    return cur.rowcount


def approve_clusters(conn: sqlite3.Connection, keys: list[str]) -> int:
    if not keys:
        return 0
    placeholders = ",".join("?" * len(keys))
    cur = conn.execute(
        f"UPDATE unsub_targets SET approved = 1 WHERE cluster_key IN ({placeholders})",
        keys,
    )
    return cur.rowcount


def list_targets(conn: sqlite3.Connection, *, pending_only: bool = False) -> list[sqlite3.Row]:
    """The unsubscribe list, joined to sender detail for display."""
    where = "WHERE u.status = 'pending'" if pending_only else ""
    return conn.execute(
        f"""
        SELECT u.*, c.display_name, c.message_count, c.unread_count, c.sender_domain
        FROM unsub_targets u
        LEFT JOIN clusters c ON c.key = u.cluster_key
        {where}
        ORDER BY c.message_count DESC
        """
    ).fetchall()


def _record(
    conn: sqlite3.Connection, cluster_key: str, result: UnsubResult, *, evidence: str | None
) -> None:
    conn.execute(
        """
        UPDATE unsub_targets
        SET status = ?, attempts = attempts + 1, error = ?, evidence_path = ?,
            updated_at = ?
        WHERE cluster_key = ?
        """,
        (
            result.status,
            None if result.ok else result.detail,
            evidence,
            datetime.now(UTC).isoformat(),
            cluster_key,
        ),
    )


def run_unsubscribes(
    *,
    limit: int | None = None,
    one_click_only: bool = False,
    use_browser: bool = True,
    headed: bool = True,
    dry_run: bool = False,
    progress=None,
) -> UnsubRun:
    """Work through the approved unsubscribe list."""
    emit = progress or (lambda _: None)
    run = UnsubRun()
    journal = Journal()

    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT u.*, c.display_name
            FROM unsub_targets u
            LEFT JOIN clusters c ON c.key = u.cluster_key
            WHERE u.approved = 1 AND u.status IN ('pending', 'failed')
            ORDER BY c.message_count DESC
            """
        ).fetchall()
        # All available mechanisms per cluster, so a failed tier can escalate.
        header_by_cluster = {
            r["cluster_key"]: (r["list_unsubscribe"], r["list_unsubscribe_post"])
            for r in conn.execute(
                """
                SELECT m.cluster_key, m.list_unsubscribe, m.list_unsubscribe_post
                FROM messages m
                JOIN (
                    SELECT cluster_key, MAX(date_ts) AS newest
                    FROM messages WHERE list_unsubscribe IS NOT NULL
                    GROUP BY cluster_key
                ) latest
                  ON latest.cluster_key = m.cluster_key AND latest.newest = m.date_ts
                """
            )
        }

    if limit:
        rows = rows[:limit]

    if not rows:
        emit("no approved unsubscribe targets")
        return run

    if dry_run:
        for row in rows:
            emit(f"[dry run] would unsubscribe {row['display_name']} via {row['method']}")
        run.skipped = len(rows)
        return run

    browser = None
    browser_ctx = None
    try:
        for index, row in enumerate(rows, start=1):
            cluster_key = row["cluster_key"]
            name = row["display_name"] or cluster_key
            headers = header_by_cluster.get(cluster_key, (None, None))
            targets = parse_targets(*headers) or []

            if one_click_only:
                targets = [t for t in targets if t.method == "one_click"]
            if not use_browser:
                targets = [t for t in targets if t.method != "http"]

            if not targets:
                run.skipped += 1
                emit(f"[{index}/{len(rows)}] {name}: no usable mechanism, skipped")
                continue

            result: UnsubResult | None = None
            evidence: str | None = None

            for target in targets:
                emit(f"[{index}/{len(rows)}] {name} via {target.method}")

                if target.method == "one_click":
                    result = post_one_click(target.endpoint)
                elif target.method == "http":
                    if browser is None:
                        from .browser import BrowserUnsubscriber

                        browser_ctx = BrowserUnsubscriber(headed=headed)
                        browser = browser_ctx.__enter__()
                    result = browser.unsubscribe(cluster_key, target.endpoint)
                    if "evidence:" in result.detail:
                        evidence = result.detail.split("evidence:")[-1].strip(" )")
                elif target.method == "mailto":
                    from .mailto import send_mailto_unsubscribe

                    result = send_mailto_unsubscribe(target.endpoint)

                if result and result.ok:
                    break
                # needs_manual on this tier means "try the next mechanism".
                emit(f"    {result.status}: {result.detail}" if result else "    no result")

            run.attempted += 1
            result = result or UnsubResult(False, "failed", "no mechanism succeeded")

            entry = Entry(
                op="unsubscribe",
                cluster_key=cluster_key,
                after={
                    "method": result.status,
                    "endpoint": targets[0].endpoint if targets else "",
                    "detail": result.detail,
                },
            )
            journal.record(entry)
            journal.commit(entry, error=None if result.ok else result.detail)

            with db.session() as conn:
                _record(conn, cluster_key, result, evidence=evidence)

            if result.ok:
                run.done += 1
            elif result.status == "needs_manual":
                run.needs_manual += 1
                run.manual_list.append(
                    (name, targets[0].endpoint if targets else "", result.detail)
                )
            else:
                run.failed += 1

            # Politeness delay. Rapid-fire requests across many senders look like an
            # attack and get rate-limited or blocked.
            if index < len(rows):
                time.sleep(config.TUNABLES.unsub_delay_seconds)
    finally:
        if browser_ctx is not None:
            browser_ctx.__exit__(None, None, None)

    return run


def unsub_report(conn: sqlite3.Connection) -> dict[str, object]:
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM unsub_targets GROUP BY status"
        )
    }
    by_method = {
        r["method"]: r["n"]
        for r in conn.execute(
            "SELECT method, COUNT(*) AS n FROM unsub_targets GROUP BY method"
        )
    }
    messages = conn.execute(
        """
        SELECT COALESCE(SUM(c.message_count), 0)
        FROM unsub_targets u JOIN clusters c ON c.key = u.cluster_key
        """
    ).fetchone()[0]
    manual = [
        (r["display_name"] or r["cluster_key"], r["endpoint"], r["error"])
        for r in conn.execute(
            """
            SELECT u.cluster_key, u.endpoint, u.error, c.display_name
            FROM unsub_targets u LEFT JOIN clusters c ON c.key = u.cluster_key
            WHERE u.status = 'needs_manual'
            ORDER BY c.message_count DESC
            """
        )
    ]
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_method": by_method,
        "messages_covered": messages,
        "needs_manual": manual,
        "approved": conn.execute(
            "SELECT COUNT(*) FROM unsub_targets WHERE approved = 1"
        ).fetchone()[0],
    }
