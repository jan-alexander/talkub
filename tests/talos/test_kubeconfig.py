import pytest
from helpers import need

from talkub.config import NodeSpec, TailscaleSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import CommandFailed
from talkub.talos import kubeconfig, tailscale

pytestmark = need("age", "age-keygen", "sops", "talosctl")

KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n"


def _add_controlplane(repo, name: str, ip: str) -> None:
    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip)
    repo.write_settings(settings)


def _stored(repo):
    return yamlio.load(Context.open(repo).sops.decrypt(repo.kubeconfig))


def _node(argv: list[str]) -> str:
    return argv[argv.index("-n") + 1]


def test_refresh_stores_the_kubeconfig_encrypted(talos_repo, spy):
    talosctl = spy("talosctl").on("kubeconfig", stdout=KUBECONFIG)
    kubeconfig.refresh(Context.open(talos_repo))
    assert _stored(talos_repo) == yamlio.load(KUBECONFIG)
    assert talos_repo.kubeconfig.read_bytes() != KUBECONFIG.encode()
    argv = talosctl.argvs[0]
    assert _node(argv) == "10.0.0.10"
    assert argv[argv.index("kubeconfig") :] == ["kubeconfig", "-", "--merge=false"]


def test_refresh_leaves_an_unchanged_kubeconfig_alone(talos_repo, spy, capsys):
    spy("talosctl").on("kubeconfig", stdout=KUBECONFIG)
    kubeconfig.refresh(Context.open(talos_repo))
    before = talos_repo.kubeconfig.read_bytes()
    kubeconfig.refresh(Context.open(talos_repo))
    assert talos_repo.kubeconfig.read_bytes() == before
    assert "kubeconfig unchanged" in capsys.readouterr().out


def test_refresh_falls_back_to_the_next_controlplane(talos_repo, spy):
    _add_controlplane(talos_repo, "cp-02", "10.0.0.12")
    talosctl = spy("talosctl").on("10.0.0.10", "kubeconfig", rc=1, stderr="connection refused")
    talosctl.on("kubeconfig", stdout=KUBECONFIG)
    kubeconfig.refresh(Context.open(talos_repo))
    assert [_node(a) for a in talosctl.argvs] == ["10.0.0.10", "10.0.0.12"]
    assert _stored(talos_repo) == yamlio.load(KUBECONFIG)


def test_refresh_skips_workers(talos_repo, spy):
    settings = talos_repo.load_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker")
    talos_repo.write_settings(settings)
    talosctl = spy("talosctl").on("10.0.0.10", "kubeconfig", rc=1, stderr="connection refused")
    with pytest.raises(CommandFailed):
        kubeconfig.refresh(Context.open(talos_repo))
    assert [_node(a) for a in talosctl.argvs] == ["10.0.0.10"]


def test_refresh_fails_when_no_controlplane_answers(talos_repo, spy):
    before = talos_repo.kubeconfig.read_bytes()
    spy("talosctl").on("kubeconfig", rc=1, stderr="rpc error: connection refused")
    with pytest.raises(CommandFailed) as err:
        kubeconfig.refresh(Context.open(talos_repo))
    assert "connection refused" in (err.value.hint or "")
    assert "talkub bootstrap" in (err.value.hint or "")
    assert talos_repo.kubeconfig.read_bytes() == before


def test_refresh_after_lockdown_points_at_the_tailnet(talos_repo, spy):
    settings = talos_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    talos_repo.write_settings(settings)
    tailscale.firewall_patch_path(talos_repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(talos_repo).write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    talosctl = spy("talosctl").on("kubeconfig", stdout=KUBECONFIG)
    kubeconfig.refresh(Context.open(talos_repo))
    assert _node(talosctl.argvs[0]) == "cp-01.t.ts.net"
    assert _stored(talos_repo)["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
