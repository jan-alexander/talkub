"""talkub status: target vs. actual per node, plus Argo CD applications."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from talkub.config import Settings
from talkub.context import Context
from talkub.core.ephemeral import private_file
from talkub.core.errors import TalkubError
from talkub.kube.session import kube_session
from talkub.talos import inspect
from talkub.talos.client import talos_session
from talkub.talos.lifecycle import is_bootstrapped, ordered_nodes, prepare

KUBECTL_TIMEOUT = "--request-timeout=10s"


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
class StatusReport:
    settings: Settings
    nodes: list[NodeStatus]
    kube: str  # "ok" | "not bootstrapped" | "unavailable"
    applications: dict[str, int] | None = None


def collect(ctx: Context) -> StatusReport:
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

    nodes: list[NodeStatus] = []
    with talos_session(ctx) as session:
        for name in order:
            address = settings.mgmt_address(name)
            version = session.run(address, ["version"], check=False)
            if version.returncode != 0:
                nodes.append(NodeStatus(name, address, reachable=False))
                continue
            node = NodeStatus(
                name,
                address,
                reachable=True,
                talos=inspect.server_version(version.stdout.decode()),
            )
            extensions = session.run(address, ["get", "extensions", "-o", "json"], check=False)
            node.schematic = inspect.schematic_id(extensions.stdout.decode()) if extensions.returncode == 0 else None
            if name in configs:
                with private_file(f"{name}.yaml", configs[name]) as path:
                    dry_run = session.run(
                        address,
                        ["apply-config", "--file", str(path), "--dry-run"],
                        check=False,
                    )
                if dry_run.returncode == 0:
                    node.config = inspect.config_drift((dry_run.stdout + (dry_run.stderr or b"")).decode())
            nodes.append(node)

    if not is_bootstrapped(ctx):
        return StatusReport(settings, nodes, kube="not bootstrapped")
    try:
        with kube_session(ctx) as kube:
            kubelets = kube.kubectl(["get", "nodes", "-o", "json", KUBECTL_TIMEOUT], check=False)
            apps = kube.kubectl(
                [
                    "get",
                    "applications.argoproj.io",
                    "-n",
                    "argocd",
                    "-o",
                    "json",
                    KUBECTL_TIMEOUT,
                ],
                check=False,
            )
    except TalkubError as err:
        print(f"talkub: cannot query Kubernetes ({err})", file=sys.stderr)
        return StatusReport(settings, nodes, kube="unavailable")
    if kubelets.returncode != 0:
        return StatusReport(settings, nodes, kube="unavailable")
    versions = inspect.kubelet_versions(kubelets.stdout.decode())
    for node in nodes:
        node.kubelet = versions.get(node.name)
    applications = inspect.application_summary(apps.stdout.decode()) if apps.returncode == 0 else None
    return StatusReport(settings, nodes, kube="ok", applications=applications)


def _compare(actual: str | None, wanted: str | None, width: int | None = None) -> str:
    def cut(value: str | None) -> str | None:
        return value[:width] if value and width else value

    if actual is None:
        return "unknown"
    if wanted is not None and actual != wanted:
        return f"{cut(actual)} (want {cut(wanted)})"
    return cut(actual) or "unknown"


def render(report: StatusReport) -> str:
    settings = report.settings
    rows = [("NODE", "ADDRESS", "TALOS", "SCHEMATIC", "CONFIG", "KUBELET")]
    for node in report.nodes:
        if not node.reachable:
            rows.append((node.name, node.address, "unreachable", "-", "-", "-"))
            continue
        wanted = settings.schematics.get(settings.node_platform(node.name))
        rows.append(
            (
                node.name,
                node.address,
                _compare(node.talos, settings.talos_version),
                _compare(node.schematic, wanted.id if wanted else None, width=12),
                node.config,
                _compare(node.kubelet, settings.kubernetes_version) if node.kubelet else "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        f"cluster {settings.name}: talos {settings.talos_version}, kubernetes {settings.kubernetes_version}",
        "",
        *("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows),
        "",
    ]
    if report.kube != "ok":
        lines.append(f"kubernetes: {report.kube}")
    elif report.applications is None:
        lines.append("argocd: applications not available")
    elif not report.applications:
        lines.append("argocd: no applications")
    else:
        summary = ", ".join(f"{count} {state}" for state, count in sorted(report.applications.items()))
        lines.append(f"argocd: {summary}")
    return "\n".join(lines)
