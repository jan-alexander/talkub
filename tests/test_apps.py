from talkub import apps
from talkub.config import ChartSpec
from talkub.core import yamlio


def _chart():
    return ChartSpec(
        repo="https://coredns.github.io/helm", chart="coredns", version="1.47.1"
    )


def test_helm_app_has_leading_comment():
    text = apps.helm_app(
        "coredns",
        _chart(),
        "https://git.example/repo.git",
        "gitops/values/coredns.yaml",
        "kube-system",
    )
    assert text.startswith("# Managed by talkub.\n")


def test_helm_app_parses_and_has_both_sources():
    text = apps.helm_app(
        "coredns",
        _chart(),
        "https://git.example/repo.git",
        "gitops/values/coredns.yaml",
        "kube-system",
    )
    doc = yamlio.load(text)
    assert doc["apiVersion"] == "argoproj.io/v1alpha1"
    assert doc["kind"] == "Application"
    assert doc["metadata"] == {"name": "coredns", "namespace": "argocd"}
    spec = doc["spec"]
    assert spec["project"] == "default"
    sources = spec["sources"]
    assert len(sources) == 2
    chart_source, git_source = sources
    assert chart_source["repoURL"] == "https://coredns.github.io/helm"
    assert chart_source["chart"] == "coredns"
    assert chart_source["targetRevision"] == "1.47.1"
    assert chart_source["helm"]["valueFiles"] == ["$values/gitops/values/coredns.yaml"]
    assert git_source["repoURL"] == "https://git.example/repo.git"
    assert git_source["targetRevision"] == "HEAD"
    assert git_source["ref"] == "values"
    assert spec["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "kube-system",
    }
    assert spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}


def test_helm_app_sync_options_default_server_side_only():
    text = apps.helm_app(
        "coredns",
        _chart(),
        "https://git.example/repo.git",
        "gitops/values/coredns.yaml",
        "kube-system",
    )
    doc = yamlio.load(text)
    assert doc["spec"]["syncPolicy"]["syncOptions"] == ["ServerSideApply=true"]


def test_helm_app_sync_options_create_namespace():
    text = apps.helm_app(
        "tailscale-operator",
        _chart(),
        "https://git.example/repo.git",
        "gitops/values/x.yaml",
        "tailscale",
        create_namespace=True,
    )
    doc = yamlio.load(text)
    assert doc["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true",
        "ServerSideApply=true",
    ]


def test_helm_app_sync_options_no_server_side():
    text = apps.helm_app(
        "coredns",
        _chart(),
        "https://git.example/repo.git",
        "gitops/values/coredns.yaml",
        "kube-system",
        server_side=False,
    )
    doc = yamlio.load(text)
    assert "syncOptions" not in doc["spec"]["syncPolicy"]


def test_directory_app_parses_with_recurse():
    text = apps.directory_app(
        "tailscale-operator-config",
        "https://git.example/repo.git",
        "gitops/manifests/tailscale",
        "tailscale",
    )
    doc = yamlio.load(text)
    assert doc["apiVersion"] == "argoproj.io/v1alpha1"
    assert doc["kind"] == "Application"
    source = doc["spec"]["source"]
    assert source["repoURL"] == "https://git.example/repo.git"
    assert source["targetRevision"] == "HEAD"
    assert source["path"] == "gitops/manifests/tailscale"
    assert source["directory"] == {"recurse": True}
    assert doc["spec"]["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "tailscale",
    }
    assert doc["spec"]["syncPolicy"]["syncOptions"] == ["ServerSideApply=true"]


def test_directory_app_skip_dry_run_on_missing():
    text = apps.directory_app(
        "tailscale-operator-config",
        "https://git.example/repo.git",
        "gitops/manifests/tailscale",
        "tailscale",
        skip_dry_run_on_missing=True,
    )
    doc = yamlio.load(text)
    assert doc["spec"]["syncPolicy"]["syncOptions"] == [
        "ServerSideApply=true",
        "SkipDryRunOnMissingResource=true",
    ]


RETRY = {"limit": 10, "backoff": {"duration": "10s", "factor": 2, "maxDuration": "3m"}}


def test_both_renderers_retry_failed_syncs():
    helm = yamlio.load(
        apps.helm_app(
            "coredns",
            _chart(),
            "https://g/r.git",
            "gitops/values/coredns.yaml",
            "kube-system",
        )
    )
    directory = yamlio.load(
        apps.directory_app("cfg", "https://g/r.git", "gitops/manifests/x", "x")
    )
    assert helm["spec"]["syncPolicy"]["retry"] == RETRY
    assert directory["spec"]["syncPolicy"]["retry"] == RETRY


def test_sync_wave_annotation_only_when_given():
    plain = yamlio.load(
        apps.helm_app("coredns", _chart(), "https://g/r.git", "v.yaml", "kube-system")
    )
    assert "annotations" not in plain["metadata"]
    helm = yamlio.load(
        apps.helm_app(
            "op", _chart(), "https://g/r.git", "v.yaml", "tailscale", sync_wave="0"
        )
    )
    directory = yamlio.load(
        apps.directory_app("cfg", "https://g/r.git", "p", "tailscale", sync_wave="1")
    )
    assert helm["metadata"]["annotations"] == {"argocd.argoproj.io/sync-wave": "0"}
    assert directory["metadata"]["annotations"] == {"argocd.argoproj.io/sync-wave": "1"}


def test_directory_app_create_namespace_and_ignore_differences():
    ignore = [{"group": "", "kind": "Service", "jsonPointers": ["/spec/externalName"]}]
    doc = yamlio.load(
        apps.directory_app(
            "cfg",
            "https://g/r.git",
            "p",
            "tailscale",
            create_namespace=True,
            skip_dry_run_on_missing=True,
            ignore_differences=ignore,
        )
    )
    assert doc["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true",
        "ServerSideApply=true",
        "SkipDryRunOnMissingResource=true",
        "RespectIgnoreDifferences=true",
    ]
    assert doc["spec"]["ignoreDifferences"] == ignore
    plain = yamlio.load(apps.directory_app("cfg", "https://g/r.git", "p", "tailscale"))
    assert "ignoreDifferences" not in plain["spec"]
