"""TUI smoke test via Textual's pilot harness.

Not a UI-design test — it verifies the app mounts against a real database, every tab
renders without raising, and an approval keypress actually persists. A review screen
that crashes on an empty table, or silently fails to save an approval, would be worse
than no review screen at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ecs import config, db, guards
from ecs.cluster import build_clusters
from ecs.plan import build_plan

NOW = datetime.now(UTC).isoformat()


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A small but complete database, with the app pointed at it."""
    path = tmp_path / "tui.db"
    monkeypatch.setattr(config, "DB_PATH", path)

    conn = db.connect(path)
    for i in range(5):
        conn.execute(
            """
            INSERT INTO messages(
                id, thread_id, from_addr, from_name, from_domain, to_addrs, subject,
                subject_norm, date_ts, snippet, size_estimate, label_ids, list_id,
                list_unsubscribe, list_unsubscribe_post, has_attachment,
                has_calendar_invite, is_starred, is_important, is_unread
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"m{i}", f"t{i}", "deals@shop.example.com", "Shop",
                "shop.example.com", "[]", f"Sale number {i}", "sale", 1_700_000_000,
                "", 100, '["INBOX"]', None,
                "<https://shop.example.com/u/x>", "List-Unsubscribe=One-Click",
                0, 0, 0, 0, 1,
            ),
        )
    build_clusters(conn)
    guards.evaluate_all(conn)
    key = conn.execute("SELECT key FROM clusters").fetchone()["key"]

    conn.execute(
        """
        INSERT INTO triage_verdicts(cluster_key, category, disposition, confidence,
            is_mixed, keep_signals, rationale, model, created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (key, "marketing_promo", "unsubscribe", 0.95, 0, "[]", "bulk promo",
         "claude-haiku-4-5", NOW),
    )
    conn.execute(
        """
        INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities, notes,
                                  model, created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            json.dumps([{"label": "Shopping/Promos", "purpose": "deals"}]),
            json.dumps([{"cluster_key": key, "label": "", "disposition": "unsubscribe",
                         "reason": "never opened", "filter_worthy": True}]),
            json.dumps([{"cluster_key": key, "observation": "high volume, zero opens",
                          "recommended_action": "unsubscribe"}]),
            json.dumps([{"cluster_key": key, "question": "Drop this retailer entirely?",
                          "why_uncertain": "you did buy from them once"}]),
            "", "claude-opus-5", NOW,
        ),
    )
    conn.execute(
        "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
        "VALUES(?,?,?,?,?)",
        (key, 0, "Expired offers, safe to remove.", "claude-fable-5", NOW),
    )
    build_plan(conn)
    conn.commit()
    conn.close()
    return path, key


@pytest.mark.asyncio
async def test_app_mounts_and_every_tab_renders(seeded_db):
    from ecs.tui.app import ReviewApp

    app = ReviewApp()
    async with app.run_test() as pilot:
        for tab in ("tab-overview", "tab-unsub", "tab-delete", "tab-taxonomy", "tab-queue"):
            app.query_one("#tabs").active = tab
            await pilot.pause()
        # Reaching here means no tab raised while rendering.
        assert app.query_one("#overview-table").row_count == 1


@pytest.mark.asyncio
async def test_veto_on_the_unsubscribe_tab_persists(seeded_db):
    path, key = seeded_db
    from ecs.tui.app import ReviewApp

    app = ReviewApp()
    async with app.run_test() as pilot:
        app.query_one("#tabs").active = "tab-unsub"
        await pilot.pause()
        await pilot.press("a")  # approve
        await pilot.pause()

    conn = db.connect(path)
    assert conn.execute(
        "SELECT approved FROM unsub_targets WHERE cluster_key = ?", (key,)
    ).fetchone()["approved"] == 1
    conn.close()


@pytest.mark.asyncio
async def test_queue_decision_is_written_immediately(seeded_db):
    """Quitting mid-review must not lose answered questions."""
    path, key = seeded_db
    from ecs.tui.app import ReviewApp

    app = ReviewApp()
    async with app.run_test() as pilot:
        app.query_one("#tabs").active = "tab-queue"
        await pilot.pause()
        await pilot.press("s")  # archive
        await pilot.pause()

    conn = db.connect(path)
    row = conn.execute(
        "SELECT * FROM decisions WHERE cluster_key = ?", (key,)
    ).fetchone()
    assert row is not None
    assert row["disposition"] == "archive"
    conn.close()


@pytest.mark.asyncio
async def test_empty_database_does_not_crash_the_app(tmp_path, monkeypatch):
    """An empty plan is a normal state, not an error."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "empty.db")
    from ecs.tui.app import ReviewApp

    app = ReviewApp()
    async with app.run_test() as pilot:
        for tab in ("tab-overview", "tab-delete", "tab-taxonomy", "tab-queue"):
            app.query_one("#tabs").active = tab
            await pilot.pause()
        assert app.query_one("#overview-table").row_count == 0
