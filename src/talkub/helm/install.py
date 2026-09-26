"""`helm` command lines: naming a chart and installing an Argo CD app's chart source directly."""

from __future__ import annotations

from contextlib import ExitStack

from talkub.argoapps import HelmSource
from talkub.ephemeral import private_file
from talkub.kube import KubeSession
from talkub.repo import ChartSpec


def chart_ref(chart: ChartSpec) -> list[str]:
    """helm arguments naming the chart: an OCI reference, or chart name plus --repo."""
    if chart.repo.startswith("oci://"):
        return [f"{chart.repo.rstrip('/')}/{chart.chart}"]
    return [chart.chart, "--repo", chart.repo]


def helm_install(
    kube: KubeSession, source: HelmSource, *, create_namespace: bool
) -> None:
    """`helm upgrade --install` one Argo CD app's chart source as Argo CD would render it."""
    chart = ChartSpec(repo=source.repo, chart=source.chart, version=source.version)
    argv = [
        "upgrade",
        "--install",
        source.release,
        *chart_ref(chart),
        "--version",
        source.version,
        "-n",
        source.namespace,
    ]
    for value_file in source.value_files:
        argv += ["-f", str(value_file)]
    with ExitStack() as stack:
        if source.inline_values is not None:
            inline = stack.enter_context(
                private_file("values.yaml", source.inline_values.encode())
            )
            argv += ["-f", str(inline)]
        argv += ["--wait", "--timeout", "10m"]
        if create_namespace:
            argv.append("--create-namespace")
        kube.helm(argv)
