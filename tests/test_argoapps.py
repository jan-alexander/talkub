"""`talkub.argoapps`: shared Argo CD Application parser (moved out of `talkub.lint`, Task 1)."""

from __future__ import annotations

from importlib import resources
from string import Template

import pytest

from talkub import apps, coredns, yamlio
from talkub.argoapps import HelmSource, app_source, chart_source_indices, helm_sources, set_target_revision
from talkub.errors import ValidationError
from talkub.findings import Finding
from talkub.repo import ChartSpec


def _write_app_doc(repo, name, doc):
    apps_dir = repo.gitops_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / f"{name}.yaml").write_text(yamlio.dump(doc))


def _app_doc(name, *, namespace="ns1", source=None, sources=None):
    spec = {"destination": {"namespace": namespace}} if namespace is not None else {}
    if sources is not None:
        spec["sources"] = sources
    elif source is not None:
        spec["source"] = source
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name}, "spec": spec,
    }


def _render_app_template(name, values):
    path = resources.files("talkub") / "templates" / "repo" / "gitops" / "apps" / name
    return Template(path.read_text()).substitute(values)


def test_helm_sources_parses_real_app_templates(empty_repo):
    values = {
        "argocd_repo": "https://argoproj.github.io/argo-helm", "argocd_chart": "argo-cd",
        "argocd_version": "10.9.2",
        "sealed_secrets_repo": "https://bitnami.github.io/sealed-secrets", "sealed_secrets_chart": "sealed-secrets",
        "sealed_secrets_version": "2.20.0",
        "git_url": "https://git.example.com/demo.git",
    }
    apps_dir = empty_repo.root / "gitops" / "apps"
    apps_dir.mkdir(parents=True)
    (apps_dir / "argocd.yaml").write_text(_render_app_template("argocd.yaml.tmpl", values))
    (apps_dir / "sealed-secrets.yaml").write_text(_render_app_template("sealed-secrets.yaml.tmpl", values))
    values_dir = empty_repo.root / "gitops" / "values"
    values_dir.mkdir(parents=True)
    (values_dir / "argocd.yaml").write_text("x: 1\n")
    (values_dir / "sealed-secrets.yaml").write_text("x: 1\n")

    chart = ChartSpec("https://coredns.github.io/helm", "coredns", "1.47.1")
    (apps_dir / "coredns.yaml").write_text(
        apps.helm_app("coredns", chart, values["git_url"], "gitops/values/coredns.yaml", "kube-system")
    )
    (values_dir / "coredns.yaml").write_text(coredns.values())

    sources, findings = helm_sources(empty_repo)
    assert findings == []
    by_app = {s.app: s for s in sources}
    assert set(by_app) == {"argocd", "sealed-secrets", "coredns"}

    argocd = by_app["argocd"]
    assert argocd == HelmSource(
        app="argocd", release="argocd", namespace="argocd",
        repo="https://argoproj.github.io/argo-helm", chart="argo-cd", version="10.9.2",
        value_files=(values_dir / "argocd.yaml",), inline_values=None,
    )
    sealed = by_app["sealed-secrets"]
    assert sealed.namespace == "kube-system"
    assert sealed.value_files == (values_dir / "sealed-secrets.yaml",)
    coredns_source = by_app["coredns"]
    assert coredns_source.namespace == "kube-system"
    assert coredns_source.value_files == (values_dir / "coredns.yaml",)


def test_helm_sources_release_name_overrides_metadata_name(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0", "helm": {"releaseName": "custom-release"}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert findings == []
    assert sources[0].release == "custom-release"


def test_helm_sources_namespace_defaults_to_default(empty_repo):
    doc = _app_doc("myapp", namespace=None, sources=[{"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"}])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert findings == []
    assert sources[0].namespace == "default"
    assert sources[0].release == "myapp"


def test_helm_sources_missing_values_file_is_an_error(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0",
         "helm": {"valueFiles": ["$values/gitops/values/missing.yaml"]}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert sources[0].value_files == ()
    assert findings == [Finding("error", "app myapp", "values file gitops/values/missing.yaml not found")]


def test_helm_sources_unsupported_value_file_entry_is_a_warning(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0",
         "helm": {"valueFiles": ["some/other/path.yaml"]}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert sources[0].value_files == ()
    assert findings == [Finding("warning", "app myapp", "valueFiles entry 'some/other/path.yaml' is unsupported, skipped")]


def test_helm_sources_inline_values_string(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0", "helm": {"values": "foo: bar\n"}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert findings == []
    assert sources[0].inline_values == "foo: bar\n"


def test_helm_sources_inline_values_object_is_dumped(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0", "helm": {"valuesObject": {"foo": "bar"}}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert findings == []
    assert yamlio.load(sources[0].inline_values) == {"foo": "bar"}


def test_helm_sources_skips_non_application_docs(empty_repo):
    apps_dir = empty_repo.root / "gitops" / "apps"
    apps_dir.mkdir(parents=True)
    (apps_dir / "cm.yaml").write_text(
        yamlio.dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}})
    )
    sources, findings = helm_sources(empty_repo)
    assert sources == [] and findings == []


def test_helm_sources_skips_file_with_invalid_yaml_syntax(empty_repo):
    apps_dir = empty_repo.root / "gitops" / "apps"
    apps_dir.mkdir(parents=True)
    (apps_dir / "broken.yaml").write_text("apiVersion: v1\nkind: [oops\n")
    sources, findings = helm_sources(empty_repo)  # must not raise
    assert sources == [] and findings == []


def test_helm_sources_handles_non_mapping_spec_destination_and_helm(empty_repo):
    doc = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "weird"},
        "spec": {
            "destination": "not-a-mapping",
            "sources": [{"repoURL": "r", "chart": "c", "targetRevision": "1.0.0", "helm": "not-a-mapping"}],
        },
    }
    _write_app_doc(empty_repo, "weird", doc)
    sources, findings = helm_sources(empty_repo)  # must not raise AttributeError
    assert findings == []
    assert sources == [
        HelmSource(
            app="weird", release="weird", namespace="default", repo="r", chart="c", version="1.0.0",
            value_files=(), inline_values=None,
        )
    ]


def test_helm_sources_handles_non_mapping_metadata_and_top_level_spec(empty_repo):
    doc = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": "oops", "spec": "oops"}
    _write_app_doc(empty_repo, "x", doc)
    sources, findings = helm_sources(empty_repo)  # must not raise
    assert sources == [] and findings == []


def test_helm_sources_ignores_sops_yaml_files(empty_repo):
    doc = _app_doc("secret", sources=[{"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"}])
    _write_app_doc(empty_repo, "secret", doc)
    (empty_repo.gitops_dir / "apps" / "secret.yaml").rename(empty_repo.gitops_dir / "apps" / "secret.sops.yaml")
    sources, findings = helm_sources(empty_repo)
    assert sources == [] and findings == []


def test_helm_sources_skips_source_without_chart(empty_repo):
    doc = _app_doc(
        "dirapp",
        source={"repoURL": "r", "targetRevision": "HEAD", "path": "gitops/manifests/x", "directory": {"recurse": True}},
    )
    _write_app_doc(empty_repo, "dirapp", doc)
    sources, findings = helm_sources(empty_repo)
    assert sources == [] and findings == []


# --- app_source ---


def test_app_source_ok(empty_repo):
    doc = _app_doc("myapp", sources=[{"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"}])
    _write_app_doc(empty_repo, "myapp", doc)
    source = app_source(empty_repo, "myapp")
    assert source == HelmSource(
        app="myapp", release="myapp", namespace="ns1", repo="r", chart="c", version="1.0.0",
        value_files=(), inline_values=None,
    )


def test_app_source_missing_file_is_a_validation_error(empty_repo):
    with pytest.raises(ValidationError, match="gitops/apps/myapp.yaml has no Helm chart source"):
        app_source(empty_repo, "myapp")


def test_app_source_no_chart_source_is_a_validation_error(empty_repo):
    doc = _app_doc(
        "dirapp",
        source={"repoURL": "r", "targetRevision": "HEAD", "path": "gitops/manifests/x", "directory": {"recurse": True}},
    )
    _write_app_doc(empty_repo, "dirapp", doc)
    with pytest.raises(ValidationError, match="gitops/apps/dirapp.yaml has no Helm chart source"):
        app_source(empty_repo, "dirapp")


def test_app_source_two_chart_sources_is_a_validation_error(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c1", "targetRevision": "1.0.0"},
        {"repoURL": "r", "chart": "c2", "targetRevision": "2.0.0"},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    with pytest.raises(ValidationError, match="gitops/apps/myapp.yaml has no Helm chart source"):
        app_source(empty_repo, "myapp")


def test_app_source_missing_values_file_is_a_validation_error(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0",
         "helm": {"valueFiles": ["$values/gitops/values/missing.yaml"]}},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    with pytest.raises(ValidationError, match="values file gitops/values/missing.yaml not found"):
        app_source(empty_repo, "myapp")


# --- chart_source_indices ---


def test_chart_source_indices_singular_source_is_none(empty_repo):
    doc = _app_doc("myapp", source={"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"})
    _write_app_doc(empty_repo, "myapp", doc)
    assert chart_source_indices(empty_repo, "myapp") == [None]


def test_chart_source_indices_multi_source_is_absolute_position(empty_repo):
    doc = _app_doc("myapp", sources=[
        {"repoURL": "r", "targetRevision": "HEAD", "ref": "values"},
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"},
    ])
    _write_app_doc(empty_repo, "myapp", doc)
    assert chart_source_indices(empty_repo, "myapp") == [1]


def test_chart_source_indices_non_list_sources_does_not_fall_back_to_source(empty_repo):
    """`sources` present but not a list is invalid (matches `_chart_sources`/`app_source`): a
    `source` present alongside it must not be used as a fallback."""
    doc = _app_doc("myapp", source={"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"})
    doc["spec"]["sources"] = "oops"
    _write_app_doc(empty_repo, "myapp", doc)
    assert chart_source_indices(empty_repo, "myapp") == []


def test_chart_source_indices_missing_file_is_empty(empty_repo):
    assert chart_source_indices(empty_repo, "nope") == []


# --- set_target_revision ---


def test_set_target_revision_updates_version_and_returns_true(empty_repo):
    _write_app_doc(empty_repo, "myapp", _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"},
    ]))
    changed = set_target_revision(empty_repo, "myapp", "2.0.0")
    assert changed is True
    assert app_source(empty_repo, "myapp").version == "2.0.0"


def test_set_target_revision_same_version_is_a_noop(empty_repo):
    path = empty_repo.gitops_dir / "apps" / "myapp.yaml"
    _write_app_doc(empty_repo, "myapp", _app_doc("myapp", sources=[
        {"repoURL": "r", "chart": "c", "targetRevision": "1.0.0"},
    ]))
    before = path.read_bytes()
    changed = set_target_revision(empty_repo, "myapp", "1.0.0")
    assert changed is False
    assert path.read_bytes() == before


def test_set_target_revision_keeps_comments_and_leaves_values_ref_alone(empty_repo):
    apps_dir = empty_repo.gitops_dir / "apps"
    apps_dir.mkdir(parents=True)
    path = apps_dir / "myapp.yaml"
    path.write_text(
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: myapp\n"
        "spec:\n"
        "  destination:\n"
        "    namespace: ns1\n"
        "  sources:\n"
        "    - repoURL: r\n"
        "      chart: c\n"
        "      targetRevision: 1.0.0  # chart version\n"
        "    - repoURL: r\n"
        "      targetRevision: HEAD\n"
        "      ref: values\n"
    )
    before = path.read_text()

    changed = set_target_revision(empty_repo, "myapp", "2.0.0")

    assert changed is True
    after = path.read_text()
    assert after == before.replace("targetRevision: 1.0.0  # chart version", "targetRevision: 2.0.0  # chart version")
    assert "targetRevision: HEAD" in after
