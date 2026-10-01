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


def test_latest_version_schemeless_oci_repo():
    # Argo CD's form of an OCI chart repo: no oci:// scheme in repoURL.
    runner = FakeRunner(outputs={"helm": CHART_YAML})
    charts.latest_version(runner, "code.forgejo.org/forgejo-helm", "forgejo")
    assert runner.calls[0].argv == [
        "helm",
        "show",
        "chart",
        "oci://code.forgejo.org/forgejo-helm/forgejo",
    ]


def test_show_chart_drops_the_oci_pull_preamble():
    # helm 4 prints "Pulled:"/"Digest:" lines on stdout before the document of an OCI chart.
    preamble = b"Pulled: code.forgejo.org/forgejo-helm/forgejo:17.1.7\nDigest: sha256:abc\n"
    runner = FakeRunner(outputs={"helm": preamble + CHART_YAML})
    doc = charts.show_chart(runner, "code.forgejo.org/forgejo-helm", "forgejo", "17.1.7")
    assert doc == {"apiVersion": "v2", "name": "argo-cd", "version": "10.9.2", "appVersion": "v3.5.3"}


def test_strip_pull_preamble_only_touches_leading_lines():
    assert charts.strip_pull_preamble(b"Pulled: a:1\nDigest: sha256:x\n---\nkind: X\n") == b"---\nkind: X\n"
    assert charts.strip_pull_preamble(b"kind: X\nDigest: not a preamble\n") == b"kind: X\nDigest: not a preamble\n"
    assert charts.strip_pull_preamble(b"") == b""


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
