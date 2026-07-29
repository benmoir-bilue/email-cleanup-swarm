"""Paths, model IDs, and tunables.

Everything that a human might reasonably want to change lives here, so the rest
of the codebase can stay free of magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "email-cleanup-swarm"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


CONFIG_DIR = _config_dir()
DATA_DIR = _data_dir()

TOKEN_PATH = CONFIG_DIR / "token.json"
DB_PATH = DATA_DIR / "index.db"
JOURNAL_PATH = DATA_DIR / "journal.jsonl"
UNSUB_EVIDENCE_DIR = DATA_DIR / "unsub-evidence"

# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

# gmail.modify covers labels, archive, and trash. gmail.settings.basic covers
# filter creation. Deliberately NOT requesting https://mail.google.com/ — without
# it the token is structurally incapable of permanently deleting anything, which
# is the backstop behind the whole "trash only" safety posture.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

CLIENT_SECRET_ENV = "ECS_CLIENT_SECRET"

# Which slice of the inbox a run targets.
#
# Gmail's category tabs are ordinary labels, and every inbox message carries at most
# one. Selecting a tab by intersecting labels (INBOX + CATEGORY_*) is deliberately
# preferred over a `category:promotions` search string: the search index under-reports
# badly at this scale, which is what truncated the first index run to a quarter of the
# mailbox.
#
# A handful of messages carry no CATEGORY_* label at all; Gmail displays those in
# Primary, so the `primary` scope can't catch them by label alone and they're picked up
# by the `all` scope only. That gap is reported rather than hidden.
INBOX_SCOPES: dict[str, list[str]] = {
    "all": ["INBOX"],
    "primary": ["INBOX", "CATEGORY_PERSONAL"],
    "updates": ["INBOX", "CATEGORY_UPDATES"],
    "promotions": ["INBOX", "CATEGORY_PROMOTIONS"],
    "social": ["INBOX", "CATEGORY_SOCIAL"],
    "forums": ["INBOX", "CATEGORY_FORUMS"],
}

# Composite scopes, indexed as several passes.
INBOX_SCOPE_GROUPS: dict[str, list[str]] = {
    "human": ["primary", "updates"],
    "bulk": ["promotions", "social", "forums"],
}

# Gmail caps batch requests at 100 entries, and batchModify at 1000 ids.
METADATA_BATCH_SIZE = 100
MODIFY_BATCH_SIZE = 200  # deliberately below the cap so a wave stays reviewable

# Headers worth paying for on a metadata fetch. Gmail bills quota per header set,
# so this list is kept tight.
METADATA_HEADERS = [
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "List-Id",
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "Precedence",
    "Auto-Submitted",
    "Content-Type",
]

# ---------------------------------------------------------------------------
# Models
#
# Tiering rationale: deterministic code handles all 7k messages; Haiku classifies
# ~350 clusters; Opus does the one-shot strategic design; Fable argues against
# every proposed deletion.
# ---------------------------------------------------------------------------

MODEL_TRIAGE = "claude-haiku-4-5"  # $1 / $5 per MTok
MODEL_STRATEGIST = "claude-opus-5"  # $5 / $25 per MTok
MODEL_CHALLENGER = "claude-fable-5"  # $10 / $50 per MTok
MODEL_ESCALATE = "claude-haiku-4-5"

# Fable 5 declines requests it reads as high-risk. Route those to Opus rather
# than losing the challenge entirely.
CHALLENGER_FALLBACK = "claude-opus-5"

PRICING_PER_MTOK = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}

# Haiku 4.5 won't cache a prefix under 4096 tokens — below that a cache_control
# marker only buys you the write premium. Checked before we bother setting one.
CACHE_MIN_TOKENS = {
    "claude-haiku-4-5": 4096,
    "claude-opus-5": 512,
    "claude-fable-5": 512,
}


@dataclass(frozen=True)
class Tunables:
    """Knobs that change how aggressive or expensive a run is."""

    # Clustering
    max_sample_subjects: int = 8
    """Subjects included in a cluster digest sent to the triage model."""

    min_cluster_size_for_rule: int = 3
    """Below this, a cluster is too small to justify a standing Gmail filter."""

    # Triage
    keep_confidence_floor: float = 0.75
    """A trash disposition below this confidence is routed to human review."""

    # Escalation budget. Per-email body reads are the only stage that scales with
    # message count rather than cluster count.
    #
    # `None` means uncapped: every message in a mixed cluster gets an individual
    # decision. That is the point of the stage — a capped run leaves the remainder
    # with no per-message verdict, and the only safe thing to do with an unreviewed
    # message in a cluster known to be mixed is to inherit the cluster's disposition,
    # which is exactly the coarse call escalation exists to avoid.
    #
    # Set an integer to cap it; the CLI reports the estimated cost before running and
    # asks for confirmation past `escalate_confirm_over`.
    max_escalated_messages: int | None = None

    escalate_confirm_over: int = 3000
    """Ask before escalating more than this many messages."""

    # Challenge
    challenge_group_size: int = 20
    """Clusters per Fable 5 request. Larger groups are cheaper but blunter."""

    # Apply
    wave_size: int = MODIFY_BATCH_SIZE
    checkpoint_between_waves: bool = True

    # Unsubscribe
    unsub_concurrency: int = 4
    unsub_delay_seconds: float = 1.5
    """Politeness delay. Hammering unsubscribe endpoints looks like an attack."""

    unsub_timeout_seconds: float = 20.0

    # Batch API polling
    batch_poll_seconds: int = 20
    batch_timeout_seconds: int = 60 * 60 * 6

    protected_domains: tuple[str, ...] = field(
        default_factory=lambda: (
            # Anything from these is never a deletion candidate regardless of
            # what a model thinks, because the cost of being wrong is unbounded.
            "ato.gov.au",
            "servicesaustralia.gov.au",
            "mygov.au",
            "medicare.gov.au",
        )
    )


TUNABLES = Tunables()


def ensure_dirs() -> None:
    """Create the config and data directories with private permissions."""
    for path in (CONFIG_DIR, DATA_DIR, UNSUB_EVIDENCE_DIR):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def client_secret_path() -> Path:
    raw = os.environ.get(CLIENT_SECRET_ENV)
    if not raw:
        raise RuntimeError(
            f"{CLIENT_SECRET_ENV} is not set. Copy .env.example to .env and point it "
            "at your Google OAuth client secret JSON (Desktop app credentials)."
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{CLIENT_SECRET_ENV} points at a missing file: {path}")
    return path
