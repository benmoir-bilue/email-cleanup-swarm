"""Stage 6 — strategy with Opus 5.

One call, the whole mailbox in context. This is the stage that earns the Opus price:
designing a label taxonomy that fits *this* mailbox rather than a generic template,
and finding the weak signals that per-cluster classification structurally cannot see
— a sender whose behaviour changed, a "newsletter" that quietly carries invoices, a
correspondent who went quiet.

Haiku sees one cluster at a time. Opus sees all ~350 at once, which is the only
vantage point from which cross-cluster patterns are visible.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .. import config, db
from .client import CostTracker, call_json

_OBJ = "object"

SCHEMA = {
    "type": _OBJ,
    "properties": {
        "taxonomy": {
            "type": "array",
            "items": {
                "type": _OBJ,
                "properties": {
                    "label": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["label", "purpose"],
                "additionalProperties": False,
            },
        },
        # Category rules cover the whole mailbox in ~25 entries. Per-cluster rules
        # would need one entry per cluster, which at 1,871 clusters is ~112k output
        # tokens — past Opus 5's 128k ceiling once thinking shares the budget, so the
        # response would truncate mid-JSON.
        "category_rules": {
            "type": "array",
            "items": {
                "type": _OBJ,
                "properties": {
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "disposition": {
                        "type": "string",
                        "enum": ["keep", "archive", "trash", "unsubscribe"],
                    },
                    "reason": {"type": "string"},
                    "filter_worthy": {"type": "boolean"},
                },
                "required": [
                    "category",
                    "label",
                    "disposition",
                    "reason",
                    "filter_worthy",
                ],
                "additionalProperties": False,
            },
        },
        "cluster_overrides": {
            "type": "array",
            "items": {
                "type": _OBJ,
                "properties": {
                    "cluster_key": {"type": "string"},
                    "label": {"type": "string"},
                    "disposition": {
                        "type": "string",
                        "enum": ["keep", "archive", "trash", "unsubscribe"],
                    },
                    "reason": {"type": "string"},
                    "filter_worthy": {"type": "boolean"},
                },
                "required": [
                    "cluster_key",
                    "label",
                    "disposition",
                    "reason",
                    "filter_worthy",
                ],
                "additionalProperties": False,
            },
        },
        "weak_signals": {
            "type": "array",
            "items": {
                "type": _OBJ,
                "properties": {
                    "cluster_key": {"type": "string"},
                    "observation": {"type": "string"},
                    "recommended_action": {"type": "string"},
                },
                "required": ["cluster_key", "observation", "recommended_action"],
                "additionalProperties": False,
            },
        },
        "ambiguities": {
            "type": "array",
            "items": {
                "type": _OBJ,
                "properties": {
                    "cluster_key": {"type": "string"},
                    "question": {"type": "string"},
                    "why_uncertain": {"type": "string"},
                },
                "required": ["cluster_key", "question", "why_uncertain"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": [
        "taxonomy",
        "category_rules",
        "cluster_overrides",
        "weak_signals",
        "ambiguities",
        "notes",
    ],
    "additionalProperties": False,
}

SYSTEM = """You are designing the filing system for one person's personal Gmail \
inbox, which has accumulated roughly 7,000 unsorted messages.

You are given every sender cluster in the mailbox, with a cheap first-pass \
classification already attached to each. Your job is the part that requires seeing \
the whole picture at once.

Produce four things.

1. `taxonomy` — the label tree this specific mailbox needs. Derive it from what is \
actually here, not from a generic template. Use nested Gmail labels with "/" \
(e.g. "Finance/Receipts", "Travel/Bookings"). Aim for 12-25 labels: enough that \
things are findable, few enough that filing is unambiguous. Every label must be \
somewhere a real message in this mailbox belongs. Do not invent labels for \
categories that aren't present.

2. `category_rules` — one rule per first-pass category present in the data, mapping \
it to a label and a disposition. These cover the whole mailbox by default, so there \
must be a rule for every category you see in the cluster list. Set `filter_worthy` \
true when senders in that category will keep sending similar mail, so a standing \
Gmail filter should handle it in future.

3. `cluster_overrides` — individual clusters where the category rule would be wrong. \
Only list a cluster when its correct treatment genuinely differs from its category's \
rule; the category rules should be doing most of the work. Use the exact cluster_key \
strings provided. Expect to need a few dozen of these, not hundreds.

Always name a `label` from your taxonomy in both, including for things you mark for \
deletion. A later stage can overturn a deletion, and when it does the message still \
has to be filed somewhere sensible — anything with no label ends up in a generic \
"unsorted" bucket, which is the outcome the person is trying to escape. Never leave \
`label` empty and never invent a label that isn't in your taxonomy.

4. `weak_signals` — the observations that only the full view makes visible. This is \
the most valuable output. Look for things like:
   - a cluster classified as marketing that also carries transactional records
   - a sender whose character changed over time (a service that became a \
newsletter, a person who became an automated notification)
   - two clusters that are really the same relationship under different addresses
   - a correspondent who was frequent and then went silent
   - subscriptions that appear to be actively billing but never engaged with
   - anything that suggests the cheap first-pass classification got it wrong
Report these only where you actually see evidence in the data. An empty list is a \
valid answer; padding it with speculation is worse than saying nothing.

5. `ambiguities` — clusters where you genuinely cannot tell what the person would \
want, and a human should decide. Keep this list short and high-value: each entry \
costs the person a decision. If a reasonable default exists, use it in `rules` and \
don't ask.

Context you should use:
- A deterministic guard system has already permanently protected every message \
matching financial, legal, medical, identity, security, warranty, or travel \
patterns, plus everything from anyone the person has ever emailed. Those cannot be \
deleted regardless of what you say, so do not spend effort being protective. \
Recommend what actually serves the person.
- Deletion means Gmail Trash, which is reversible for 30 days. It is not permanent.
- The person's instruction was: sort logically, delete what will never be needed \
again, and if in doubt keep it.

Write reasons and observations in plain prose, one or two sentences each. Be \
concrete and specific — name the sender and the evidence. Do not pad, and do not \
restate the input back at me.

Reply only with the JSON object."""


def _fmt_month(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")


def build_context(conn: sqlite3.Connection) -> str:
    """Render the whole mailbox as a compact table.

    One line per cluster. At ~350 clusters this lands around 40-60k tokens, which is
    comfortable in a 1M window, but the format stays terse because it's also what
    Opus has to hold in working memory while reasoning across all of it.
    """
    rows = conn.execute(
        """
        SELECT c.*, v.category, v.disposition AS triage_disposition,
               v.confidence, v.is_mixed, v.keep_signals, v.rationale
        FROM clusters c
        LEFT JOIN triage_verdicts v ON v.cluster_key = c.key
        ORDER BY c.message_count DESC
        """
    ).fetchall()

    labels = [r["name"] for r in conn.execute("SELECT name FROM existing_labels")]
    user_labels = [
        n for n in labels if not n.isupper() and "/" not in n[:1]
    ]

    total_messages = db.message_count(conn)
    protected_senders = conn.execute(
        "SELECT COUNT(*) FROM protected_senders"
    ).fetchone()[0]
    protected_messages = conn.execute(
        "SELECT COUNT(*) FROM message_guards WHERE never_trash = 1"
    ).fetchone()[0]

    # Explicit user rules are authoritative. Put them at the top of the context so the
    # taxonomy Opus designs includes these labels rather than inventing parallel ones
    # ("Projects/Pond" alongside a rule that already says "Projects/Zephyr").
    from ..filing_rules import load_rules

    user_rules = load_rules()
    rules_block: list[str] = []
    if user_rules:
        rules_block = [
            "# Filing rules the owner has already decided — TREAT AS FIXED",
            "These labels are non-negotiable and are applied before your rules. Include",
            "every one of them verbatim in your taxonomy, and do not invent a competing",
            "label for the same purpose. Design the rest of the tree around them.",
            "",
        ]
        for rule in user_rules:
            criteria = []
            if rule.senders:
                criteria.append(f"senders matching {rule.senders}")
            if rule.subject_contains:
                criteria.append(f"subjects containing {rule.subject_contains}")
            rules_block.append(
                f"  {rule.label}  <- {rule.name}: {'; '.join(criteria)}"
                + (f"  ({rule.note})" if rule.note else "")
            )
        rules_block.append("")

    header = rules_block + [
        "# Mailbox overview",
        f"inbox messages: {total_messages:,}",
        f"clusters: {len(rows):,}",
        f"known correspondents (ever emailed): {protected_senders:,}",
        f"messages already permanently protected by guards: {protected_messages:,}",
        "",
        "# Labels that already exist (avoid collisions, reuse where sensible)",
        ", ".join(sorted(user_labels)[:80]) or "(none beyond Gmail defaults)",
        "",
        "# Clusters",
        "Format: key | sender | n msgs (unread) | span | unsub | first-pass category"
        " / disposition / confidence / mixed | guards | sample subjects",
        "",
    ]

    lines: list[str] = []
    for r in rows:
        guard_info = json.loads(r["guard_flags"] or "{}")
        protected = guard_info.get("protected_messages", 0)
        guard_note = f"{protected} protected" if protected else "-"
        if r["never_trash"]:
            guard_note += ", CLUSTER PROTECTED"

        subjects = json.loads(r["sample_subjects"] or "[]")
        subject_blob = " ~ ".join(s[:90] for s in subjects[:5])

        confidence = r["confidence"]
        conf_txt = f"{confidence:.2f}" if confidence is not None else "-"

        lines.append(
            f"{r['key']} | {r['display_name'][:70]} | "
            f"{r['message_count']}({r['unread_count']}) | "
            f"{_fmt_month(r['first_ts'])}..{_fmt_month(r['last_ts'])} | "
            f"{r['unsub_method']} | "
            f"{r['category'] or '?'} / {r['triage_disposition'] or '?'} / {conf_txt}"
            f" / {'MIXED' if r['is_mixed'] else 'uniform'} | "
            f"{guard_note} | {subject_blob}"
        )

    return "\n".join(header + lines)


def run_strategy(
    *, tracker: CostTracker | None = None, progress=None
) -> dict[str, int]:
    """Run the single Opus 5 strategy call and persist its output."""
    emit = progress or (lambda _: None)
    tracker = tracker or CostTracker()

    with db.session() as conn:
        if db.cluster_count(conn) == 0:
            raise RuntimeError("no clusters — run `ecs cluster` first")
        context = build_context(conn)

    emit(f"sending {len(context) // 4:,} est. tokens to {config.MODEL_STRATEGIST}")

    payload = call_json(
        config.MODEL_STRATEGIST,
        system=SYSTEM,
        user=context,
        schema=SCHEMA,
        # Generous: on Opus 5 thinking is ON by default and shares this budget with
        # the answer. A rule per cluster is a lot of output; too tight a ceiling
        # truncates mid-JSON.
        max_tokens=64000,
        effort="high",
        thinking=True,
        tracker=tracker,
    )

    now = datetime.now(UTC).isoformat()
    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs(
                taxonomy, rules, weak_signals, ambiguities, notes, model, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                json.dumps(payload.get("taxonomy", [])),
                json.dumps(
                    {
                        "category_rules": payload.get("category_rules", []),
                        "cluster_overrides": payload.get("cluster_overrides", []),
                    }
                ),
                json.dumps(payload.get("weak_signals", [])),
                json.dumps(payload.get("ambiguities", [])),
                payload.get("notes", ""),
                config.MODEL_STRATEGIST,
                now,
            ),
        )

    emit(f"cost so far: ${tracker.total_cost:.2f}")
    return {
        "labels": len(payload.get("taxonomy", [])),
        "category_rules": len(payload.get("category_rules", [])),
        "cluster_overrides": len(payload.get("cluster_overrides", [])),
        "weak_signals": len(payload.get("weak_signals", [])),
        "ambiguities": len(payload.get("ambiguities", [])),
    }


def latest_strategy(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Most recent strategy run, parsed."""
    row = conn.execute(
        "SELECT * FROM strategy_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None

    # The `rules` column held a flat list in the original per-cluster design and now
    # holds {category_rules, cluster_overrides}. Accept both so an existing run is
    # still readable.
    raw = json.loads(row["rules"])
    if isinstance(raw, list):
        category_rules: list[dict] = []
        cluster_overrides = raw
    else:
        category_rules = raw.get("category_rules", [])
        cluster_overrides = raw.get("cluster_overrides", [])

    return {
        "taxonomy": json.loads(row["taxonomy"]),
        "category_rules": category_rules,
        "cluster_overrides": cluster_overrides,
        "weak_signals": json.loads(row["weak_signals"]),
        "ambiguities": json.loads(row["ambiguities"]),
        "notes": row["notes"],
        "model": row["model"],
        "created_at": row["created_at"],
    }
