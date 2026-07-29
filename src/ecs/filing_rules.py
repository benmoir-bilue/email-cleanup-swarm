"""User-supplied filing rules.

Some filing decisions cannot be inferred from the mail itself. That a cluster of
messages from two unrelated-looking companies is actually one project you know about
is knowledge that lives in your head — no model reading subject lines will produce
that grouping, and no amount of prompt tuning will fix it.

So this is the channel for it: a TOML file of explicit rules, evaluated before any
model verdict is consulted. Rules feed the strategist's context too, so the taxonomy
it designs includes the labels you asked for rather than inventing parallel ones.

Two design choices worth stating:

* **First match wins, top to bottom.** Ordering is how you express specificity —
  a project rule above a broader "someone's mail" rule sends their project forwards to
  the project folder and everything else to their personal folder. Precedence you can
  read off the file beats a scoring system you have to simulate in your head.

* **Rules cannot cause deletion of a protected record.** They sit above the model
  stages in precedence but still below the keep-signal guards, so an over-broad rule
  costs you a misfiling, never a lost receipt.
"""

from __future__ import annotations

import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("filing-rules.toml")

VALID_DISPOSITIONS = {"keep", "archive", "trash", "unsubscribe"}


@dataclass
class FilingRule:
    name: str
    label: str
    disposition: str = "archive"
    senders: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)
    match: str = "any"  # any | all
    unsubscribe: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in VALID_DISPOSITIONS:
            raise ValueError(
                f"rule {self.name!r}: disposition must be one of "
                f"{sorted(VALID_DISPOSITIONS)}, got {self.disposition!r}"
            )
        if self.match not in ("any", "all"):
            raise ValueError(f"rule {self.name!r}: match must be 'any' or 'all'")
        if not (self.senders or self.subject_contains or self.body_contains):
            raise ValueError(f"rule {self.name!r}: needs at least one match criterion")
        # Normalise once so matching is a cheap substring test per message.
        self.senders = [s.lower() for s in self.senders]
        self.subject_contains = [s.lower() for s in self.subject_contains]
        self.body_contains = [s.lower() for s in self.body_contains]

    # -- matching ---------------------------------------------------------

    def _sender_hit(self, from_addr: str, from_name: str) -> bool:
        haystack = f"{from_addr} {from_name}".lower()
        return any(s in haystack for s in self.senders)

    def _subject_hit(self, subject: str) -> bool:
        low = subject.lower()
        return any(_contains(low, term) for term in self.subject_contains)

    def _body_hit(self, snippet: str) -> bool:
        low = snippet.lower()
        return any(_contains(low, term) for term in self.body_contains)

    def matches(
        self, *, from_addr: str, from_name: str, subject: str, snippet: str
    ) -> bool:
        checks: list[bool] = []
        if self.senders:
            checks.append(self._sender_hit(from_addr or "", from_name or ""))
        if self.subject_contains:
            checks.append(self._subject_hit(subject or ""))
        if self.body_contains:
            checks.append(self._body_hit(snippet or ""))
        if not checks:
            return False
        return all(checks) if self.match == "all" else any(checks)


def _contains(haystack: str, term: str) -> bool:
    """Substring match, but word-bounded for short terms.

    Without this, a term like "dam" matches "Amsterdam", "damage" and "Adam" — which
    would drag unrelated mail into a project folder. Long terms stay plain substrings
    so "acme" still matches "Acme Corporation".
    """
    if len(term) <= 4:
        return re.search(rf"\b{re.escape(term)}\b", haystack) is not None
    return term in haystack


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_rules(path: Path | None = None) -> list[FilingRule]:
    """Read rules from TOML. Returns [] when the file is absent."""
    target = path or DEFAULT_PATH
    if not target.is_file():
        return []

    with target.open("rb") as fh:
        data = tomllib.load(fh)

    rules: list[FilingRule] = []
    for entry in data.get("rule", []):
        rules.append(
            FilingRule(
                name=entry.get("name", "(unnamed)"),
                label=entry["label"],
                disposition=entry.get("disposition", "archive"),
                senders=entry.get("senders", []),
                subject_contains=entry.get("subject_contains", []),
                body_contains=entry.get("body_contains", []),
                match=entry.get("match", "any"),
                unsubscribe=entry.get("unsubscribe", False),
                note=entry.get("note", ""),
            )
        )
    return rules


def rule_labels(rules: list[FilingRule]) -> list[str]:
    """Every label the rules reference, for seeding the taxonomy."""
    seen: list[str] = []
    for rule in rules:
        if rule.label not in seen:
            seen.append(rule.label)
    return seen


def first_match(
    rules: list[FilingRule],
    *,
    from_addr: str,
    from_name: str,
    subject: str,
    snippet: str,
) -> FilingRule | None:
    for rule in rules:
        if rule.matches(
            from_addr=from_addr, from_name=from_name, subject=subject, snippet=snippet
        ):
            return rule
    return None


# ---------------------------------------------------------------------------
# Preview — how many messages would each rule claim?
# ---------------------------------------------------------------------------


def preview(
    conn: sqlite3.Connection, rules: list[FilingRule]
) -> list[dict[str, object]]:
    """Count matches per rule against the indexed mailbox.

    Run this before trusting a rule. An over-broad sender term (a bare first name) can
    quietly claim mail from unrelated people, and seeing the count and a few example
    subjects catches that in seconds.
    """
    rows = conn.execute(
        "SELECT from_addr, from_name, subject, snippet FROM messages"
    ).fetchall()

    counts: dict[str, int] = {r.name: 0 for r in rules}
    examples: dict[str, list[str]] = {r.name: [] for r in rules}
    senders: dict[str, set[str]] = {r.name: set() for r in rules}

    for row in rows:
        rule = first_match(
            rules,
            from_addr=row["from_addr"] or "",
            from_name=row["from_name"] or "",
            subject=row["subject"] or "",
            snippet=row["snippet"] or "",
        )
        if rule is None:
            continue
        counts[rule.name] += 1
        senders[rule.name].add(row["from_addr"] or "?")
        if len(examples[rule.name]) < 5:
            examples[rule.name].append((row["subject"] or "(no subject)")[:70])

    return [
        {
            "name": rule.name,
            "label": rule.label,
            "disposition": rule.disposition,
            "matches": counts[rule.name],
            "distinct_senders": len(senders[rule.name]),
            "examples": examples[rule.name],
        }
        for rule in rules
    ]
