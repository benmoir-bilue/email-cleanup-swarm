"""Stage 8 — per-message escalation with Haiku 4.5.

The only stage that scales with message count rather than cluster count. It runs on
unprotected messages inside clusters that triage flagged as *mixed*.

Those clusters are exactly where bulk action fails. A retailer that sends both
promotions and order receipts cannot be handled correctly at the cluster level — the
choice would be "delete the receipts" or "keep 300 promos". Escalation reads the
individual messages and splits them, so each one ends up genuinely filed or genuinely
deleted rather than parked in an archive limbo.

Uncapped by default. A cap is available, but capping is a real loss of quality, not a
cost optimisation: an unreviewed message in a cluster *known* to be mixed has no
individual verdict, so the plan can only fall back to the cluster's coarse disposition
— precisely the call this stage exists to refine. The CLI estimates the cost up front
and asks for confirmation instead of silently truncating.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .. import config, db
from ..config import PRICING_PER_MTOK
from .client import BatchRequest, CostTracker, build_params, run_batch
from .triage import _safe_id

SCHEMA = {
    "type": "object",
    "properties": {
        "disposition": {
            "type": "string",
            "enum": ["keep", "archive", "trash"],
        },
        "kind": {
            "type": "string",
            "enum": [
                "receipt",
                "invoice",
                "statement",
                "booking",
                "notification",
                "promotion",
                "newsletter",
                "personal",
                "security",
                "other",
            ],
        },
        "vendor": {"type": "string"},
        "amount": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["disposition", "kind", "vendor", "amount", "rationale"],
    "additionalProperties": False,
}

SYSTEM = """You are deciding what to do with individual emails from a personal \
mailbox. Each one comes from a sender whose mail is known to be MIXED — the same \
sender sends both disposable and worth-keeping messages — so each message has to be \
judged on its own.

Choose a disposition:
- "archive": has future reference value. Receipts, invoices, statements, bookings, \
confirmations of things that happened, real correspondence, anything proving a \
transaction or commitment.
- "trash": no future value. Promotions, expired offers, "we miss you" nudges, \
newsletters, notifications about things already resolved.
- "keep": still needs action from the person. Rare — use only if something is \
plainly outstanding.

Also record:
- "kind": what this message actually is.
- "vendor": the company or person it concerns, or "" if unclear.
- "amount": any monetary amount central to the message (e.g. "$149.00"), or "" if \
none. This helps label receipts usefully.
- "rationale": one short sentence.

Judge from the content, not the sender's general reputation. A promotional sender \
can still send a genuine receipt, and a transactional sender can still send \
marketing.

Reply only with the JSON object."""


def estimate_cost(candidates: int) -> dict[str, float]:
    """Rough cost for escalating N messages via the Batch API.

    Per message: ~700 input tokens (headers plus a body truncated to 2,500 chars) and
    ~120 output. Batch pricing halves both. Deliberately an over-estimate — being
    surprised by a bill is worse than being surprised by a refund.
    """
    rate_in, rate_out = PRICING_PER_MTOK.get(config.MODEL_ESCALATE, (1.0, 5.0))
    tokens_in = candidates * 700
    tokens_out = candidates * 120
    cost = (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000 * 0.5
    return {
        "messages": candidates,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost": cost,
    }


def _candidates(conn: sqlite3.Connection, *, limit: int | None) -> list[sqlite3.Row]:
    """Unprotected messages inside mixed clusters, newest first.

    Protected messages are excluded: they cannot be trashed regardless of outcome, so
    the only thing escalation could add is a label — and the guard category already
    supplies one. Spending tokens on them would be waste.
    """
    return conn.execute(
        """
        SELECT m.id, m.subject, m.snippet, m.from_addr, m.from_name, m.date_ts,
               m.cluster_key, c.display_name
        FROM messages m
        JOIN clusters c ON c.key = m.cluster_key
        JOIN triage_verdicts v ON v.cluster_key = m.cluster_key
        LEFT JOIN message_guards g ON g.message_id = m.id
        WHERE v.is_mixed = 1
          AND COALESCE(g.never_trash, 0) = 0
          AND m.id NOT IN (SELECT message_id FROM escalations)
        ORDER BY m.date_ts DESC
        """
        + ("LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()


def _count_all_candidates(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM messages m
        JOIN triage_verdicts v ON v.cluster_key = m.cluster_key
        LEFT JOIN message_guards g ON g.message_id = m.id
        WHERE v.is_mixed = 1
          AND COALESCE(g.never_trash, 0) = 0
          AND m.id NOT IN (SELECT message_id FROM escalations)
        """
    ).fetchone()[0]


def build_prompt(row: sqlite3.Row, body: str | None) -> str:
    when = (
        datetime.fromtimestamp(row["date_ts"], tz=UTC).strftime("%Y-%m-%d")
        if row["date_ts"]
        else "unknown date"
    )
    parts = [
        f"From: {row['from_name'] or ''} <{row['from_addr'] or ''}>",
        f"Date: {when}",
        f"Subject: {row['subject'] or '(no subject)'}",
        "",
    ]
    if body:
        parts.append(body)
    else:
        # No body available (fetch failed or was skipped) — the snippet is still
        # usually enough to separate a receipt from a promotion.
        parts.append(row["snippet"] or "(no preview available)")
    return "\n".join(parts)


def escalate_messages(
    *,
    limit: int | None = None,
    fetch_bodies: bool = True,
    tracker: CostTracker | None = None,
    progress=None,
) -> dict[str, int]:
    """Classify individual messages in mixed clusters."""
    rep = progress or (lambda _: None)
    has_bar = hasattr(rep, "stage")
    tracker = tracker or CostTracker()
    # `limit` overrides the configured budget; both None means escalate everything,
    # which is the default and the point of the stage.
    budget = limit if limit is not None else config.TUNABLES.max_escalated_messages

    with db.session() as conn:
        total_candidates = _count_all_candidates(conn)
        rows = _candidates(conn, limit=budget)

    if not rows:
        rep("no messages need escalation")
        return {
            "candidates": total_candidates,
            "escalated": 0,
            "skipped": 0,
            "failed": 0,
        }

    skipped = max(0, total_candidates - len(rows))
    if skipped:
        warn = getattr(rep, "warn", rep)
        warn(
            f"budget cap {budget:,} reached: {skipped:,} messages will NOT get an "
            "individual decision and will inherit their cluster's disposition"
        )

    if fetch_bodies:
        from ..gmail.bodies import fetch_bodies as do_fetch

        rep(f"fetching {len(rows):,} message bodies")
        do_fetch([r["id"] for r in rows], progress=rep)

    with db.session() as conn:
        bodies = {
            r["message_id"]: r["text"]
            for r in conn.execute(
                "SELECT message_id, text FROM bodies WHERE message_id IN "
                f"({','.join('?' * len(rows))})",
                [r["id"] for r in rows],
            )
        }

    requests = [
        BatchRequest(
            custom_id=_safe_id(row["id"]),
            params=build_params(
                config.MODEL_ESCALATE,
                system=SYSTEM,
                messages=[
                    {"role": "user", "content": build_prompt(row, bodies.get(row["id"]))}
                ],
                max_tokens=1200,
                schema=SCHEMA,
            ),
        )
        for row in rows
    ]
    id_to_message = {_safe_id(r["id"]): r["id"] for r in rows}

    rep(f"escalating {len(requests):,} messages with {config.MODEL_ESCALATE}")
    results, errors = run_batch(
        requests, model=config.MODEL_ESCALATE, tracker=tracker, progress=rep, resume_key="batch.escalate"
    )

    now = datetime.now(UTC).isoformat()
    out_rows = []
    for custom_id, payload in results.items():
        message_id = id_to_message.get(custom_id)
        if message_id is None:
            continue
        entities = {
            k: v
            for k, v in (
                ("vendor", payload.get("vendor", "")),
                ("amount", payload.get("amount", "")),
            )
            if v
        }
        out_rows.append(
            (
                message_id,
                payload.get("disposition", "archive"),
                payload.get("kind", "other"),
                json.dumps(entities),
                payload.get("rationale", ""),
                config.MODEL_ESCALATE,
                now,
            )
        )

    with db.session() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO escalations(
                message_id, disposition, label_hint, entities, rationale,
                model, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            out_rows,
        )
        db.kv_set(conn, "escalate.skipped", skipped)

    return {
        "candidates": total_candidates,
        "escalated": len(out_rows),
        "skipped": skipped,
        "failed": len(errors),
    }


def escalate_report(conn: sqlite3.Connection) -> dict[str, object]:
    total = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
    by_disposition = {
        r["disposition"]: r["n"]
        for r in conn.execute(
            "SELECT disposition, COUNT(*) AS n FROM escalations GROUP BY disposition"
        )
    }
    by_kind = {
        r["label_hint"]: r["n"]
        for r in conn.execute(
            "SELECT label_hint, COUNT(*) AS n FROM escalations "
            "GROUP BY label_hint ORDER BY n DESC"
        )
    }
    return {
        "escalated": total,
        "by_disposition": by_disposition,
        "by_kind": by_kind,
        "skipped_over_budget": db.kv_get(conn, "escalate.skipped", 0),
    }
