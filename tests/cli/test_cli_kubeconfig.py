import pytest
from helpers import need

from talkub.cli.main import main

pytestmark = need("age", "age-keygen", "sops", "talosctl")

KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters: []\n"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_kubeconfig_writes_and_says_what_to_commit(talos_repo, spy, capfd):
    spy("talosctl").on("kubeconfig", stdout=KUBECONFIG)
    assert _run(["--repo", str(talos_repo.root), "kubeconfig"]) in (0, None)
    out = capfd.readouterr().out
    assert "wrote kubernetes/kubeconfig.sops.yaml" in out
    assert "next: commit kubernetes/kubeconfig.sops.yaml" in out


def test_kubeconfig_reports_unreachable_controlplanes(talos_repo, spy, capfd):
    spy("talosctl").on("kubeconfig", rc=1, stderr="connection refused")
    assert _run(["--repo", str(talos_repo.root), "kubeconfig"]) == 1
    err = capfd.readouterr().err
    assert "no control plane returned a kubeconfig" in err
    assert "cp-01 (10.0.0.10): connection refused" in err
