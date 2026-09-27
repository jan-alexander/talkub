from pathlib import Path

from helpers import FakeRunner

from talkub.argocd.app_files import HelmSource
from talkub.config import ChartSpec
from talkub.helm.install import chart_ref, helm_install
from talkub.kube.session import KubeSession


def test_chart_ref_oci_is_a_single_reference():
    chart = ChartSpec(repo="oci://ghcr.io/argoproj/argo-helm/", chart="argo-cd", version="1")
    assert chart_ref(chart) == ["oci://ghcr.io/argoproj/argo-helm/argo-cd"]


def test_chart_ref_https_uses_repo_flag():
    chart = ChartSpec(repo="https://charts.example.org", chart="demo", version="1")
    assert chart_ref(chart) == ["demo", "--repo", "https://charts.example.org"]


def test_helm_install_oci_chart(tmp_path):
    runner = FakeRunner()
    source = HelmSource(
        app="argocd",
        release="argocd",
        namespace="argocd",
        repo="oci://ghcr.io/argoproj/argo-helm/",
        chart="argo-cd",
        version="10.9.2",
        value_files=(tmp_path / "argocd.yaml",),
    )
    helm_install(KubeSession(runner, Path("kc")), source, create_namespace=True)
    [call] = runner.calls
    assert call.argv[:5] == [
        "helm",
        "upgrade",
        "--install",
        "argocd",
        "oci://ghcr.io/argoproj/argo-helm/argo-cd",
    ]
    assert "--repo" not in call.argv
    assert call.argv[call.argv.index("--version") + 1] == "10.9.2"
    assert call.argv[call.argv.index("-f") + 1] == str(tmp_path / "argocd.yaml")
    assert call.argv[-1] == "--create-namespace"
    assert call.env == {"KUBECONFIG": "kc"}
