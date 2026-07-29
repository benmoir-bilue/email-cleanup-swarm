# Email Cleanup Swarm

Agentic cleanup for a personal Gmail inbox with thousands of unsorted messages. A
tiered swarm of Claude models does the triage; every irreversible action waits behind
human approval.

Point it at a mailbox with 7,000 unread newsletters, receipts, and forgotten
mailing-list mail, and it will cluster that mail, decide what's safe to file, archive,
or trash, draft an unsubscribe pass, and show you the whole plan before touching
anything.

> **Disclaimer.** This is a personal project, shared as-is, not a polished product.
> It mutates and deletes mail in a real Gmail account via the Gmail API and spends
> real money on Anthropic API calls. Nothing here is guaranteed to be bug-free, and
> the guardrails described below (guards, precedence, dry runs) reduce risk but don't
> eliminate it. You should be comfortable reading Python, reviewing what a CLI is
> about to do before running it with `--apply`, and recovering if something doesn't
> go the way you expected, before pointing this at a mailbox you care about. Use at
> your own risk — see [LICENSE](LICENSE), which disclaims warranty and liability.

## What it does

- **Indexes** your inbox and clusters ~7,000 messages into a few hundred sender/topic
  groups (by mailing-list ID, sender, and subject pattern) — deterministic code, no
  model involved.
- **Classifies clusters** with a tiered swarm of Claude models: Haiku triages each
  cluster cheaply, Opus designs a labeling strategy across all of them at once, and
  Fable argues *against* every proposed deletion before it's allowed to happen.
- **Protects anything that looks important** — tax, finance, receipts, insurance,
  legal, identity, medical, travel, warranty, security, property, education, starred
  mail, calendar invites, anyone you've ever emailed — before any model sees the
  mailbox, and no model verdict can override it.
- **Shows you a plan**, in a terminal UI, that you approve, veto, or answer questions
  about — nothing is applied without a review pass.
- **Applies changes in reversible waves**: labels and archiving first, deletions
  (Trash, not permanent delete) opt-in and last, with a journal-backed `undo`.
- **Unsubscribes**, using only the sender-declared `List-Unsubscribe` header — never
  links scraped from a message body — via RFC 8058 one-click, a real (visible)
  browser, or `mailto:`, in that order of preference.
- **Emits standing Gmail filters** so senders you've dealt with once stay filed
  automatically going forward.

## What it doesn't do

- **It doesn't permanently delete anything.** The OAuth scope requested
  (`gmail.modify` + `gmail.settings.basic`) is structurally incapable of it — the
  worst case is Trash, which Gmail purges after 30 days.
- **It doesn't run unattended.** Deletions and unsubscribes require an explicit
  `--apply`/`--approve` flag; everything defaults to a dry run.
- **It doesn't work on shared or organizational inboxes.** It's built for one personal
  Gmail account and actively refuses to run against the wrong account (see
  [Account binding](#account-binding) below).
- **It isn't a general email client.** There's no compose, no search UI beyond the
  review TUI, no IMAP/Exchange support — Gmail via the Gmail API only.
- **It doesn't read message bodies by default.** Only metadata (headers, snippets) is
  used for triage; full bodies are fetched only for the subset of messages that reach
  per-message escalation, and are stored locally, never sent anywhere except the
  Anthropic API for classification.

## The core idea

Classifying thousands of emails individually would cost far more and produce a review
queue nobody would finish. So deterministic code first collapses the mailbox into a
few hundred **sender clusters** (by `List-Id`, then sender address, then domain +
normalized subject signature). The models then reason about *clusters* — "this
newsletter, 340 messages, 2019–2026" — and you approve a few hundred decisions instead
of thousands.

That one decision is what makes the whole thing affordable and reviewable.

## Model tiering

| Stage | Model | Why |
|---|---|---|
| Index, cluster, guards | none | Deterministic. Free. No LLM touches the raw mailbox. |
| Triage (per cluster) | Haiku 4.5, Batch API | High volume, simple judgement, 50% batch discount |
| Strategy (1 call) | Opus 5 | Sees all clusters at once — the only vantage point for cross-cluster patterns |
| Adversarial challenge | Fable 5 | Argues *against* every proposed deletion |
| Escalation (mixed clusters) | Haiku 4.5, Batch API | Per-message reads where cluster-level answers are known to be wrong |

A full run over ~7,000 messages / ~350 clusters costs roughly **$5–6** in Anthropic
API usage.

## Safety

"If in doubt keep it" is enforced in code, not in a prompt — a model can be argued out
of a prompt.

- **Trash only.** The OAuth scope is `gmail.modify` + `gmail.settings.basic`, which is
  structurally incapable of permanent deletion. Google purges Trash after 30 days, so
  every deletion is reversible for a month.
- **Keep-signal guards** run before any model sees anything. 12 categories (tax,
  finance, receipts, insurance, legal, identity, medical, travel, warranty, security,
  property, education) plus starred, calendar invites, replied-to threads, and every
  address you've ever emailed. A guard hit cannot be overridden by any model.
- **Two-tier protection.** Sender-level guards protect a whole correspondent.
  Message-level guards protect individual records *inside* an otherwise disposable
  cluster — so a 300-message promo list gets cleaned out while the two receipts buried
  in it survive. That's the case the design exists for.
- **Adversarial pass.** Fable 5 reviews every proposed deletion and must name a
  concrete reason to refute it. Generic "you might want this someday" reasoning is
  explicitly ruled out, because it applies to all mail and protects nothing.
- **Precedence moves only toward safety.** Human > guards > challenger > escalation >
  strategy > triage. Nothing in that chain can turn a "keep" into a "delete" — pinned
  by a test.
- **Dry run by default**, waves of 200 with checkpoints, and a journal-backed `undo`.

## Installation

You'll need [`uv`](https://docs.astral.sh/uv/) (Python package/venv manager) and a
Google account whose mailbox you want to clean up. Two ways to get set up:

### Option A — let Claude Code do it

Clone the repo, open it in [Claude Code](https://claude.com/claude-code), and ask it
to set the project up for you:

```bash
git clone <this-repo-url>
cd email-cleanup-swarm
claude
```

Then, in the Claude Code session, say something like:

> Set up this project — install dependencies, walk me through creating a Google OAuth
> client and an Anthropic API key, and fill in my .env file.

Claude will run `setup.sh`, and then walk you through the Google Cloud Console and
Anthropic Console steps interactively rather than you following the manual steps
below.

### Option B — run the setup script yourself

```bash
git clone <this-repo-url>
cd email-cleanup-swarm
./setup.sh
```

This installs dependencies, optionally installs the Playwright browser used for
unsubscribe flows, and creates a `.env` file for you to fill in. Then continue with
[Configuring .env](#configuring-env) below.

### Option C — fully manual

```bash
uv sync
uv sync --extra browser && uv run playwright install chromium   # optional, for browser unsubscribes
cp .env.example .env                                            # then edit it — see below
```

## Configuring .env

`.env` needs two things: a Google OAuth client, and Anthropic credentials.

### 1. Google OAuth client (Gmail access)

This app talks to Gmail via the official Gmail API using OAuth — it never handles
your Google password, and the scopes it requests cannot permanently delete mail (see
[Safety](#safety)).

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a
   new project (or pick an existing one you're happy to use for this).
2. **APIs & Services → Library** — search for **Gmail API** and enable it.
3. **APIs & Services → OAuth consent screen** — configure it:
   - User type: **External**
   - Publishing status: leave it in **Testing** — this is a personal tool, not
     something you're distributing, so it never needs Google's app-verification review
   - Under **Test users**, add the Google account whose mailbox you're cleaning up
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**
   - Download the resulting JSON file
5. In `.env`, point `ECS_CLIENT_SECRET` at that file's path:
   ```
   ECS_CLIENT_SECRET=/path/to/client_secret_xxx.apps.googleusercontent.com.json
   ```

The scopes requested at consent time are exactly `gmail.modify` and
`gmail.settings.basic` — visible in the consent screen so you can verify this
yourself before approving.

### 2. Anthropic API key

The model swarm runs on the Anthropic API.

1. Create a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).
2. In `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

If you already authenticate the Anthropic SDK another way on this machine (e.g.
`ant auth login`), you can leave `ANTHROPIC_API_KEY` unset — it resolves a profile
automatically.

**Never commit `.env`.** It's already listed in `.gitignore`; keep it that way, and
never paste its contents anywhere (issues, chat, screenshots) — the Anthropic key and
the path to your OAuth client secret are the only two secrets this project has.

### Account binding

This tool is meant for a **personal** mailbox, but a Google consent screen hands over
whichever account the browser happens to be signed into. Authorise the wrong one and
the next `ecs apply` files and trashes thousands of emails from an account you didn't
mean to touch.

So the first `ecs auth` makes you confirm the address out loud and binds this working
directory to it. Every later command that touches the mailbox verifies the live token
still resolves to that account, and hard-stops on a mismatch:

```
This working directory is bound to you@gmail.com, but the stored token authorises
someone-else@example.com.

Refusing to touch the wrong mailbox.
```

Belt and braces for the first run, especially if you're signed into more than one
Google account in your browser:

```bash
uv run ecs auth --expect you@gmail.com    # fails outright if consent returns anything else
```

Use an incognito window for the consent flow if more than one account is signed in.
To switch accounts later you need `ecs auth --reauth --rebind`, and you should start a
clean working directory — the index and journal belong to the old mailbox.

This app has its own token at `~/.config/email-cleanup-swarm/token.json` and never
reads any other credential store on the machine (there's a test asserting that).
`ecs status` shows the bound account.

## Usage

Free stages first; nothing costs money before `analyze`.

```bash
uv run ecs auth                     # OAuth consent; prints the account it authorised
uv run ecs index                    # inbox metadata + Sent-derived protected senders
uv run ecs cluster                  # thousands of messages -> a few hundred clusters
uv run ecs guards                   # compute the hard constraints
uv run ecs rules --test             # optional: check your filing-rules.toml against the index

uv run ecs analyze --stage triage --limit 10   # try 10 clusters first (a couple of cents)
uv run ecs analyze                             # full swarm: triage -> strategy -> challenge -> escalate

uv run ecs plan                     # merge everything into one reviewable plan
uv run ecs review                   # TUI: approve, veto, answer the queue

uv run ecs apply                    # DRY RUN — prints the diff, mutates nothing
uv run ecs apply --safe-only --apply            # labels + archive only, no deletions
uv run ecs apply --apply --limit 5              # smoke-test 5 messages
uv run ecs undo --last-wave                     # verify the round-trip
uv run ecs apply --approve-all --apply          # the full run

uv run ecs unsub                    # the unsubscribe list (listing only)
uv run ecs unsub --approve-all --approve        # one-click, then browser, then mailto
uv run ecs filters --apply          # standing Gmail rules so it stays clean

uv run ecs inbox-zero --apply       # archive everything already labelled, out of the inbox
uv run ecs mark-read --apply        # mark labelled/archived mail as read
```

`ecs status` shows run state at any point. Every stage is resumable and idempotent.

### Suggested first pass

Do the non-destructive half first, look at the result in Gmail, then decide about
deletions:

```bash
uv run ecs apply --safe-only --apply    # empties the inbox, files everything, deletes nothing
```

### Filing rules (optional)

Some filing decisions can't be inferred from the mail itself — e.g. that two
unrelated-looking senders both belong to one project only you know about. Copy
[`filing-rules.example.toml`](filing-rules.example.toml) to `filing-rules.toml` (which
is gitignored, so it stays local to you) and edit it; `ecs rules --test` shows what
each rule would claim before you trust it.

## Review TUI

Five tabs: **Overview** (all clusters), **Unsubscribe** (pre-approved, per-row veto),
**Delete plan** (grouped by reason, with Fable's rebuttal shown inline next to each
proposal), **Taxonomy** (the label tree Opus designed, plus weak signals), **Queue**
(one ambiguity at a time).

`a`/`r` approve or veto · `A` approve everything on the tab · `k`/`s`/`x` answer a queue
question · `q` quit. Approvals write to SQLite immediately, so quitting loses nothing.

## Unsubscribe

Three tiers, tried in order per sender:

1. **RFC 8058 one-click** — a single HTTPS POST. No browser, real status code. Covers
   most modern bulk senders.
2. **Browser** (Playwright, headed by default so you can watch and intervene) — finds
   and clicks the confirm control, screenshots the result as evidence.
3. **mailto:** — sends the unsubscribe email, preserving any token the sender encoded
   in the query string.

Only URLs from `List-Unsubscribe` headers are ever visited — never links scraped from
message bodies. Each target gets a fresh isolated browser context with downloads
refused. Failures land in a `needs-manual` bucket with clickable links rather than
being retried forever.

Unsubscribing is the one genuinely irreversible action here, which is why it never
happens as a side effect of anything else.

## Layout

```
src/ecs/
  cli.py            Typer commands
  config.py         paths, model ids, tunables
  db.py             SQLite schema (one file holds the whole run)
  journal.py        append-only mutation log + undo planner
  cluster.py        deterministic grouping, subject normalisation
  guards.py         keep-signal patterns, protected senders, never-trash rules
  filing_rules.py   optional user-supplied filing rules (filing-rules.toml)
  plan.py           precedence resolution -> ActionPlan
  apply.py          waves, checkpoints, undo
  filters.py        Gmail filter emission
  gmail/            auth, index, bodies, mutate  (mutate.py is the only writer)
  agents/           client, triage, strategist, challenger, escalate
  unsub/            parse, oneclick, mailto, browser, run
  tui/              review interface
```

## Data

Nothing leaves your machine except message digests sent to the Anthropic API for
classification. Bodies are fetched only for messages that reach escalation, truncated
to 2,500 chars, and stored locally.

- index + journal: `~/.local/share/email-cleanup-swarm/`
- token: `~/.config/email-cleanup-swarm/token.json` (mode 0600)
- unsubscribe screenshots: `~/.local/share/email-cleanup-swarm/unsub-evidence/`

None of the above ever leaves your machine, and none of it is part of this repo.

## Tests

```bash
uv run pytest
```

200+ tests, all against synthetic fixture data — no real mailbox content is used
anywhere in the test suite. Coverage is concentrated where a bug would lose mail: the
undo planner, guard evaluation and cluster roll-up, plan precedence, per-model request
shapes (a wrong shape is a runtime 400 mid-batch), and an end-to-end pipeline over a
synthetic mailbox built with the shapes that actually stress the design.

## License

[MIT](LICENSE) — provided as-is, with no warranty. See the disclaimer at the top of
this file before running it against a mailbox you care about.
