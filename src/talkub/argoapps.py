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

from talkub.config import Repo
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.findings import Finding

# Label that marks a Secret as Argo CD configuration (repository, cluster, ...).
SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"

_VALUES_PREFIX = "$values/"

_NO_CHART_SOURCE_HINT = (
    "add exactly one Helm chart source under spec.source or spec.sources"
)


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


def _chart_sources(spec: Mapping) -> list[tuple[int | None, Mapping]]:
    """Every entry of `spec.sources` (or the single `spec.source`) that has a `chart` key, paired
    with its absolute position in `spec.sources` (`None` for the singular `spec.source`) — the
    single predicate for what counts as a chart source, and the only place that knows the index
    used to pick `status.sync.revisions[i]` for a multi-source app (see `chart_source_indices`)."""
    raw_sources = spec.get("sources")
    if raw_sources is None:
        single = spec.get("source")
        return (
            [(None, single)]
            if isinstance(single, Mapping) and "chart" in single
            else []
        )
    if not isinstance(raw_sources, list):
        return []
    return [
        (index, source)
        for index, source in enumerate(raw_sources)
        if isinstance(source, Mapping) and "chart" in source
    ]


def _scalar_str(source: Mapping, field: str, name: str, findings: list[Finding]) -> str:
    """`source[field]` normalised to `str`: a `str` stays; `None`/missing -> ""; any other scalar
    (e.g. a ruamel `ScalarFloat` from an unquoted YAML number) is a parsing mistake, not a valid
    value -> an error finding, "" used instead, so every `HelmSource` field stays a `str`."""
    value = source.get(field)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    findings.append(Finding("error", f"app {name}", f"{field} must be a quoted string"))
    return ""


def _target_revision_str(source: Mapping, name: str, findings: list[Finding]) -> str:
    """Like `_scalar_str`, but for `targetRevision` specifically: also an error finding (a
    different one) when the value, once a `str`, is empty — a chart source must always be pinned
    to a version, and `versions`/`app_source` need a plain `str` to tell "unpinned" from "pinned"
    without re-inspecting the raw YAML value."""
    value = source.get("targetRevision")
    if value is None:
        value = ""
    elif not isinstance(value, str):
        findings.append(
            Finding("error", f"app {name}", "targetRevision must be a quoted string")
        )
        return ""
    if not value:
        findings.append(Finding("error", f"app {name}", "targetRevision is empty"))
    return value


def _build_helm_source(
    repo: Repo, name: str, namespace: str, source: Mapping
) -> tuple[HelmSource, list[Finding]]:
    """A `HelmSource` for `source` (a single `chart` entry), plus findings for its value files:
    an error if a `$values/`-prefixed entry doesn't resolve to a repo file, a warning (skipped,
    not fatal) for any other, unsupported entry."""
    findings: list[Finding] = []
    helm = _as_mapping(source.get("helm"))
    release = _scalar_str(helm, "releaseName", name, findings) or name

    value_files: list[Path] = []
    for entry in helm.get("valueFiles") or []:
        if not isinstance(entry, str):
            continue
        if entry.startswith(_VALUES_PREFIX):
            candidate = repo.root / entry[len(_VALUES_PREFIX) :]
            if candidate.is_file():
                value_files.append(candidate)
            else:
                findings.append(
                    Finding(
                        "error",
                        f"app {name}",
                        f"values file {repo.rel(candidate)} not found",
                    )
                )
        else:
            findings.append(
                Finding(
                    "warning",
                    f"app {name}",
                    f"valueFiles entry {entry!r} is unsupported, skipped",
                )
            )

    inline_values: str | None = None
    if "valuesObject" in helm:
        inline_values = yamlio.dump(helm["valuesObject"])
    elif "values" in helm:
        inline_values = helm["values"]

    helm_source = HelmSource(
        app=name,
        release=release,
        namespace=namespace,
        repo=_scalar_str(source, "repoURL", name, findings),
        chart=_scalar_str(source, "chart", name, findings),
        version=_target_revision_str(source, name, findings),
        value_files=tuple(value_files),
        inline_values=inline_values,
    )
    return helm_source, findings


def _file_chart_sources(
    repo: Repo, path: Path
) -> tuple[list[HelmSource], list[Finding]]:
    """Every Helm source (a `chart` entry) of every Argo CD Application document in `path`, the
    per-file body shared by `helm_sources` (whole-repo scan, keyed by `metadata.name`) and
    `app_chart_sources` (one file, keyed by its stem). Empty, with no findings, if `path` doesn't
    exist or isn't valid YAML (a syntax error is reported elsewhere, by yamllint)."""
    sources: list[HelmSource] = []
    findings: list[Finding] = []
    try:
        docs = yamlio.load_all(path.read_text())
    except (OSError, YAMLError):
        return sources, findings
    for doc in docs:
        if not isinstance(doc, Mapping) or doc.get("kind") != "Application":
            continue
        metadata = _as_mapping(doc.get("metadata"))
        name = str(metadata.get("name", path.stem))
        spec = _as_mapping(doc.get("spec"))
        destination = _as_mapping(spec.get("destination"))
        namespace = destination.get("namespace") or "default"

        for _index, source in _chart_sources(spec):
            helm_source, source_findings = _build_helm_source(
                repo, name, namespace, source
            )
            sources.append(helm_source)
            findings.extend(source_findings)
    return sources, findings


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
        file_sources, file_findings = _file_chart_sources(repo, path)
        sources.extend(file_sources)
        findings.extend(file_findings)
    return sources, findings


def app_chart_sources(repo: Repo, app: str) -> tuple[list[HelmSource], list[Finding]]:
    """Like `helm_sources`, but scoped to `gitops/apps/<app>.yaml` alone and keyed by that file's
    stem rather than by whatever `metadata.name` its Application(s) declare — for callers
    (`versions`, `chart upgrade --all`) that address an app by file stem and must not conflate it
    with a differently-named `metadata.name` (see `chart_app_stems`, `is_single_chart_app`)."""
    return _file_chart_sources(repo, repo.gitops_dir / "apps" / f"{app}.yaml")


def chart_app_stems(repo: Repo) -> list[str]:
    """Every `gitops/apps/*.yaml` file stem that has at least one Helm chart source, in file
    order — whether or not it's a *single* one (see `is_single_chart_app`). A file with none at
    all (a plain directory app, a git-values-only app, ...) is not a "chart app" and is left out
    entirely, not just marked invalid."""
    apps_dir = repo.gitops_dir / "apps"
    if not apps_dir.is_dir():
        return []
    stems: list[str] = []
    for path in sorted(apps_dir.glob("*.yaml")):
        if path.name.endswith(".sops.yaml"):
            continue
        sources, _findings = _file_chart_sources(repo, path)
        if sources:
            stems.append(path.stem)
    return stems


def chart_source_indices(repo: Repo, app: str) -> list[int | None]:
    """For `gitops/apps/<app>.yaml`'s Application, the absolute `spec.sources` position of each
    chart source, in the same order `helm_sources` returns them for that app (`None` for the
    singular `spec.source`) — for picking `status.sync.revisions[i]` vs. `status.sync.revision`
    of the running Argo CD Application. Empty if the app file is missing, invalid YAML, or has no
    Application document."""
    path = repo.gitops_dir / "apps" / f"{app}.yaml"
    try:
        docs = yamlio.load_all(path.read_text())
    except (OSError, YAMLError):
        return []
    for doc in docs:
        if not isinstance(doc, Mapping) or doc.get("kind") != "Application":
            continue
        spec = _as_mapping(doc.get("spec"))
        return [index for index, _source in _chart_sources(spec)]
    return []


def _locate_single_chart_source(repo: Repo, key: str) -> tuple[list, Mapping]:
    """The parsed documents of `gitops/apps/<key>.yaml` plus the raw (ruamel) mapping of its sole
    Helm chart source, for callers that need to look at (or mutate) that one source directly.

    Raises `ValidationError` unless the file exists, is valid YAML, has exactly one `Application`
    document, and that Application has exactly one `chart` source: a missing file and invalid YAML
    each get their own message (and hint); only a file that parses but doesn't have exactly one
    chart source keeps the generic "has no Helm chart source" message.
    """
    path = repo.gitops_dir / "apps" / f"{key}.yaml"
    if not path.is_file():
        raise ValidationError(
            f"gitops/apps/{key}.yaml does not exist",
            hint="run `talkub init`, or create the app file",
        )
    try:
        docs = yamlio.load_all(path.read_text())
    except YAMLError as err:
        raise ValidationError(
            f"gitops/apps/{key}.yaml is not valid YAML: {err}"
        ) from None

    error = ValidationError(
        f"gitops/apps/{key}.yaml has no Helm chart source", hint=_NO_CHART_SOURCE_HINT
    )
    applications = [
        doc
        for doc in docs
        if isinstance(doc, Mapping) and doc.get("kind") == "Application"
    ]
    if len(applications) != 1:
        raise error
    spec = _as_mapping(applications[0].get("spec"))
    chart_sources = _chart_sources(spec)
    if len(chart_sources) != 1:
        raise error
    return docs, chart_sources[0][1]


def is_single_chart_app(repo: Repo, key: str) -> bool:
    """Whether `gitops/apps/<key>.yaml` holds exactly one Application document with exactly one
    Helm chart source — the shape `chart upgrade <key>`/`--all` and `versions` require to address
    an app by file stem. `False` for a missing file, invalid YAML, zero/multiple Application
    documents, or zero/multiple chart sources."""
    try:
        _locate_single_chart_source(repo, key)
    except ValidationError:
        return False
    return True


def app_source(repo: Repo, key: str) -> HelmSource:
    """The single `HelmSource` of `gitops/apps/<key>.yaml`'s single Argo CD Application.

    Unlike `helm_sources`, a missing value file is a `ValidationError`, not a Finding, since there
    is no report to attach it to and no other source to fall back on.
    """
    docs, source = _locate_single_chart_source(repo, key)
    application = next(
        doc
        for doc in docs
        if isinstance(doc, Mapping) and doc.get("kind") == "Application"
    )
    metadata = _as_mapping(application.get("metadata"))
    name = str(metadata.get("name", key))
    spec = _as_mapping(application.get("spec"))
    destination = _as_mapping(spec.get("destination"))
    namespace = destination.get("namespace") or "default"

    helm_source, findings = _build_helm_source(repo, name, namespace, source)
    if not helm_source.version:
        raise ValidationError(
            f"gitops/apps/{key}.yaml: targetRevision is empty",
            hint="pin a chart version in targetRevision",
        )
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
