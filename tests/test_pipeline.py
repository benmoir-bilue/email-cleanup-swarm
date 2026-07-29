"""End-to-end pipeline over a synthetic mailbox.

Builds a mailbox with the specific shapes that matter — a big promo list hiding two
receipts, a real correspondent, a government sender, a mixed retailer — then runs
cluster → guards → plan → dry-run and asserts the outcome is what a careful human
would want. Model stages are stubbed with fixtures so this runs free and offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ecs import db, guards
from ecs.apply import approve_non_destructive, dry_run
from ecs.cluster import build_clusters, cluster_report, normalize_subject, sender_domain
from ecs.filters import build_filter_specs
from ecs.plan import build_plan, plan_report

NOW = datetime.now(UTC)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "e2e.db")
    yield connection
    connection.close()


def msg(conn, mid, *, sender, subject, days_ago=30, thread=None, snippet="",
        list_id=None, list_unsub=None, list_unsub_post=None, starred=0, unread=1):
    ts = int((NOW - timedelta(days=days_ago)).timestamp())
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
            mid, thread or f"th-{mid}", sender, sender.split("@")[0],
            sender_domain(sender), "[]", subject, normalize_subject(subject), ts,
            snippet, 2048, json.dumps(["INBOX", "UNREAD"] if unread else ["INBOX"]),
            list_id, list_unsub, list_unsub_post, 0, 0, starred, 0, unread,
        ),
    )


@pytest.fixture
def mailbox(conn):
    """A mailbox with the shapes that actually stress the design."""
    # 1. Large promo list with RFC 8058 one-click... hiding two real receipts.
    for i in range(120):
        msg(conn, f"promo{i}", sender="deals@megashop.example.com",
            subject=f"Flash sale #{i} — up to 70% off!", days_ago=i * 3,
            list_id="deals.megashop.example.com",
            list_unsub="<https://megashop.example.com/u/tok123>",
            list_unsub_post="List-Unsubscribe=One-Click")
    for i in range(2):
        msg(conn, f"shopreceipt{i}", sender="deals@megashop.example.com",
            subject=f"Payment receipt for order {i}", days_ago=200 + i,
            list_id="deals.megashop.example.com",
            snippet="Tax invoice attached. Total $249.00",
            list_unsub="<https://megashop.example.com/u/tok123>",
            list_unsub_post="List-Unsubscribe=One-Click")

    # 2. A real person, in threads that were replied to.
    for i in range(6):
        msg(conn, f"jane{i}", sender="jane@friends.example.com",
            subject=f"Re: dinner on Saturday {i}", thread=f"jt{i}", days_ago=i * 10)
    conn.executemany(
        "INSERT INTO protected_senders(addr, sent_count) VALUES(?,?)",
        [("jane@friends.example.com", 14)],
    )
    conn.executemany(
        "INSERT INTO replied_threads(thread_id) VALUES(?)",
        [(f"jt{i}",) for i in range(4)],
    )

    # 3. Government sender — protected by domain.
    for i in range(3):
        msg(conn, f"ato{i}", sender="noreply@ato.gov.au",
            subject=f"Your notice of assessment {2023 + i}", days_ago=400 - i * 100)

    # 4. Mixed retailer: order updates and marketing under one sender.
    for i in range(20):
        subject = ("Your order has shipped" if i % 2 else "Weekend deals inside")
        msg(conn, f"mixed{i}", sender="hello@mixedco.example.com",
            subject=f"{subject} {i}", days_ago=i * 5,
            list_unsub="<mailto:stop@mixedco.example.com>")

    # 5. Dead newsletter, never opened, mailto-only unsubscribe.
    for i in range(45):
        msg(conn, f"news{i}", sender="digest@newsy.example.com",
            subject=f"Weekly digest — issue {i}", days_ago=i * 7,
            list_id="weekly.newsy.example.com",
            list_unsub="<https://newsy.example.com/unsub?u=9>")

    # 6. Starred one-off.
    msg(conn, "starred1", sender="random@example.com",
        subject="Interesting article you wanted", starred=1)

    build_clusters(conn)
    guards.evaluate_all(conn)
    return conn


def add_triage(conn, key, disposition, **kw):
    conn.execute(
        """
        INSERT OR REPLACE INTO triage_verdicts(
            cluster_key, category, disposition, confidence, is_mixed, keep_signals,
            rationale, model, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            key, kw.get("category", "marketing_promo"), disposition,
            kw.get("confidence", 0.95), kw.get("is_mixed", 0), "[]",
            kw.get("rationale", "classified"), "claude-haiku-4-5",
            NOW.isoformat(),
        ),
    )


def key_for(conn, sender_or_list):
    row = conn.execute(
        "SELECT key FROM clusters WHERE sender_addr = ? OR list_id = ?",
        (sender_or_list, sender_or_list),
    ).fetchone()
    return row["key"] if row else None


class TestClusteringOnRealisticShapes:
    def test_compression_is_substantial(self, mailbox):
        stats = cluster_report(mailbox)
        # ~197 messages collapse to a handful of senders.
        assert stats["messages"] > 190
        assert stats["clusters"] <= 8
        assert stats["compression"] > 20

    def test_list_id_groups_promos_and_receipts_together(self, mailbox):
        key = key_for(mailbox, "deals.megashop.example.com")
        cluster = mailbox.execute(
            "SELECT * FROM clusters WHERE key = ?", (key,)
        ).fetchone()
        assert cluster["message_count"] == 122
        assert cluster["unsub_method"] == "one_click"

    def test_template_subjects_do_not_fragment_the_cluster(self, mailbox):
        """120 subjects each with a different number must not make 120 clusters."""
        assert mailbox.execute(
            "SELECT COUNT(*) FROM clusters WHERE list_id = ?",
            ("deals.megashop.example.com",),
        ).fetchone()[0] == 1


class TestGuardsOnRealisticShapes:
    def test_correspondent_cluster_is_protected_wholesale(self, mailbox):
        cluster = mailbox.execute(
            "SELECT * FROM clusters WHERE sender_addr = ?",
            ("jane@friends.example.com",),
        ).fetchone()
        assert bool(cluster["never_trash"]) is True

    def test_government_domain_is_protected(self, mailbox):
        cluster = mailbox.execute(
            "SELECT * FROM clusters WHERE sender_addr = ?", ("noreply@ato.gov.au",)
        ).fetchone()
        assert bool(cluster["never_trash"]) is True

    def test_promo_cluster_stays_deletable_but_its_receipts_do_not(self, mailbox):
        cluster = mailbox.execute(
            "SELECT * FROM clusters WHERE list_id = ?",
            ("deals.megashop.example.com",),
        ).fetchone()
        assert bool(cluster["never_trash"]) is False

        protected = {
            r["message_id"]
            for r in mailbox.execute(
                "SELECT message_id FROM message_guards WHERE never_trash = 1"
            )
        }
        assert "shopreceipt0" in protected
        assert "shopreceipt1" in protected
        assert "promo0" not in protected


class TestFullPlan:
    @pytest.fixture
    def planned(self, mailbox):
        promo = key_for(mailbox, "deals.megashop.example.com")
        jane = key_for(mailbox, "jane@friends.example.com")
        ato = key_for(mailbox, "noreply@ato.gov.au")
        mixed = key_for(mailbox, "hello@mixedco.example.com")
        news = key_for(mailbox, "weekly.newsy.example.com")

        add_triage(mailbox, promo, "unsubscribe", rationale="pure promotional bulk")
        add_triage(mailbox, jane, "keep", category="personal_correspondence")
        add_triage(mailbox, ato, "archive", category="government_official")
        add_triage(mailbox, mixed, "trash", is_mixed=1, category="marketing_promo")
        add_triage(mailbox, news, "unsubscribe", category="newsletter")

        mailbox.execute(
            """
            INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities,
                                      notes, model, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                json.dumps([
                    {"label": "Finance/Receipts", "purpose": "purchase records"},
                    {"label": "Finance/Tax", "purpose": "ATO and tax records"},
                    {"label": "People/Friends", "purpose": "personal threads"},
                    {"label": "Services/Notifications", "purpose": "order updates"},
                ]),
                json.dumps([
                    {"cluster_key": promo, "label": "", "disposition": "unsubscribe",
                     "reason": "120 unopened promos", "filter_worthy": True},
                    {"cluster_key": jane, "label": "People/Friends",
                     "disposition": "archive", "reason": "real friend",
                     "filter_worthy": True},
                    {"cluster_key": ato, "label": "Finance/Tax",
                     "disposition": "archive", "reason": "tax records",
                     "filter_worthy": True},
                    {"cluster_key": mixed, "label": "Services/Notifications",
                     "disposition": "trash", "reason": "mostly marketing",
                     "filter_worthy": False},
                    {"cluster_key": news, "label": "", "disposition": "unsubscribe",
                     "reason": "45 issues, never opened", "filter_worthy": True},
                ]),
                "[]", "[]", "", "claude-opus-5", NOW.isoformat(),
            ),
        )

        # Fable upholds the promo deletion but challenges the newsletter.
        mailbox.executemany(
            "INSERT INTO challenges(cluster_key, refuted, argument, model, created_at) "
            "VALUES(?,?,?,?,?)",
            [
                (promo, 0, "Expired offers, safe to remove.", "claude-fable-5",
                 NOW.isoformat()),
                (news, 1, "Subject lines suggest paid subscriber content.",
                 "claude-fable-5", NOW.isoformat()),
            ],
        )

        # Escalation splits the mixed retailer.
        for i in range(20):
            disposition = "archive" if i % 2 else "trash"
            kind = "notification" if i % 2 else "promotion"
            mailbox.execute(
                """
                INSERT INTO escalations(message_id, disposition, label_hint, entities,
                                        rationale, model, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (f"mixed{i}", disposition, kind, "{}", "reviewed",
                 "claude-haiku-4-5", NOW.isoformat()),
            )

        build_plan(mailbox)
        return {
            "promo": promo, "jane": jane, "ato": ato, "mixed": mixed, "news": news
        }

    def actions(self, conn, mid):
        return {
            (r["action"], r["label"])
            for r in conn.execute(
                "SELECT action, label FROM plan_actions WHERE message_id = ?", (mid,)
            )
        }

    def test_promo_backlog_is_trashed(self, mailbox, planned):
        assert self.actions(mailbox, "promo0") == {("trash", None)}

    def test_receipts_buried_in_the_promo_list_survive_and_get_filed(
        self, mailbox, planned
    ):
        """The behaviour the whole design exists to produce."""
        acts = self.actions(mailbox, "shopreceipt0")
        assert ("trash", None) not in acts
        assert ("add_label", "Finance/Receipts") in acts
        assert ("archive", None) in acts

    def test_friend_stays_in_the_inbox_when_triage_says_keep(self, mailbox, planned):
        acts = self.actions(mailbox, "jane0")
        assert ("trash", None) not in acts
        # Strategy said archive; it is labelled and archived, not deleted.
        assert ("add_label", "People/Friends") in acts

    def test_tax_mail_is_filed_under_tax(self, mailbox, planned):
        assert ("add_label", "Finance/Tax") in self.actions(mailbox, "ato0")
        assert ("trash", None) not in self.actions(mailbox, "ato0")

    def test_escalation_splits_the_mixed_retailer(self, mailbox, planned):
        # Odd indices were archive/notification, even were trash/promotion.
        assert ("trash", None) in self.actions(mailbox, "mixed0")
        archived = self.actions(mailbox, "mixed1")
        assert ("trash", None) not in archived
        assert ("add_label", "Services/Notifications") in archived

    def test_challenged_newsletter_is_not_deleted(self, mailbox, planned):
        acts = self.actions(mailbox, "news0")
        assert ("trash", None) not in acts
        assert any(label and "Review" in label for _, label in acts)

    def test_challenged_cluster_is_dropped_from_the_unsubscribe_worklist(
        self, mailbox, planned
    ):
        keys = {
            r["cluster_key"] for r in mailbox.execute("SELECT cluster_key FROM unsub_targets")
        }
        assert planned["promo"] in keys
        assert planned["news"] not in keys

    def test_starred_message_is_never_trashed(self, mailbox, planned):
        assert ("trash", None) not in self.actions(mailbox, "starred1")

    def test_inbox_ends_up_essentially_empty(self, mailbox, planned):
        stats = plan_report(mailbox)
        total = db.message_count(mailbox)
        # Only the explicit "keep" cluster stays behind.
        assert stats["messages_left_in_inbox"] < total * 0.1
        assert stats["messages_to_trash"] > 100

    def test_one_click_is_the_chosen_mechanism_where_offered(self, mailbox, planned):
        row = mailbox.execute(
            "SELECT * FROM unsub_targets WHERE cluster_key = ?", (planned["promo"],)
        ).fetchone()
        assert row["method"] == "one_click"


class TestDryRunAndApproval:
    def test_dry_run_reports_without_mutating(self, mailbox):
        add_triage(mailbox, key_for(mailbox, "weekly.newsy.example.com"), "trash")
        build_plan(mailbox)
        stats = dry_run(mailbox)
        assert stats["total_actions"] > 0
        # Nothing approved yet, so an --apply run would do nothing.
        assert dry_run(mailbox, only_approved=True)["total_actions"] == 0

    def test_safe_only_approval_excludes_every_deletion(self, mailbox):
        add_triage(mailbox, key_for(mailbox, "weekly.newsy.example.com"), "trash")
        build_plan(mailbox)
        approve_non_destructive(mailbox)

        approved_actions = {
            r["action"]
            for r in mailbox.execute(
                "SELECT DISTINCT action FROM plan_actions WHERE approved = 1"
            )
        }
        assert "trash" not in approved_actions
        assert approved_actions <= {"add_label", "archive"}


class TestFilterEmission:
    def test_filters_come_only_from_filter_worthy_rules(self, mailbox):
        promo = key_for(mailbox, "deals.megashop.example.com")
        jane = key_for(mailbox, "jane@friends.example.com")
        mailbox.execute(
            """
            INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities,
                                      notes, model, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                json.dumps([{"label": "People/Friends", "purpose": "p"}]),
                json.dumps([
                    {"cluster_key": promo, "label": "", "disposition": "trash",
                     "reason": "promos", "filter_worthy": True},
                    {"cluster_key": jane, "label": "People/Friends",
                     "disposition": "archive", "reason": "friend",
                     "filter_worthy": False},
                ]),
                "[]", "[]", "", "claude-opus-5", NOW.isoformat(),
            ),
        )
        specs = build_filter_specs(mailbox)
        keys = {s.cluster_key for s in specs}
        assert promo in keys
        assert jane not in keys  # not filter_worthy

    def test_guard_protected_clusters_never_get_a_filter(self, mailbox):
        ato = key_for(mailbox, "noreply@ato.gov.au")
        mailbox.execute(
            """
            INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities,
                                      notes, model, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "[]",
                json.dumps([{"cluster_key": ato, "label": "Finance/Tax",
                             "disposition": "archive", "reason": "tax",
                             "filter_worthy": True}]),
                "[]", "[]", "", "claude-opus-5", NOW.isoformat(),
            ),
        )
        assert build_filter_specs(mailbox) == []

    def test_list_id_clusters_filter_on_the_list(self, mailbox):
        news = key_for(mailbox, "weekly.newsy.example.com")
        mailbox.execute(
            """
            INSERT INTO strategy_runs(taxonomy, rules, weak_signals, ambiguities,
                                      notes, model, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "[]",
                json.dumps([{"cluster_key": news, "label": "", "disposition": "trash",
                             "reason": "dead newsletter", "filter_worthy": True}]),
                "[]", "[]", "", "claude-opus-5", NOW.isoformat(),
            ),
        )
        (spec,) = build_filter_specs(mailbox)
        assert spec.criteria == {"query": "list:weekly.newsy.example.com"}
        assert spec.trash is True
