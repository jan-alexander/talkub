"""Shared parser for Argo CD Application files under `gitops/apps/*.yaml`.

`helm_sources` scans every app file for Helm chart sources (used by `talkub lint` to render and
validate them). `app_source` and `set_target_revision` look at a single, named app file and assume
it has exactly one Helm chart source (used by bootstrap/upgrade code that needs *the* chart of a
given app, not just "every chart source in the repo").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.findings import Finding
from talkub.repo import Repo

_VALUES_PREFIX = "$values/"

_NO_CHART_SOURCE_HINT = "add exactly one Helm chart source under spec.source or spec.sources"


def _as_mapping(value: object) -> Mapping:
    """`value` if it's a mapping, else `{}` — Application YAML is untrusted repo content, and a
    `spec`/`destination`/`helm`/`metadata` that turns out to be a string or a list must be skipped,
    not crash with an AttributeError."""
    return value if isinstance(value, Mapping) else {}


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


def _chart_sources(spec: Mapping) -> list[Mapping]:
    """Every entry of `spec.sources` (or the single `spec.source`) that has a `chart` key."""
    raw_sources = spec.get("sources")
    if raw_sources is None:
        single = spec.get("source")
        raw_sources = [single] if single else []
    if not isinstance(raw_sources, list):
        raw_sources = []
    return [source for source in raw_sources if isinstance(source, Mapping) and "chart" in source]


def _build_helm_source(repo: Repo, name: str, namespace: str, source: Mapping) -> tuple[HelmSource, list[Finding]]:
    """A `HelmSource` for `source` (a single `chart` entry), plus findings for its value files:
    an error if a `$values/`-prefixed entry doesn't resolve to a repo file, a warning (skipped,
    not fatal) for any other, unsupported entry."""
    findings: list[Finding] = []
    helm = _as_mapping(source.get("helm"))
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
                findings.append(Finding("error", f"app {name}", f"values file {repo.rel(candidate)} not found"))
        else:
            findings.append(Finding("warning", f"app {name}", f"valueFiles entry {entry!r} is unsupported, skipped"))

    inline_values: str | None = None
    if "valuesObject" in helm:
        inline_values = yamlio.dump(helm["valuesObject"])
    elif "values" in helm:
        inline_values = helm["values"]

    helm_source = HelmSource(
        app=name,
        release=release,
        namespace=namespace,
        repo=source.get("repoURL", ""),
        chart=source["chart"],
        version=source.get("targetRevision", ""),
        value_files=tuple(value_files),
        inline_values=inline_values,
    )
    return helm_source, findings


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
        if path.name.endswith(".sops.yaml"):
            continue
        try:
            docs = yamlio.load_all(path.read_text())
        except YAMLError:
            continue  # yamllint already reports the syntax error as a finding
        for doc in docs:
            if not isinstance(doc, Mapping) or doc.get("kind") != "Application":
                continue
            metadata = _as_mapping(doc.get("metadata"))
            name = str(metadata.get("name", path.stem))
            spec = _as_mapping(doc.get("spec"))
            destination = _as_mapping(spec.get("destination"))
            namespace = destination.get("namespace") or "default"

            for source in _chart_sources(spec):
                helm_source, source_findings = _build_helm_source(repo, name, namespace, source)
                sources.append(helm_source)
                findings.extend(source_findings)
    return sources, findings


def _locate_single_chart_source(repo: Repo, key: str) -> tuple[list, Mapping]:
    """The parsed documents of `gitops/apps/<key>.yaml` plus the raw (ruamel) mapping of its sole
    Helm chart source, for callers that need to look at (or mutate) that one source directly.

    Raises `ValidationError` unless the file exists, is valid YAML, has exactly one `Application`
    document, and that Application has exactly one `chart` source.
    """
    path = repo.gitops_dir / "apps" / f"{key}.yaml"
    error = ValidationError(f"gitops/apps/{key}.yaml has no Helm chart source", hint=_NO_CHART_SOURCE_HINT)
    if not path.is_file():
        raise error
    try:
        docs = yamlio.load_all(path.read_text())
    except YAMLError:
        raise error from None

    applications = [doc for doc in docs if isinstance(doc, Mapping) and doc.get("kind") == "Application"]
    if len(applications) != 1:
        raise error
    spec = _as_mapping(applications[0].get("spec"))
    chart_sources = _chart_sources(spec)
    if len(chart_sources) != 1:
        raise error
    return docs, chart_sources[0]


def app_source(repo: Repo, key: str) -> HelmSource:
    """The single `HelmSource` of `gitops/apps/<key>.yaml`'s single Argo CD Application.

    Unlike `helm_sources`, a missing value file is a `ValidationError`, not a Finding, since there
    is no report to attach it to and no other source to fall back on.
    """
    docs, source = _locate_single_chart_source(repo, key)
    application = next(doc for doc in docs if isinstance(doc, Mapping) and doc.get("kind") == "Application")
    metadata = _as_mapping(application.get("metadata"))
    name = str(metadata.get("name", key))
    spec = _as_mapping(application.get("spec"))
    destination = _as_mapping(spec.get("destination"))
    namespace = destination.get("namespace") or "default"

    helm_source, findings = _build_helm_source(repo, name, namespace, source)
    errors = [finding for finding in findings if finding.level == "error"]
    if errors:
        raise ValidationError(errors[0].message)
    return helm_source


def set_target_revision(repo: Repo, app: str, version: str) -> bool:
    """Set the `targetRevision` of `gitops/apps/<app>.yaml`'s single Helm chart source to `version`,
    round-tripping the YAML (comments and the file's other content, including any other source's
    own `targetRevision`, are left untouched). Writes only if the version actually changed; returns
    whether it did.
    """
    docs, source = _locate_single_chart_source(repo, app)
    if str(source.get("targetRevision", "")) == version:
        return False
    source["targetRevision"] = version
    path = repo.gitops_dir / "apps" / f"{app}.yaml"
    text = yamlio.dump(docs[0]) if len(docs) == 1 else yamlio.dump_all(docs)
    path.write_text(text)
    return True
