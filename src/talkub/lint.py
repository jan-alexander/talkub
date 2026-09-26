"""`talkub lint`: style/syntax checks for a cluster repo, key-free.

Task 1 covers yamllint; Task 2 adds kubeconform on gitops manifests. Helm rendering is added by a
later task and calls into the same `LintReport`.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from yamllint import linter
from yamllint.config import YamlLintConfig

from talkub.context import Context
from talkub.repo import Repo

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


def kubeconform_argv(version: str, *, offline: bool) -> list[str]:
    """argv for `kubeconform -output json ...` (without the input files).

    kubeconform has no offline flag; `offline` doesn't change the argv — whether the check runs
    at all when offline (an empty schema cache skips it entirely) is decided by `lint_manifests`.
    """
    del offline
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


def _resources(stdout: str) -> list[dict]:
    if not stdout.strip():
        return []
    return json.loads(stdout).get("resources", [])


def _is_core_group(version: str) -> bool:
    """True for no group ("v1") or a Kubernetes-owned group ("apps", "...k8s.io"); False for a CRD group."""
    group = version.split("/", 1)[0] if "/" in version else ""
    return not group or "." not in group or group.endswith(".k8s.io")


def _needs_master_fallback(stdout: str) -> bool:
    """True if a core/Kubernetes-owned resource was skipped or errored: the requested version's
    schemas are probably missing entirely, rather than this being a genuine unknown-CRD skip."""
    return any(
        res.get("status") in ("statusSkipped", "statusError") and _is_core_group(res.get("version", ""))
        for res in _resources(stdout)
    )


def parse_kubeconform(stdout: str, prefix: str | None = None) -> tuple[list[Finding], list[str]]:
    """Parse `kubeconform -output json -verbose` output into findings and skipped reasons.

    `prefix` is prepended to the path (e.g. an app name like ``"coredns: "`` for Helm output);
    plain manifest linting passes `None` since `filename` is already repo-relative.
    """
    findings: list[Finding] = []
    skipped: list[str] = []
    for res in _resources(stdout):
        ident = f"{res.get('kind')}/{res.get('name')}"
        status = res.get("status")
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


def _cache_populated(cache: Path) -> bool:
    return cache.is_dir() and any(cache.rglob("*"))


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
    result = ctx.runner.run(kubeconform_argv(version, offline=offline) + rel_files, cwd=repo.root, check=False)
    stdout = result.stdout.decode()

    if _needs_master_fallback(stdout):
        result = ctx.runner.run(kubeconform_argv("master", offline=offline) + rel_files, cwd=repo.root, check=False)
        stdout = result.stdout.decode()
        findings, skipped = parse_kubeconform(stdout)
        report.findings.extend(findings)
        report.skipped.extend(skipped)
        report.findings.append(
            Finding(
                "warning", "kubeconform",
                f"kubernetes schemas for {version} not available; validated against master",
            )
        )
        return

    findings, skipped = parse_kubeconform(stdout)
    report.findings.extend(findings)
    report.skipped.extend(skipped)


def run(ctx: Context, *, offline: bool) -> LintReport:
    report = LintReport()
    lint_yaml(ctx.repo, report)
    lint_manifests(ctx, report, offline=offline)
    return report
