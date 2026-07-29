"""Deterministic clustering. No model involved.

This is the single biggest cost and reviewability lever in the system. Classifying
7,000 messages individually would cost ~50x more and produce a review queue no
human would ever finish. Collapsing them into a few hundred sender clusters means
the models reason about "this newsletter, 340 messages, 2019-2026" instead of 340
near-identical emails, and you approve ~350 decisions instead of 7,000.

Clustering must stay purely deterministic: same mailbox in, same clusters out. That
property is what makes every downstream stage independently re-runnable.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from . import config

# ---------------------------------------------------------------------------
# Subject normalisation
# ---------------------------------------------------------------------------

_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|vs|rv)\s*:\s*)+", re.IGNORECASE)
_BRACKET_TAG = re.compile(r"[\[\(]\s*[^\]\)]{1,40}\s*[\]\)]")
_HAS_DIGIT = re.compile(r"\d")
_ISO_DATE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")
_MONTH_NAME = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_WEEKDAY = re.compile(
    r"\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b", re.IGNORECASE
)
_CURRENCY = re.compile(r"[$£€¥]\s?[\d,]+(?:\.\d{2})?")
_EMOJI = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f000-\U0001f2ff" "]+"
)
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_subject(subject: str | None) -> str:
    """Reduce a subject line to a stable signature.

    "Re: [Acme] Invoice #INV-2024-0918 for $1,240.00 — due Fri 12 Sep"
        -> "invoice for due"

    The goal is that every message from a given automated sender collapses onto the
    same signature, so template-generated mail clusters together regardless of the
    order numbers, dates, and amounts interpolated into it.
    """
    if not subject:
        return ""

    s = subject.strip()
    # Strip nested Re:/Fwd: chains first so bracket tags behind them are reachable.
    while True:
        stripped = _REPLY_PREFIX.sub("", s)
        if stripped == s:
            break
        s = stripped

    s = _EMOJI.sub(" ", s)
    s = _BRACKET_TAG.sub(" ", s)
    s = _CURRENCY.sub(" ", s)
    s = _ISO_DATE.sub(" ", s)
    s = s.lower()
    s = _MONTH_NAME.sub(" ", s)
    s = _WEEKDAY.sub(" ", s)

    # Drop whole whitespace-delimited tokens containing a digit, *before* stripping
    # punctuation. Order matters: "#INV-2024-0918" has to die as one unit, because
    # cleaning punctuation first would leave a stray "inv" behind and different
    # senders use different reference prefixes (INV-, ORD-, REF-, PO-).
    kept = [tok for tok in s.split() if not _HAS_DIGIT.search(tok)]
    s = " ".join(kept)

    s = _NON_WORD.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()

    # Keep the leading tokens: automated subjects front-load their identity
    # ("Your order has shipped", "Weekly digest for ...").
    tokens = [t for t in s.split() if len(t) > 1]
    return " ".join(tokens[:6])


def normalize_list_id(raw: str | None) -> str | None:
    """Extract the bare list identifier from a List-Id header.

    'Acme News <news.acme.example.com>' -> 'news.acme.example.com'
    """
    if not raw:
        return None
    match = re.search(r"<([^>]+)>", raw)
    value = match.group(1) if match else raw
    value = value.strip().strip("<>").lower()
    return value or None


# Two-label public suffixes, so "ato.gov.au" isn't truncated to "gov.au".
#
# Getting this wrong is not cosmetic: truncating to the suffix would collapse every
# Australian government sender into one cluster, and would stop the protected-domain
# guard (which lists "ato.gov.au") from ever matching. A full Public Suffix List via
# `publicsuffix2` would be more complete, but it's a data-file dependency for a
# problem that this covers for a personal Australian mailbox.
_MULTI_LABEL_SUFFIXES = frozenset(
    {
        # Australia
        "com.au", "net.au", "org.au", "gov.au", "edu.au", "asn.au", "id.au", "csiro.au",
        # United Kingdom
        "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
        "ac.uk", "gov.uk", "nhs.uk", "police.uk", "mod.uk",
        # New Zealand
        "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "school.nz", "geek.nz",
        # Other common
        "com.br", "com.cn", "com.hk", "com.mx", "com.sg", "com.tr", "com.tw",
        "co.in", "co.jp", "co.kr", "co.za", "co.il", "com.ar", "com.co",
        "go.jp", "ne.jp", "or.jp", "ac.jp", "gov.hk", "edu.hk",
    }
)


def sender_domain(addr: str | None) -> str | None:
    """Reduce an address to its registrable domain.

    Bulk senders rotate deep per-send subdomains, so collapsing to the registrable
    domain is what makes their mail cluster together:

        bounce.mail1.sendgrid.acme.com -> acme.com
        mail.acme.com                  -> acme.com
        ato.gov.au                     -> ato.gov.au   (suffix preserved)
    """
    if not addr or "@" not in addr:
        return None
    domain = addr.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return domain or None

    parts = domain.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ---------------------------------------------------------------------------
# Unsubscribe mechanism detection
# ---------------------------------------------------------------------------


def unsub_method(list_unsub: str | None, list_unsub_post: str | None) -> str:
    """Classify the best available unsubscribe mechanism.

    RFC 8058 one-click is by far the most reliable: a single HTTPS POST, no page to
    scrape, no button to find. Worth detecting separately because it lets us skip
    the browser entirely for most modern senders.
    """
    if not list_unsub:
        return "none"
    has_http = "http" in list_unsub.lower()
    if (
        list_unsub_post
        and "one-click" in list_unsub_post.lower()
        and has_http
    ):
        return "one_click"
    if has_http:
        return "http"
    if "mailto:" in list_unsub.lower():
        return "mailto"
    return "none"


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    key: str
    kind: str  # list_id | sender | domain_subject
    display_name: str
    sender_addr: str | None = None
    sender_domain: str | None = None
    list_id: str | None = None
    message_ids: list[str] = field(default_factory=list)
    unread_count: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    unsub_methods: set[str] = field(default_factory=set)
    subjects: list[str] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.message_ids)

    def best_unsub_method(self) -> str:
        for method in ("one_click", "http", "mailto"):
            if method in self.unsub_methods:
                return method
        return "none"

    def sample_subjects(self, limit: int) -> list[str]:
        """A spread of distinct subjects, not just the most recent N.

        Sampling across the cluster is what lets the triage model notice a mixed
        cluster — e.g. a promo list that also carries order receipts.
        """
        seen: list[str] = []
        for subject in self.subjects:
            s = (subject or "").strip()
            if s and s not in seen:
                seen.append(s)
        if len(seen) <= limit:
            return seen
        step = len(seen) / limit
        return [seen[int(i * step)] for i in range(limit)]


def cluster_key_for(
    *, list_id: str | None, from_addr: str | None, domain: str | None, subject_norm: str
) -> tuple[str, str]:
    """Return (key, kind) for one message, by precedence.

    1. List-Id  — authoritative machine identity for a mailing list, and stable
       even when the envelope sender rotates per-send.
    2. Sender address — the common case for both people and transactional senders.
    3. Domain + subject signature — catches senders that rotate the local part
       (noreply-8f2a@..., bounce+xyz@...) but send the same template.
    """
    if list_id:
        return f"list:{list_id}", "list_id"
    if from_addr:
        return f"addr:{from_addr}", "sender"
    if domain:
        return f"dom:{domain}|{subject_norm}", "domain_subject"
    return f"sig:{subject_norm}", "domain_subject"


def build_clusters(conn: sqlite3.Connection) -> list[Cluster]:
    """Group every indexed message into clusters and persist the result."""
    clusters: dict[str, Cluster] = {}
    assignments: list[tuple[str, str]] = []

    rows = conn.execute(
        """
        SELECT id, from_addr, from_name, from_domain, subject, subject_norm,
               date_ts, list_id, list_unsubscribe, list_unsubscribe_post,
               is_unread
        FROM messages
        """
    )

    for row in rows:
        list_id = normalize_list_id(row["list_id"])
        key, kind = cluster_key_for(
            list_id=list_id,
            from_addr=row["from_addr"],
            domain=row["from_domain"],
            subject_norm=row["subject_norm"] or "",
        )

        cluster = clusters.get(key)
        if cluster is None:
            cluster = Cluster(
                key=key,
                kind=kind,
                display_name=_display_name(row, list_id, kind),
                sender_addr=row["from_addr"],
                sender_domain=row["from_domain"],
                list_id=list_id,
            )
            clusters[key] = cluster

        cluster.message_ids.append(row["id"])
        cluster.subjects.append(row["subject"] or "")
        if row["is_unread"]:
            cluster.unread_count += 1

        ts = row["date_ts"]
        if ts is not None:
            cluster.first_ts = ts if cluster.first_ts is None else min(cluster.first_ts, ts)
            cluster.last_ts = ts if cluster.last_ts is None else max(cluster.last_ts, ts)

        cluster.unsub_methods.add(
            unsub_method(row["list_unsubscribe"], row["list_unsubscribe_post"])
        )
        assignments.append((key, row["id"]))

    # Persist. Guard flags and never_trash are filled in later by guards.py, which
    # needs the cluster rows to exist first.
    conn.execute("DELETE FROM clusters")
    conn.executemany(
        """
        INSERT INTO clusters(
            key, kind, display_name, sender_addr, sender_domain, list_id,
            message_count, unread_count, first_ts, last_ts,
            has_unsub, unsub_method, guard_flags, never_trash, sample_subjects
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                c.key,
                c.kind,
                c.display_name,
                c.sender_addr,
                c.sender_domain,
                c.list_id,
                c.message_count,
                c.unread_count,
                c.first_ts,
                c.last_ts,
                1 if c.best_unsub_method() != "none" else 0,
                c.best_unsub_method(),
                json.dumps([]),
                0,
                json.dumps(c.sample_subjects(config.TUNABLES.max_sample_subjects)),
            )
            for c in clusters.values()
        ],
    )
    conn.executemany(
        "UPDATE messages SET cluster_key = ? WHERE id = ?",
        assignments,
    )
    return list(clusters.values())


def _display_name(row: sqlite3.Row, list_id: str | None, kind: str) -> str:
    name = (row["from_name"] or "").strip().strip('"')
    addr = row["from_addr"] or ""
    if kind == "list_id" and list_id:
        return f"{name or addr or list_id} ({list_id})"
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name or (row["from_domain"] or "unknown sender")


def cluster_report(conn: sqlite3.Connection) -> dict[str, object]:
    """Summary stats for `ecs cluster --report`."""
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_clusters = conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
    by_kind: dict[str, int] = defaultdict(int)
    for row in conn.execute("SELECT kind, COUNT(*) AS n FROM clusters GROUP BY kind"):
        by_kind[row["kind"]] = row["n"]
    with_unsub = conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE has_unsub = 1"
    ).fetchone()[0]
    singletons = conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE message_count = 1"
    ).fetchone()[0]
    return {
        "messages": total_messages,
        "clusters": total_clusters,
        "by_kind": dict(by_kind),
        "with_unsubscribe": with_unsub,
        "singletons": singletons,
        "compression": (total_messages / total_clusters) if total_clusters else 0.0,
    }
