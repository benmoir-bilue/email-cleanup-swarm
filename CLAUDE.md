# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI (`ecs`) that runs a tiered swarm of Claude models over a personal Gmail inbox to
triage, label, archive, trash (never permanently delete), and unsubscribe — with every
irreversible action gated behind human approval. See `README.md` for the full
user-facing overview, safety model, and setup instructions.

## Commands

```bash
uv sync                                          # install deps
uv sync --extra browser && uv run playwright install chromium   # optional, for browser unsubscribes

uv run pytest                                    # full suite (200+ tests, offline, no network/model calls)
uv run pytest tests/test_plan.py                 # one file
uv run pytest tests/test_plan.py::TestPrecedence::test_human_decision_beats_every_model   # one test
uv run pytest -k guards                          # by keyword

uv run ecs --help                                # CLI entrypoint (also: python -m ecs.cli)
```

There is no configured linter/formatter/type-checker in this repo (no ruff/black/mypy
config) — don't assume one exists.

Tests are pure and offline: model API calls are stubbed with fixtures (see
`tests/test_pipeline.py` for the end-to-end shape), and each test isolates itself from
the real environment by `monkeypatch.setattr(config, "DB_PATH"/"TOKEN_PATH", tmp_path / ...)`
rather than touching `~/.config` or `~/.local/share`. Follow that pattern for new tests.

## Architecture

### Pipeline stages

The CLI (`cli.py`) drives a linear pipeline where each stage reads/writes one SQLite
file (`db.py`, one file for the whole run) and is independently re-runnable:

```
auth -> index -> cluster -> guards -> analyze(triage -> strategy -> challenge -> escalate) -> plan -> review -> apply -> undo
```

Source modules refer to these by stage number in their docstrings/schema comments
(`db.py`'s `triage_verdicts` table is "Stage 5", `strategy_runs` is "Stage 6",
`challenges` is "Stage 7", `escalations` is "Stage 8"; `plan.py` is "Stage 9";
`apply.py` is "Stage 10") — useful for finding a stage from a table name or vice versa.

- **`cluster.py`** — deterministic, no model. Collapses thousands of messages into a
  few hundred sender clusters (`List-Id`, then sender address, then domain + a
  normalized subject signature via `normalize_subject`). Must stay pure: same mailbox
  in, same clusters out — every downstream stage's re-runnability depends on this.
- **`guards.py`** — deterministic hard constraints, evaluated before any model runs.
  Two tiers: cluster-level (`protected_sender`, `protected_domain` — protects a whole
  correspondent) and message-level (protects individual records, e.g. a receipt buried
  in a disposable promo cluster). A `never_trash` guard hit is absolute; no model or
  precedence rule downstream can override it.
- **`agents/`** — one module per swarm tier, each a distinct model with distinct API
  quirks, all funneled through `agents/client.py`:
  - `triage.py` (Haiku 4.5, Batch API) — per-cluster classification, ~350 requests
    instead of thousands.
  - `strategist.py` (Opus 5, single call) — sees every cluster at once; designs the
    label taxonomy and category-level rules (the only stage with that vantage point).
  - `challenger.py` (Fable 5) — argues *against* every proposed deletion; a refutation
    must name a concrete scenario, not generic "might want this someday" reasoning.
  - `escalate.py` (Haiku 4.5, Batch API) — the only stage that scales with message
    count, not cluster count; per-message re-classification, but only inside clusters
    triage flagged `is_mixed`.
  - `client.py` — per-model request-shape differences are centralized here because
    getting one wrong is an HTTP 400, not a degraded result: Haiku only accepts the
    legacy `thinking: {type: "enabled", budget_tokens: N}` form and rejects
    `output_config.effort`; Opus has thinking on by default so `max_tokens` must cover
    thinking + answer; Fable rejects any explicit `thinking` config and needs 30-day
    data retention enabled at the org level. All three can return a 200 with
    `stop_reason: "refusal"`, so every read goes through `extract_text`, which checks
    for that first.
- **`filing_rules.py`** — optional user-supplied TOML (`filing-rules.toml`, gitignored;
  `filing-rules.example.toml` is the template) for filing knowledge no model can infer
  (e.g. two unrelated-looking senders are actually one project). First match wins, top
  to bottom. Feeds both the strategist's context (so its taxonomy doesn't invent
  parallel labels) and `plan.py`'s precedence.
- **`plan.py`** ("Stage 9") — merges every stage's verdict into one `ActionPlan` via a
  strict precedence, highest authority first: **human decision > guards > user filing
  rules > challenger > escalation > strategy > triage**. `build_plan` computes each
  message bottom-up (starts from the triage floor, then applies strategy, escalation,
  challenger, filing rules, guards, human decision in that order) so that every
  override only ever moves in the safe direction — nothing in the chain can turn a
  "keep" into a "delete" (there's a test pinning this). A mixed cluster with no
  individual escalation verdict inherits the cluster's own disposition rather than
  inventing a third outcome.
- **`journal.py` + `apply.py`** ("Stage 10") — `journal.py` is pure bookkeeping: an
  append-only JSONL log of intents, each later marked committed, from which it can
  compute the exact inverse for `undo` (e.g. add label, then trash). It never touches
  Gmail. `apply.py` is the only orchestrator that executes a plan, in waves (default
  200) with checkpoints between them, labels-before-trash so undo unwinds correctly,
  and `--apply` required to mutate anything (dry run is the default everywhere).
  `gmail/mutate.py` is the only module that actually calls a Gmail write endpoint.
- **`gmail/`** — `auth.py` (OAuth + **account binding**: the working directory is
  bound to one Gmail address on first `ecs auth`, and every later mailbox-touching
  command hard-stops if the live token resolves to a different account — this exists
  specifically to stop a multi-account machine from trashing the wrong mailbox),
  `index.py` (metadata + Sent-derived protected senders/replied threads), `batch.py`,
  `bodies.py` (fetched only for escalated messages), `mutate.py`, `quota.py`.
- **`unsub/`** — three tiers tried in order per sender, only ever against URLs taken
  from the `List-Unsubscribe`/`List-Unsubscribe-Post` headers (never links scraped
  from a message body): RFC 8058 one-click POST (`oneclick.py`), a headed Playwright
  browser (`browser.py`) that screenshots the result, then `mailto.py`. `run.py`
  orchestrates the fallback chain.
- **`tui/app.py`** — the review interface (Textual), five tabs over the plan.

### Config

`config.py` is the single place for paths, OAuth scopes, model IDs/pricing, batch
sizes, and the `Tunables` dataclass (confidence floors, wave size, escalation budget,
`protected_domains` — currently Australian tax/government agencies as a safety
default). Change tunables there, not inline in the stage that uses them.

### CLI conventions

`cli.py` imports stage dependencies (Playwright, Textual, Gmail client) inside each
command function body rather than at module load time, so `ecs --help` stays fast and
the CLI still loads when an optional dependency is absent. Preserve this when adding
commands.
