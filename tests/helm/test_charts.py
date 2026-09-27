from helpers import FakeRunner

from talkub.helm import charts

CHART_YAML = b"apiVersion: v2\nname: argo-cd\nversion: 10.9.2\nappVersion: v3.5.3\n"


def test_latest_version_http_repo():
    runner = FakeRunner(outputs={"helm": CHART_YAML})
    assert charts.latest_version(runner, "https://argoproj.github.io/argo-helm", "argo-cd") == "10.9.2"
    assert runner.calls[0].argv == [
        "helm",
        "show",
        "chart",
        "argo-cd",
        "--repo",
        "https://argoproj.github.io/argo-helm",
    ]


def test_latest_version_oci_repo():
    runner = FakeRunner(outputs={"helm": CHART_YAML})
    charts.latest_version(runner, "oci://ghcr.io/example/charts", "thing")
    assert runner.calls[0].argv == [
        "helm",
        "show",
        "chart",
        "oci://ghcr.io/example/charts/thing",
    ]


def test_resolve_defaults_covers_both_charts():
    specs = charts.resolve_defaults(FakeRunner(outputs={"helm": CHART_YAML}))
    assert set(specs) == {"argocd", "sealed-secrets"}
    assert specs["sealed-secrets"].repo == "https://bitnami.github.io/sealed-secrets"


def test_catalog_has_every_chart_talkub_writes_apps_for():
    assert charts.CATALOG == {
        "argocd": ("https://argoproj.github.io/argo-helm", "argo-cd"),
        "sealed-secrets": (
            "https://bitnami.github.io/sealed-secrets",
            "sealed-secrets",
        ),
        "coredns": ("https://coredns.github.io/helm", "coredns"),
        "tailscale-operator": (
            "https://pkgs.tailscale.com/helmcharts",
            "tailscale-operator",
        ),
    }
