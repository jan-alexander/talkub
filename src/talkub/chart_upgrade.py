"""`talkub chart upgrade`: bump one Argo CD app's Helm chart (or every chart app) to a new version.

Step order per app: resolve the target version, show `chart: old -> new` / `appVersion` / a
unified diff of the chart's default values, ask for confirmation, round-trip `targetRevision`,
then lint just that app. A lint failure leaves the file changed (the round-trip already happened)
so the user can inspect or fix it; it is reported as a failure, not undone.
"""

from __future__ import annotations

import difflib
import shutil
from collections.abc import Callable

from talkub import argoapps, bootstrap, charts
from talkub.context import Context
from talkub.errors import TalkubError, ValidationError
from talkub.lint import LintReport, lint_helm_app
from talkub.repo import ChartSpec
from talkub.runner import require_tool

Confirm = Callable[[str], bool]

MAX_DIFF_LINES = 200

# Apps that roll out their own upgrade; everything else is rolled out by the running Argo CD.
SELF_MANAGED = {"argocd"}


def chart_apps(ctx: Context) -> list[str]:
    """Every app under `gitops/apps/*.yaml` with a Helm chart source, in file order (for `--all`)."""
    sources, _findings = argoapps.helm_sources(ctx.repo)
    apps: list[str] = []
    for source in sources:
        if source.app not in apps:
            apps.append(source.app)
    return apps


def _show_values(ctx: Context, chart: ChartSpec) -> str:
    argv = ["helm", "show", "values", *bootstrap.chart_ref(chart), "--version", chart.version]
    return ctx.runner.run(argv).stdout.decode()


def _diff(old: str, new: str, *, old_label: str, new_label: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), old_label, new_label))
    if not lines:
        return ""
    if len(lines) > MAX_DIFF_LINES:
        omitted = len(lines) - MAX_DIFF_LINES
        lines = [*lines[:MAX_DIFF_LINES], f"... ({omitted} more lines)\n"]
    return "".join(lines)


def _upgrade_one(ctx: Context, app: str, version: str | None, confirm: Confirm) -> bool:
    """Upgrade one app's chart. True on success (including "already at X"), False if the user
    declined or the post-upgrade lint found an error."""
    source = argoapps.app_source(ctx.repo, app)
    target = version or charts.latest_version(ctx.runner, source.repo, source.chart)
    if target == source.version:
        print(f"{app}: already at {target}")
        return True

    old_chart = ChartSpec(source.repo, source.chart, source.version)
    new_chart = ChartSpec(source.repo, source.chart, target)
    old_doc = charts.show_chart(ctx.runner, old_chart.repo, old_chart.chart, old_chart.version)
    new_doc = charts.show_chart(ctx.runner, new_chart.repo, new_chart.chart, new_chart.version)

    print(f"{app}: chart: {source.version} → {target}")
    old_app_version, new_app_version = old_doc.get("appVersion"), new_doc.get("appVersion")
    if isinstance(old_app_version, str) and isinstance(new_app_version, str):
        print(f"{app}: appVersion: {old_app_version} → {new_app_version}")

    diff = _diff(
        _show_values(ctx, old_chart), _show_values(ctx, new_chart),
        old_label=f"{source.chart}-{source.version}/values.yaml", new_label=f"{source.chart}-{target}/values.yaml",
    )
    if diff:
        print(diff, end="")

    if not confirm(f"Upgrade {app} to {target}?"):
        print(f"{app}: aborted; nothing was changed")
        return False

    # helm renders the app for lint right after; check it's there *before* writing anything, so a
    # missing helm is a clean failure (with the standard brew hint) instead of a changed file that
    # `lint_helm_app` can only silently skip.
    require_tool("helm")
    argoapps.set_target_revision(ctx.repo, app, target)

    report = LintReport()
    lint_helm_app(ctx, report, argoapps.app_source(ctx.repo, app))
    if report.errors:
        for line in report.render():
            print(f"   {line}")
        print(f"{app}: lint found errors; fix the values or revert the change")
        return False

    # kubeconform missing (or any other skip lint_helm_app reported) means the chart rendered but
    # was never actually validated against the cluster's Kubernetes schemas; still a success, but
    # worth calling out rather than looking identical to a fully validated upgrade.
    if report.skipped or shutil.which("kubeconform") is None:
        for reason in report.skipped:
            print(f"{app}: {reason}")
        print(f"{app}: note: rendered with helm but not validated against Kubernetes schemas (kubeconform not found)")

    if app in SELF_MANAGED:
        print(f"{app}: commit and push — Argo CD upgrades itself")
    else:
        print(f"{app}: commit and push — Argo CD rolls it out")
    return True


def upgrade(ctx: Context, app: str | None, *, all_apps: bool, version: str | None, confirm: Confirm) -> int:
    """`talkub chart upgrade (<app> | --all) [--version X]`. Returns the process exit code."""
    if all_apps and app is not None:
        raise ValidationError("give an app or --all, not both")
    if not all_apps and app is None:
        raise ValidationError("give an app or --all")
    if all_apps and version is not None:
        raise ValidationError("--version cannot be combined with --all")

    apps = chart_apps(ctx) if all_apps else [app]
    failed: list[str] = []
    for name in apps:
        if not _upgrade_app(ctx, name, version, confirm, isolate=all_apps):
            failed.append(name)

    if all_apps:
        print(f"upgrade: {len(apps) - len(failed)}/{len(apps)} succeeded")
    return 1 if failed else 0


def _upgrade_app(ctx: Context, app: str, version: str | None, confirm: Confirm, *, isolate: bool) -> bool:
    """`_upgrade_one`, but with `--all`'s per-app isolation: a `TalkubError` (a broken app file, a
    failed `helm`/kubeconform call, ...) is reported like any other failure and does not abort the
    rest of `--all`. In single-app mode it keeps propagating, for a normal CLI error exit."""
    if not isolate:
        return _upgrade_one(ctx, app, version, confirm)
    try:
        return _upgrade_one(ctx, app, version, confirm)
    except TalkubError as err:
        print(f"{app}: {err}")
        if err.hint:
            print(f"{app}: hint: {err.hint}")
        return False
