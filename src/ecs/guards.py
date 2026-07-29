"""Hard constraints. Code, not prompting.

The instruction behind this module is "if in doubt keep it". That is a safety property, and safety
properties don't belong in prompts — a model can be argued out of a prompt. Every
guard here is evaluated before a model sees anything, and a `never_trash` result
overrides every downstream verdict unconditionally.

Two tiers, deliberately:

  * **Cluster-level guards** protect an entire sender. Used only for signals that
    genuinely apply to everything from that source — a real correspondent, a
    government agency.

  * **Message-level guards** protect individual messages inside an otherwise
    disposable cluster. This is what stops the system from having to choose between
    "delete 340 promotional emails" and "keep them all because one of them is a
    receipt". The receipt is protected; the other 339 still go.

Over-firing here costs cleanup, not data. Under-firing costs data. The patterns are
tuned accordingly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import config

# ---------------------------------------------------------------------------
# Keep-signal patterns, grouped so a hit also suggests a label.
#
# Tuned for precision on the expensive-to-lose categories. Generic marketing words
# ("payment", "confirmation", "order") are deliberately excluded on their own —
# they appear in a large share of promotional mail and would protect everything.
# ---------------------------------------------------------------------------

KEEP_SIGNALS: dict[str, re.Pattern[str]] = {
    "tax": re.compile(
        r"\b(?:tax\s*(?:return|invoice|receipt|statement|assessment|file\s*number)"
        r"|notice\s+of\s+assessment|\bATO\b|australian\s+taxation"
        r"|group\s+certificate|payment\s+summary|income\s+statement"
        r"|\bTFN\b|deducti(?:on|ble)|BAS\s+statement|GST\s+(?:return|statement))\b",
        re.IGNORECASE,
    ),
    "finance": re.compile(
        r"\b(?:invoice|tax\s*invoice|remittance|bpay|direct\s+debit"
        r"|(?:bank|account|credit\s+card)\s+statement|annual\s+statement"
        r"|superannuation|\bsuper\s+(?:fund|contribution|statement)\b"
        r"|dividend|share\s+(?:purchase|sale)|capital\s+gain"
        r"|loan\s+(?:statement|approval|contract)|mortgage)\b",
        re.IGNORECASE,
    ),
    "receipt": re.compile(
        r"\b(?:receipt|proof\s+of\s+purchase|purchase\s+confirmation"
        r"|your\s+(?:order|purchase)\s+(?:receipt|invoice)"
        r"|payment\s+(?:received|receipt|confirmation)"
        r"|paid\s+in\s+full|transaction\s+(?:receipt|record))\b",
        re.IGNORECASE,
    ),
    "insurance": re.compile(
        r"\b(?:insurance|policy\s*(?:number|document|schedule|renewal)"
        r"|certificate\s+of\s+currency|premium\s+(?:notice|due)"
        r"|claim\s+(?:number|reference|lodged|approved)"
        r"|\bPDS\b|product\s+disclosure)\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b(?:contract|executed\s+agreement|deed|settlement\s+(?:statement|date)"
        r"|lease\s+(?:agreement|renewal)|tenancy\s+agreement"
        r"|letter\s+of\s+(?:engagement|offer|demand)|notice\s+to\s+(?:vacate|remedy)"
        r"|terms\s+of\s+engagement|power\s+of\s+attorney|\bNDA\b"
        r"|non[-\s]?disclosure|statutory\s+declaration)\b",
        re.IGNORECASE,
    ),
    "identity": re.compile(
        r"\b(?:passport|visa\s+(?:grant|application|approved)|\bVEVO\b"
        r"|driver'?s?\s+licence|driver'?s?\s+license|birth\s+certificate"
        r"|medicare\s+(?:card|number)|citizenship"
        r"|proof\s+of\s+identity|\b100\s+points\b)\b",
        re.IGNORECASE,
    ),
    "medical": re.compile(
        r"\b(?:pathology|radiology|test\s+results|prescription|\bscript\b"
        r"|specialist\s+referral|referral\s+letter|discharge\s+summary"
        r"|immunisation|vaccination\s+(?:record|certificate)"
        r"|surgery\s+(?:date|booking)|\bMRI\b|\bCT\s+scan\b)\b",
        re.IGNORECASE,
    ),
    "travel": re.compile(
        r"\b(?:boarding\s+pass|itinerary|e[-\s]?ticket|booking\s+reference"
        r"|\bPNR\b|flight\s+(?:confirmation|booking)|check[-\s]?in\s+(?:open|now)"
        r"|hotel\s+(?:confirmation|reservation)|reservation\s+(?:number|confirmed)"
        r"|travel\s+insurance)\b",
        re.IGNORECASE,
    ),
    "warranty": re.compile(
        r"\b(?:warranty|guarantee\s+(?:certificate|period)|extended\s+cover"
        r"|serial\s+number|registration\s+(?:certificate|confirmation)"
        r"|service\s+history)\b",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\b(?:recovery\s+(?:code|key|kit)|backup\s+code|seed\s+phrase"
        r"|two[-\s]?factor|\b2FA\b|security\s+key|master\s+password"
        r"|account\s+recovery)\b",
        re.IGNORECASE,
    ),
    "property": re.compile(
        r"\b(?:rates\s+notice|body\s+corporate|strata|owners\s+corporation"
        r"|building\s+(?:inspection|report)|conveyanc|title\s+(?:deed|search)"
        r"|land\s+tax|water\s+(?:rates|notice))\b",
        re.IGNORECASE,
    ),
    "education": re.compile(
        r"\b(?:transcript|academic\s+record|certificate\s+of\s+completion"
        r"|\bAHPRA\b|professional\s+registration|\bCPD\b\s+record"
        r"|qualification|graduation)\b",
        re.IGNORECASE,
    ),
}

# Categories strong enough to protect a message on their own — currently all of
# them. Guards stay binary and unarguable on purpose.
#
# `travel` is the one genuinely time-dependent category: a 2019 boarding pass is
# worthless, an itinerary for next month is not. Encoding that here would mean
# putting a staleness heuristic inside a safety guard, which is the wrong place for
# a judgement call. It protects unconditionally, and the escalation stage decides
# whether old travel mail gets archived under Travel/ or dropped.
CRITICAL_CATEGORIES = frozenset(KEEP_SIGNALS)

# Bulk-mail markers. Their *absence* is weak evidence a human wrote the message.
_BULK_HINTS = re.compile(r"\b(?:list|bulk|auto[-_]?reply|junk)\b", re.IGNORECASE)


@dataclass
class MessageGuard:
    message_id: str
    never_trash: bool = False
    flags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


def scan_keep_signals(*parts: str | None) -> list[str]:
    """Return the keep-signal categories present in the given text fragments."""
    haystack = " ".join(p for p in parts if p)
    if not haystack:
        return []
    return [name for name, pattern in KEEP_SIGNALS.items() if pattern.search(haystack)]


def evaluate_message(
    row: sqlite3.Row,
    *,
    protected: set[str],
    replied: set[str],
    protected_domains: frozenset[str],
) -> MessageGuard:
    """Apply every guard to one message."""
    guard = MessageGuard(message_id=row["id"])

    # --- Cluster-strength signals, evaluated per message ------------------
    if row["from_addr"] and row["from_addr"] in protected:
        guard.flags.append("protected_sender")
        guard.never_trash = True

    if row["from_domain"] and row["from_domain"] in protected_domains:
        guard.flags.append("protected_domain")
        guard.never_trash = True

    if row["thread_id"] and row["thread_id"] in replied:
        guard.flags.append("replied_thread")
        guard.never_trash = True

    # --- Message-strength signals ----------------------------------------
    if row["is_starred"]:
        guard.flags.append("starred")
        guard.never_trash = True

    if row["has_calendar_invite"]:
        # A calendar invite means a commitment was made. Keep it.
        guard.flags.append("calendar_invite")
        guard.never_trash = True

    categories = scan_keep_signals(row["subject"], row["snippet"])
    if categories:
        guard.categories = categories
        guard.flags.extend(f"keep:{c}" for c in categories)
        if CRITICAL_CATEGORIES.intersection(categories):
            guard.never_trash = True

    # --- Weak signals: recorded, never protective on their own -----------
    # An attachment on a promotional email is usually a brochure. Combined with a
    # keep-signal category it's meaningful, and that case is already covered above.
    if row["has_attachment"]:
        guard.flags.append("has_attachment")
    if row["is_important"]:
        # Gmail's own guess. Useful context for the models, too noisy to protect on.
        guard.flags.append("gmail_important")

    return guard


def evaluate_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Compute and persist guards for every indexed message, then roll up to clusters."""
    protected = {
        r["addr"] for r in conn.execute("SELECT addr FROM protected_senders")
    }
    replied = {
        r["thread_id"] for r in conn.execute("SELECT thread_id FROM replied_threads")
    }
    protected_domains = frozenset(config.TUNABLES.protected_domains)

    rows = conn.execute(
        """
        SELECT id, thread_id, from_addr, from_domain, subject, snippet,
               is_starred, is_important, has_attachment, has_calendar_invite
        FROM messages
        """
    ).fetchall()

    guards = [
        evaluate_message(
            row,
            protected=protected,
            replied=replied,
            protected_domains=protected_domains,
        )
        for row in rows
    ]

    conn.execute("DELETE FROM message_guards")
    conn.executemany(
        """
        INSERT INTO message_guards(message_id, never_trash, flags, categories)
        VALUES(?,?,?,?)
        """,
        [
            (
                g.message_id,
                1 if g.never_trash else 0,
                json.dumps(g.flags),
                json.dumps(g.categories),
            )
            for g in guards
        ],
    )

    rolled = _roll_up_to_clusters(conn)
    protected_count = sum(1 for g in guards if g.never_trash)

    return {
        "messages_evaluated": len(guards),
        "messages_protected": protected_count,
        "clusters_protected": rolled,
    }


def _roll_up_to_clusters(conn: sqlite3.Connection) -> int:
    """Summarise message guards onto their clusters.

    A cluster is marked `never_trash` only when protection applies to the sender as
    a whole — a real correspondent or a protected domain. A cluster where *some*
    messages tripped keep-signals stays deletable; those specific messages are
    individually protected and the cluster is flagged for escalation instead. This
    is the distinction that lets a 340-message promo list be cleaned out while the
    two receipts buried in it survive.
    """
    cluster_protected = 0
    clusters = conn.execute(
        "SELECT key, message_count FROM clusters"
    ).fetchall()

    for cluster in clusters:
        rows = conn.execute(
            """
            SELECT g.never_trash, g.flags
            FROM message_guards g
            JOIN messages m ON m.id = g.message_id
            WHERE m.cluster_key = ?
            """,
            (cluster["key"],),
        ).fetchall()
        if not rows:
            continue

        flag_counter: Counter[str] = Counter()
        protected_msgs = 0
        for row in rows:
            if row["never_trash"]:
                protected_msgs += 1
            for flag in json.loads(row["flags"]):
                flag_counter[flag] += 1

        total = len(rows)
        sender_level = {"protected_sender", "protected_domain"}
        # Sender-level protection: fires if it applies to most of the cluster.
        # (Not "any" — a single spoofed From shouldn't immunise a whole list.)
        never_trash = any(
            flag_counter.get(flag, 0) >= max(1, total // 2) for flag in sender_level
        )
        # A conversation you actually participated in is protected wholesale.
        if flag_counter.get("replied_thread", 0) >= max(1, total // 4):
            never_trash = True

        if never_trash:
            cluster_protected += 1

        conn.execute(
            """
            UPDATE clusters
            SET guard_flags = ?, never_trash = ?
            WHERE key = ?
            """,
            (
                json.dumps(
                    {
                        "counts": dict(flag_counter.most_common()),
                        "protected_messages": protected_msgs,
                        "total_messages": total,
                    }
                ),
                1 if never_trash else 0,
                cluster["key"],
            ),
        )
    return cluster_protected


def guard_report(conn: sqlite3.Connection) -> dict[str, object]:
    """Summary for `ecs guards --report`."""
    total = conn.execute("SELECT COUNT(*) FROM message_guards").fetchone()[0]
    protected = conn.execute(
        "SELECT COUNT(*) FROM message_guards WHERE never_trash = 1"
    ).fetchone()[0]
    clusters_protected = conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE never_trash = 1"
    ).fetchone()[0]

    category_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    for row in conn.execute("SELECT flags, categories FROM message_guards"):
        for c in json.loads(row["categories"]):
            category_counts[c] += 1
        for f in json.loads(row["flags"]):
            flag_counts[f] += 1

    return {
        "messages_evaluated": total,
        "messages_protected": protected,
        "clusters_protected": clusters_protected,
        "protected_senders": conn.execute(
            "SELECT COUNT(*) FROM protected_senders"
        ).fetchone()[0],
        "replied_threads": conn.execute(
            "SELECT COUNT(*) FROM replied_threads"
        ).fetchone()[0],
        "by_category": dict(category_counts.most_common()),
        "by_flag": dict(flag_counts.most_common()),
    }
