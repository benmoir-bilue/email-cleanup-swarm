"""Precedence in the merge layer.

Multiple stages express opinions about the same message and they disagree. The
invariant that matters: every override must move in the safe direction. Nothing in
the chain may turn a "keep" into a "delete".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ecs import db, guards
from ecs.cluster import build_clusters
from ecs.plan import FALLBACK_LABEL, REVIEW_LABEL, build_plan, resolve_label

NOW = datetime.now(UTC).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "plan.db")
    yield connection
    connection.close()


def add_message(conn, mid, *, from_addr="deals@shop.example.com", subject="Sale today",
                snippet="", thread_id=None, list_unsub=None, list_unsub_post=None):
    from ecs.cluster import normalize_subject, sender_domain

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
            mid, thread_id or f"t-{mid}", from_addr, "S", sender_domain(from_addr),
            "[]", subject, normalize_subject(subject), 1_700_000_000, snippet, 100,
            "[]", None, list_unsub, list_unsub_post, 0, 0, 0, 0, 1,
        ),
    )


def set_triage(conn, key, disposition, *, confidence=0.95, category="marketing_promo",
               is_mixed=0, rationale="promotional"):
    conn.execute(
        """
        INSERT OR REPLACE INTO triage_verdicts(
            cluster_key, category, disposition, confidence, is_mixed,
            keep_signals, rationale, model, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (key, category, disposition, confidence, is_mixed, "[]", rationale, "haiku", NOW),
    )


def set_strategy(conn, *, taxonomy, rules=None, category_rules=None, legacy=False):
    """Persist a strategy run.

    `legacy=True` writes the original flat per-cluster list, so the backwards-compat
    reader stays exercised.
    """
    if legacy:
        payload = json.dumps(rules or [])
    else:
        payload = json.dumps(
            {
                "category_rules": category_rules or [],
                "cluster_overrides": rules or [],
            }
        )
    conn.execute(
        """
        INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities,
                                  notes, model, created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (json.dumps(taxonomy), payload, "[]", "[]", "", "opus", NOW),
    )


def actions_for(conn, mid):
    return {
        (r["action"], r["label"], r["source"])
        for r in conn.execute(
            "SELECT action, label, source FROM plan_actions WHERE message_id = ?", (mid,)
        )
    }


def dispositions(conn, mid):
    return {r["action"] for r in conn.execute(
        "SELECT action FROM plan_actions WHERE message_id = ?", (mid,)
    )}


class TestResolveLabel:
    TAXONOMY = [
        "Finance/Receipts", "Finance/Tax", "Travel/Bookings",
        "Services/Notifications", "Reading/Newsletters",
    ]

    def test_matches_guard_category_to_a_model_designed_label(self):
        assert resolve_label(self.TAXONOMY, ["tax"]) == "Finance/Tax"
        assert resolve_label(self.TAXONOMY, ["receipt"]) == "Finance/Receipts"
        assert resolve_label(self.TAXONOMY, ["travel"]) == "Travel/Bookings"

    def test_survives_arbitrary_model_naming(self):
        """Nothing may be hardcoded to specific label names."""
        alt = ["Money/Purchases", "Money/ATO", "Trips/Flights"]
        assert resolve_label(alt, ["receipt"]) == "Money/Purchases"
        assert resolve_label(alt, ["tax"]) == "Money/ATO"

    def test_falls_back_when_nothing_matches(self):
        assert resolve_label(self.TAXONOMY, ["quantum_physics"]) == FALLBACK_LABEL

    def test_empty_taxonomy_falls_back(self):
        assert resolve_label([], ["tax"]) == FALLBACK_LABEL


class TestPrecedence:
    def _promo_cluster(self, conn, n=5):
        for i in range(n):
            add_message(conn, f"m{i}")
        build_clusters(conn)
        guards.evaluate_all(conn)
        return conn.execute("SELECT key FROM clusters").fetchone()["key"]

    def test_triage_is_the_floor(self, conn):
        key = self._promo_cluster(conn)
        set_triage(conn, key, "trash")
        build_plan(conn)
        assert dispositions(conn, "m0") == {"trash"}

    def test_strategy_overrides_triage(self, conn):
        key = self._promo_cluster(conn)
        set_triage(conn, key, "trash")
        set_strategy(
            conn,
            taxonomy=[{"label": "Reading/Newsletters", "purpose": "p"}],
            rules=[{
                "cluster_key": key, "label": "Reading/Newsletters",
                "disposition": "archive", "reason": "worth skimming",
                "filter_worthy": True,
            }],
        )
        build_plan(conn)
        assert actions_for(conn, "m0") == {
            ("add_label", "Reading/Newsletters", "strategy-override"),
            ("archive", None, "strategy-override"),
        }

    def test_challenger_demotes_a_deletion_to_review(self, conn):
        key = self._promo_cluster(conn)
        set_triage(conn, key, "trash")
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 1, "Still billing monthly", "fable", NOW),
        )
        build_plan(conn)

        assert "trash" not in dispositions(conn, "m0")
        assert ("add_label", REVIEW_LABEL, "challenge") in actions_for(conn, "m0")

    def test_challenger_upholding_a_deletion_leaves_it_alone(self, conn):
        key = self._promo_cluster(conn)
        set_triage(conn, key, "trash")
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 0, "Expired promos, safe to remove", "fable", NOW),
        )
        build_plan(conn)
        assert dispositions(conn, "m0") == {"trash"}

    def test_guard_hard_downgrades_a_deletion(self, conn):
        """A keep-signal message inside a trash-bound cluster must survive."""
        add_message(conn, "promo", subject="Flash sale")
        add_message(conn, "receipt", subject="Payment receipt for your order")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "trash")
        set_strategy(
            conn,
            taxonomy=[{"label": "Finance/Receipts", "purpose": "p"}],
            rules=[{
                "cluster_key": key, "label": "", "disposition": "trash",
                "reason": "promotional", "filter_worthy": True,
            }],
        )
        build_plan(conn)

        # The promo goes...
        assert dispositions(conn, "promo") == {"trash"}
        # ...the receipt is downgraded to archive and filed sensibly.
        assert "trash" not in dispositions(conn, "receipt")
        assert ("add_label", "Finance/Receipts", "guard") in actions_for(conn, "receipt")

    def test_escalation_splits_a_mixed_cluster(self, conn):
        add_message(conn, "promo")
        add_message(conn, "order")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "trash", is_mixed=1)
        set_strategy(
            conn,
            taxonomy=[{"label": "Finance/Receipts", "purpose": "p"}],
            rules=[{"cluster_key": key, "label": "", "disposition": "trash",
                    "reason": "mostly promos", "filter_worthy": False}],
        )
        for mid, disp, kind in [("promo", "trash", "promotion"), ("order", "archive", "receipt")]:
            conn.execute(
                """
                INSERT INTO escalations(message_id, disposition, label_hint, entities,
                                        rationale, model, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (mid, disp, kind, json.dumps({"vendor": "Shop", "amount": "$49"}),
                 "r", "haiku", NOW),
            )
        build_plan(conn)

        assert dispositions(conn, "promo") == {"trash"}
        assert ("add_label", "Finance/Receipts", "escalate") in actions_for(conn, "order")

    def test_human_decision_beats_every_model(self, conn):
        key = self._promo_cluster(conn)
        set_triage(conn, key, "trash")
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 1, "model wanted to keep this", "fable", NOW),
        )
        conn.execute(
            "INSERT INTO decisions(cluster_key, disposition, label, unsubscribe, "
            "note, decided_at) VALUES(?,?,?,?,?,?)",
            (key, "trash", None, 0, "I never want this again", NOW),
        )
        build_plan(conn)
        assert dispositions(conn, "m0") == {"trash"}

    def test_guard_survives_even_a_human_delete_ruling(self, conn):
        """The scope can't hard-delete, and the guard exists to survive a hasty click."""
        add_message(conn, "taxdoc", subject="Your notice of assessment")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "archive")
        conn.execute(
            "INSERT INTO decisions(cluster_key, disposition, label, unsubscribe, "
            "note, decided_at) VALUES(?,?,?,?,?,?)",
            (key, "trash", None, 0, "delete it", NOW),
        )
        build_plan(conn)
        assert "trash" not in dispositions(conn, "taxdoc")


class TestConfidenceFloor:
    def test_low_confidence_deletion_is_routed_to_review(self, conn):
        add_message(conn, "m0")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "trash", confidence=0.4)
        build_plan(conn)

        assert "trash" not in dispositions(conn, "m0")
        assert ("add_label", REVIEW_LABEL, "triage") in actions_for(conn, "m0")


class TestUnsubscribeTargets:
    def test_one_click_target_is_extracted_for_unsubscribe_clusters(self, conn):
        add_message(
            conn, "m0",
            list_unsub="<https://shop.example.com/u/abc>, <mailto:u@shop.example.com>",
            list_unsub_post="List-Unsubscribe=One-Click",
        )
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "unsubscribe")
        build_plan(conn)

        row = conn.execute("SELECT * FROM unsub_targets").fetchone()
        assert row["method"] == "one_click"
        assert row["endpoint"] == "https://shop.example.com/u/abc"
        # Unsubscribing also clears the backlog.
        assert dispositions(conn, "m0") == {"trash"}

    def test_challenged_unsubscribe_is_removed_from_the_worklist(self, conn):
        add_message(conn, "m0", list_unsub="<https://shop.example.com/u/abc>")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "unsubscribe")
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 1, "This is an active paid subscription", "fable", NOW),
        )
        build_plan(conn)
        assert conn.execute("SELECT COUNT(*) FROM unsub_targets").fetchone()[0] == 0


class TestUnescalatedMixedClusters:
    """Messages in a mixed cluster that never got an individual verdict.

    The user's requirement is that everything ends up genuinely filed or genuinely
    deleted — nothing parked in an archive limbo. So an unreviewed message inherits its
    cluster's decision rather than defaulting to a holding bucket.
    """

    def _mixed_cluster(self, conn, disposition):
        add_message(conn, "a", subject="Weekend deals")
        add_message(conn, "b", subject="Order shipped")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, disposition, is_mixed=1)
        set_strategy(
            conn,
            taxonomy=[{"label": "Shopping/Orders", "purpose": "p"}],
            rules=[{"cluster_key": key, "label": "Shopping/Orders",
                    "disposition": disposition, "reason": "mixed retailer",
                    "filter_worthy": False}],
        )
        return key

    def test_unescalated_message_inherits_a_trash_decision(self, conn):
        self._mixed_cluster(conn, "trash")
        build_plan(conn)
        # Genuinely deleted, not parked in archive.
        assert dispositions(conn, "a") == {"trash"}

    def test_the_inheritance_is_attributed_so_it_is_auditable(self, conn):
        self._mixed_cluster(conn, "trash")
        build_plan(conn)
        sources = {
            r["source"]
            for r in conn.execute(
                "SELECT source FROM plan_actions WHERE message_id = 'a'"
            )
        }
        assert "cluster-inherited" in sources

    def test_guards_still_protect_an_unescalated_message(self, conn):
        """Inheriting a deletion must not bypass the keep-signal guards."""
        add_message(conn, "promo", subject="Weekend deals")
        add_message(conn, "receipt", subject="Payment receipt for your order")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "trash", is_mixed=1)
        build_plan(conn)

        assert dispositions(conn, "promo") == {"trash"}
        assert "trash" not in dispositions(conn, "receipt")

    def test_an_escalated_message_is_not_marked_inherited(self, conn):
        key = self._mixed_cluster(conn, "trash")
        conn.execute(
            """
            INSERT INTO escalations(message_id, disposition, label_hint, entities,
                                    rationale, model, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            ("a", "archive", "receipt", "{}", "r", "haiku", NOW),
        )
        build_plan(conn)
        sources = {
            r["source"]
            for r in conn.execute(
                "SELECT source FROM plan_actions WHERE message_id = 'a'"
            )
        }
        assert "escalate" in sources
        assert "cluster-inherited" not in sources


class TestNothingIsLeftUnfiled:
    def test_strategy_label_is_used_when_category_matching_fails(self, conn):
        """A retained message must not land in the junk drawer if any label is known."""
        add_message(conn, "m0", subject="Something inscrutable")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        # A taxonomy whose words match no category hint.
        set_triage(conn, key, "archive", category="other")
        set_strategy(
            conn,
            taxonomy=[{"label": "Zzyzx/Widgets", "purpose": "p"}],
            rules=[{"cluster_key": key, "label": "Zzyzx/Widgets",
                    "disposition": "archive", "reason": "r", "filter_worthy": False}],
        )
        build_plan(conn)
        assert ("add_label", "Zzyzx/Widgets", "strategy-override") in actions_for(
            conn, "m0"
        )

    def test_report_surfaces_the_holding_buckets(self, conn):
        add_message(conn, "m0", subject="Unclear thing")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        # Low-confidence trash routes to the review bucket.
        set_triage(conn, key, "trash", confidence=0.3)
        build_plan(conn)

        from ecs.plan import plan_report

        report = plan_report(conn)
        assert report["needs_decision"] == 1
        assert "unsorted" in report
        assert "cluster_inherited" in report


class TestSafetyInvariant:
    def test_no_override_ever_creates_a_deletion(self, conn):
        """The property that makes the whole chain trustworthy."""
        add_message(conn, "keeper", subject="Your tax invoice")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        # Triage says keep; every later stage tries to escalate to deletion.
        set_triage(conn, key, "keep")
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 0, "fine to delete", "fable", NOW),
        )
        build_plan(conn)
        assert "trash" not in dispositions(conn, "keeper")


class TestCategoryRules:
    """Category rules govern the mailbox; overrides handle the exceptions.

    At 1,871 clusters a rule per cluster would exceed the model's output ceiling, so
    the strategist emits ~25 category rules plus a short override list instead.
    """

    def _cluster(self, conn, category):
        add_message(conn, "m0", subject="Something")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, "archive", category=category)
        return key

    def test_category_rule_applies_without_a_per_cluster_entry(self, conn):
        self._cluster(conn, "newsletter")
        set_strategy(
            conn,
            taxonomy=[{"label": "Reading/Newsletters", "purpose": "p"}],
            category_rules=[{
                "category": "newsletter", "label": "Reading/Newsletters",
                "disposition": "archive", "reason": "skim later",
                "filter_worthy": True,
            }],
        )
        build_plan(conn)
        assert ("add_label", "Reading/Newsletters", "strategy-category") in actions_for(
            conn, "m0"
        )

    def test_cluster_override_beats_its_category_rule(self, conn):
        key = self._cluster(conn, "newsletter")
        set_strategy(
            conn,
            taxonomy=[
                {"label": "Reading/Newsletters", "purpose": "p"},
                {"label": "Finance/Statements", "purpose": "p"},
            ],
            category_rules=[{
                "category": "newsletter", "label": "Reading/Newsletters",
                "disposition": "trash", "reason": "bulk", "filter_worthy": True,
            }],
            rules=[{
                "cluster_key": key, "label": "Finance/Statements",
                "disposition": "archive",
                "reason": "this 'newsletter' actually carries statements",
                "filter_worthy": True,
            }],
        )
        build_plan(conn)
        acts = actions_for(conn, "m0")
        assert ("trash", None, "strategy-category") not in acts
        assert ("add_label", "Finance/Statements", "strategy-override") in acts

    def test_category_with_no_rule_falls_back_to_triage(self, conn):
        self._cluster(conn, "spam_phishing")
        set_strategy(
            conn,
            taxonomy=[{"label": "Reading/Newsletters", "purpose": "p"}],
            category_rules=[{
                "category": "newsletter", "label": "Reading/Newsletters",
                "disposition": "trash", "reason": "bulk", "filter_worthy": True,
            }],
        )
        build_plan(conn)
        sources = {
            r["source"]
            for r in conn.execute("SELECT source FROM plan_actions WHERE message_id='m0'")
        }
        assert sources == {"triage"}

    def test_legacy_flat_rule_list_is_still_readable(self, conn):
        """An index built before the restructure must not break."""
        key = self._cluster(conn, "newsletter")
        set_strategy(
            conn,
            taxonomy=[{"label": "Reading/Newsletters", "purpose": "p"}],
            rules=[{
                "cluster_key": key, "label": "Reading/Newsletters",
                "disposition": "archive", "reason": "r", "filter_worthy": True,
            }],
            legacy=True,
        )
        build_plan(conn)
        assert ("add_label", "Reading/Newsletters", "strategy-override") in actions_for(
            conn, "m0"
        )


class TestApprovalsSurviveRebuild:
    """Reviewing 30,000 actions is real work; a plan rebuild must not discard it.

    But an approval must only carry over for an action that is genuinely unchanged —
    if a later stage changes the decision, that action has to be re-reviewed.
    """

    def _setup(self, conn, disposition="trash"):
        add_message(conn, "m0")
        build_clusters(conn)
        guards.evaluate_all(conn)
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        set_triage(conn, key, disposition)
        build_plan(conn)
        return key

    def test_approval_carries_across_an_unchanged_rebuild(self, conn):
        self._setup(conn)
        conn.execute("UPDATE plan_actions SET approved = 1")
        build_plan(conn)
        assert conn.execute(
            "SELECT approved FROM plan_actions WHERE message_id='m0'"
        ).fetchone()["approved"] == 1

    def test_a_changed_decision_loses_its_approval(self, conn):
        """A deletion approved before the challenger ran must not stay approved
        once the challenger demotes it."""
        key = self._setup(conn, "trash")
        conn.execute("UPDATE plan_actions SET approved = 1")

        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 1, "still an active subscription", "fable", NOW),
        )
        build_plan(conn)

        rows = conn.execute(
            "SELECT action, label, approved FROM plan_actions WHERE message_id='m0'"
        ).fetchall()
        # The trash action is gone; the replacement review action is unapproved.
        assert all(r["action"] != "trash" for r in rows)
        assert all(r["approved"] == 0 for r in rows)

    def test_applied_marker_survives_so_work_is_not_repeated(self, conn):
        self._setup(conn)
        conn.execute(
            "UPDATE plan_actions SET approved = 1, applied_at = ?, wave = 1", (NOW,)
        )
        build_plan(conn)
        row = conn.execute(
            "SELECT applied_at, wave FROM plan_actions WHERE message_id='m0'"
        ).fetchone()
        assert row["applied_at"] == NOW
        assert row["wave"] == 1
