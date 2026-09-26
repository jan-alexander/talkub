"""`talkub lint`: style/syntax checks for a cluster repo, key-free.

Task 1 covers yamllint only; kubeconform and Helm rendering are added by later tasks
and call into the same `LintReport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from yamllint import linter
from yamllint.config import YamlLintConfig

from talkub.context import Context
from talkub.repo import Repo

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


def run(ctx: Context, *, offline: bool) -> LintReport:
    report = LintReport()
    lint_yaml(ctx.repo, report)
    return report
