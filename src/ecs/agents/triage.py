"""Stage 5 — per-cluster triage with Haiku 4.5 over the Batch API.

This is the high-volume stage, and it runs on clusters rather than messages: ~350
requests instead of ~7,000. Combined with the Batch API's 50% discount that puts
the whole stage under a dollar.

Haiku is asked to characterise the *dominant nature* of a cluster and, critically,
to flag when a cluster is mixed. It is explicitly told not to worry about protecting
individual records — `guards.py` has already done that in code, and duplicating the
concern here would just make Haiku conservative about everything.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .. import config, db
from .client import BatchRequest, CostTracker, build_params, run_batch

CATEGORIES = [
    "newsletter",
    "marketing_promo",
    "transactional_receipt",
    "transactional_notification",
    "shipping_delivery",
    "subscription_billing",
    "bill_invoice",
    "financial_statement",
    "account_security",
    "social_notification",
    "forum_digest",
    "code_dev_notification",
    "service_alert",
    "travel_booking",
    "calendar_meeting",
    "event_invite",
    "personal_correspondence",
    "work_correspondence",
    "recruitment",
    "government_official",
    "medical_health",
    "education_course",
    "survey_feedback",
    "spam_phishing",
    "other",
]

DISPOSITIONS = ["keep", "archive", "trash", "unsubscribe"]

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "disposition": {"type": "string", "enum": DISPOSITIONS},
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "is_mixed": {"type": "boolean"},
        "keep_signals": {
            "type": "array",
            "items": {"type": "string"},
        },
        "rationale": {"type": "string"},
    },
    "required": [
        "category",
        "disposition",
        "confidence",
        "is_mixed",
        "keep_signals",
        "rationale",
    ],
    "additionalProperties": False,
}

# Confidence is an enum rather than a 0-1 number: strict JSON schema can't express
# numeric bounds, and small models produce better-calibrated buckets than they do
# spurious decimals like 0.87.
CONFIDENCE_VALUES = {"low": 0.4, "medium": 0.7, "high": 0.95}

SYSTEM = """You triage clusters of email from one person's personal Gmail inbox. \
Each cluster is all the mail from a single sender or mailing list.

Your job is to characterise what a cluster IS and recommend what to do with it.

Dispositions:
- "keep": genuinely still actionable and belongs in the inbox. Rare. Use only when \
the person plausibly still needs to act on this.
- "archive": worth retaining but does not belong in an inbox. Records, receipts, \
confirmations, real correspondence, anything with future reference value.
- "trash": no future value. Expired promotions, stale alerts, superseded \
notifications, content that was disposable the moment it was read.
- "unsubscribe": same as trash, AND the person should stop receiving it. Use for \
ongoing bulk mail they clearly never engage with.

Set "is_mixed": true when the cluster contains materially different kinds of mail \
under one sender — for example a retailer that sends both promotions and order \
receipts, or a bank that sends both marketing and statements. This is important: a \
mixed cluster gets examined message-by-message instead of being actioned in bulk. \
When in doubt about whether a cluster is uniform, say it is mixed.

Set "confidence": "high" only when the sample subjects are clearly consistent and \
the disposition is obvious. Use "low" when the cluster is small, ambiguous, or the \
subjects don't tell you much.

List anything in "keep_signals" that suggests reference value — receipts, tax, \
legal, medical, identity, warranty, bookings.

You do NOT need to be protective of individual important messages. A separate \
deterministic system has already permanently protected anything matching \
financial, legal, medical, identity, or security patterns, plus everything from \
real correspondents. Classify the cluster's dominant character and let that system \
do its job. Being needlessly conservative here means the inbox never gets cleaned.

Reply only with the JSON object."""


def _fmt_date(ts: int | None) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")


def build_digest(row: sqlite3.Row) -> str:
    """Compact, information-dense description of one cluster.

    Kept tight on purpose — this text is multiplied by ~350 requests, so every
    redundant line is real money.
    """
    subjects = json.loads(row["sample_subjects"] or "[]")
    guard_info = json.loads(row["guard_flags"] or "{}")
    protected = guard_info.get("protected_messages", 0)
    total = row["message_count"]

    lines = [
        f"Sender: {row['display_name']}",
        f"Messages: {total}  Unread: {row['unread_count']}",
        f"Span: {_fmt_date(row['first_ts'])} to {_fmt_date(row['last_ts'])}",
        f"Unsubscribe available: {row['unsub_method']}",
    ]
    if row["list_id"]:
        lines.append(f"Mailing list: {row['list_id']}")
    if protected:
        lines.append(
            f"Note: {protected} of {total} messages already flagged as records "
            "by the keep-signal scanner"
        )

    lines.append("Sample subjects:")
    lines.extend(f"  - {s[:160]}" for s in subjects)
    return "\n".join(lines)


def triage_clusters(
    *,
    limit: int | None = None,
    only_missing: bool = True,
    tracker: CostTracker | None = None,
    progress=None,
) -> dict[str, int]:
    """Classify every cluster. Idempotent — re-runs only fill gaps by default."""
    emit = progress or (lambda _: None)
    tracker = tracker or CostTracker()

    with db.session() as conn:
        sql = "SELECT * FROM clusters"
        if only_missing:
            sql += (
                " WHERE key NOT IN (SELECT cluster_key FROM triage_verdicts)"
            )
        sql += " ORDER BY message_count DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        clusters = conn.execute(sql).fetchall()

    if not clusters:
        emit("no clusters need triage")
        return {"requested": 0, "classified": 0, "failed": 0}

    # The system prompt sits well under Haiku 4.5's 4096-token cache minimum, so a
    # cache_control marker here would buy only the write premium. Skipped
    # deliberately; the Batch API discount is where the saving comes from.
    requests = [
        BatchRequest(
            custom_id=_safe_id(row["key"]),
            params=build_params(
                config.MODEL_TRIAGE,
                system=SYSTEM,
                messages=[{"role": "user", "content": build_digest(row)}],
                max_tokens=1500,
                schema=SCHEMA,
            ),
        )
        for row in clusters
    ]
    id_to_key = {_safe_id(row["key"]): row["key"] for row in clusters}

    emit(f"triaging {len(requests):,} clusters with {config.MODEL_TRIAGE}")
    results, errors = run_batch(
        requests, model=config.MODEL_TRIAGE, tracker=tracker, progress=emit, resume_key="batch.triage"
    )

    now = datetime.now(UTC).isoformat()
    rows = []
    for custom_id, payload in results.items():
        key = id_to_key.get(custom_id)
        if key is None:
            continue
        rows.append(
            (
                key,
                payload.get("category", "other"),
                payload.get("disposition", "archive"),
                CONFIDENCE_VALUES.get(payload.get("confidence", "low"), 0.4),
                1 if payload.get("is_mixed") else 0,
                json.dumps(payload.get("keep_signals", [])),
                payload.get("rationale", ""),
                config.MODEL_TRIAGE,
                now,
            )
        )

    with db.session() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO triage_verdicts(
                cluster_key, category, disposition, confidence, is_mixed,
                keep_signals, rationale, model, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    for custom_id, error in errors.items():
        emit(f"[warn] {id_to_key.get(custom_id, custom_id)}: {error}")

    return {
        "requested": len(requests),
        "classified": len(rows),
        "failed": len(errors),
    }


def _safe_id(cluster_key: str) -> str:
    """Batch custom_ids allow a limited character set; cluster keys don't.

    Hashing keeps it deterministic and collision-free enough at this scale, and the
    mapping back is held in memory for the request's lifetime.
    """
    import hashlib

    return "c_" + hashlib.sha256(cluster_key.encode()).hexdigest()[:40]


def triage_report(conn: sqlite3.Connection) -> dict[str, object]:
    """Summary for the CLI after triage runs."""
    total = conn.execute("SELECT COUNT(*) FROM triage_verdicts").fetchone()[0]
    by_disposition = {
        r["disposition"]: r["n"]
        for r in conn.execute(
            "SELECT disposition, COUNT(*) AS n FROM triage_verdicts "
            "GROUP BY disposition ORDER BY n DESC"
        )
    }
    by_category = {
        r["category"]: r["n"]
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM triage_verdicts "
            "GROUP BY category ORDER BY n DESC"
        )
    }
    mixed = conn.execute(
        "SELECT COUNT(*) FROM triage_verdicts WHERE is_mixed = 1"
    ).fetchone()[0]
    low_confidence = conn.execute(
        "SELECT COUNT(*) FROM triage_verdicts WHERE confidence < ?",
        (config.TUNABLES.keep_confidence_floor,),
    ).fetchone()[0]

    # How many actual messages each disposition covers — the number that matters
    # for "will my inbox actually be empty".
    messages_by_disposition = {
        r["disposition"]: r["n"]
        for r in conn.execute(
            """
            SELECT v.disposition, SUM(c.message_count) AS n
            FROM triage_verdicts v JOIN clusters c ON c.key = v.cluster_key
            GROUP BY v.disposition
            """
        )
    }

    return {
        "clusters_triaged": total,
        "by_disposition": by_disposition,
        "messages_by_disposition": messages_by_disposition,
        "by_category": by_category,
        "mixed_clusters": mixed,
        "low_confidence": low_confidence,
    }
