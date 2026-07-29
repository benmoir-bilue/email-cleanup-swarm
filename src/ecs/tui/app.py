"""The review interface.

Five tabs over the same plan, because the decisions have genuinely different shapes:

  * **Overview** — every cluster, sortable, to get oriented.
  * **Unsubscribe** — the list you asked for, pre-approved as requested, with
    per-row veto.
  * **Delete plan** — grouped by reason, showing Fable's rebuttal inline wherever it
    argued against a deletion. This is the screen where trust is won or lost, so the
    counter-argument is shown next to the proposal rather than hidden a keypress away.
  * **Taxonomy** — the label tree Opus designed, before anything is created.
  * **Queue** — the ambiguity queue, one decision at a time, keyboard-driven.

Approval state is written straight to SQLite on each keypress, so quitting halfway
loses nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
)

from .. import db
from ..agents.strategist import latest_strategy

NOW = lambda: datetime.now(UTC).isoformat()  # noqa: E731


def _fmt_month(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")


class ReviewApp(App):
    """Review and approve the plan before anything touches Gmail."""

    CSS_PATH = "app.css"
    TITLE = "Email Cleanup Swarm"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "approve", "Approve row"),
        Binding("r", "reject", "Reject row"),
        Binding("A", "approve_all_tab", "Approve all in tab"),
        Binding("k", "decide_keep", "Queue: keep"),
        Binding("x", "decide_trash", "Queue: trash"),
        Binding("s", "decide_archive", "Queue: archive"),
        Binding("j", "queue_next", "Queue: skip"),
        Binding("?", "help_panel", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.conn = db.connect()
        self.queue: list[dict] = []
        self.queue_index = 0

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="tabs"):
            with TabPane("Overview", id="tab-overview"):
                yield Static(id="overview-summary", classes="summary")
                yield DataTable(id="overview-table", cursor_type="row")
            with TabPane("Unsubscribe", id="tab-unsub"):
                yield Static(id="unsub-summary", classes="summary")
                yield DataTable(id="unsub-table", cursor_type="row")
            with TabPane("Delete plan", id="tab-delete"):
                yield Static(id="delete-summary", classes="summary")
                yield DataTable(id="delete-table", cursor_type="row")
                yield Static(id="delete-detail", classes="detail")
            with TabPane("Taxonomy", id="tab-taxonomy"):
                yield Static(id="taxonomy-summary", classes="summary")
                yield DataTable(id="taxonomy-table", cursor_type="row")
            with TabPane("Queue", id="tab-queue"):
                yield Static(id="queue-progress")
                yield Vertical(Static(id="queue-body"))
        yield Footer()

    def on_mount(self) -> None:
        self._load_overview()
        self._load_unsub()
        self._load_delete()
        self._load_taxonomy()
        self._load_queue()

    # -- Overview ---------------------------------------------------------

    def _load_overview(self) -> None:
        table = self.query_one("#overview-table", DataTable)
        table.clear(columns=True)
        table.add_columns("sender", "msgs", "span", "category", "plan", "label", "source")

        rows = self.conn.execute(
            """
            SELECT c.key, c.display_name, c.message_count, c.first_ts, c.last_ts,
                   c.never_trash, v.category,
                   (SELECT action FROM plan_actions p
                     WHERE p.cluster_key = c.key
                     ORDER BY CASE action WHEN 'trash' THEN 0 ELSE 1 END LIMIT 1) AS action,
                   (SELECT label FROM plan_actions p
                     WHERE p.cluster_key = c.key AND label IS NOT NULL LIMIT 1) AS label,
                   (SELECT source FROM plan_actions p
                     WHERE p.cluster_key = c.key LIMIT 1) AS source
            FROM clusters c
            LEFT JOIN triage_verdicts v ON v.cluster_key = c.key
            ORDER BY c.message_count DESC
            """
        ).fetchall()

        for r in rows:
            action = r["action"] or "-"
            marker = "🔒 " if r["never_trash"] else ""
            table.add_row(
                marker + (r["display_name"] or "")[:48],
                f"{r['message_count']:,}",
                f"{_fmt_month(r['first_ts'])}..{_fmt_month(r['last_ts'])}",
                r["category"] or "-",
                action,
                (r["label"] or "-")[:26],
                r["source"] or "-",
                key=r["key"],
            )

        total = db.message_count(self.conn)
        trash = self.conn.execute(
            "SELECT COUNT(DISTINCT message_id) FROM plan_actions WHERE action='trash'"
        ).fetchone()[0]
        archive = self.conn.execute(
            "SELECT COUNT(DISTINCT message_id) FROM plan_actions WHERE action='archive'"
        ).fetchone()[0]
        protected = self.conn.execute(
            "SELECT COUNT(*) FROM message_guards WHERE never_trash=1"
        ).fetchone()[0]
        self.query_one("#overview-summary", Static).update(
            f"{len(rows):,} clusters over {total:,} inbox messages  •  "
            f"archive {archive:,}  •  trash {trash:,}  •  "
            f"{protected:,} messages guard-protected (🔒 = whole sender protected)\n"
            "Nothing is applied from this screen. Review the other tabs, then run "
            "[b]ecs apply[/b]."
        )

    # -- Unsubscribe ------------------------------------------------------

    def _load_unsub(self) -> None:
        table = self.query_one("#unsub-table", DataTable)
        table.clear(columns=True)
        table.add_columns("✓", "sender", "msgs", "unread", "method", "status")

        rows = self.conn.execute(
            """
            SELECT u.*, c.display_name, c.message_count, c.unread_count
            FROM unsub_targets u LEFT JOIN clusters c ON c.key = u.cluster_key
            ORDER BY c.message_count DESC
            """
        ).fetchall()

        for r in rows:
            table.add_row(
                "[green]yes[/green]" if r["approved"] else "[red]no[/red]",
                (r["display_name"] or r["cluster_key"])[:46],
                f"{r['message_count'] or 0:,}",
                f"{r['unread_count'] or 0:,}",
                r["method"],
                r["status"],
                key=r["cluster_key"],
            )

        approved = sum(1 for r in rows if r["approved"])
        covered = sum(r["message_count"] or 0 for r in rows)
        self.query_one("#unsub-summary", Static).update(
            f"{len(rows):,} senders can be unsubscribed, covering {covered:,} messages. "
            f"[green]{approved:,} approved[/green].\n"
            "Pre-approved as requested — press [b]r[/b] to veto a row, [b]a[/b] to "
            "re-approve, [b]A[/b] to approve all. Then run [b]ecs unsub --approve[/b]."
        )

    # -- Delete plan ------------------------------------------------------

    def _load_delete(self) -> None:
        table = self.query_one("#delete-table", DataTable)
        table.clear(columns=True)
        table.add_columns("✓", "sender", "msgs", "reason", "challenged")

        rows = self.conn.execute(
            """
            SELECT p.cluster_key, c.display_name, COUNT(*) AS n,
                   MIN(p.reason) AS reason, MIN(p.approved) AS approved,
                   ch.refuted, ch.argument
            FROM plan_actions p
            LEFT JOIN clusters c ON c.key = p.cluster_key
            LEFT JOIN challenges ch ON ch.cluster_key = p.cluster_key
            WHERE p.action = 'trash'
            GROUP BY p.cluster_key
            ORDER BY n DESC
            """
        ).fetchall()

        self._delete_detail = {}
        for r in rows:
            key = r["cluster_key"] or "?"
            table.add_row(
                "[green]yes[/green]" if r["approved"] else "[red]no[/red]",
                (r["display_name"] or key)[:44],
                f"{r['n']:,}",
                (r["reason"] or "")[:40],
                "upheld" if r["refuted"] == 0 else "-",
                key=key,
            )
            self._delete_detail[key] = {
                "name": r["display_name"] or key,
                "reason": r["reason"] or "",
                "argument": r["argument"] or "",
                "refuted": r["refuted"],
                "n": r["n"],
            }

        total = sum(r["n"] for r in rows)
        rescued = self.conn.execute(
            """
            SELECT COALESCE(SUM(c.message_count),0) FROM challenges ch
            JOIN clusters c ON c.key = ch.cluster_key WHERE ch.refuted = 1
            """
        ).fetchone()[0]
        self.query_one("#delete-summary", Static).update(
            f"{total:,} messages proposed for Trash across {len(rows):,} senders. "
            f"The adversarial pass already rescued {rescued:,} messages.\n"
            "Everything here goes to Gmail Trash (recoverable for 30 days), never "
            "permanently deleted. [b]a[/b]/[b]r[/b] to approve/veto, [b]A[/b] for all."
        )
        self._show_delete_detail()

    def _show_delete_detail(self) -> None:
        table = self.query_one("#delete-table", DataTable)
        panel = self.query_one("#delete-detail", Static)
        if not table.row_count:
            panel.update("Nothing proposed for deletion.")
            return
        try:
            key = table.get_row_at(table.cursor_row)
            row_key = list(self._delete_detail)[table.cursor_row]
        except Exception:
            return
        info = self._delete_detail.get(row_key)
        if not info:
            return
        text = [
            f"[b]{info['name']}[/b] — {info['n']:,} messages",
            "",
            f"[b]Why it's proposed:[/b] {info['reason']}",
        ]
        if info["argument"]:
            verdict = (
                "[red]challenged — demoted to review[/red]"
                if info["refuted"]
                else "[green]deletion upheld[/green]"
            )
            text += ["", f"[b]Adversarial review[/b] ({verdict}):", info["argument"]]
        panel.update("\n".join(text))

    def on_data_table_row_highlighted(self, event) -> None:
        if event.data_table.id == "delete-table":
            self._show_delete_detail()

    # -- Taxonomy ---------------------------------------------------------

    def _load_taxonomy(self) -> None:
        table = self.query_one("#taxonomy-table", DataTable)
        table.clear(columns=True)
        table.add_columns("label", "messages", "purpose")

        strategy = latest_strategy(self.conn)
        if not strategy:
            self.query_one("#taxonomy-summary", Static).update(
                "No strategy run yet. Run [b]ecs analyze --stage strategy[/b]."
            )
            return

        counts = {
            r["label"]: r["n"]
            for r in self.conn.execute(
                "SELECT label, COUNT(*) AS n FROM plan_actions "
                "WHERE label IS NOT NULL GROUP BY label"
            )
        }
        for entry in strategy["taxonomy"]:
            label = entry.get("label", "")
            table.add_row(
                label, f"{counts.get(label, 0):,}", (entry.get("purpose") or "")[:60]
            )

        weak = strategy["weak_signals"]
        lines = [
            f"{len(strategy['taxonomy'])} labels designed by {strategy['model']} "
            f"for this specific mailbox."
        ]
        if weak:
            lines.append(f"\n[b]{len(weak)} weak signals found:[/b]")
            for signal in weak[:6]:
                lines.append(f"  • {signal.get('observation', '')[:150]}")
        self.query_one("#taxonomy-summary", Static).update("\n".join(lines))

    # -- Ambiguity queue --------------------------------------------------

    def _load_queue(self) -> None:
        strategy = latest_strategy(self.conn)
        ambiguities = strategy["ambiguities"] if strategy else []
        decided = {
            r["cluster_key"]
            for r in self.conn.execute("SELECT cluster_key FROM decisions")
        }
        self.queue = [
            a for a in ambiguities if a.get("cluster_key") not in decided
        ]
        self.queue_index = 0
        self._render_queue()

    def _render_queue(self) -> None:
        body = self.query_one("#queue-body", Static)
        progress = self.query_one("#queue-progress", Static)

        if not self.queue:
            progress.update("")
            body.update(
                "[green]Queue empty.[/green] Either nothing was ambiguous, or every "
                "question has been answered."
            )
            return

        if self.queue_index >= len(self.queue):
            progress.update("")
            body.update("[green]All questions answered.[/green]")
            return

        item = self.queue[self.queue_index]
        key = item.get("cluster_key", "")
        cluster = self.conn.execute(
            "SELECT * FROM clusters WHERE key = ?", (key,)
        ).fetchone()

        progress.update(
            f"Question {self.queue_index + 1} of {len(self.queue)}"
        )

        lines = [f"[b]{item.get('question', '(no question)')}[/b]", ""]
        if cluster:
            subjects = json.loads(cluster["sample_subjects"] or "[]")
            lines += [
                f"Sender: {cluster['display_name']}",
                f"Messages: {cluster['message_count']:,} "
                f"({cluster['unread_count']:,} never opened)",
                f"Span: {_fmt_month(cluster['first_ts'])} to "
                f"{_fmt_month(cluster['last_ts'])}",
                "",
                "Sample subjects:",
            ]
            lines += [f"  • {s[:100]}" for s in subjects[:6]]
        lines += [
            "",
            f"[dim]Why this needs you: {item.get('why_uncertain', '')}[/dim]",
            "",
            "[b]k[/b] keep in inbox   [b]s[/b] archive with label   "
            "[b]x[/b] move to Trash   [b]j[/b] skip",
        ]
        body.update("\n".join(lines))

    def _record_decision(self, disposition: str) -> None:
        if not self.queue or self.queue_index >= len(self.queue):
            return
        item = self.queue[self.queue_index]
        key = item.get("cluster_key", "")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO decisions(
                cluster_key, disposition, label, unsubscribe, note, decided_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (key, disposition, None, 0, "decided in review queue", NOW()),
        )
        self.conn.commit()
        self.notify(f"recorded: {disposition}", timeout=2)
        self.queue_index += 1
        self._render_queue()

    # -- actions ----------------------------------------------------------

    @property
    def _active_tab(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def _set_approval(self, approved: bool) -> None:
        tab = self._active_tab
        if tab == "tab-unsub":
            table = self.query_one("#unsub-table", DataTable)
            if not table.row_count:
                return
            key = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
            self.conn.execute(
                "UPDATE unsub_targets SET approved = ? WHERE cluster_key = ?",
                (1 if approved else 0, key),
            )
            self.conn.commit()
            self._load_unsub()
            table.move_cursor(row=min(table.cursor_row, table.row_count - 1))
        elif tab == "tab-delete":
            table = self.query_one("#delete-table", DataTable)
            if not table.row_count:
                return
            key = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
            self.conn.execute(
                "UPDATE plan_actions SET approved = ? "
                "WHERE cluster_key = ? AND action = 'trash'",
                (1 if approved else 0, key),
            )
            self.conn.commit()
            self._load_delete()
            table.move_cursor(row=min(table.cursor_row, table.row_count - 1))
        else:
            self.notify("nothing to approve on this tab", severity="warning", timeout=2)

    def action_approve(self) -> None:
        self._set_approval(True)

    def action_reject(self) -> None:
        self._set_approval(False)

    def action_approve_all_tab(self) -> None:
        tab = self._active_tab
        if tab == "tab-unsub":
            n = self.conn.execute(
                "UPDATE unsub_targets SET approved = 1"
            ).rowcount
            self.conn.commit()
            self._load_unsub()
            self.notify(f"approved {n} unsubscribes", timeout=3)
        elif tab == "tab-delete":
            n = self.conn.execute(
                "UPDATE plan_actions SET approved = 1 WHERE action = 'trash'"
            ).rowcount
            self.conn.commit()
            self._load_delete()
            self.notify(f"approved {n} delete actions", timeout=3)
        elif tab == "tab-taxonomy":
            n = self.conn.execute(
                "UPDATE plan_actions SET approved = 1 "
                "WHERE action IN ('add_label','archive')"
            ).rowcount
            self.conn.commit()
            self.notify(f"approved {n} label/archive actions", timeout=3)
        else:
            self.notify("nothing to approve on this tab", severity="warning", timeout=2)

    def action_decide_keep(self) -> None:
        self._record_decision("keep")

    def action_decide_archive(self) -> None:
        self._record_decision("archive")

    def action_decide_trash(self) -> None:
        self._record_decision("trash")

    def action_queue_next(self) -> None:
        if self.queue:
            self.queue_index += 1
            self._render_queue()

    def action_help_panel(self) -> None:
        self.notify(
            "a/r approve or veto the highlighted row · A approve everything on this "
            "tab · k/s/x answer a queue question · q quit. "
            "Approvals save immediately.",
            timeout=8,
        )

    def on_unmount(self) -> None:
        self.conn.close()


def run() -> None:
    ReviewApp().run()
