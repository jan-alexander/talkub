"""talkub versions: target vs. running vs. latest for Talos, Kubernetes and every chart app.

`collect` never raises for a network/cluster failure — each row falls back to "?" (or, with
`offline=True`, "-" for LATEST everywhere) so one broken component does not hide the rest.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from talkub import argoapps, charts
from talkub.argoapps import HelmSource
from talkub.config.repo import Repo, Settings
from talkub.context import Context
from talkub.core.errors import TalkubError, ValidationError
from talkub.kube import kube_session
from talkub.lifecycle import ordered_nodes
from talkub.talos import inspect
from talkub.talos.client import talos_session

GITHUB_API = "https://api.github.com"
K8S_RELEASE_URL = "https://dl.k8s.io/release/stable.txt"
KUBECTL_TIMEOUT = "--request-timeout=10s"

_TALOS_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_K8S_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Row:
    component: str
    target: str
    running: str
    latest: str
    note: str = ""


def _github_api() -> str:
    return os.environ.get("TALKUB_GITHUB_API", GITHUB_API).rstrip("/")


def _k8s_release_url() -> str:
    return os.environ.get("TALKUB_K8S_RELEASE_URL", K8S_RELEASE_URL)


def _get_text(url: str) -> str | None:
    """`GET url`'s body, or `None` on any network/HTTP error."""
    request = urllib.request.Request(url, headers={"User-Agent": "talkub"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _talos_latest() -> str | None:
    text = _get_text(f"{_github_api()}/repos/siderolabs/talos/releases/latest")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) else None


def _kubernetes_latest() -> str | None:
    text = _get_text(_k8s_release_url())
    if text is None:
        return None
    stripped = text.strip().removeprefix("v")
    return stripped or None


def _version_tuple(value: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _talos_running(ctx: Context, settings: Settings) -> str:
    """Distinct `talosctl version` server tags across every node, comma-joined; "?" on any
    error (undecryptable talosconfig, unreachable node, unparseable output)."""
    try:
        order = ordered_nodes(settings, [])
        with talos_session(ctx) as session:
            versions: list[str] = []
            for name in order:
                result = session.run(
                    settings.mgmt_address(name), ["version"], check=False
                )
                if result.returncode != 0:
                    return "?"
                version = inspect.server_version(result.stdout.decode())
                if version is None:
                    return "?"
                if version not in versions:
                    versions.append(version)
            return ", ".join(versions) if versions else "?"
    except TalkubError:
        return "?"


def _kubernetes_running(ctx: Context) -> str:
    """Distinct kubelet versions across every node, comma-joined; "?" on any error."""
    try:
        with kube_session(ctx) as kube:
            result = kube.kubectl(
                ["get", "nodes", "-o", "json", KUBECTL_TIMEOUT], check=False
            )
    except TalkubError:
        return "?"
    if result.returncode != 0:
        return "?"
    versions = inspect.kubelet_versions(result.stdout.decode())
    if not versions:
        return "?"
    distinct: list[str] = []
    for version in versions.values():
        if version not in distinct:
            distinct.append(version)
    return ", ".join(distinct)


def _talos_row(ctx: Context, settings: Settings, *, offline: bool) -> Row:
    running = _talos_running(ctx, settings)
    latest = "-" if offline else (_talos_latest() or "?")
    return Row("talos", settings.talos_version, running, latest)


def _kubernetes_row(ctx: Context, settings: Settings, *, offline: bool) -> Row:
    running = _kubernetes_running(ctx)
    latest = "-" if offline else (_kubernetes_latest() or "?")
    note = ""
    if latest not in ("?", "-"):
        target_v, latest_v = (
            _version_tuple(settings.kubernetes_version),
            _version_tuple(latest),
        )
        if target_v is not None and latest_v is not None and latest_v > target_v:
            note = "check the Talos support matrix before upgrading"
    return Row("kubernetes", settings.kubernetes_version, running, latest, note)


def _chart_running(ctx: Context, source: HelmSource, source_index: int | None) -> str:
    try:
        with kube_session(ctx) as kube:
            result = kube.kubectl(
                [
                    "-n",
                    "argocd",
                    "get",
                    "application",
                    source.app,
                    "-o",
                    "json",
                    KUBECTL_TIMEOUT,
                ],
                check=False,
            )
    except TalkubError:
        return "?"
    if result.returncode != 0:
        return "?"
    revision = inspect.application_sync_revision(
        result.stdout.decode(), source_index=source_index
    )
    return revision or "?"


def _app_version_note(ctx: Context, source: HelmSource, latest: str) -> str:
    """`appVersion a -> b`, via `helm show chart` of the target and latest chart version;
    empty if either lookup fails or the appVersion did not change."""
    try:
        old = charts.show_chart(
            ctx.runner, source.repo, source.chart, source.version
        ).get("appVersion")
        new = charts.show_chart(ctx.runner, source.repo, source.chart, latest).get(
            "appVersion"
        )
    except TalkubError:
        return ""
    if isinstance(old, str) and isinstance(new, str) and old != new:
        return f"appVersion {old} → {new}"
    return ""


def _chart_row(
    ctx: Context,
    component: str,
    source: HelmSource,
    source_index: int | None,
    *,
    offline: bool,
) -> Row:
    """One chart app's row. `component` is the `gitops/apps/*.yaml` file stem (see `_chart_rows`);
    `source.app` — the Application's `metadata.name`, which may differ — is still what identifies
    the actual Argo CD Application resource to `_chart_running`."""
    running = _chart_running(ctx, source, source_index)
    target = (
        source.version or "?"
    )  # an invalid/empty targetRevision must not crash `versions`
    if offline:
        return Row(component, target, running, "-")
    try:
        latest = charts.latest_version(ctx.runner, source.repo, source.chart)
    except TalkubError:
        return Row(component, target, running, "?")
    note = _app_version_note(ctx, source, latest) if latest != source.version else ""
    return Row(component, target, running, latest, note)


def _chart_rows(ctx: Context, *, offline: bool) -> list[Row]:
    """One row per `gitops/apps/*.yaml` file stem with a Helm chart source: a normal row for a
    single-chart file, a "not a single-chart app" note for one with more than one (or a
    malformed/multi-Application file) — never one row per `metadata.name`, which could differ
    from the file stem `chart upgrade` and `--all` actually address."""
    rows: list[Row] = []
    for stem in argoapps.chart_app_stems(ctx.repo):
        if not argoapps.is_single_chart_app(ctx.repo, stem):
            rows.append(Row(stem, "?", "?", "?", "not a single-chart app"))
            continue
        sources, _findings = argoapps.app_chart_sources(ctx.repo, stem)
        source = sources[0]
        indices = argoapps.chart_source_indices(ctx.repo, stem)
        source_index = indices[0] if indices else None
        rows.append(_chart_row(ctx, stem, source, source_index, offline=offline))
    return rows


def collect(ctx: Context, *, offline: bool = False) -> list[Row]:
    """Talos, Kubernetes and every chart app (`gitops/apps/*.yaml` with a Helm chart source)."""
    settings = ctx.repo.load_settings()
    rows = [
        _talos_row(ctx, settings, offline=offline),
        _kubernetes_row(ctx, settings, offline=offline),
    ]
    rows.extend(_chart_rows(ctx, offline=offline))
    return rows


def render(rows: list[Row]) -> str:
    header = ("COMPONENT", "TARGET", "RUNNING", "LATEST", "NOTE")
    body = [
        (row.component, row.target, row.running, row.latest, row.note) for row in rows
    ]
    table = [header, *body]
    widths = [max(len(entry[i]) for entry in table) for i in range(len(header))]
    lines = []
    for index, entry in enumerate(table):
        marker = " "
        if index > 0:
            row = rows[index - 1]
            if row.latest not in ("?", "-") and row.latest != row.target:
                marker = "*"
        line = (
            marker
            + " "
            + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(entry))
        )
        lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def set_version(repo: Repo, component: str, version: str) -> str:
    """Validate and store a new target Talos/Kubernetes version in `.talkub.yaml`; returns a
    follow-up hint for the caller to print."""
    settings = repo.load_settings()
    if component == "talos":
        if not _TALOS_VERSION_RE.match(version):
            raise ValidationError(
                f"not a Talos version: {version!r}",
                hint="expected vMAJOR.MINOR.PATCH, e.g. v1.14.0",
            )
        settings.talos_version = version
        hint = "run `talkub upgrade talos`"
    elif component == "kubernetes":
        if not _K8S_VERSION_RE.match(version):
            raise ValidationError(
                f"not a Kubernetes version: {version!r}",
                hint="expected MAJOR.MINOR.PATCH, e.g. 1.37.0",
            )
        settings.kubernetes_version = version.removeprefix("v")
        hint = "run `talkub upgrade k8s`"
    else:
        raise ValidationError(
            f"unknown component: {component!r}", hint="use one of: talos, kubernetes"
        )
    repo.write_settings(settings)
    return hint
