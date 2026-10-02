"""status."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence

import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from talkub.config import Settings
from talkub.overview import status as status_flow
from talkub.overview.status import APP_HEADER, NODE_HEADER, KubeStatus, NodeStatus, StatusReport

PENDING = Text("…", style="dim")

_WARN = {"OutOfSync", "Progressing", "Suspended", "Running", "Unknown"}
_BAD = {"Degraded", "Missing", "Failed", "Error"}


def _state(value: str) -> Text:
    """A sync/health/operation cell: red when broken, yellow when in flux, plain otherwise."""
    if value in _BAD:
        return Text(value, style="bold red")
    if value in _WARN:
        return Text(value, style="yellow")
    return Text(value)


def _node_table(
    settings: Settings,
    names: Sequence[str],
    nodes: Mapping[str, NodeStatus],
    kube: KubeStatus | None,
    steps: Mapping[str, str] | None = None,
) -> Table:
    """The node table: one line per node in `names`. A node still being looked up has dimmed
    cells and, while a call runs (`steps[name]`), a spinner plus the call's name in KUBELET's
    place; the KUBELET cell stays pending until the Kubernetes side (`kube`) is in."""
    table = Table(box=None, show_edge=False, pad_edge=False, header_style="bold", collapse_padding=True)
    table.add_column("", width=1, no_wrap=True)
    for title in NODE_HEADER:
        table.add_column(title, no_wrap=True)
    for name in names:
        node = nodes.get(name)
        if node is None:
            step = (steps or {}).get(name)
            marker = Spinner("dots", style="cyan") if step else " "
            table.add_row(marker, name, PENDING, PENDING, PENDING, PENDING, Text(step or "", style="dim"))
            continue
        kubelet = kube.kubelets.get(name) if kube is not None else None
        cells = status_flow.node_cells(settings, node, kubelet)
        waiting = kube is None and node.reachable  # the KUBELET cell is still on its way
        marker = Spinner("dots", style="cyan") if waiting else " "
        last = PENDING if waiting else Text(cells[-1])
        table.add_row(marker, *(_mark(cell) for cell in cells[:-1]), last)
    return table


def _mark(cell: str) -> Text:
    """A node cell: drift and mismatches stand out."""
    if cell in ("unreachable", "changes") or "(want " in cell:
        return Text(cell, style="bold yellow")
    return Text(cell)


def _app_table(kube: KubeStatus) -> Table:
    table = Table(box=None, show_edge=False, pad_edge=False, header_style="bold", collapse_padding=True)
    for title in APP_HEADER:
        table.add_column(title, no_wrap=title != "NOTE")
    for app in kube.application_details:
        name, sync, health, operation, revision, notes = status_flow.app_cells(app)
        table.add_row(name, _state(sync), _state(health), _state(operation), revision, Text("\n".join(notes)))
    return table


def _argocd_lines(kube: KubeStatus, *, apps: bool) -> list[RenderableType]:
    """The Argo CD summary line plus, with `apps`, the application table, or else the per-app
    error lines (as the plain text output has them)."""
    lines: list[RenderableType] = [Text(status_flow.argocd_line(kube.kube, kube.applications))]
    if apps and kube.application_details:
        lines.extend([Text(""), _app_table(kube)])
    elif not apps and kube.kube == "ok" and kube.applications:
        lines.extend(Text(f"  {name}: {error}") for name, error in kube.application_errors)
    return lines


def _as_kube(report: StatusReport) -> KubeStatus:
    """The Kubernetes side of a finished report, for rendering it like a live update."""
    return KubeStatus(
        report.kube,
        kubelets={node.name: node.kubelet for node in report.nodes if node.kubelet},
        applications=report.applications,
        application_errors=report.application_errors,
        application_details=report.application_details,
    )


def _header(settings: Settings) -> Text:
    return Text(f"cluster {settings.name}: talos {settings.talos_version}, kubernetes {settings.kubernetes_version}")


def _final(report: StatusReport, *, apps: bool) -> Group:
    """The finished status, styled like the live view."""
    nodes = {node.name: node for node in report.nodes}
    kube = _as_kube(report)
    return Group(
        _header(report.settings),
        Text(""),
        _node_table(report.settings, [node.name for node in report.nodes], nodes, kube),
        Text(""),
        *_argocd_lines(kube, apps=apps),
    )


class _LiveStatus:
    """Drives a `rich.live.Live` view from `collect`'s callbacks, which arrive from worker
    threads: every node is listed up front with pending cells, a running one shows a spinner and
    the call in progress, rows fill in as nodes complete, and the Argo CD line (plus the
    application table with `apps`) replaces its spinner once the Kubernetes side is in.
    Transient — the final result is printed separately once collecting is done."""

    def __init__(self, console: Console, settings: Settings, names: Sequence[str], *, apps: bool) -> None:
        self._settings = settings
        self._names: Sequence[str] = names
        self._apps = apps
        self._nodes: dict[str, NodeStatus] = {}
        self._kube: KubeStatus | None = None
        self._steps: dict[str, str] = {}
        self._lock = threading.Lock()
        self._live: Live = Live(self._render(), console=console, transient=True, refresh_per_second=12)

    def _render(self) -> Group:
        parts: list[RenderableType] = [
            _header(self._settings),
            Text(""),
            _node_table(self._settings, self._names, self._nodes, self._kube, self._steps),
            Text(""),
        ]
        if self._kube is None:
            step = self._steps.get("kubernetes")
            line = Table.grid(padding=(0, 1))
            line.add_row(
                Spinner("dots", style="cyan") if step else " ", Text("argocd: "), PENDING, Text(step or "", style="dim")
            )
            parts.append(line)
        else:
            parts.extend(_argocd_lines(self._kube, apps=self._apps))
        return Group(*parts)

    def _refresh(self) -> None:
        with self._lock:
            self._live.update(self._render())

    def on_step(self, subject: str, step: str) -> None:
        self._steps[subject] = step
        self._refresh()

    def on_node(self, node: NodeStatus) -> None:
        self._nodes[node.name] = node
        self._steps.pop(node.name, None)
        self._refresh()

    def on_kube(self, kube: KubeStatus) -> None:
        self._kube = kube
        self._steps.pop("kubernetes", None)
        self._refresh()

    def __enter__(self) -> _LiveStatus:
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._live.stop()


def status(
    ctx: typer.Context,
    apps: bool = typer.Option(
        False, "--apps", "-a", help="One line per Argo CD Application: sync, health, operation, revision, problems."
    ),
) -> None:
    """Show target vs. actual per node (Talos, schematic, config drift, kubelet) and Argo CD apps."""
    context = ctx.obj.context()
    console = Console()
    if not console.is_terminal:
        print(status_flow.render(status_flow.collect(context), apps=apps))
        return
    settings = context.repo.load_settings()
    with _LiveStatus(console, settings, status_flow.nodes_in_order(settings), apps=apps) as live:
        report = status_flow.collect(context, on_step=live.on_step, on_node=live.on_node, on_kube=live.on_kube)
    console.print(_final(report, apps=apps))
