"""Stage 11 — emit Gmail filters so the cleanup holds.

Sorting 7,000 messages once is a one-off win; without standing rules the inbox refills
within weeks. This turns the approved plan into server-side Gmail filters that apply
the same decisions to future mail, with no client running.

Only clusters the strategist marked `filter_worthy` become filters, and only above a
size threshold — a rule matching a sender who wrote once is clutter, and Gmail's filter
list is a resource a human has to maintain by hand later.

Filters are journalled like every other mutation, so `ecs undo` removes exactly the
ones this tool created.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import config, db
from .agents.strategist import latest_strategy
from .journal import Entry, Journal


@dataclass
class FilterSpec:
    cluster_key: str
    display_name: str
    criteria: dict[str, str]
    label: str | None
    trash: bool
    message_count: int

    def describe(self) -> str:
        match = ", ".join(f"{k}:{v}" for k, v in self.criteria.items())
        action = "delete" if self.trash else f"label {self.label!r} + archive"
        return f"{match} -> {action}"


def _criteria_for(row: sqlite3.Row) -> dict[str, str] | None:
    """Build filter criteria matching how the cluster was formed.

    Mirrors the clustering precedence: a List-Id cluster filters on the list, a sender
    cluster on the address, a rotating-sender cluster on the domain.
    """
    if row["kind"] == "list_id" and row["list_id"]:
        return {"query": f"list:{row['list_id']}"}
    if row["kind"] == "sender" and row["sender_addr"]:
        return {"from": row["sender_addr"]}
    if row["sender_domain"]:
        # Domain-level match. Broader than the cluster, so it's only emitted when the
        # cluster genuinely came from rotating local parts at one domain.
        return {"from": f"@{row['sender_domain']}"}
    return None


def build_filter_specs(conn: sqlite3.Connection) -> list[FilterSpec]:
    """Derive the filter set from the approved plan and strategy rules."""
    strategy = latest_strategy(conn)
    if not strategy:
        return []

    # Same two-tier resolution as the plan: category rule by default, cluster
    # override where one exists.
    category_rules = {r["category"]: r for r in strategy["category_rules"]}
    cluster_overrides = {r["cluster_key"]: r for r in strategy["cluster_overrides"]}
    categories = {
        r["cluster_key"]: r["category"]
        for r in conn.execute("SELECT cluster_key, category FROM triage_verdicts")
    }

    def rule_for(cluster_key: str) -> dict | None:
        if cluster_key in cluster_overrides:
            return cluster_overrides[cluster_key]
        return category_rules.get(categories.get(cluster_key, ""))
    decisions = {
        r["cluster_key"]: r
        for r in conn.execute("SELECT cluster_key, disposition, label FROM decisions")
    }
    challenged = {
        r["cluster_key"]
        for r in conn.execute("SELECT cluster_key FROM challenges WHERE refuted = 1")
    }

    specs: list[FilterSpec] = []
    min_size = config.TUNABLES.min_cluster_size_for_rule

    for row in conn.execute("SELECT * FROM clusters ORDER BY message_count DESC"):
        key = row["key"]
        rule = rule_for(key)
        if not rule or not rule.get("filter_worthy"):
            continue
        if row["message_count"] < min_size:
            continue
        # A cluster protected by guards should never get an auto-delete rule, and its
        # mail is worth seeing.
        if row["never_trash"]:
            continue

        disposition = rule.get("disposition")
        label = rule.get("label")
        decision = decisions.get(key)
        if decision:
            disposition = decision["disposition"]
            label = decision["label"] or label

        if disposition == "unsubscribe":
            disposition = "trash"

        # Never auto-delete something the challenger argued against. Its future mail
        # should keep arriving until you decide otherwise.
        if disposition == "trash" and key in challenged:
            continue
        if disposition == "keep":
            continue  # nothing to automate; it belongs in the inbox

        criteria = _criteria_for(row)
        if criteria is None:
            continue

        specs.append(
            FilterSpec(
                cluster_key=key,
                display_name=row["display_name"],
                criteria=criteria,
                label=label if disposition == "archive" else None,
                trash=disposition == "trash",
                message_count=row["message_count"],
            )
        )
    return specs


def apply_filters(
    specs: list[FilterSpec], *, dry_run: bool = True, progress=None
) -> dict[str, int]:
    """Create the filters in Gmail. Requires the gmail.settings.basic scope."""
    emit = progress or (lambda _: None)

    if dry_run:
        for spec in specs:
            emit(f"[dry run] {spec.describe()}")
        return {"created": 0, "skipped": len(specs), "failed": 0}

    from .gmail.auth import service
    from .gmail.mutate import create_filter, ensure_labels, list_filters

    svc = service()
    journal = Journal()

    # Don't duplicate a filter that already matches the same criteria.
    existing = list_filters(svc)
    existing_criteria = {
        json.dumps(f.get("criteria", {}), sort_keys=True) for f in existing
    }

    wanted_labels = sorted({s.label for s in specs if s.label})
    label_ids: dict[str, str] = {}
    if wanted_labels:
        label_ids, created = ensure_labels(svc, wanted_labels, progress=emit)
        for name, label_id in created:
            entry = Entry(op="create_label", after={"name": name, "label_id": label_id})
            journal.record(entry)
            journal.commit(entry)

    created_count = 0
    skipped = 0
    failed = 0

    for spec in specs:
        fingerprint = json.dumps(spec.criteria, sort_keys=True)
        if fingerprint in existing_criteria:
            skipped += 1
            emit(f"skipped (filter already exists): {spec.describe()}")
            continue

        action: dict[str, list[str]] = {}
        if spec.trash:
            action["addLabelIds"] = ["TRASH"]
        else:
            label_id = label_ids.get(spec.label or "")
            if not label_id:
                failed += 1
                emit(f"[warn] no label id for {spec.label!r}, skipping filter")
                continue
            action["addLabelIds"] = [label_id]
            action["removeLabelIds"] = ["INBOX"]

        entry = Entry(
            op="create_filter",
            cluster_key=spec.cluster_key,
            after={"criteria": spec.criteria, "action": action},
        )
        journal.record(entry)
        try:
            filter_id = create_filter(svc, spec.criteria, action)
        except Exception as exc:
            failed += 1
            journal.commit(entry, error=str(exc))
            emit(f"[warn] failed to create filter for {spec.display_name}: {exc}")
            continue

        entry.after["filter_id"] = filter_id
        journal.commit(entry)
        existing_criteria.add(fingerprint)
        created_count += 1
        emit(f"created: {spec.describe()}")

    return {"created": created_count, "skipped": skipped, "failed": failed}


def filter_report(conn: sqlite3.Connection) -> dict[str, object]:
    specs = build_filter_specs(conn)
    return {
        "candidates": len(specs),
        "archive_rules": sum(1 for s in specs if not s.trash),
        "delete_rules": sum(1 for s in specs if s.trash),
        "messages_covered": sum(s.message_count for s in specs),
        "specs": specs,
    }
