import pytest

from helpers import need
from talkub import tailscale, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops")

TALOSCONFIG = b"context: test\ncontexts:\n    test:\n        endpoints: [10.0.0.10]\n        nodes: [10.0.0.10]\n        key: a2V5\n"
KUBECONFIG = b"apiVersion: v1\nkind: Config\nclusters:\n- name: test\n  cluster:\n    server: https://10.0.0.10:6443\n"
KEY = "tskey-auth-secretvalue"


@pytest.fixture
def repo(repo_with_configs):
    """Two control planes and a worker, real-looking talosconfig/kubeconfig (encrypted)."""
    ctx = Context.open(repo_with_configs)
    public = ctx.keyring.public_key()
    for path, data in ((repo_with_configs.talosconfig, TALOSCONFIG), (repo_with_configs.kubeconfig, KUBECONFIG)):
        atomic_write(path, ctx.sops.encrypt(data, filename=path.name, public_key=public))
    settings = repo_with_configs.load_settings()
    settings.nodes["cp-02"] = NodeSpec(ip="10.0.0.12")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker", mgmt="w-01.lan")
    repo_with_configs.write_settings(settings)
    return repo_with_configs


def _plain_files(root):
    return [p for p in root.rglob("*") if p.is_file() and ".sops." not in p.name and p.suffix != ".age"]


def test_enable_keeps_addresses_and_encrypts_key(repo):
    before_talosconfig = repo.talosconfig.read_bytes()
    notes = tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    settings = repo.load_settings()
    assert settings.tailscale.tailnet == "t.ts.net"
    assert settings.tailscale.node_cidr == "10.0.0.0/24"
    assert settings.nodes["cp-01"].mgmt is None  # addresses untouched: LAN still works
    assert repo.talosconfig.read_bytes() == before_talosconfig
    assert tailscale.EXTENSION in settings.schematics["metal"].extensions
    assert not settings.schematics["metal"].is_current  # resolved by the next image show / plan / upgrade
    assert not tailscale.is_locked_down(repo)
    patch = Context.open(repo).sops.decrypt(tailscale.extension_patch_path(repo))
    assert f"TS_AUTHKEY={KEY}".encode() in patch
    assert not any(KEY.encode() in p.read_bytes() for p in _plain_files(repo.root))
    assert any("upgrade talos" in n for n in notes)  # the repo has a kubeconfig → running cluster


def test_enable_twice_and_bad_input(repo):
    with pytest.raises(ValidationError):
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey="nope")
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY, node_cidr="10.0.0.0/16")
    assert repo.load_settings().tailscale.node_cidr == "10.0.0.0/16"
    with pytest.raises(ValidationError) as exc:
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    assert "already enabled" in str(exc.value)


def test_lockdown_switches_clients_to_the_tailnet(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    notes = tailscale.lockdown(Context.open(repo))
    settings = repo.load_settings()
    assert settings.nodes["cp-01"].mgmt == "cp-01.t.ts.net"
    assert settings.nodes["cp-02"].mgmt == "cp-02.t.ts.net"
    assert settings.nodes["w-01"].mgmt == "w-01.lan"  # explicit mgmt is kept
    assert settings.endpoint == "https://10.0.0.10:6443"  # cluster endpoint unchanged
    ctx = Context.open(repo)
    talosconfig = yamlio.load(ctx.sops.decrypt(repo.talosconfig).decode())["contexts"]["test"]
    assert list(talosconfig["endpoints"]) == ["cp-01.t.ts.net", "cp-02.t.ts.net"]
    assert list(talosconfig["nodes"]) == ["cp-01.t.ts.net"]
    kubeconfig = yamlio.load(ctx.sops.decrypt(repo.kubeconfig).decode())
    assert kubeconfig["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
    assert tailscale.is_locked_down(repo)
    assert "10.0.0.0/24" in tailscale.firewall_patch_path(repo).read_text()
    assert any("talkub apply" in n for n in notes)


def test_lockdown_requires_enable_and_runs_once(repo):
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo))
    assert "tailscale enable" in exc.value.hint
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    with pytest.raises(ValidationError):
        tailscale.lockdown(Context.open(repo))


def test_lockdown_without_kubeconfig(repo):
    repo.kubeconfig.unlink()
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    assert not repo.kubeconfig.exists()
