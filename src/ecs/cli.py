"""Command-line entrypoints.

Imports inside command bodies are deliberate: it keeps `ecs --help` fast and lets
the CLI load even when an optional dependency (Playwright, Textual) is absent.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import config, db

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agentic Gmail inbox cleanup. Nothing mutates without --apply.",
)
console = Console()


def _require_account() -> str:
    """Guard every mailbox-touching command against the wrong account."""
    from .gmail.auth import WrongAccountError, assert_account

    try:
        return assert_account()
    except WrongAccountError as exc:
        console.print(f"[bold red]Wrong mailbox.[/bold red]\n\n{exc}")
        raise typer.Exit(1) from exc


@app.command()
def auth(
    reauth: bool = typer.Option(
        False, "--reauth", help="Discard the stored token and re-run the consent flow."
    ),
    rebind: bool = typer.Option(
        False,
        "--rebind",
        help="Deliberately re-point this directory at a different account.",
    ),
    expect: str | None = typer.Option(
        None,
        "--expect",
        help="Fail unless the authorised account matches this address exactly.",
    ),
) -> None:
    """Authorise against your personal Gmail and store a token locally.

    The first successful run binds this directory to the account it authorised.
    Later commands refuse to run against a different mailbox.
    """
    from .gmail.auth import authorise, bind_account, bound_account, unbind_account, whoami

    existing = bound_account()
    if rebind and existing:
        console.print(f"[yellow]Unbinding from {existing}.[/yellow]")
        unbind_account()
        existing = None

    creds = authorise(force=reauth or rebind)
    profile = whoami(creds)
    email = profile["emailAddress"]

    console.print(f"\n[green]Authorised[/green] as [bold]{email}[/bold]")
    console.print(f"  messages in mailbox: {profile.get('messagesTotal', 0):,}")
    console.print(f"  threads in mailbox:  {profile.get('threadsTotal', 0):,}")
    console.print(f"  token: {config.TOKEN_PATH} [dim](this app only)[/dim]")
    console.print(
        "  scopes: gmail.modify + gmail.settings.basic "
        "[dim](cannot permanently delete)[/dim]"
    )

    if expect and email.lower() != expect.lower():
        console.print(
            f"\n[bold red]Account mismatch.[/bold red] Expected {expect}, "
            f"got {email}. Nothing was bound."
        )
        console.print(
            "[dim]Sign out of the other Google account, or use an incognito window, "
            "then run `ecs auth --reauth` again.[/dim]"
        )
        raise typer.Exit(1)

    if existing:
        if existing.lower() != email.lower():
            console.print(
                f"\n[bold red]This directory is bound to {existing}[/bold red], but "
                f"you just authorised {email}."
            )
            console.print(
                "[dim]Run `ecs auth --reauth` and sign in as the bound account, or "
                "`ecs auth --reauth --rebind` to switch deliberately.[/dim]"
            )
            raise typer.Exit(1)
        console.print(f"\n[green]Confirmed[/green] — still bound to {existing}.")
        return

    # First bind. Make the user say the account out loud, because a consent screen
    # hands over whichever Google account the browser was signed into, and cleaning
    # up the wrong mailbox is exactly the failure this prevents.
    console.print(
        "\n[bold yellow]Confirm this is the right mailbox.[/bold yellow] "
        "Everything this tool does — labelling, archiving, moving to Trash, "
        "unsubscribing — will happen to this account."
    )
    if not typer.confirm(f"Clean up {email}?", default=False):
        console.print(
            "[yellow]Not bound.[/yellow] Sign out of the wrong Google account (or use "
            "an incognito window) and run [bold]ecs auth --reauth[/bold]."
        )
        raise typer.Exit(1)

    bind_account(email)
    console.print(f"[green]Bound[/green] to {email}. Later commands will verify this.")
    console.print("\nNext: [bold]ecs index[/bold]")


@app.command()
def index(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="all | primary | updates | promotions | social | forums | human | bulk",
    ),
    breakdown: bool = typer.Option(
        False,
        "--breakdown",
        help="Just report how many messages are in each category tab, then stop.",
    ),
    reset: bool = typer.Option(
        False,
        "--reset",
        help="Clear the local index first. Needed when narrowing scope, so messages "
        "outside the new scope don't linger in the plan.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Index only the first N messages (for a quick trial)."
    ),
    skip_sent: bool = typer.Option(
        False, "--skip-sent", help="Skip the Sent pass. Weakens protected-sender signal."
    ),
    sent_limit: int = typer.Option(
        5000, "--sent-limit", help="Max Sent messages to scan for protected senders."
    ),
) -> None:
    """Index inbox metadata, protected senders, and existing labels.

    Safe to interrupt — progress is checkpointed and a re-run resumes.
    """
    from .gmail.index import (
        inbox_breakdown,
        index_inbox,
        index_labels,
        index_sent_senders,
    )
    from .progress import Reporter

    account = _require_account()
    console.print(f"[dim]mailbox: {account}[/dim]\n")

    # --- breakdown only ---------------------------------------------------
    if breakdown:
        from .gmail.auth import service

        with Reporter("Inbox breakdown", console=console) as rep:
            rep.stage("counting each tab", total=5)
            counts = inbox_breakdown(service(), progress=rep)
            rep.advance(5)

        table = Table(title="Inbox by category tab")
        table.add_column("tab", style="cyan")
        table.add_column("messages", justify="right")
        table.add_column("share", justify="right")
        total = counts["inbox_total"]
        for name in ("primary", "updates", "promotions", "social", "forums"):
            n = counts[name]
            table.add_row(name, f"{n:,}", f"{100 * n / total:.0f}%")
        if counts["uncategorised"]:
            table.add_row(
                "[dim]uncategorised[/dim]",
                f"{counts['uncategorised']:,}",
                "[dim]shows in Primary[/dim]",
            )
        table.add_row("[bold]INBOX total[/bold]", f"[bold]{total:,}[/bold]", "")
        console.print(table)
        console.print(
            "\n[dim]Pick a scope with e.g. [bold]ecs index --scope human[/bold] "
            "(primary + updates) or [bold]--scope all[/bold].[/dim]"
        )
        raise typer.Exit(0)

    # --- resolve scope ----------------------------------------------------
    if scope in config.INBOX_SCOPE_GROUPS:
        passes = config.INBOX_SCOPE_GROUPS[scope]
    elif scope in config.INBOX_SCOPES:
        passes = [scope]
    else:
        console.print(
            f"[red]Unknown scope[/red] {scope!r}. Choose from "
            f"{sorted(set(config.INBOX_SCOPES) | set(config.INBOX_SCOPE_GROUPS))}"
        )
        raise typer.Exit(1)

    if reset:
        with db.session() as conn:
            before = db.message_count(conn)
            conn.execute("DELETE FROM messages")
            for key in (
                "index.inbox.page_token",
                "index.inbox.listing_complete",
                "index.inbox.pending_ids",
                "index.inbox.scope",
                "index.inbox.failed_ids",
            ):
                db.kv_delete(conn, key)
            for stage in ("cluster", "guards", "triage", "plan", "unsub"):
                db.reset_stage(conn, stage)
        console.print(
            f"[yellow]Reset:[/yellow] cleared {before:,} indexed messages and "
            "downstream stages (model verdicts kept where still valid)"
        )

    with Reporter("Indexing", console=console) as rep:
        result = {"written": 0, "total": 0, "failed": 0, "in_scope": 0}
        for pass_scope in passes:
            rep.log(f"scope: {pass_scope}", style="cyan")
            part = index_inbox(limit=limit, progress=rep, scope=pass_scope)
            result["written"] += part["written"]
            result["failed"] += part["failed"]
            result["in_scope"] += part["in_scope"]
            result["total"] = part["total"]
        rep.log(
            f"inbox done: {result['written']:,} newly indexed, "
            f"{result['total']:,} stored",
            style="green",
        )

        sent = None
        if not skip_sent:
            sent = index_sent_senders(max_messages=sent_limit, progress=rep)
            rep.log(
                f"sent done: {sent['protected_senders']:,} protected senders, "
                f"{sent['replied_threads']:,} replied threads",
                style="green",
            )

        rep.stage("reading labels")
        label_count = index_labels()
        rep.log(f"{label_count} existing labels recorded", style="green")

    table = Table(title="Index complete", show_header=False)
    table.add_column("", style="cyan")
    table.add_column("")
    table.add_row("messages stored", f"{result['total']:,}")
    table.add_row("newly indexed this run", f"{result['written']:,}")
    if result["in_scope"]:
        table.add_row("ids in scope", f"{result['in_scope']:,}")
    if result["failed"]:
        table.add_row("still failing", f"[yellow]{result['failed']:,}[/yellow]")
    if sent:
        table.add_row("protected senders", f"{sent['protected_senders']:,}")
        table.add_row("replied threads", f"{sent['replied_threads']:,}")
    table.add_row("existing labels", str(label_count))
    console.print(table)

    if result["failed"]:
        console.print(
            f"[yellow]{result['failed']:,} messages could not be fetched after two "
            "retry passes. Re-run `ecs index` to try again — it only fetches the "
            "gap.[/yellow]"
        )

    console.print("\nNext: [bold]ecs cluster[/bold] (free, no API calls)")


@app.command()
def cluster(
    report: bool = typer.Option(
        False, "--report", help="Only print stats; don't rebuild clusters."
    ),
) -> None:
    """Group messages into sender clusters. Deterministic, no model, no cost."""
    from .cluster import build_clusters, cluster_report

    with db.session() as conn:
        if db.message_count(conn) == 0:
            console.print("[red]Nothing indexed yet.[/red] Run [bold]ecs index[/bold].")
            raise typer.Exit(1)
        if not report:
            build_clusters(conn)
        stats = cluster_report(conn)

    table = Table(title="Clustering", show_header=False)
    table.add_column("", style="cyan")
    table.add_column("")
    table.add_row("messages", f"{stats['messages']:,}")
    table.add_row("clusters", f"{stats['clusters']:,}")
    table.add_row("compression", f"{stats['compression']:.1f}x")
    table.add_row("with unsubscribe", f"{stats['with_unsubscribe']:,}")
    table.add_row("single-message clusters", f"{stats['singletons']:,}")
    for kind, n in sorted(stats["by_kind"].items()):
        table.add_row(f"  by {kind}", f"{n:,}")
    console.print(table)
    console.print("\nNext: [bold]ecs guards[/bold] (free, no API calls)")


@app.command()
def guards(
    report: bool = typer.Option(
        False, "--report", help="Only print stats; don't recompute."
    ),
) -> None:
    """Compute keep-signal guards. These hard-override every model verdict."""
    from .guards import evaluate_all, guard_report

    with db.session() as conn:
        if db.cluster_count(conn) == 0:
            console.print("[red]No clusters yet.[/red] Run [bold]ecs cluster[/bold].")
            raise typer.Exit(1)
        if not report:
            evaluate_all(conn)
        stats = guard_report(conn)

    table = Table(title="Guards — messages that cannot be trashed", show_header=False)
    table.add_column("", style="cyan")
    table.add_column("")
    table.add_row("messages evaluated", f"{stats['messages_evaluated']:,}")
    table.add_row("messages protected", f"{stats['messages_protected']:,}")
    table.add_row("clusters protected wholesale", f"{stats['clusters_protected']:,}")
    table.add_row("protected senders", f"{stats['protected_senders']:,}")
    table.add_row("replied threads", f"{stats['replied_threads']:,}")
    console.print(table)

    if stats["by_category"]:
        cat = Table(title="Keep-signal hits by category")
        cat.add_column("category", style="cyan")
        cat.add_column("messages", justify="right")
        for name, n in stats["by_category"].items():
            cat.add_row(name, f"{n:,}")
        console.print(cat)

    console.print("\nNext: [bold]ecs analyze[/bold] [dim](first stage that costs money)[/dim]")


@app.command()
def analyze(
    stage: str = typer.Option(
        "all",
        "--stage",
        help="triage | strategy | challenge | escalate | all",
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Cap units processed (clusters, or messages for escalate)."
    ),
    redo: bool = typer.Option(
        False, "--redo", help="Re-run even where verdicts already exist."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip cost confirmations."
    ),
) -> None:
    """Run the model swarm. This is the first stage that costs money."""
    from .agents.client import CostTracker

    valid = {"triage", "strategy", "challenge", "escalate", "all"}
    if stage not in valid:
        console.print(f"[red]Unknown stage[/red] {stage!r}. Choose from {sorted(valid)}.")
        raise typer.Exit(1)

    with db.session() as conn:
        if db.cluster_count(conn) == 0:
            console.print("[red]No clusters.[/red] Run [bold]ecs cluster[/bold] first.")
            raise typer.Exit(1)

    tracker = CostTracker()
    emit = lambda m: console.print(f"[dim]{m}[/dim]")  # noqa: E731
    stages = (
        ["triage", "strategy", "challenge", "escalate"] if stage == "all" else [stage]
    )

    for name in stages:
        console.rule(f"[bold cyan]{name}")
        if name == "triage":
            from .agents.triage import triage_clusters, triage_report

            result = triage_clusters(
                limit=limit, only_missing=not redo, tracker=tracker, progress=emit
            )
            console.print(
                f"classified {result['classified']:,}/{result['requested']:,} clusters"
                + (f", {result['failed']} failed" if result["failed"] else "")
            )
            with db.session() as conn:
                stats = triage_report(conn)
            for disposition, n in stats["by_disposition"].items():
                msgs = stats["messages_by_disposition"].get(disposition, 0)
                console.print(f"  {disposition}: {n:,} clusters ({msgs:,} messages)")
            console.print(
                f"  [yellow]{stats['mixed_clusters']:,} mixed[/yellow] "
                f"(will be escalated), "
                f"{stats['low_confidence']:,} low-confidence"
            )

        elif name == "strategy":
            from .agents.strategist import run_strategy

            result = run_strategy(tracker=tracker, progress=emit)
            console.print(
                f"{result['labels']} labels, {result['rules']:,} rules, "
                f"[bold]{result['weak_signals']} weak signals[/bold], "
                f"{result['ambiguities']} questions for you"
            )

        elif name == "challenge":
            from .agents.challenger import challenge_deletions

            result = challenge_deletions(
                limit=limit, only_missing=not redo, tracker=tracker, progress=emit
            )
            console.print(
                f"reviewed {result['reviewed']:,} delete candidates: "
                f"[green]{result['upheld']:,} upheld[/green], "
                f"[yellow]{result['refuted']:,} challenged and demoted to review[/yellow]"
            )

        elif name == "escalate":
            from .agents.escalate import (
                _count_all_candidates,
                escalate_messages,
                estimate_cost,
            )

            with db.session() as conn:
                candidates = _count_all_candidates(conn)

            if candidates == 0:
                console.print("no messages need escalation")
                continue

            planned = min(candidates, limit) if limit else candidates
            est = estimate_cost(planned)
            console.print(
                f"{candidates:,} messages sit in mixed clusters and need an "
                f"individual decision."
            )
            console.print(
                f"Escalating {planned:,} of them: ~{est['input_tokens'] / 1e6:.1f}M "
                f"input tokens, estimated [bold]${est['cost']:.2f}[/bold] "
                f"(Haiku 4.5 via Batch API, 50% off)"
            )
            if planned < candidates:
                console.print(
                    f"  [yellow]{candidates - planned:,} would be left without an "
                    "individual verdict and would inherit their cluster's "
                    "decision[/yellow]"
                )

            if planned >= config.TUNABLES.escalate_confirm_over and not yes:
                if not typer.confirm(
                    f"Escalate {planned:,} messages for ~${est['cost']:.2f}?",
                    default=True,
                ):
                    console.print("[yellow]Skipped escalation.[/yellow]")
                    continue

            result = escalate_messages(limit=limit, tracker=tracker, progress=emit)
            console.print(
                f"escalated {result['escalated']:,} of {result['candidates']:,} "
                "messages in mixed clusters"
            )
            if result["skipped"]:
                console.print(
                    f"  [yellow]{result['skipped']:,} not escalated — these inherit "
                    "their cluster's disposition rather than being parked in "
                    "archive[/yellow]"
                )

    console.rule("[bold]cost")
    console.print(tracker.summary())
    console.print("\nNext: [bold]ecs plan[/bold]")


@app.command("plan")
def plan_cmd(
    report: bool = typer.Option(False, "--report", help="Only print stats."),
) -> None:
    """Merge every verdict into one reviewable plan. Guards hard-override models."""
    from .plan import build_plan, plan_report

    with db.session() as conn:
        if db.cluster_count(conn) == 0:
            console.print("[red]No clusters.[/red] Run [bold]ecs cluster[/bold] first.")
            raise typer.Exit(1)
        if not report:
            build_plan(conn)
        stats = plan_report(conn)

    table = Table(title="Action plan", show_header=False)
    table.add_column("", style="cyan")
    table.add_column("")
    for action, n in stats["by_action"].items():
        table.add_row(action, f"{n:,}")
    table.add_row("", "")
    table.add_row("messages to Trash", f"{stats['messages_to_trash']:,}")
    table.add_row("messages left in inbox", f"{stats['messages_left_in_inbox']:,}")
    table.add_row("unsubscribe targets", str(sum(stats["unsubscribe_by_method"].values())))
    console.print(table)

    # Anything in a holding bucket is a message the system declined to decide about.
    # Report it loudly — a half-finished job shouldn't read as a complete one.
    holding = stats["unsorted"] + stats["needs_decision"]
    if holding:
        console.print(
            f"\n[bold yellow]{holding:,} messages are in a holding bucket, not "
            "genuinely filed:[/bold yellow]"
        )
        if stats["unsorted"]:
            console.print(
                f"  {stats['unsorted']:,} in [bold]{'Review/Unsorted'}[/bold] — no "
                "category could be inferred"
            )
        if stats["needs_decision"]:
            console.print(
                f"  {stats['needs_decision']:,} in [bold]Review/Needs decision[/bold] "
                "— deletion was challenged or confidence was low"
            )
        console.print(
            "  [dim]Fix with: run [bold]ecs analyze --stage escalate[/bold] to get "
            "per-message verdicts, or answer the queue in [bold]ecs review[/bold].[/dim]"
        )
    else:
        console.print(
            "\n[green]Every message is either filed under a real label or slated "
            "for Trash — nothing parked in a holding bucket.[/green]"
        )

    if stats["cluster_inherited"]:
        console.print(
            f"[dim]{stats['cluster_inherited']:,} messages in mixed clusters took "
            "their cluster's decision for lack of an individual review.[/dim]"
        )

    if stats["by_source"]:
        src = Table(title="Which stage decided")
        src.add_column("source", style="cyan")
        src.add_column("actions", justify="right")
        for source, n in stats["by_source"].items():
            src.add_row(source, f"{n:,}")
        console.print(src)

    if stats["labels"]:
        lbl = Table(title="Labels to be applied")
        lbl.add_column("label", style="cyan")
        lbl.add_column("messages", justify="right")
        for label, n in list(stats["labels"].items())[:30]:
            lbl.add_row(label, f"{n:,}")
        console.print(lbl)

    console.print("\nNext: [bold]ecs review[/bold] to approve, or [bold]ecs apply[/bold] for a dry run")


@app.command()
def review() -> None:
    """Open the review TUI. Nothing mutates until you approve it here."""
    from .tui.app import run

    with db.session() as conn:
        if conn.execute("SELECT COUNT(*) FROM plan_actions").fetchone()[0] == 0:
            console.print("[red]No plan to review.[/red] Run [bold]ecs plan[/bold].")
            raise typer.Exit(1)
    run()


@app.command()
def apply(
    execute: bool = typer.Option(
        False, "--apply", help="Actually mutate the mailbox. Omit for a dry run."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Touch at most N messages. Use --limit 5 to smoke-test."
    ),
    approve_all: bool = typer.Option(
        False, "--approve-all", help="Approve everything pending, then apply."
    ),
    safe_only: bool = typer.Option(
        False,
        "--safe-only",
        help="Approve and apply only labelling and archiving — no deletions.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Run unattended: no inter-wave prompts, and larger waves since nobody "
        "is inspecting between them.",
    ),
    wave_size: int | None = typer.Option(
        None,
        "--wave-size",
        help="Messages per wave. Defaults to 200 when prompting, 1000 with --yes.",
    ),
) -> None:
    """Apply the approved plan. Dry run unless --apply is given."""
    from .apply import (
        apply_plan,
        approve_all as do_approve_all,
        approve_non_destructive,
        dry_run,
    )

    with db.session() as conn:
        if safe_only:
            n = approve_non_destructive(conn)
            console.print(f"approved {n:,} non-destructive actions")
        elif approve_all:
            n = do_approve_all(conn)
            console.print(f"approved {n:,} actions")

        plan_stats = dry_run(conn, only_approved=execute)

    if plan_stats["total_actions"] == 0:
        console.print(
            "[yellow]Nothing approved to apply.[/yellow] Use [bold]ecs review[/bold], "
            "or [bold]--safe-only[/bold] / [bold]--approve-all[/bold]."
        )
        raise typer.Exit(0)

    table = Table(
        title="DRY RUN — no changes made" if not execute else "About to apply",
        show_header=False,
    )
    table.add_column("", style="cyan")
    table.add_column("")
    for action, n in plan_stats["by_action"].items():
        table.add_row(action, f"{n:,}")
    console.print(table)

    if plan_stats["labels_to_create"]:
        console.print(
            f"\n[bold]{len(plan_stats['labels_to_create'])} labels to create:[/bold] "
            + ", ".join(plan_stats["labels_to_create"][:20])
        )

    if plan_stats["trash_by_cluster"]:
        trash = Table(title="Senders whose mail moves to Trash (recoverable 30 days)")
        trash.add_column("sender", style="cyan")
        trash.add_column("messages", justify="right")
        trash.add_column("reason")
        for name, n, reason in plan_stats["trash_by_cluster"][:25]:
            trash.add_row((name or "?")[:44], f"{n:,}", (reason or "")[:44])
        console.print(trash)

    if not execute:
        console.print(
            "\n[dim]This was a dry run. Re-run with [bold]--apply[/bold] to execute.[/dim]"
        )
        raise typer.Exit(0)

    # Last chance to catch a wrong-mailbox situation, immediately before the only
    # commands in the system that mutate anything.
    account = _require_account()
    console.print(f"\n[bold]Applying to {account}[/bold]")

    # Unattended runs get bigger waves: the small wave size exists so a human can
    # inspect Gmail between them, and with --yes nobody is looking. 30,000 actions in
    # waves of 200 is 150 pointless round trips.
    effective_wave = wave_size or (1000 if yes else 200)
    total_actions = plan_stats["total_actions"]
    waves = -(-total_actions // effective_wave)
    console.print(
        f"[dim]{total_actions:,} actions in {waves} wave(s) of {effective_wave:,}"
        + ("" if yes else " — you'll be asked between each")
        + "[/dim]"
    )

    from .progress import Reporter

    def checkpoint(wave_no: int, result) -> bool:
        if yes:
            return True
        console.print(f"\n[bold]Wave {wave_no} complete:[/bold] {result.summary()}")
        return typer.confirm("Continue to the next wave?", default=True)

    with Reporter("Applying", console=console) as rep:
        rep.counter("labelled", "labelled", style="green")
        rep.counter("archived", "archived", style="green")
        rep.counter("trashed", "trashed", style="yellow")
        rep.counter("failed", "failed", style="red")
        result = apply_plan(
            limit=limit,
            wave_size=effective_wave,
            checkpoint=checkpoint,
            progress=rep,
        )

    console.print(f"\n[green]Applied:[/green] {result.summary()}")
    for err in result.errors[:10]:
        console.print(f"  [yellow]{err}[/yellow]")
    console.print(
        "\n[dim]Undo with [bold]ecs undo --last-wave[/bold] or "
        "[bold]ecs undo --all[/bold].[/dim]"
    )


@app.command()
def undo(
    last_wave: bool = typer.Option(False, "--last-wave", help="Reverse the last wave only."),
    all_changes: bool = typer.Option(False, "--all", help="Reverse everything journalled."),
    since: str | None = typer.Option(None, "--since", help="ISO timestamp cutoff."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Reverse mutations. Restores labels and pulls messages back out of Trash."""
    from .apply import undo as do_undo
    from .journal import Journal

    journal = Journal()
    wave = None
    if last_wave:
        wave = journal.last_wave()
        if wave is None:
            console.print("[yellow]No waves recorded.[/yellow]")
            raise typer.Exit(0)
    elif not all_changes and not since:
        console.print(
            "[red]Choose a scope:[/red] --last-wave, --all, or --since <timestamp>."
        )
        raise typer.Exit(1)

    steps = journal.undo_plan(since=since, wave=wave)
    if not steps:
        console.print("[yellow]Nothing to undo.[/yellow]")
        raise typer.Exit(0)

    counts: dict[str, int] = {}
    for step in steps:
        counts[step["op"]] = counts.get(step["op"], 0) + 1
    console.print(f"[bold]{len(steps):,} reversal steps:[/bold]")
    for op, n in sorted(counts.items()):
        console.print(f"  {op}: {n:,}")
    if counts.get("noop"):
        console.print(
            f"  [yellow]{counts['noop']} unsubscribes cannot be reversed[/yellow]"
        )

    if not yes and not typer.confirm("Proceed with undo?", default=True):
        raise typer.Exit(0)

    _require_account()
    result = do_undo(
        since=since, wave=wave, progress=lambda m: console.print(f"[dim]{m}[/dim]")
    )
    console.print(
        f"[green]Undone:[/green] {result['restored']:,} restored from Trash, "
        f"{result['relabelled']:,} relabelled, "
        f"{result['labels_deleted']} labels removed"
    )
    if result["irreversible"]:
        console.print(
            f"[yellow]{result['irreversible']} unsubscribes were not reversed "
            "(they can't be).[/yellow]"
        )


@app.command()
def unsub(
    approve: bool = typer.Option(
        False, "--approve", help="Execute approved unsubscribes."
    ),
    approve_all: bool = typer.Option(
        False, "--approve-all", help="Approve every target, then execute."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Process at most N senders."),
    one_click_only: bool = typer.Option(
        False, "--one-click-only", help="RFC 8058 POST only — no browser, no mailto."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Skip senders that need a browser page."
    ),
    headless: bool = typer.Option(
        False, "--headless", help="Run the browser hidden. Default is visible."
    ),
) -> None:
    """List and execute unsubscribes. Prints the list unless --approve is given."""
    from .unsub.run import (
        approve_all as do_approve_all,
        list_targets,
        run_unsubscribes,
        unsub_report,
    )

    with db.session() as conn:
        if approve_all:
            n = do_approve_all(conn)
            console.print(f"approved {n:,} unsubscribe targets")
        targets = list_targets(conn)
        stats = unsub_report(conn)

    if not targets:
        console.print(
            "[yellow]No unsubscribe targets.[/yellow] Run [bold]ecs plan[/bold] first."
        )
        raise typer.Exit(0)

    table = Table(title=f"Unsubscribe list — {len(targets):,} senders")
    table.add_column("✓", width=3)
    table.add_column("sender", style="cyan")
    table.add_column("msgs", justify="right")
    table.add_column("unread", justify="right")
    table.add_column("method")
    table.add_column("status")
    for row in targets[:60]:
        table.add_row(
            "[green]y[/green]" if row["approved"] else "[red]n[/red]",
            (row["display_name"] or row["cluster_key"])[:44],
            f"{row['message_count'] or 0:,}",
            f"{row['unread_count'] or 0:,}",
            row["method"],
            row["status"],
        )
    console.print(table)
    if len(targets) > 60:
        console.print(f"[dim]... and {len(targets) - 60:,} more[/dim]")
    console.print(
        f"\ncovering {stats['messages_covered']:,} messages · "
        f"{stats['approved']:,} approved · by method: {stats['by_method']}"
    )

    if not approve:
        console.print(
            "\n[dim]Listing only. Re-run with [bold]--approve[/bold] to execute "
            "(or [bold]--approve-all --approve[/bold] to take the lot).[/dim]"
        )
        raise typer.Exit(0)

    account = _require_account()
    console.print(f"\n[bold]Unsubscribing on behalf of {account}[/bold]")

    result = run_unsubscribes(
        limit=limit,
        one_click_only=one_click_only,
        use_browser=not no_browser,
        headed=not headless,
        progress=lambda m: console.print(f"[dim]{m}[/dim]"),
    )
    console.print(f"\n[green]Unsubscribe run:[/green] {result.summary()}")

    if result.manual_list:
        manual = Table(title="Needs manual follow-up")
        manual.add_column("sender", style="cyan")
        manual.add_column("link")
        manual.add_column("why")
        for name, endpoint, why in result.manual_list[:25]:
            manual.add_row(name[:30], endpoint[:50], why[:40])
        console.print(manual)


@app.command()
def filters(
    execute: bool = typer.Option(
        False, "--apply", help="Create the filters. Omit for a dry run."
    ),
) -> None:
    """Create Gmail filters so the cleanup holds for future mail."""
    from .filters import apply_filters, filter_report

    with db.session() as conn:
        stats = filter_report(conn)

    if not stats["candidates"]:
        console.print(
            "[yellow]No filter candidates.[/yellow] Filters come from strategy rules "
            "marked filter-worthy — run [bold]ecs analyze --stage strategy[/bold]."
        )
        raise typer.Exit(0)

    table = Table(
        title=f"{stats['candidates']} filters "
        + ("to create" if execute else "(DRY RUN)")
    )
    table.add_column("sender", style="cyan")
    table.add_column("matches")
    table.add_column("action")
    for spec in stats["specs"][:40]:
        table.add_row(
            spec.display_name[:34],
            ", ".join(f"{k}:{v}" for k, v in spec.criteria.items())[:40],
            "delete" if spec.trash else f"label {spec.label!r} + archive",
        )
    console.print(table)
    console.print(
        f"\n{stats['archive_rules']} archive rules, {stats['delete_rules']} delete "
        f"rules, covering {stats['messages_covered']:,} historical messages"
    )

    if not execute:
        console.print("\n[dim]Dry run. Re-run with [bold]--apply[/bold].[/dim]")
        raise typer.Exit(0)

    _require_account()
    result = apply_filters(
        stats["specs"], dry_run=False, progress=lambda m: console.print(f"[dim]{m}[/dim]")
    )
    console.print(
        f"[green]Filters:[/green] {result['created']} created, "
        f"{result['skipped']} already existed, {result['failed']} failed"
    )


@app.command("inbox-zero")
def inbox_zero(
    execute: bool = typer.Option(
        False, "--apply", help="Actually archive them. Omit for a dry run."
    ),
    include_unlabelled: bool = typer.Option(
        False,
        "--include-unlabelled",
        help="Also archive messages carrying no label. Off by default — an unlabelled "
        "archived message is out of the inbox and filed nowhere.",
    ),
) -> None:
    """Archive every inbox message that already carries a label.

    Removes INBOX only; the message keeps its label and stays fully searchable.
    Journalled, so `ecs undo` puts it back.
    """
    from .gmail.auth import service
    from .gmail.mutate import archive_messages, partition_inbox_by_label
    from .journal import Entry, Journal
    from .progress import Reporter

    account = _require_account()
    svc = service()

    with Reporter("Inbox zero", console=console) as rep:
        rep.stage("checking which messages carry a label")
        labelled, unlabelled = partition_inbox_by_label(svc, progress=rep)
        rep.log(
            f"{len(labelled):,} labelled, {len(unlabelled):,} unlabelled", style="cyan"
        )

        targets = labelled + (unlabelled if include_unlabelled else [])

        if execute and targets:
            rep.stage("removing INBOX", total=len(targets))
            journal = Journal()
            done = 0
            for start in range(0, len(targets), 500):
                chunk = targets[start : start + 500]
                entries = [
                    Entry(
                        op="modify",
                        message_id=m["id"],
                        before={"labelIds": m["labelIds"]},
                        after={"addLabelIds": [], "removeLabelIds": ["INBOX"]},
                    )
                    for m in chunk
                ]
                journal.record_all(entries)
                try:
                    archive_messages(svc, [m["id"] for m in chunk])
                except Exception as exc:
                    for entry in entries:
                        journal.commit(entry, error=str(exc))
                    rep.error(f"chunk failed: {exc}")
                    continue
                for entry in entries:
                    journal.commit(entry)
                done += len(chunk)
                rep.advance(len(chunk))
            rep.log(f"archived {done:,}", style="green")

    if unlabelled and not include_unlabelled:
        table = Table(
            title=f"{len(unlabelled)} message(s) left in the inbox — no label to file under"
        )
        table.add_column("from", style="cyan")
        table.add_column("subject")
        for m in unlabelled[:20]:
            table.add_row(m["from"][:38], m["subject"][:52])
        console.print(table)
        console.print(
            "[dim]Left deliberately: archiving these would put them out of the inbox "
            "and filed nowhere. Label them by hand, add a filing rule, or pass "
            "[bold]--include-unlabelled[/bold] to archive anyway.[/dim]"
        )

    if not execute:
        console.print(
            f"\n[yellow]Dry run.[/yellow] {len(labelled):,} labelled messages would be "
            f"archived from [bold]{account}[/bold]. Re-run with [bold]--apply[/bold]."
        )
    else:
        console.print(
            "\n[dim]Reversible — the prior label set is journalled. "
            "[bold]ecs undo --all[/bold] restores INBOX.[/dim]"
        )


@app.command("mark-read")
def mark_read_cmd(
    execute: bool = typer.Option(
        False, "--apply", help="Actually mark them read. Omit for a dry run."
    ),
    everywhere: bool = typer.Option(
        False,
        "--all-mail",
        help="Include unread mail outside the inbox, not just inbox unread.",
    ),
    labelled_only: bool = typer.Option(
        False,
        "--labelled-only",
        help="Only mark messages that carry a label. Leaves unfiled mail unread so it "
        "still stands out.",
    ),
) -> None:
    """Mark unread messages as read.

    Reversible: the prior label set is journalled, so `ecs undo` puts UNREAD back.
    Goes through batchModify (1,000 ids per call, one concurrent operation), so it
    doesn't hit the write-concurrency ceiling that the trash path did.
    """
    from .gmail.auth import service
    from .gmail.mutate import list_unread, mark_read
    from .journal import Entry, Journal
    from .progress import Reporter

    account = _require_account()
    svc = service()

    with Reporter("Marking read", console=console) as rep:
        rep.stage("finding unread messages")
        unread = list_unread(
            svc, inbox_only=not everywhere, labelled_only=labelled_only,
            progress=rep,
        )
        rep.log(
            f"{len(unread):,} unread "
            + ("across the mailbox" if everywhere else "in the inbox")
            + (" carrying a label" if labelled_only else ""),
            style="cyan",
        )

        if not unread:
            rep.log("nothing to do", style="green")
            raise typer.Exit(0)

        if not execute:
            rep.log("dry run — nothing changed", style="yellow")
        else:
            rep.stage("removing UNREAD", total=len(unread))
            journal = Journal()
            done = 0
            # Chunked so the journal is written incrementally; an interrupted run
            # leaves an accurate record rather than an all-or-nothing gap.
            for start in range(0, len(unread), 500):
                chunk = unread[start : start + 500]
                entries = [
                    Entry(
                        op="modify",
                        message_id=m["id"],
                        before={"labelIds": m["labelIds"]},
                        after={"addLabelIds": [], "removeLabelIds": ["UNREAD"]},
                    )
                    for m in chunk
                ]
                journal.record_all(entries)
                try:
                    mark_read(svc, [m["id"] for m in chunk])
                except Exception as exc:
                    for entry in entries:
                        journal.commit(entry, error=str(exc))
                    rep.error(f"chunk failed: {exc}")
                    continue
                for entry in entries:
                    journal.commit(entry)
                done += len(chunk)
                rep.advance(len(chunk))
            rep.log(f"marked {done:,} as read", style="green")

    if not execute:
        console.print(
            f"\n[yellow]Dry run.[/yellow] {len(unread):,} messages would be marked "
            f"read in [bold]{account}[/bold]. Re-run with [bold]--apply[/bold]."
        )
    else:
        console.print(
            "\n[dim]Reversible: [bold]ecs undo --all[/bold] restores UNREAD "
            "(along with any other journalled changes).[/dim]"
        )


@app.command()
def rules(
    test: bool = typer.Option(
        False, "--test", help="Count what each rule would claim against the index."
    ),
) -> None:
    """Show your filing rules from filing-rules.toml.

    These are explicit instructions that no model can infer — which senders belong to
    which project, where a particular person's mail goes. They outrank every model
    verdict but never override the keep-signal guards.
    """
    from .filing_rules import DEFAULT_PATH, load_rules, preview

    try:
        loaded = load_rules()
    except (ValueError, KeyError) as exc:
        console.print(f"[red]Invalid {DEFAULT_PATH}:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not loaded:
        console.print(
            f"[yellow]No rules found.[/yellow] Create [bold]{DEFAULT_PATH}[/bold] to "
            "tell the system where specific senders or projects should file."
        )
        raise typer.Exit(0)

    console.print(
        f"[green]{len(loaded)} rules[/green] from {DEFAULT_PATH} "
        "[dim](first match wins, top to bottom)[/dim]\n"
    )

    if not test:
        table = Table(show_header=True)
        table.add_column("#", width=2)
        table.add_column("rule", style="cyan")
        table.add_column("files to")
        table.add_column("action")
        for i, rule in enumerate(loaded, start=1):
            table.add_row(str(i), rule.name[:34], rule.label, rule.disposition)
        console.print(table)
        console.print(
            "\n[dim]Run [bold]ecs rules --test[/bold] to see how many messages each "
            "would claim before trusting them.[/dim]"
        )
        raise typer.Exit(0)

    with db.session() as conn:
        if db.message_count(conn) == 0:
            console.print("[red]Nothing indexed.[/red] Run [bold]ecs index[/bold].")
            raise typer.Exit(1)
        results = preview(conn, loaded)

    total = 0
    for entry in results:
        total += entry["matches"]
        console.print(
            f"[cyan]{entry['name']}[/cyan] -> [bold]{entry['label']}[/bold] "
            f"({entry['disposition']})"
        )
        console.print(
            f"  {entry['matches']:,} messages from {entry['distinct_senders']} senders"
        )
        for example in entry["examples"]:
            console.print(f"    [dim]· {example}[/dim]")
        if entry["matches"] == 0:
            console.print(
                "    [yellow]matches nothing — check the sender or subject terms[/yellow]"
            )
        console.print()

    with db.session() as conn:
        mailbox = db.message_count(conn)
    console.print(
        f"[green]{total:,} of {mailbox:,} messages[/green] "
        f"({100 * total / mailbox:.0f}%) filed by explicit rule, no model involved."
    )


@app.command()
def status() -> None:
    """Show what has been indexed and which stages have run."""
    with db.session() as conn:
        table = Table(title="Email Cleanup Swarm — run state", show_header=False)
        table.add_column("", style="cyan")
        table.add_column("")

        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        bound = db.kv_get(conn, "account.email")
        table.add_row(
            "bound mailbox",
            f"[bold]{bound}[/bold]" if bound else "[yellow]not bound — run ecs auth[/yellow]",
        )
        table.add_row("", "")
        table.add_row("inbox messages indexed", f"{db.message_count(conn):,}")
        table.add_row("clusters", f"{db.cluster_count(conn):,}")
        table.add_row(
            "protected senders", f"{count('SELECT COUNT(*) FROM protected_senders'):,}"
        )
        table.add_row(
            "existing labels", f"{count('SELECT COUNT(*) FROM existing_labels'):,}"
        )
        table.add_row("", "")
        table.add_row(
            "triage verdicts", f"{count('SELECT COUNT(*) FROM triage_verdicts'):,}"
        )
        table.add_row(
            "strategy runs", f"{count('SELECT COUNT(*) FROM strategy_runs'):,}"
        )
        table.add_row("challenges", f"{count('SELECT COUNT(*) FROM challenges'):,}")
        table.add_row("escalations", f"{count('SELECT COUNT(*) FROM escalations'):,}")
        table.add_row("human decisions", f"{count('SELECT COUNT(*) FROM decisions'):,}")
        table.add_row("", "")
        table.add_row(
            "planned actions", f"{count('SELECT COUNT(*) FROM plan_actions'):,}"
        )
        table.add_row(
            "  approved",
            f"{count('SELECT COUNT(*) FROM plan_actions WHERE approved = 1'):,}",
        )
        table.add_row(
            "  applied",
            f"{count('SELECT COUNT(*) FROM plan_actions WHERE applied_at IS NOT NULL'):,}",
        )
        table.add_row(
            "unsubscribe targets", f"{count('SELECT COUNT(*) FROM unsub_targets'):,}"
        )
        console.print(table)

    from .journal import Journal

    summary = Journal().summary()
    if summary:
        console.print("\n[bold]Journal[/bold] (committed mutations)")
        for op, n in sorted(summary.items()):
            console.print(f"  {op}: {n:,}")
    else:
        console.print("\n[dim]Journal empty — nothing has been mutated yet.[/dim]")

    console.print(f"\n[dim]db:      {config.DB_PATH}[/dim]")
    console.print(f"[dim]journal: {config.JOURNAL_PATH}[/dim]")


if __name__ == "__main__":
    app()
