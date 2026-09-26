"""`talkub lint`: style/syntax checks for a cluster repo, key-free.

Task 1 covers yamllint; Task 2 adds kubeconform on gitops manifests; Task 3 renders each Argo CD
Application's Helm source with `helm template` and validates the result with kubeconform too.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from yamllint import linter
from yamllint.config import YamlLintConfig

from talkub import bootstrap, yamlio
from talkub.context import Context
from talkub.ephemeral import private_tempdir
from talkub.repo import ChartSpec, Repo

# CRD schemas not covered by the default (Kubernetes-owned) schema location.
DATREE_LOCATION = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

DEFAULT_YAMLLINT = """\
# Managed by talkub. yamllint configuration for this cluster repo.
extends: relaxed
ignore: |
  *.sops.yaml
rules:
  line-length: disable
"""


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    path: str  # repo-relative, or an app name prefix like "app coredns"
    message: str
    line: int | None = None


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable reasons

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "warning")

    def render(self) -> list[str]:
        lines = []
        for f in self.findings:
            where = f"{f.path}:{f.line}" if f.line is not None else f.path
            lines.append(f"{where}: {f.level}: {f.message}")
        lines.extend(f"skipped: {reason}" for reason in self.skipped)
        lines.append(f"lint: {self.errors} errors, {self.warnings} warnings, {len(self.skipped)} skipped")
        return lines


def yaml_files(repo: Repo) -> list[Path]:
    """All *.yaml/*.yml under the repo root, excluding .git/, sorted."""
    return sorted(
        path
        for path in repo.root.rglob("*")
        if path.is_file()
        and path.suffix in (".yaml", ".yml")
        and ".git" not in path.relative_to(repo.root).parts
    )


def _yamllint_config(repo: Repo) -> YamlLintConfig:
    local = repo.root / ".yamllint"
    if local.is_file():
        return YamlLintConfig(content=local.read_text())
    return YamlLintConfig(content=DEFAULT_YAMLLINT)


def lint_yaml(repo: Repo, report: LintReport) -> None:
    conf = _yamllint_config(repo)
    for path in yaml_files(repo):
        relpath = repo.rel(path)
        if conf.is_file_ignored(relpath):
            continue
        for problem in linter.run(path.read_text(), conf, relpath):
            report.findings.append(Finding(problem.level, relpath, problem.message, problem.line))


def cache_dir() -> Path:
    """`$XDG_CACHE_HOME/talkub/kubeconform`, defaulting to `~/.cache/talkub/kubeconform`."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "talkub" / "kubeconform"


def manifest_files(repo: Repo) -> list[Path]:
    """`gitops/apps/**` and `gitops/manifests/**` `*.y*ml`, excluding `*.sops.yaml`."""
    files: set[Path] = set()
    for sub in ("apps", "manifests"):
        base = repo.gitops_dir / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*.y*ml"):
            if path.is_file() and not path.name.endswith(".sops.yaml"):
                files.add(path)
    return sorted(files)


def kubeconform_argv(version: str) -> list[str]:
    """argv for `kubeconform -output json ...` (without the input files).

    Whether the check runs at all when offline (an empty schema cache skips it entirely) is
    decided by `lint_manifests`; kubeconform itself has no offline flag.
    """
    return [
        "kubeconform",
        "-output", "json",
        "-summary",
        "-verbose",  # so skipped resources are listed too, not just invalid/error ones
        "-strict",
        "-ignore-missing-schemas",
        "-kubernetes-version", version,
        "-cache", str(cache_dir()),
        "-schema-location", "default",
        "-schema-location", DATREE_LOCATION,
    ]


_DOWNLOAD_ERROR_PREFIX = "failed downloading schema"


def _try_resources(stdout: str) -> list[dict] | None:
    """Parsed `resources` list, or `None` if `stdout` isn't the JSON kubeconform normally emits."""
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data.get("resources", [])


def _is_core_group(version: str) -> bool:
    """True for no group ("v1") or a Kubernetes-owned group ("apps", "...k8s.io"); False for a CRD group."""
    group = version.split("/", 1)[0] if "/" in version else ""
    return not group or "." not in group or group.endswith(".k8s.io")


def _is_download_error(res: dict) -> bool:
    """True for a schema-fetch failure (e.g. an unreachable host); unrelated to the k8s version."""
    return str(res.get("msg", "")).startswith(_DOWNLOAD_ERROR_PREFIX)


def _resource_key(res: dict) -> tuple:
    return (res.get("filename"), res.get("kind"), res.get("name"))


def _needs_master_fallback(stdout: str) -> bool:
    """True if a core/Kubernetes-owned resource was skipped, or errored for a reason other than a
    download failure: the requested version's schemas are probably missing entirely, rather than
    this being a genuine unknown-CRD skip or an unrelated network problem."""
    resources = _try_resources(stdout)
    if resources is None:
        return False
    return any(
        _is_core_group(res.get("version", ""))
        and (res.get("status") == "statusSkipped" or (res.get("status") == "statusError" and not _is_download_error(res)))
        for res in resources
    )


def _parse_resources(
    resources: list[dict], prefix: str | None, *, offline: bool
) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    skipped: list[str] = []
    for res in resources:
        ident = f"{res.get('kind')}/{res.get('name')}"
        status = res.get("status")
        if status == "statusError" and offline and _is_download_error(res):
            skipped.append(f"schema not cached for {ident} (offline)")
            continue
        if status in ("statusInvalid", "statusError"):
            path = f"{prefix}{res.get('filename', '')}" if prefix else res.get("filename", "")
            errors = res.get("validationErrors") or []
            if errors:
                for err in errors:
                    findings.append(Finding("error", path, f"{ident}: {err['path']}: {err['msg']}"))
            else:
                findings.append(Finding("error", path, f"{ident}: {res.get('msg', '')}"))
        elif status == "statusSkipped":
            skipped.append(f"no schema for {ident} ({res.get('version', '')})")
    return findings, skipped


def parse_kubeconform(
    stdout: str, prefix: str | None = None, *, offline: bool = False
) -> tuple[list[Finding], list[str]]:
    """Parse `kubeconform -output json -verbose` output into findings and skipped reasons.

    `prefix` is prepended to the path (e.g. an app name like ``"coredns: "`` for Helm output);
    plain manifest linting passes `None` since `filename` is already repo-relative. When `offline`,
    a `statusError` caused by a schema download failure becomes a skipped reason rather than an
    error, since we expect the network to be unavailable; other errors (e.g. a malformed resource)
    still count as errors.
    """
    resources = _try_resources(stdout)
    if resources is None:
        first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
        return [Finding("error", "kubeconform", f"kubeconform produced unexpected output: {first_line}")], []
    return _parse_resources(resources, prefix, offline=offline)


def _cache_populated(cache: Path) -> bool:
    return cache.is_dir() and any(cache.rglob("*"))


def _kubeconform_with_fallback(
    ctx: Context,
    version: str,
    args: list[str],
    *,
    offline: bool,
    input: bytes | None = None,
    prefix: str | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run kubeconform over `args` (repo-relative files, or `["-"]` with `input` on stdin),
    retrying with `-kubernetes-version master` if `version`'s own schemas seem to be missing
    entirely. Shared by manifest linting (Task 2) and Helm-rendered app linting (Task 3)."""
    repo = ctx.repo
    result = ctx.runner.run(kubeconform_argv(version) + args, cwd=repo.root, check=False, input=input)
    stdout = result.stdout.decode()

    if _needs_master_fallback(stdout):
        # A CRD's validity doesn't depend on -kubernetes-version: keep those findings from this
        # run rather than losing them when we discard the rest for the master rerun below.
        carried = [
            res
            for res in (_try_resources(stdout) or [])
            if res.get("status") == "statusInvalid" and not _is_core_group(res.get("version", ""))
        ]
        result = ctx.runner.run(kubeconform_argv("master") + args, cwd=repo.root, check=False, input=input)
        stdout = result.stdout.decode()
        master_resources = _try_resources(stdout) or []
        seen = {_resource_key(res) for res in master_resources}
        merged = master_resources + [res for res in carried if _resource_key(res) not in seen]
        findings, skipped = _parse_resources(merged, prefix, offline=offline)
        findings.append(
            Finding(
                "warning", "kubeconform",
                f"kubernetes schemas for {version} not available; validated against master",
            )
        )
        return findings, skipped

    return parse_kubeconform(stdout, prefix, offline=offline)


def lint_manifests(ctx: Context, report: LintReport, *, offline: bool) -> None:
    repo = ctx.repo
    files = manifest_files(repo)
    if not files:
        return
    if shutil.which("kubeconform") is None:
        report.skipped.append("kubeconform not found (brew install kubeconform)")
        return

    cache = cache_dir()
    if offline and not _cache_populated(cache):
        report.skipped.append("kubeconform: no schema cache yet (run `talkub lint` online once)")
        return
    cache.mkdir(parents=True, exist_ok=True)

    version = repo.load_settings().kubernetes_version.lstrip("v")
    rel_files = [repo.rel(path) for path in files]
    findings, skipped = _kubeconform_with_fallback(ctx, version, rel_files, offline=offline)
    report.findings.extend(findings)
    report.skipped.extend(skipped)


_VALUES_PREFIX = "$values/"


@dataclass(frozen=True)
class HelmSource:
    app: str
    release: str
    namespace: str
    repo: str
    chart: str
    version: str
    value_files: tuple[Path, ...] = ()
    inline_values: str | None = None


def helm_sources(repo: Repo) -> tuple[list[HelmSource], list[Finding]]:
    """Every Helm source (a `chart` entry) of every Argo CD Application under `gitops/apps/*.yaml`.

    Non-Application documents and sources without `chart` (git-values sources, plain directory
    apps) are skipped. `helm.valueFiles` entries prefixed `$values/` resolve to a repo-local path
    (an error finding if missing); other entries are unsupported (a warning finding, skipped)."""
    sources: list[HelmSource] = []
    findings: list[Finding] = []
    apps_dir = repo.gitops_dir / "apps"
    if not apps_dir.is_dir():
        return sources, findings

    for path in sorted(apps_dir.glob("*.yaml")):
        for doc in yamlio.load_all(path.read_text()):
            if not isinstance(doc, Mapping) or doc.get("kind") != "Application":
                continue
            name = str((doc.get("metadata") or {}).get("name", path.stem))
            spec = doc.get("spec") or {}
            namespace = ((spec.get("destination") or {}).get("namespace")) or "default"

            raw_sources = spec.get("sources")
            if raw_sources is None:
                single = spec.get("source")
                raw_sources = [single] if single else []

            for source in raw_sources:
                if not isinstance(source, Mapping) or "chart" not in source:
                    continue
                helm = source.get("helm") or {}
                release = helm.get("releaseName") or name

                value_files: list[Path] = []
                for entry in helm.get("valueFiles") or []:
                    if not isinstance(entry, str):
                        continue
                    if entry.startswith(_VALUES_PREFIX):
                        candidate = repo.root / entry[len(_VALUES_PREFIX):]
                        if candidate.is_file():
                            value_files.append(candidate)
                        else:
                            findings.append(
                                Finding("error", f"app {name}", f"values file {repo.rel(candidate)} not found")
                            )
                    else:
                        findings.append(
                            Finding("warning", f"app {name}", f"valueFiles entry {entry!r} is unsupported, skipped")
                        )

                inline_values: str | None = None
                if "valuesObject" in helm:
                    inline_values = yamlio.dump(helm["valuesObject"])
                elif "values" in helm:
                    inline_values = helm["values"]

                sources.append(
                    HelmSource(
                        app=name,
                        release=release,
                        namespace=namespace,
                        repo=source.get("repoURL", ""),
                        chart=source["chart"],
                        version=source.get("targetRevision", ""),
                        value_files=tuple(value_files),
                        inline_values=inline_values,
                    )
                )
    return sources, findings


def _render_helm(ctx: Context, source: HelmSource, tmp: Path, index: int, report: LintReport) -> bytes | None:
    """`helm template` one `HelmSource`; `None` (plus a Finding) on failure."""
    chart = ChartSpec(repo=source.repo, chart=source.chart, version=source.version)
    argv = [
        "helm", "template", source.release, *bootstrap.chart_ref(chart),
        "--version", source.version, "-n", source.namespace,
    ]
    for value_file in source.value_files:
        argv += ["-f", str(value_file)]
    if source.inline_values is not None:
        inline_path = tmp / f"{index}-values.yaml"
        inline_path.write_text(source.inline_values)
        argv += ["-f", str(inline_path)]

    result = ctx.runner.run(argv, check=False)
    if result.returncode != 0:
        stderr_lines = result.stderr.decode(errors="replace").splitlines()[:5]
        report.findings.append(
            Finding("error", f"app {source.app}", "helm template failed: " + "; ".join(stderr_lines))
        )
        return None
    return result.stdout


def lint_helm_apps(ctx: Context, report: LintReport, *, offline: bool) -> None:
    if offline:
        report.skipped.append("helm apps: skipped (--offline)")
        return

    repo = ctx.repo
    sources, findings = helm_sources(repo)
    report.findings.extend(findings)
    if not sources:
        return
    if shutil.which("helm") is None:
        report.skipped.append("helm not found (brew install helm)")
        return

    # kubeconform missing is already reported as a skip reason by lint_manifests; don't duplicate it.
    kubeconform_available = shutil.which("kubeconform") is not None
    version = repo.load_settings().kubernetes_version.lstrip("v")
    if kubeconform_available:
        cache_dir().mkdir(parents=True, exist_ok=True)

    with private_tempdir() as tmp:
        for index, source in enumerate(sources):
            manifest = _render_helm(ctx, source, tmp, index, report)
            if manifest is None or not kubeconform_available:
                continue
            findings, skipped = _kubeconform_with_fallback(
                ctx, version, ["-"], offline=False, input=manifest, prefix=f"app {source.app}: "
            )
            report.findings.extend(findings)
            report.skipped.extend(skipped)


def run(ctx: Context, *, offline: bool) -> LintReport:
    report = LintReport()
    lint_yaml(ctx.repo, report)
    lint_manifests(ctx, report, offline=offline)
    lint_helm_apps(ctx, report, offline=offline)
    return report
