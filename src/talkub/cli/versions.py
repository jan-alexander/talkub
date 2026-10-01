"""versions, versions set."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence

import typer
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from talkub.overview import versions as versions_flow
from talkub.overview.versions import Row

app = typer.Typer(help="Target vs. running vs. latest versions: Talos, Kubernetes and chart apps.")

PENDING = Text("…", style="dim")


def _table(names: Sequence[str], rows: Mapping[str, Row], steps: Mapping[str, str] | None = None) -> Table:
    """The versions table: one line per component in `names`. A finished row is starred when
    outdated (as in the plain text output) with LATEST highlighted; an unfinished one has dimmed
    cells and, while a lookup runs (`steps[name]`), a spinner plus the lookup's name in NOTE."""
    table = Table(box=None, show_edge=False, pad_edge=False, header_style="bold", collapse_padding=True)
    table.add_column("", width=1, no_wrap=True)
    for title in ("COMPONENT", "TARGET", "RUNNING", "LATEST", "NOTE"):
        table.add_column(title, no_wrap=True)
    for name in names:
        row = rows.get(name)
        if row is None:
            step = (steps or {}).get(name)
            marker = Spinner("dots", style="cyan") if step else " "
            table.add_row(marker, name, PENDING, PENDING, PENDING, Text(step or "", style="dim"))
            continue
        latest = Text(row.latest, style="bold yellow") if row.outdated else Text(row.latest)
        table.add_row("*" if row.outdated else " ", row.component, row.target, row.running, latest, row.note)
    return table


class _LiveTable:
    """Drives a `rich.live.Live` table from `collect`'s callbacks, which arrive from worker
    threads: every component is listed up front with pending cells, a running one shows a spinner
    and the lookup in progress, and rows fill in as they complete. Transient — the final result is
    printed separately once collecting is done."""

    def __init__(self, console: Console, names: Sequence[str]) -> None:
        self._names: Sequence[str] = names
        self._rows: dict[str, Row] = {}
        self._steps: dict[str, str] = {}
        self._lock = threading.Lock()
        self._live: Live = Live(self._render(), console=console, transient=True, refresh_per_second=12)

    def _render(self) -> Table:
        return _table(self._names, self._rows, self._steps)

    def _refresh(self) -> None:
        with self._lock:
            self._live.update(self._render())

    def on_step(self, component: str, step: str) -> None:
        self._steps[component] = step
        self._refresh()

    def on_row(self, row: Row) -> None:
        self._rows[row.component] = row
        self._steps.pop(row.component, None)
        self._refresh()

    def __enter__(self) -> _LiveTable:
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._live.stop()


@app.callback(invoke_without_command=True)
def show(
    ctx: typer.Context,
    offline: bool = typer.Option(False, "--offline", help="Skip latest-version lookups (no network)."),
    only: list[str] | None = typer.Option(
        None,
        "--only",
        metavar="COMPONENT",
        help="Only this component (talos, kubernetes or a chart app); repeatable.",
    ),
    outdated: bool = typer.Option(False, "--outdated", help="Only rows whose LATEST differs from TARGET."),
) -> None:
    """Show target vs. running vs. latest versions for Talos, Kubernetes and every chart app."""
    if ctx.invoked_subcommand is not None:
        return
    context = ctx.obj.context()
    console = Console()
    if not console.is_terminal:
        rows = versions_flow.collect(context, offline=offline, only=only)
        _print_final(console, rows, outdated=outdated)
        return
    names = versions_flow.components(context.repo)
    if only:
        names = [name for name in names if name in only]
    with _LiveTable(console, names) as live:
        rows = versions_flow.collect(context, offline=offline, only=only, on_step=live.on_step, on_row=live.on_row)
    _print_final(console, rows, outdated=outdated)


def _print_final(console: Console, rows: list[Row], *, outdated: bool) -> None:
    if outdated:
        rows = versions_flow.outdated(rows)
        if not rows:
            print("all components are up to date")
            return
    if console.is_terminal:
        console.print(_table([row.component for row in rows], {row.component: row for row in rows}))
    else:
        print(versions_flow.render(rows), end="")


@app.command("set")
def set_(
    ctx: typer.Context,
    component: str = typer.Argument(..., help="talos or kubernetes."),
    version: str = typer.Argument(..., help="New target version."),
) -> None:
    """Set the target Talos or Kubernetes version in .talkub.yaml."""
    print(versions_flow.set_version(ctx.obj.repo(), component, version))
