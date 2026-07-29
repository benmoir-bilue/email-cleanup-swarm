"""Guards are the safety backbone. If these pass, mail cannot be lost by accident.

These run against a real SQLite database rather than mocks, because the cluster
roll-up logic is where the interesting behaviour lives.
"""

from __future__ import annotations

import json

import pytest

from ecs import db, guards
from ecs.cluster import build_clusters


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def add_message(
    conn,
    *,
    mid: str,
    thread_id: str = "t1",
    from_addr: str = "noreply@promo.example.com",
    from_domain: str | None = None,
    subject: str = "Big sale this weekend",
    snippet: str = "Shop now and save",
    starred: int = 0,
    important: int = 0,
    attachment: int = 0,
    invite: int = 0,
    list_id: str | None = None,
):
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
            mid,
            thread_id,
            from_addr,
            "Sender",
            from_domain if from_domain is not None else sender_domain(from_addr),
            "[]",
            subject,
            normalize_subject(subject),
            1_700_000_000,
            snippet,
            1000,
            "[]",
            list_id,
            None,
            None,
            attachment,
            invite,
            starred,
            important,
            0,
        ),
    )


def guard_for(conn, mid: str):
    row = conn.execute(
        "SELECT * FROM message_guards WHERE message_id = ?", (mid,)
    ).fetchone()
    return {
        "never_trash": bool(row["never_trash"]),
        "flags": json.loads(row["flags"]),
        "categories": json.loads(row["categories"]),
    }


class TestProtectiveSignals:
    def test_protected_sender_protects(self, conn):
        add_message(conn, mid="m1", from_addr="jane@friend.example.com")
        conn.execute(
            "INSERT INTO protected_senders(addr, sent_count) VALUES(?,?)",
            ("jane@friend.example.com", 4),
        )
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is True
        assert "protected_sender" in g["flags"]

    def test_protected_domain_protects(self, conn):
        add_message(conn, mid="m1", from_addr="noreply@ato.gov.au")
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is True
        assert "protected_domain" in g["flags"]

    def test_replied_thread_protects(self, conn):
        add_message(conn, mid="m1", thread_id="thread-42")
        conn.execute("INSERT INTO replied_threads(thread_id) VALUES(?)", ("thread-42",))
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is True
        assert "replied_thread" in g["flags"]

    def test_starred_protects(self, conn):
        add_message(conn, mid="m1", starred=1)
        build_clusters(conn)
        guards.evaluate_all(conn)
        assert guard_for(conn, "m1")["never_trash"] is True

    def test_calendar_invite_protects(self, conn):
        add_message(conn, mid="m1", invite=1)
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is True
        assert "calendar_invite" in g["flags"]

    @pytest.mark.parametrize(
        "subject,category",
        [
            ("Your 2024 notice of assessment", "tax"),
            ("Tax invoice for your purchase", "finance"),
            ("Payment receipt - thank you", "receipt"),
            ("Your policy schedule is attached", "insurance"),
            ("Executed agreement for signature", "legal"),
            ("Your visa grant notice", "identity"),
            ("Pathology test results available", "medical"),
            ("Your boarding pass for flight", "travel"),
            ("Warranty certificate enclosed", "warranty"),
            ("Your account recovery code", "security"),
            ("Council rates notice 2025", "property"),
        ],
    )
    def test_critical_keep_signals_protect(self, conn, subject, category):
        add_message(conn, mid="m1", subject=subject)
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is True, f"{category} should protect"
        assert category in g["categories"]

    def test_keep_signal_matches_in_snippet_not_just_subject(self, conn):
        add_message(
            conn,
            mid="m1",
            subject="Order update",
            snippet="Attached is your tax invoice for the transaction",
        )
        build_clusters(conn)
        guards.evaluate_all(conn)
        assert guard_for(conn, "m1")["never_trash"] is True


class TestWeakSignalsDoNotProtect:
    """Signals recorded for model context but too noisy to protect on their own."""

    def test_attachment_alone_does_not_protect(self, conn):
        # A PDF on a promotional email is a brochure, not a record.
        add_message(conn, mid="m1", subject="Spring catalogue", attachment=1)
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is False
        assert "has_attachment" in g["flags"]

    def test_gmail_important_alone_does_not_protect(self, conn):
        add_message(conn, mid="m1", subject="Weekend deals", important=1)
        build_clusters(conn)
        guards.evaluate_all(conn)

        g = guard_for(conn, "m1")
        assert g["never_trash"] is False
        assert "gmail_important" in g["flags"]

    def test_plain_promotional_mail_is_not_protected(self, conn):
        add_message(conn, mid="m1", subject="50% off everything today only")
        build_clusters(conn)
        guards.evaluate_all(conn)
        assert guard_for(conn, "m1")["never_trash"] is False


class TestClusterRollUp:
    def test_one_receipt_in_a_promo_list_protects_the_message_not_the_cluster(
        self, conn
    ):
        """The single most important behaviour in the system.

        A 300-message promo list containing two receipts must not become
        undeletable, and the receipts must not be deleted. Both, not either.
        """
        for i in range(300):
            add_message(
                conn,
                mid=f"promo{i}",
                from_addr="deals@shop.example.com",
                subject="Flash sale ends tonight",
            )
        for i in range(2):
            add_message(
                conn,
                mid=f"receipt{i}",
                from_addr="deals@shop.example.com",
                subject="Payment receipt for your order",
            )

        build_clusters(conn)
        guards.evaluate_all(conn)

        cluster = conn.execute(
            "SELECT * FROM clusters WHERE sender_addr = ?", ("deals@shop.example.com",)
        ).fetchone()
        # Cluster stays deletable...
        assert bool(cluster["never_trash"]) is False
        # ...but the receipts inside it are individually protected.
        assert guard_for(conn, "receipt0")["never_trash"] is True
        assert guard_for(conn, "receipt1")["never_trash"] is True
        assert guard_for(conn, "promo0")["never_trash"] is False

        flags = json.loads(cluster["guard_flags"])
        assert flags["protected_messages"] == 2
        assert flags["total_messages"] == 302

    def test_cluster_from_protected_sender_is_protected_wholesale(self, conn):
        for i in range(10):
            add_message(
                conn,
                mid=f"m{i}",
                from_addr="jane@friend.example.com",
                subject="Re: dinner plans",
                thread_id=f"t{i}",
            )
        conn.execute(
            "INSERT INTO protected_senders(addr, sent_count) VALUES(?,?)",
            ("jane@friend.example.com", 12),
        )
        build_clusters(conn)
        guards.evaluate_all(conn)

        cluster = conn.execute(
            "SELECT * FROM clusters WHERE sender_addr = ?", ("jane@friend.example.com",)
        ).fetchone()
        assert bool(cluster["never_trash"]) is True

    def test_a_few_replied_threads_protect_the_whole_cluster(self, conn):
        """Replying even occasionally means this is a conversation, not a broadcast."""
        for i in range(8):
            add_message(
                conn, mid=f"m{i}", from_addr="bob@work.example.com", thread_id=f"t{i}"
            )
        # Replied to 2 of 8 threads — over the 1/4 rollup threshold.
        for t in ("t0", "t1"):
            conn.execute("INSERT INTO replied_threads(thread_id) VALUES(?)", (t,))

        build_clusters(conn)
        guards.evaluate_all(conn)

        cluster = conn.execute(
            "SELECT * FROM clusters WHERE sender_addr = ?", ("bob@work.example.com",)
        ).fetchone()
        assert bool(cluster["never_trash"]) is True

    def test_single_spoofed_sender_does_not_immunise_a_large_list(self, conn):
        """Sender-level protection needs a majority, not a single message."""
        conn.execute(
            "INSERT INTO protected_senders(addr, sent_count) VALUES(?,?)",
            ("jane@friend.example.com", 3),
        )
        # 1 message appearing to be from a protected sender, 99 clearly bulk, all
        # sharing a List-Id so they land in one cluster.
        add_message(
            conn,
            mid="spoof",
            from_addr="jane@friend.example.com",
            list_id="bulk.example.com",
        )
        for i in range(99):
            add_message(
                conn,
                mid=f"bulk{i}",
                from_addr="blast@bulk.example.com",
                list_id="bulk.example.com",
            )

        build_clusters(conn)
        guards.evaluate_all(conn)

        cluster = conn.execute(
            "SELECT * FROM clusters WHERE list_id = ?", ("bulk.example.com",)
        ).fetchone()
        assert cluster["message_count"] == 100
        assert bool(cluster["never_trash"]) is False
        # The individual message is still protected.
        assert guard_for(conn, "spoof")["never_trash"] is True


class TestReport:
    def test_report_shape(self, conn):
        add_message(conn, mid="m1", subject="Your tax invoice")
        add_message(conn, mid="m2", subject="Weekend sale")
        build_clusters(conn)
        guards.evaluate_all(conn)

        report = guards.guard_report(conn)
        assert report["messages_evaluated"] == 2
        assert report["messages_protected"] == 1
        assert "finance" in report["by_category"]
