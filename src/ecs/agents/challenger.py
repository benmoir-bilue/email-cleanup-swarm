"""Stage 7 — adversarial review of every proposed deletion, with Fable 5.

Runs only over clusters marked for trash or unsubscribe. The model's job is to argue
*against* deletion. Anything it successfully challenges is demoted out of the delete
plan and into the human review queue.

The calibration here is the whole game. A capable model told to "default to keeping"
will refute every single deletion — every email is theoretically useful to someone
someday — and the system becomes an expensive no-op. So the instruction is
specifically that a refutation must name a concrete, plausible scenario. Generic
"you might want this one day" reasoning is explicitly ruled out, because it applies
uniformly to all mail and therefore protects nothing.

Fable 5 API notes: the `thinking` parameter must be omitted entirely (any explicit
config returns 400), depth is controlled by `output_config.effort`, and the org needs
30-day data retention — a zero-data-retention org gets a 400 on every request. If
Fable is unavailable, `config.CHALLENGER_FALLBACK` runs the same prompt on Opus 5.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import anthropic

from .. import config, db
from .client import CostTracker, RefusalError, call_json

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cluster_key": {"type": "string"},
                    "refuted": {"type": "boolean"},
                    "argument": {"type": "string"},
                },
                "required": ["cluster_key", "refuted", "argument"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

SYSTEM = """You are the last check before email is deleted from someone's personal \
mailbox. Every cluster below has been marked for deletion by an earlier stage. Your \
job is to try to refute those decisions.

For each cluster, set `refuted` true only if you can name a specific, plausible \
scenario in which this person would need this mail again — a tax or warranty period \
that hasn't expired, a record they'd need to prove something, a relationship the \
earlier stage misread as automated, a subscription that's still billing them, \
evidence in the subject lines that the cluster contains something other than what \
it was labelled.

Do not refute on the grounds that email in general might theoretically be useful \
one day. That argument is true of every message ever sent, so it protects nothing \
and only prevents the mailbox from being cleaned. If your reasoning for keeping a \
cluster would apply equally well to any other cluster in the list, that is a sign \
it isn't a real objection.

Deletion here means Gmail Trash, which is reversible for 30 days. Anything matching \
financial, legal, medical, identity, security, warranty, or travel patterns has \
already been permanently protected by a separate deterministic system and is not in \
this list. So the bar for refuting is genuinely: is there specific evidence this \
particular cluster was misjudged?

`argument` should be one or two sentences. When you refute, state the concrete \
reason. When you don't, state briefly why the deletion looks correct.

Return a verdict for every cluster_key given, using the exact key strings.

Reply only with the JSON object."""


def _fmt_month(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")


def build_group_prompt(rows: list[sqlite3.Row]) -> str:
    """Render a group of delete candidates for review."""
    blocks = []
    for r in rows:
        subjects = json.loads(r["sample_subjects"] or "[]")
        guard_info = json.loads(r["guard_flags"] or "{}")
        protected = guard_info.get("protected_messages", 0)

        lines = [
            f"cluster_key: {r['key']}",
            f"sender: {r['display_name']}",
            f"messages: {r['message_count']} ({r['unread_count']} never opened)",
            f"span: {_fmt_month(r['first_ts'])} to {_fmt_month(r['last_ts'])}",
            f"proposed action: {r['proposed_disposition']}",
            f"classified as: {r['category'] or 'unknown'}",
            f"reason given: {r['reason'] or r['rationale'] or '-'}",
        ]
        if protected:
            lines.append(
                f"note: {protected} messages in this cluster are already protected "
                "and will not be deleted"
            )
        lines.append("sample subjects:")
        lines.extend(f"  - {s[:150]}" for s in subjects)
        blocks.append("\n".join(lines))

    return "\n\n---\n\n".join(blocks)


def _delete_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Clusters currently slated for deletion.

    Prefers the strategist's ruling where one exists, falling back to triage. Guard-
    protected clusters are excluded — they were never deletable, so spending Fable
    tokens arguing about them would be waste.
    """
    from .strategist import latest_strategy

    # Two-tier resolution, matching plan.py: a category rule governs by default and a
    # cluster override corrects it. Reading a flat `rules` list here was a real bug —
    # it raised KeyError after the strategist was restructured, silently producing zero
    # verdicts and letting 13,000 deletions through with no adversarial review.
    strategy = latest_strategy(conn)
    category_rules: dict[str, dict] = {}
    cluster_overrides: dict[str, dict] = {}
    if strategy:
        category_rules = {r["category"]: r for r in strategy["category_rules"]}
        cluster_overrides = {
            r["cluster_key"]: r for r in strategy["cluster_overrides"]
        }

    rows = conn.execute(
        """
        SELECT c.*, v.category, v.disposition AS triage_disposition, v.rationale
        FROM clusters c
        LEFT JOIN triage_verdicts v ON v.cluster_key = c.key
        WHERE c.never_trash = 0
        ORDER BY c.message_count DESC
        """
    ).fetchall()

    candidates = []
    for row in rows:
        rule = cluster_overrides.get(row["key"]) or category_rules.get(
            row["category"] or ""
        )
        disposition = (
            rule.get("disposition") if rule else row["triage_disposition"]
        )
        if disposition not in ("trash", "unsubscribe"):
            continue
        # sqlite3.Row is immutable, so carry the derived fields in a dict wrapper
        # that still supports key access.
        merged = dict(row)
        merged["proposed_disposition"] = disposition
        merged["reason"] = rule.get("reason") if rule else None
        candidates.append(merged)
    return candidates


def challenge_deletions(
    *,
    limit: int | None = None,
    only_missing: bool = True,
    tracker: CostTracker | None = None,
    progress=None,
) -> dict[str, int]:
    """Run the adversarial pass. Returns counts including how many were demoted."""
    emit = progress or (lambda _: None)
    tracker = tracker or CostTracker()

    with db.session() as conn:
        candidates = _delete_candidates(conn)
        if only_missing:
            done = {
                r["cluster_key"]
                for r in conn.execute("SELECT cluster_key FROM challenges")
            }
            candidates = [c for c in candidates if c["key"] not in done]

    if limit:
        candidates = candidates[:limit]

    if not candidates:
        emit("no delete candidates to challenge")
        return {"reviewed": 0, "refuted": 0, "upheld": 0, "failed": 0}

    group_size = config.TUNABLES.challenge_group_size
    groups = [
        candidates[i : i + group_size]
        for i in range(0, len(candidates), group_size)
    ]

    model = config.MODEL_CHALLENGER
    emit(
        f"challenging {len(candidates):,} delete candidates in "
        f"{len(groups)} groups with {model}"
    )

    all_verdicts: list[tuple] = []
    failures = 0
    now = datetime.now(UTC).isoformat()

    for idx, group in enumerate(groups, start=1):
        prompt = build_group_prompt(group)
        try:
            payload = call_json(
                model,
                system=SYSTEM,
                user=prompt,
                schema=SCHEMA,
                # Fable's thinking is always on and cannot be budgeted, so leave
                # generous headroom above the ~20 arguments of actual output.
                max_tokens=32000,
                effort="high",
                thinking=False,  # ignored for Fable; the param is omitted entirely
                tracker=tracker,
            )
        except RefusalError as exc:
            emit(f"[warn] group {idx}: {exc}")
            failures += len(group)
            continue
        except (anthropic.BadRequestError, anthropic.NotFoundError) as exc:
            # Most likely causes: the org is on zero data retention, or Fable isn't
            # enabled. Fall back to Opus rather than losing the safety pass.
            emit(f"[warn] {model} unavailable ({exc}); falling back to "
                 f"{config.CHALLENGER_FALLBACK}")
            model = config.CHALLENGER_FALLBACK
            payload = call_json(
                model,
                system=SYSTEM,
                user=prompt,
                schema=SCHEMA,
                max_tokens=32000,
                effort="high",
                thinking=True,
                tracker=tracker,
            )

        keys_in_group = {g["key"] for g in group}
        returned = set()
        group_verdicts: list[tuple] = []
        for verdict in payload.get("verdicts", []):
            key = verdict.get("cluster_key")
            if key not in keys_in_group:
                continue
            returned.add(key)
            group_verdicts.append(
                (
                    key,
                    1 if verdict.get("refuted") else 0,
                    verdict.get("argument", ""),
                    model,
                    now,
                )
            )

        # A cluster the model silently skipped has not been cleared for deletion.
        # Treat the omission as a refusal to endorse and route it to human review.
        for missing in keys_in_group - returned:
            group_verdicts.append(
                (
                    missing,
                    1,
                    "No verdict returned by the challenger; routed to human review "
                    "rather than deleted unreviewed.",
                    model,
                    now,
                )
            )

        all_verdicts.extend(group_verdicts)

        # Persist after every group, not at the end. Fable is the most expensive model
        # in the pipeline and this loop can run half an hour; batching all writes until
        # the last group means a crash at group 27 of 29 throws away every dollar spent.
        # Writing incrementally also makes the stage resumable, since `only_missing`
        # skips clusters that already have a verdict.
        with db.session() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO challenges(
                    cluster_key, refuted, argument, model, created_at
                ) VALUES(?,?,?,?,?)
                """,
                group_verdicts,
            )

        emit(
            f"group {idx}/{len(groups)} done — "
            f"{sum(1 for v in group_verdicts if v[1]) } challenged of "
            f"{len(group_verdicts)} (${tracker.total_cost:.2f} so far)"
        )

    refuted = sum(1 for v in all_verdicts if v[1] == 1)
    return {
        "reviewed": len(all_verdicts),
        "refuted": refuted,
        "upheld": len(all_verdicts) - refuted,
        "failed": failures,
    }


def challenge_report(conn: sqlite3.Connection) -> dict[str, object]:
    total = conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]
    refuted = conn.execute(
        "SELECT COUNT(*) FROM challenges WHERE refuted = 1"
    ).fetchone()[0]
    messages_saved = conn.execute(
        """
        SELECT COALESCE(SUM(c.message_count), 0)
        FROM challenges ch JOIN clusters c ON c.key = ch.cluster_key
        WHERE ch.refuted = 1
        """
    ).fetchone()[0]
    return {
        "reviewed": total,
        "refuted": refuted,
        "upheld": total - refuted,
        "messages_rescued": messages_saved,
    }
