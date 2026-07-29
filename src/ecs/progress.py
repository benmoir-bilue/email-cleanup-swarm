"""Live progress reporting.

Long stages here run for minutes — a 23,000-message index is hundreds of API calls
paced against a quota. A single spinner that says "indexing..." tells you nothing about
whether it's working, stuck, or rate-limited, which is exactly the wrong experience for
an operation touching your whole mailbox.

So: a persistent progress bar with counts, rate and ETA, above a scrolling log of what
just happened. The `Reporter` is callable, so it drops into every existing
`progress=`-style parameter without changing call sites, and stages that want a real
bar opt in by calling `stage()` / `advance()`.

Falls back to plain line-by-line printing when stdout isn't a TTY, so piping to a file
or running in CI still produces a readable record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

LOG_LINES = 10


@dataclass
class Counter:
    """A named tally shown in the stats row (indexed, failed, cost, ...)."""

    label: str
    value: float = 0
    suffix: str = ""
    style: str = "white"
    is_float: bool = False

    def render(self) -> str:
        if self.is_float:
            return f"{self.value:,.2f}{self.suffix}"
        return f"{int(self.value):,}{self.suffix}"


class Reporter:
    """Live display for a multi-stage run.

    Usage:
        with Reporter("Index") as rep:
            rep.stage("fetching metadata", total=5838)
            rep.log("listed 500 ids")
            rep.advance(100)
    """

    def __init__(
        self,
        title: str,
        *,
        console: Console | None = None,
        quiet: bool = False,
    ) -> None:
        self.title = title
        self.console = console or Console()
        self.quiet = quiet
        # Rich's Live display corrupts piped output; degrade to plain prints instead.
        self.live_enabled = self.console.is_terminal and not quiet

        self._progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
            MofNCompleteColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("elapsed •"),
            TimeRemainingColumn(),
            TextColumn("left"),
            console=self.console,
            expand=True,
        )
        self._task_id: int | None = None
        self._log: deque[Text] = deque(maxlen=LOG_LINES)
        self._counters: dict[str, Counter] = {}
        self._live: Live | None = None
        self._stage_name = ""

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> Reporter:
        if self.live_enabled:
            self._live = Live(
                self._render(),
                console=self.console,
                refresh_per_second=8,
                transient=False,
            )
            self._live.__enter__()
        elif not self.quiet:
            self.console.print(f"[bold]{self.title}[/bold]")
        return self

    def __exit__(self, *exc_info) -> None:
        if self._live is not None:
            self._refresh()
            self._live.__exit__(*exc_info)
            self._live = None

    # -- stages -----------------------------------------------------------

    def stage(self, name: str, *, total: int | None = None) -> None:
        """Begin a new stage, replacing the current bar."""
        self._stage_name = name
        if self._task_id is not None:
            self._progress.remove_task(self._task_id)
        self._task_id = self._progress.add_task(name, total=total)
        if not self.live_enabled and not self.quiet:
            suffix = f" ({total:,} items)" if total else ""
            self.console.print(f"[cyan]▸ {name}[/cyan]{suffix}")
        self._refresh()

    def set_total(self, total: int) -> None:
        if self._task_id is not None:
            self._progress.update(self._task_id, total=total)
            self._refresh()

    def advance(self, n: int = 1) -> None:
        if self._task_id is not None:
            self._progress.advance(self._task_id, n)
            self._refresh()

    def set_completed(self, n: int) -> None:
        if self._task_id is not None:
            self._progress.update(self._task_id, completed=n)
            self._refresh()

    # -- counters ---------------------------------------------------------

    def counter(
        self,
        key: str,
        label: str | None = None,
        *,
        suffix: str = "",
        style: str = "white",
        is_float: bool = False,
    ) -> None:
        self._counters[key] = Counter(
            label=label or key, suffix=suffix, style=style, is_float=is_float
        )

    def bump(self, key: str, n: float = 1) -> None:
        if key in self._counters:
            self._counters[key].value += n
            self._refresh()

    def set(self, key: str, value: float) -> None:
        if key in self._counters:
            self._counters[key].value = value
            self._refresh()

    # -- logging ----------------------------------------------------------

    def log(self, message: str, *, style: str = "dim") -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        line = Text.assemble((f"{stamp}  ", "dim"), (message, style))
        self._log.append(line)
        if not self.live_enabled and not self.quiet:
            self.console.print(f"[dim]{stamp}[/dim]  {message}")
        self._refresh()

    def warn(self, message: str) -> None:
        self.log(message, style="yellow")

    def error(self, message: str) -> None:
        self.log(message, style="red")

    def __call__(self, message: str) -> None:
        """Callable so existing `progress=` parameters keep working unchanged."""
        self.log(message)

    # -- rendering --------------------------------------------------------

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _stats_row(self) -> Table | None:
        active = [c for c in self._counters.values() if c.value or c.is_float]
        if not active:
            return None
        table = Table.grid(padding=(0, 2))
        for counter in active:
            table.add_column(justify="left")
        table.add_row(
            *[
                Text.assemble(
                    (f"{c.label} ", "dim"), (c.render(), f"bold {c.style}")
                )
                for c in active
            ]
        )
        return table

    def _render(self) -> Group:
        parts: list = [self._progress]
        stats = self._stats_row()
        if stats is not None:
            parts.append(stats)
        if self._log:
            parts.append(
                Panel(
                    Group(*self._log),
                    title="activity",
                    title_align="left",
                    border_style="grey37",
                    padding=(0, 1),
                )
            )
        return Group(*parts)


@dataclass
class NullReporter:
    """No-op reporter for tests and quiet runs."""

    calls: list[str] = field(default_factory=list)

    def __enter__(self) -> NullReporter:
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def stage(self, name: str, *, total: int | None = None) -> None:
        return None

    def set_total(self, total: int) -> None:
        return None

    def advance(self, n: int = 1) -> None:
        return None

    def set_completed(self, n: int) -> None:
        return None

    def counter(self, key: str, label: str | None = None, **kw) -> None:
        return None

    def bump(self, key: str, n: float = 1) -> None:
        return None

    def set(self, key: str, value: float) -> None:
        return None

    def log(self, message: str, *, style: str = "dim") -> None:
        self.calls.append(message)

    def warn(self, message: str) -> None:
        self.calls.append(message)

    def error(self, message: str) -> None:
        self.calls.append(message)

    def __call__(self, message: str) -> None:
        self.calls.append(message)
