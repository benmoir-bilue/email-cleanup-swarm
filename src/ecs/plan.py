"""Stage 9 — merge every verdict into one reviewable ActionPlan.

Several stages have now expressed an opinion about the same message, and they don't
always agree. This module resolves that with a strict precedence order, highest
authority first:

  1. **Human decisions** — a ruling from the TUI ambiguity queue. Absolute.
  2. **Guards** — a `never_trash` message can be relabelled or archived but never
     deleted. Enforced as a downgrade, not a suggestion.
  3. **Challenger** — a refuted deletion is demoted to review, never executed.
  4. **Escalation** — per-message verdicts, which by construction only exist for
     mixed clusters where the cluster-level answer was known to be wrong.
  5. **Strategy** — the Opus label and disposition for the cluster.
  6. **Triage** — the Haiku fallback, used where strategy has no rule.

The precedence is deliberately arranged so that every override moves in the safe
direction. Nothing in this chain can turn a "keep" into a "delete".
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from . import config, db
from .agents.strategist import latest_strategy
from .unsub.parse import best_target

Disposition = Literal["keep", "archive", "trash"]

FALLBACK_LABEL = "Review/Unsorted"
REVIEW_LABEL = "Review/Needs decision"


@dataclass
class MessagePlan:
    message_id: str
    cluster_key: str | None
    disposition: Disposition
    label: str | None
    reason: str
    source: str


# ---------------------------------------------------------------------------
# Label resolution
# ---------------------------------------------------------------------------

_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    # guard category / escalation kind -> words to look for in taxonomy labels
    "tax": ("tax", "finance", "ato"),
    "finance": ("finance", "banking", "money", "statement"),
    "receipt": ("receipt", "purchase", "order", "finance"),
    "invoice": ("invoice", "bill", "finance"),
    "statement": ("statement", "finance", "banking"),
    "insurance": ("insurance", "policy"),
    "legal": ("legal", "contract"),
    "identity": ("identity", "personal", "document"),
    "medical": ("medical", "health"),
    "travel": ("travel", "booking", "trip", "flight"),
    "booking": ("booking", "travel", "reservation"),
    "warranty": ("warranty", "purchase", "product"),
    "security": ("security", "account", "access"),
    "property": ("property", "home", "house"),
    "education": ("education", "learning", "course"),
    "personal": ("personal", "people", "correspondence"),
    "newsletter": ("newsletter", "reading", "subscription"),
    "promotion": ("promotion", "shopping", "marketing", "deals"),
    "notification": ("notification", "services", "alerts"),
}

_TOKEN = re.compile(r"[a-z]+")


def _hint_matches(hint: str, tokens: set[str]) -> bool:
    """Whether a hint word matches any token in a label.

    Stem-tolerant on purpose. The taxonomy is authored by the model at runtime, so
    exact equality is too brittle: "purchase" has to match a label token "Purchases",
    and "tax" has to match "Taxation". Prefix matching in both directions covers the
    plural/derived forms a model will naturally produce, while the length floors stop
    short hints from matching everything.
    """
    if hint in tokens:
        return True
    for token in tokens:
        if len(hint) >= 3 and token.startswith(hint):
            return True
        # Reverse direction catches abbreviations, e.g. hint "promotion" vs a label
        # token "Promo".
        if len(token) >= 4 and hint.startswith(token):
            return True
    return False


def resolve_label(
    taxonomy: list[str], hints: list[str], default: str = FALLBACK_LABEL
) -> str:
    """Pick the taxonomy label that best matches a set of category hints.

    The taxonomy is designed by Opus at runtime, so nothing here can be hardcoded to
    specific label names. Instead each label's word tokens are scored against the
    words associated with the hint categories, which survives whatever naming the
    model chose ("Finance/Receipts", "Money/Purchases", "Records/Financial").
    """
    if not taxonomy:
        return default

    wanted: list[str] = []
    for hint in hints:
        wanted.extend(_CATEGORY_HINTS.get(hint, (hint,)))
    if not wanted:
        return default

    best_label = default
    best_score = 0
    for label in taxonomy:
        tokens = set(_TOKEN.findall(label.lower()))
        score = sum(1 for w in wanted if _hint_matches(w, tokens))
        if score > best_score:
            best_label, best_score = label, score
        elif score == best_score and score > 0:
            # Prefer the more specific (deeper) label when scores tie.
            if label.count("/") > best_label.count("/"):
                best_label = label

    return best_label if best_score > 0 else default


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def build_plan(conn: sqlite3.Connection) -> dict[str, int]:
    """Compute the full plan and write it to `plan_actions`."""
    strategy = latest_strategy(conn)
    taxonomy = (
        [entry["label"] for entry in strategy["taxonomy"]] if strategy else []
    )
    # Two tiers: a rule per triage category covers the mailbox by default, and
    # per-cluster overrides correct the cases where the category is wrong. This is
    # what lets 1,871 clusters be governed by ~25 rules plus a short exception list,
    # instead of an output that would exceed the model's token ceiling.
    category_rules = (
        {r["category"]: r for r in strategy["category_rules"]} if strategy else {}
    )
    cluster_overrides = (
        {r["cluster_key"]: r for r in strategy["cluster_overrides"]}
        if strategy
        else {}
    )

    challenges = {
        r["cluster_key"]: r
        for r in conn.execute("SELECT cluster_key, refuted, argument FROM challenges")
    }
    decisions = {
        r["cluster_key"]: r
        for r in conn.execute(
            "SELECT cluster_key, disposition, label, unsubscribe, note FROM decisions"
        )
    }
    triage = {
        r["cluster_key"]: r
        for r in conn.execute(
            "SELECT cluster_key, disposition, category, is_mixed, confidence, rationale "
            "FROM triage_verdicts"
        )
    }
    escalations = {
        r["message_id"]: r
        for r in conn.execute(
            "SELECT message_id, disposition, label_hint, entities FROM escalations"
        )
    }
    # Preloaded rather than queried per message: this loop runs once per message, and
    # at 23,000+ messages a query inside it is thousands of round trips.
    cluster_names = {
        r["key"]: r["display_name"]
        for r in conn.execute("SELECT key, display_name FROM clusters")
    }

    plans: list[MessagePlan] = []
    unsub_clusters: dict[str, str] = {}  # cluster_key -> reason

    # User filing rules: explicit instructions that no model can infer. Loaded once
    # and matched per message, because a rule may key on subject as well as sender.
    from .filing_rules import first_match, load_rules, rule_labels

    user_rules = load_rules()
    if user_rules:
        # Make the rule labels available to resolve_label, so a rule's folder is a
        # first-class part of the taxonomy rather than an orphan.
        for label in rule_labels(user_rules):
            if label not in taxonomy:
                taxonomy.append(label)

    rows = conn.execute(
        """
        SELECT m.id, m.cluster_key, m.from_addr, m.from_name, m.subject, m.snippet,
               c.never_trash AS cluster_protected,
               COALESCE(g.never_trash, 0) AS msg_protected,
               g.categories
        FROM messages m
        LEFT JOIN clusters c ON c.key = m.cluster_key
        LEFT JOIN message_guards g ON g.message_id = m.id
        """
    ).fetchall()

    for row in rows:
        key = row["cluster_key"]
        guard_categories = json.loads(row["categories"] or "[]")

        # --- 6. Triage floor ---------------------------------------------
        verdict = triage.get(key)
        disposition: str = verdict["disposition"] if verdict else "archive"
        label: str | None = None
        reason = (verdict["rationale"] if verdict else "") or "no classification"
        source = "triage"

        # Low-confidence trash is not trusted; route it to review instead.
        if (
            verdict
            and disposition in ("trash", "unsubscribe")
            and verdict["confidence"] < config.TUNABLES.keep_confidence_floor
        ):
            disposition = "archive"
            label = REVIEW_LABEL
            reason = f"low confidence ({verdict['confidence']:.2f}) — kept for review"
            source = "triage"

        # --- 5. Strategy: category rule, then any cluster override -------
        rule = None
        if verdict and verdict["category"] in category_rules:
            rule = category_rules[verdict["category"]]
            source = "strategy-category"
        if key in cluster_overrides:
            rule = cluster_overrides[key]
            source = "strategy-override"
        if rule:
            disposition = rule.get("disposition", disposition)
            label = rule.get("label") or label
            reason = rule.get("reason") or reason

        if disposition == "unsubscribe":
            # Unsubscribing implies deleting the backlog too.
            unsub_clusters.setdefault(key, reason)
            disposition = "trash"

        # --- 4a. Mixed cluster, no individual verdict --------------------
        # The cluster is known to contain materially different mail, but this message
        # never got escalated (capped run, or a batch failure). There is no honest
        # per-message answer available, so inherit the cluster's disposition rather
        # than inventing a third outcome: parking it in an archive limbo would leave
        # it neither filed nor deleted, which is the worst of both.
        #
        # Safe because the guard pass below still hard-protects every record, and
        # deletion means Trash with a 30-day window.
        if verdict and verdict["is_mixed"] and row["id"] not in escalations:
            reason = (
                f"{reason} (mixed cluster, no individual review — inherited the "
                "cluster decision)"
            )
            source = "cluster-inherited"

        # --- 4b. Escalation (per-message, overrides the cluster) ---------
        escalation = escalations.get(row["id"])
        if escalation:
            disposition = escalation["disposition"]
            entities = json.loads(escalation["entities"] or "{}")
            hints = [escalation["label_hint"], *guard_categories]
            label = resolve_label(taxonomy, [h for h in hints if h], label or FALLBACK_LABEL)
            vendor = entities.get("vendor")
            amount = entities.get("amount")
            detail = " ".join(p for p in (vendor, amount) if p)
            reason = f"individually reviewed: {escalation['label_hint']}" + (
                f" ({detail})" if detail else ""
            )
            source = "escalate"

        # --- 3. Challenger: a refuted deletion is never executed ---------
        challenge = challenges.get(key)
        if disposition == "trash" and challenge and challenge["refuted"]:
            disposition = "archive"
            label = REVIEW_LABEL
            reason = f"deletion challenged: {challenge['argument']}"
            source = "challenge"
            unsub_clusters.pop(key, None)

        # --- 2b. User filing rules: explicit instruction beats every model -
        # Placed above the challenger deliberately: if the user has said where
        # something files, a model's objection doesn't get to overrule them. Still below the
        # guards, so an over-broad rule can misfile but never delete a record.
        if user_rules:
            matched = first_match(
                user_rules,
                from_addr=row["from_addr"] or "",
                from_name=row["from_name"] or "",
                subject=row["subject"] or "",
                snippet=row["snippet"] or "",
            )
            if matched is not None:
                disposition = matched.disposition
                label = matched.label
                reason = f"filing rule: {matched.name}"
                source = "user-rule"
                if matched.disposition == "unsubscribe":
                    unsub_clusters.setdefault(key, f"filing rule: {matched.name}")
                    disposition = "trash"
                elif matched.unsubscribe:
                    unsub_clusters.setdefault(key, f"filing rule: {matched.name}")

        # --- 2. Guards: hard downgrade, no exceptions --------------------
        if disposition == "trash" and (row["msg_protected"] or row["cluster_protected"]):
            disposition = "archive"
            flag = "protected sender/thread" if row["cluster_protected"] else "keep-signal match"
            label = resolve_label(
                taxonomy, guard_categories, label or FALLBACK_LABEL
            )
            reason = f"protected by guard ({flag})"
            source = "guard"

        # --- 1. Human decision: final word -------------------------------
        decision = decisions.get(key)
        if decision:
            disposition = decision["disposition"]
            label = decision["label"] or label
            reason = decision["note"] or "decided in review"
            source = "human"
            if disposition == "trash":
                # Even a human ruling cannot delete a guard-protected message; the
                # OAuth scope can't hard-delete and the guard exists precisely to
                # survive a hasty click. Downgrade and say so.
                if row["msg_protected"]:
                    disposition = "archive"
                    reason = "approved for deletion, but guard-protected — archived"
            if decision["unsubscribe"]:
                unsub_clusters.setdefault(key, "approved in review")

        if disposition not in ("keep", "archive", "trash"):
            disposition = "archive"

        # Everything retained must be genuinely filed. Try progressively weaker
        # signals before conceding to the catch-all, because a label of
        # "Review/Unsorted" is a junk drawer — it moves a message out of the inbox
        # without answering the question the user actually asked.
        if disposition != "trash" and not label:
            hints = [
                *guard_categories,
                *([verdict["category"]] if verdict else []),
            ]
            label = resolve_label(taxonomy, [h for h in hints if h], "")
            if not label and rule and rule.get("label"):
                # The strategist named a label for this cluster even though the
                # disposition changed under it; that is still the best guess available.
                label = rule["label"]
            if not label and taxonomy:
                # Last resort before the junk drawer: match on the cluster's own
                # display name, which often carries the sender's domain or purpose.
                name = cluster_names.get(key, "")
                if name:
                    label = resolve_label(taxonomy, _TOKEN.findall(name.lower()), "")
            label = label or FALLBACK_LABEL

        plans.append(
            MessagePlan(
                message_id=row["id"],
                cluster_key=key,
                disposition=disposition,  # type: ignore[arg-type]
                label=label if disposition != "trash" else None,
                reason=reason,
                source=source,
            )
        )

    _write_plan(conn, plans)
    _write_unsub_targets(conn, unsub_clusters)

    counts = {"keep": 0, "archive": 0, "trash": 0}
    for p in plans:
        counts[p.disposition] += 1
    return {
        "messages": len(plans),
        **counts,
        "unsubscribe_targets": len(unsub_clusters),
    }


def _write_plan(conn: sqlite3.Connection, plans: list[MessagePlan]) -> None:
    """Materialise the plan as discrete actions.

    An archived message produces two actions (label, then archive) so the apply stage
    can batch by action type and the journal records each mutation separately.

    Approvals and applied-markers survive a rebuild. Reviewing 30,000 actions in the
    TUI is real work, and silently discarding it because a later stage re-ran would
    make the plan feel untrustworthy. Carried over by (message_id, action, label), so
    an action whose *decision changed* correctly loses its approval and has to be
    re-reviewed — which is the point.
    """
    previous = {
        (r["message_id"], r["action"], r["label"]): (r["approved"], r["applied_at"], r["wave"])
        for r in conn.execute(
            "SELECT message_id, action, label, approved, applied_at, wave "
            "FROM plan_actions"
        )
    }

    conn.execute("DELETE FROM plan_actions")
    rows: list[tuple] = []

    def emit(message_id: str, cluster_key: str | None, action: str,
             label: str | None, reason: str, source: str) -> None:
        approved, applied_at, wave = previous.get(
            (message_id, action, label), (0, None, None)
        )
        rows.append(
            (message_id, cluster_key, action, label, reason, source,
             approved, applied_at, wave)
        )

    for p in plans:
        if p.disposition == "trash":
            emit(p.message_id, p.cluster_key, "trash", None, p.reason, p.source)
            continue
        if p.label:
            emit(p.message_id, p.cluster_key, "add_label", p.label, p.reason, p.source)
        if p.disposition == "archive":
            emit(p.message_id, p.cluster_key, "archive", None, p.reason, p.source)

    conn.executemany(
        """
        INSERT INTO plan_actions(
            message_id, cluster_key, action, label, reason, source,
            approved, applied_at, wave
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _write_unsub_targets(conn: sqlite3.Connection, clusters: dict[str, str]) -> None:
    """Build the unsubscribe worklist from clusters marked for it."""
    conn.execute("DELETE FROM unsub_targets")
    rows: list[tuple] = []

    for key in clusters:
        # Use the most recent message's headers — unsubscribe endpoints rotate, and
        # an expired token from three years ago will just 404.
        row = conn.execute(
            """
            SELECT list_unsubscribe, list_unsubscribe_post
            FROM messages
            WHERE cluster_key = ? AND list_unsubscribe IS NOT NULL
            ORDER BY date_ts DESC LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row is None:
            continue
        target = best_target(row["list_unsubscribe"], row["list_unsubscribe_post"])
        if target is None:
            continue
        rows.append((key, target.method, target.endpoint))

    conn.executemany(
        "INSERT INTO unsub_targets(cluster_key, method, endpoint) VALUES(?,?,?)",
        rows,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def plan_report(conn: sqlite3.Connection) -> dict[str, object]:
    by_action = {
        r["action"]: r["n"]
        for r in conn.execute(
            "SELECT action, COUNT(*) AS n FROM plan_actions GROUP BY action"
        )
    }
    by_source = {
        r["source"]: r["n"]
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM plan_actions GROUP BY source ORDER BY n DESC"
        )
    }
    labels = {
        r["label"]: r["n"]
        for r in conn.execute(
            "SELECT label, COUNT(*) AS n FROM plan_actions "
            "WHERE label IS NOT NULL GROUP BY label ORDER BY n DESC"
        )
    }
    trash_msgs = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM plan_actions WHERE action = 'trash'"
    ).fetchone()[0]
    inbox_after = conn.execute(
        """
        SELECT COUNT(*) FROM messages
        WHERE id NOT IN (
            SELECT message_id FROM plan_actions WHERE action IN ('archive', 'trash')
        )
        """
    ).fetchone()[0]
    unsub = {
        r["method"]: r["n"]
        for r in conn.execute(
            "SELECT method, COUNT(*) AS n FROM unsub_targets GROUP BY method"
        )
    }
    # How much ended up in a holding bucket rather than genuinely filed or deleted.
    # Surfaced deliberately: these are the messages the system failed to decide about,
    # and hiding the count would make a half-finished job look complete.
    unsorted = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM plan_actions WHERE label = ?",
        (FALLBACK_LABEL,),
    ).fetchone()[0]
    needs_decision = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM plan_actions WHERE label = ?",
        (REVIEW_LABEL,),
    ).fetchone()[0]
    inherited = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM plan_actions "
        "WHERE source = 'cluster-inherited'"
    ).fetchone()[0]

    return {
        "by_action": by_action,
        "by_source": by_source,
        "labels": labels,
        "messages_to_trash": trash_msgs,
        "messages_left_in_inbox": inbox_after,
        "unsubscribe_by_method": unsub,
        "unsorted": unsorted,
        "needs_decision": needs_decision,
        "cluster_inherited": inherited,
        "approved": conn.execute(
            "SELECT COUNT(*) FROM plan_actions WHERE approved = 1"
        ).fetchone()[0],
    }
