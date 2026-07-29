"""User filing rules.

These encode knowledge no model has — that mail from two unrelated-looking companies
is one project. They outrank every model verdict, so their precedence and their match
precision both matter.
"""

from __future__ import annotations

import pytest

from ecs import db, guards
from ecs.cluster import build_clusters
from ecs.filing_rules import FilingRule, first_match, load_rules, preview, rule_labels
from ecs.plan import build_plan

ZEPHYR_RULES = """
[[rule]]
name = "Zephyr project"
label = "Projects/Zephyr"
senders = ["zephyrpools.example.com", "zephyrsupplies.example.com"]
subject_contains = ["zephyr", "pool project"]

[[rule]]
name = "Ana"
label = "People/Ana"
senders = ["ana@example.com"]
"""


@pytest.fixture
def rules_file(tmp_path):
    path = tmp_path / "filing-rules.toml"
    path.write_text(ZEPHYR_RULES)
    return path


class TestLoading:
    def test_absent_file_yields_no_rules(self, tmp_path):
        assert load_rules(tmp_path / "nope.toml") == []

    def test_rules_load_in_file_order(self, rules_file):
        loaded = load_rules(rules_file)
        assert [r.name for r in loaded] == ["Zephyr project", "Ana"]

    def test_labels_are_extracted_for_the_taxonomy(self, rules_file):
        assert rule_labels(load_rules(rules_file)) == [
            "Projects/Zephyr",
            "People/Ana",
        ]

    def test_a_rule_with_no_criteria_is_rejected(self):
        with pytest.raises(ValueError, match="match criterion"):
            FilingRule(name="empty", label="X")

    def test_an_invalid_disposition_is_rejected(self):
        with pytest.raises(ValueError, match="disposition"):
            FilingRule(name="bad", label="X", senders=["a"], disposition="incinerate")


class TestMatching:
    def rule(self, **kw):
        return FilingRule(name="r", label="L", **kw)

    def test_sender_substring_matches_a_domain(self):
        r = self.rule(senders=["zephyrpools.example.com"])
        assert r.matches(
            from_addr="sam@zephyrpools.example.com",
            from_name="Sam", subject="hi", snippet="",
        )

    def test_sender_matches_the_display_name_too(self):
        r = self.rule(senders=["zephyr pools"])
        assert r.matches(
            from_addr="office@example.com", from_name="Zephyr Pools",
            subject="", snippet="",
        )

    def test_short_terms_are_word_bounded(self):
        """'dam' must not drag in Amsterdam, damage or Adam."""
        r = self.rule(subject_contains=["dam"])
        assert r.matches(from_addr="", from_name="", subject="the dam works", snippet="")
        assert not r.matches(
            from_addr="", from_name="", subject="flight to Amsterdam", snippet=""
        )
        assert not r.matches(
            from_addr="", from_name="", subject="water damage report", snippet=""
        )

    def test_long_terms_stay_plain_substrings(self):
        r = self.rule(subject_contains=["zephyr"])
        assert r.matches(
            from_addr="", from_name="", subject="Zephyr Pools quote", snippet=""
        )

    def test_any_is_the_default_so_either_criterion_suffices(self):
        r = self.rule(senders=["nomatch.example"], subject_contains=["zephyr"])
        assert r.matches(
            from_addr="other@x.com", from_name="", subject="Zephyr quote", snippet=""
        )

    def test_all_requires_every_criterion(self):
        r = self.rule(
            senders=["accounts@school.edu"], subject_contains=["fee"], match="all"
        )
        assert r.matches(
            from_addr="accounts@school.edu", from_name="", subject="Fee invoice",
            snippet="",
        )
        # Right sender, wrong subject.
        assert not r.matches(
            from_addr="accounts@school.edu", from_name="", subject="Newsletter",
            snippet="",
        )

    def test_body_preview_is_searched(self):
        r = self.rule(body_contains=["zephyr"])
        assert r.matches(
            from_addr="", from_name="", subject="Fwd: quote",
            snippet="attached is the Zephyr pricing",
        )


class TestFirstMatchWins:
    def test_ordering_expresses_specificity(self, rules_file):
        """Ana's project forwards go to the project; her other mail goes to her folder."""
        loaded = load_rules(rules_file)

        project = first_match(
            loaded, from_addr="ana@example.com", from_name="Ana",
            subject="Fwd: Quote for Zephyr Project", snippet="",
        )
        assert project.label == "Projects/Zephyr"

        personal = first_match(
            loaded, from_addr="ana@example.com", from_name="Ana",
            subject="lunch tomorrow?", snippet="",
        )
        assert personal.label == "People/Ana"

    def test_no_match_returns_none(self, rules_file):
        assert first_match(
            load_rules(rules_file), from_addr="stranger@x.com", from_name="",
            subject="hello", snippet="",
        ) is None


class TestPlanIntegration:
    @pytest.fixture
    def conn(self, tmp_path, monkeypatch, rules_file):
        # Point the loader at the fixture file.
        import ecs.filing_rules as fr

        monkeypatch.setattr(fr, "DEFAULT_PATH", rules_file)
        connection = db.connect(tmp_path / "rules.db")
        yield connection
        connection.close()

    def add(self, conn, mid, addr, subject):
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
            (mid, f"t{mid}", addr, "", sender_domain(addr), "[]", subject,
             normalize_subject(subject), 1_700_000_000, "", 100, "[]", None,
             None, None, 0, 0, 0, 0, 1),
        )

    def _triage(self, conn, disposition):
        for key in [r["key"] for r in conn.execute("SELECT key FROM clusters")]:
            conn.execute(
                """
                INSERT OR REPLACE INTO triage_verdicts(
                    cluster_key, category, disposition, confidence, is_mixed,
                    keep_signals, rationale, model, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (key, "marketing_promo", disposition, 0.95, 0, "[]", "r", "haiku",
                 "2026-01-01T00:00:00Z"),
            )

    def actions(self, conn, mid):
        return {
            (r["action"], r["label"], r["source"])
            for r in conn.execute(
                "SELECT action, label, source FROM plan_actions WHERE message_id = ?",
                (mid,),
            )
        }

    def test_a_rule_beats_a_model_deletion(self, conn):
        """Triage says trash; the filing rule says file it. The rule wins."""
        self.add(conn, "m1", "sam@zephyrpools.example.com", "Pool plan")
        build_clusters(conn)
        guards.evaluate_all(conn)
        self._triage(conn, "trash")
        build_plan(conn)

        acts = self.actions(conn, "m1")
        assert ("trash", None, "user-rule") not in acts
        assert ("add_label", "Projects/Zephyr", "user-rule") in acts

    def test_a_rule_beats_a_challenger_objection(self, conn):
        self.add(conn, "m1", "sam@zephyrpools.example.com", "Pool plan")
        build_clusters(conn)
        guards.evaluate_all(conn)
        self._triage(conn, "trash")
        key = conn.execute("SELECT key FROM clusters").fetchone()["key"]
        conn.execute(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            (key, 1, "model wants this reviewed", "fable", "2026-01-01T00:00:00Z"),
        )
        build_plan(conn)
        # The explicit instruction, not the model's holding bucket.
        assert ("add_label", "Projects/Zephyr", "user-rule") in self.actions(
            conn, "m1"
        )

    def test_a_rule_cannot_delete_a_guard_protected_record(self, conn):
        """An over-broad rule may misfile, but must never lose a receipt."""
        import ecs.filing_rules as fr

        path = fr.DEFAULT_PATH
        path.write_text(
            '[[rule]]\nname = "aggressive"\nlabel = "Junk"\n'
            'disposition = "trash"\nsenders = ["shop.example.com"]\n'
        )
        self.add(conn, "receipt", "sales@shop.example.com",
                 "Payment receipt for your order")
        build_clusters(conn)
        guards.evaluate_all(conn)
        self._triage(conn, "archive")
        build_plan(conn)

        acts = self.actions(conn, "receipt")
        assert not any(action == "trash" for action, _, _ in acts)

    def test_rule_labels_join_the_taxonomy(self, conn):
        self.add(conn, "m1", "sam@zephyrpools.example.com", "Pool plan")
        build_clusters(conn)
        guards.evaluate_all(conn)
        self._triage(conn, "archive")
        build_plan(conn)
        labels = {
            r["label"]
            for r in conn.execute(
                "SELECT DISTINCT label FROM plan_actions WHERE label IS NOT NULL"
            )
        }
        assert "Projects/Zephyr" in labels


class TestPreview:
    def test_counts_and_examples(self, tmp_path, rules_file):
        conn = db.connect(tmp_path / "prev.db")
        from ecs.cluster import normalize_subject

        for i in range(3):
            conn.execute(
                """
                INSERT INTO messages(
                    id, thread_id, from_addr, from_name, from_domain, to_addrs,
                    subject, subject_norm, date_ts, snippet, size_estimate, label_ids,
                    list_id, list_unsubscribe, list_unsubscribe_post, has_attachment,
                    has_calendar_invite, is_starred, is_important, is_unread
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (f"m{i}", f"t{i}", "office@zephyrpools.example.com", "", "x", "[]",
                 f"Pool plan {i}", normalize_subject("Pool plan"), 1, "", 1, "[]",
                 None, None, None, 0, 0, 0, 0, 1),
            )
        results = preview(conn, load_rules(rules_file))
        zephyr = next(r for r in results if r["label"] == "Projects/Zephyr")
        assert zephyr["matches"] == 3
        assert zephyr["distinct_senders"] == 1
        assert len(zephyr["examples"]) == 3
        conn.close()
