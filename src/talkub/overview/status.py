"""talkub status: target vs. actual per node, plus Argo CD applications.

Collecting is slow (three talosctl calls per node, two kubectl calls), so every node and the
Kubernetes side are looked up concurrently and `collect` reports progress through optional
callbacks, like `versions.collect`: `on_step(subject, step)` before every slow call (`subject` is a
node name or "kubernetes"), `on_node(node)` as soon as a node's Talos cells are complete (its
kubelet arrives with the Kubernetes side) and `on_kube(kube)` once the Kubernetes side is done.
The CLI drives a live table from them; this module itself never prints to stdout.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from talkub.config import Settings
from talkub.context import Context
from talkub.core.ephemeral import private_file
from talkub.core.errors import TalkubError
from talkub.kube.session import KubeSession, kube_session
from talkub.talos import inspect
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.inspect import ApplicationDetail
from talkub.talos.lifecycle import is_bootstrapped, ordered_nodes, prepare

KUBECTL_TIMEOUT = "--request-timeout=10s"
MAX_WORKERS = 8  # nodes looked up at the same time (plus the Kubernetes side)

NODE_HEADER = ("NODE", "ADDRESS", "TALOS", "SCHEMATIC", "CONFIG", "KUBELET")
APP_HEADER = ("APP", "SYNC", "HEALTH", "OPERATION", "REVISION", "NOTE")


@dataclass
class NodeStatus:
    name: str
    address: str
    reachable: bool
    talos: str | None = None
    schematic: str | None = None
    config: str = "unknown"
    kubelet: str | None = None


@dataclass
class KubeStatus:
    """The Kubernetes side of the report: kubelet versions per node and the Argo CD Applications."""

    kube: str  # "ok" | "not bootstrapped" | "unavailable"
    kubelets: dict[str, str] = field(default_factory=dict)
    applications: dict[str, int] | None = None
    application_errors: list[tuple[str, str]] = field(default_factory=list)  # (app, "<type>: <message>")
    application_details: list[ApplicationDetail] = field(default_factory=list)


@dataclass
class StatusReport:
    settings: Settings
    nodes: list[NodeStatus]
    kube: str  # "ok" | "not bootstrapped" | "unavailable"
    applications: dict[str, int] | None = None
    application_errors: list[tuple[str, str]] = field(default_factory=list)  # (app, "<type>: <message>")
    application_details: list[ApplicationDetail] = field(default_factory=list)


Step = Callable[[str, str], None]  # (node name or "kubernetes", what is being looked up)


def _no_step(_subject: str, _step: str) -> None:
    pass


def _node_status(session: TalosSession, settings: Settings, name: str, config: bytes | None, step: Step) -> NodeStatus:
    address = settings.mgmt_address(name)
    step(name, "talos version")
    version = session.run(address, ["version"], check=False)
    if version.returncode != 0:
        return NodeStatus(name, address, reachable=False)
    node = NodeStatus(name, address, reachable=True, talos=inspect.server_version(version.stdout.decode()))
    step(name, "extensions")
    extensions = session.run(address, ["get", "extensions", "-o", "json"], check=False)
    node.schematic = inspect.schematic_id(extensions.stdout.decode()) if extensions.returncode == 0 else None
    if config is not None:
        step(name, "config drift")
        with private_file(f"{name}.yaml", config) as path:
            dry_run = session.run(address, ["apply-config", "--file", str(path), "--dry-run"], check=False)
        if dry_run.returncode == 0:
            node.config = inspect.config_drift((dry_run.stdout + (dry_run.stderr or b"")).decode())
    return node


def _kube_status(kube: KubeSession, step: Step) -> KubeStatus:
    step("kubernetes", "nodes")
    kubelets = kube.kubectl(["get", "nodes", "-o", "json", KUBECTL_TIMEOUT], check=False)
    if kubelets.returncode != 0:
        return KubeStatus("unavailable")
    step("kubernetes", "applications")
    apps = kube.kubectl(
        ["get", "applications.argoproj.io", "-n", "argocd", "-o", "json", KUBECTL_TIMEOUT],
        check=False,
    )
    status = KubeStatus("ok", kubelets=inspect.kubelet_versions(kubelets.stdout.decode()))
    if apps.returncode == 0:
        text = apps.stdout.decode()
        status.applications = inspect.application_summary(text)
        status.application_errors = inspect.application_errors(text)
        status.application_details = inspect.application_details(text)
    return status


def _collect_kube(ctx: Context, step: Step) -> KubeStatus:
    if not is_bootstrapped(ctx):
        return KubeStatus("not bootstrapped")
    try:
        with kube_session(ctx) as kube:
            return _kube_status(kube, step)
    except TalkubError as err:
        print(f"talkub: cannot query Kubernetes ({err})", file=sys.stderr)
        return KubeStatus("unavailable")


def collect(
    ctx: Context,
    *,
    on_step: Step | None = None,
    on_node: Callable[[NodeStatus], None] | None = None,
    on_kube: Callable[[KubeStatus], None] | None = None,
) -> StatusReport:
    """Every node's Talos state plus the Kubernetes side, looked up concurrently (up to
    `MAX_WORKERS` nodes at a time, the Kubernetes side alongside) over sessions opened once here.
    The callbacks arrive from worker threads in no fixed order across subjects; the report's node
    order is the settings' order with kubelet versions merged in."""
    step = on_step or _no_step
    try:
        settings, order, configs = prepare(ctx, [], refresh=False)
    except TalkubError as err:
        print(
            f"talkub: cannot render machine configs ({err}); config drift is unknown",
            file=sys.stderr,
        )
        settings = ctx.repo.load_settings()
        order = ordered_nodes(settings, [])
        configs = {}

    def node_for(name: str) -> NodeStatus:
        node = _node_status(session, settings, name, configs.get(name), step)
        if on_node is not None:
            on_node(node)
        return node

    def kube_for() -> KubeStatus:
        kube = _collect_kube(ctx, step)
        if on_kube is not None:
            on_kube(kube)
        return kube

    with talos_session(ctx) as session, ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(order)) + 1) as pool:
        kube_future = pool.submit(kube_for)
        nodes = list(pool.map(node_for, order))
        kube = kube_future.result()
    for node in nodes:
        node.kubelet = kube.kubelets.get(node.name)
    return StatusReport(
        settings,
        nodes,
        kube.kube,
        applications=kube.applications,
        application_errors=kube.application_errors,
        application_details=kube.application_details,
    )


def nodes_in_order(settings: Settings) -> list[str]:
    """The node names `collect` reports, in table order (for a live table listing them up front)."""
    return ordered_nodes(settings, [])


def _compare(actual: str | None, wanted: str | None, width: int | None = None) -> str:
    def cut(value: str | None) -> str | None:
        return value[:width] if value and width else value

    if actual is None:
        return "unknown"
    if wanted is not None and actual != wanted:
        return f"{cut(actual)} (want {cut(wanted)})"
    return cut(actual) or "unknown"


def node_cells(settings: Settings, node: NodeStatus, kubelet: str | None) -> tuple[str, ...]:
    """The `NODE_HEADER` cells of one node; `kubelet` is `None` while unknown."""
    if not node.reachable:
        return (node.name, node.address, "unreachable", "-", "-", "-")
    wanted = settings.schematics.get(settings.node_platform(node.name))
    return (
        node.name,
        node.address,
        _compare(node.talos, settings.talos_version),
        _compare(node.schematic, wanted.id if wanted else None, width=12),
        node.config,
        _compare(kubelet, settings.kubernetes_version) if kubelet else "-",
    )


def app_cells(app: ApplicationDetail) -> tuple[str, str, str, str, str, list[str]]:
    """The `APP_HEADER` cells of one Application; NOTE is a list of lines (errors, then the
    resources that are not Healthy) for the renderer to join."""
    return (app.name, app.sync, app.health, app.operation, app.revision, [*app.errors, *app.resources])


def argocd_line(kube: str, applications: dict[str, int] | None) -> str:
    """The one-line Argo CD summary below the node table."""
    if kube != "ok":
        return f"kubernetes: {kube}"
    if applications is None:
        return "argocd: applications not available"
    if not applications:
        return "argocd: no applications"
    return "argocd: " + ", ".join(f"{count} {state}" for state, count in sorted(applications.items()))


def _text_table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]


def render(report: StatusReport, *, apps: bool = False) -> str:
    """Plain text: the node table, the Argo CD summary and, with `apps`, one line per Application
    (NOTE joined with "; ") instead of the per-app error lines."""
    settings = report.settings
    lines = [
        f"cluster {settings.name}: talos {settings.talos_version}, kubernetes {settings.kubernetes_version}",
        "",
        *_text_table([NODE_HEADER, *(node_cells(settings, node, node.kubelet) for node in report.nodes)]),
        "",
        argocd_line(report.kube, report.applications),
    ]
    if apps and report.application_details:
        rows: list[tuple[str, ...]] = [APP_HEADER]
        for app in report.application_details:
            *cells, notes = app_cells(app)
            rows.append((*cells, "; ".join(notes)))
        lines.extend(["", *_text_table(rows)])
    elif not apps and report.kube == "ok" and report.applications:
        lines.extend(f"  {name}: {error}" for name, error in report.application_errors)
    return "\n".join(lines)
