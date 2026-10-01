import pytest
from helpers import need

from talkub.config import NodeSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos import tailscale

pytestmark = need("age", "age-keygen", "sops")

TALOSCONFIG = b"context: test\ncontexts:\n    test:\n        endpoints: [10.0.0.10]\n        nodes: [10.0.0.10]\n        key: a2V5\n"
KUBECONFIG = b"apiVersion: v1\nkind: Config\nclusters:\n- name: test\n  cluster:\n    server: https://10.0.0.10:6443\n"
KEY = "tskey-auth-secretvalue"


@pytest.fixture
def talosctl(spy):
    """lockdown on a running cluster probes every node via talosctl; by default all answer."""
    return spy("talosctl")


@pytest.fixture
def repo(repo_with_configs, talosctl):
    """Two control planes and a worker, real-looking talosconfig/kubeconfig (encrypted)."""
    ctx = Context.open(repo_with_configs)
    public = ctx.keyring.public_key()
    for path, data in (
        (repo_with_configs.talosconfig, TALOSCONFIG),
        (repo_with_configs.kubeconfig, KUBECONFIG),
    ):
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
    assert "nodeCidr" not in repo.settings_file.read_text()
    assert settings.nodes["cp-01"].mgmt is None  # addresses untouched: LAN still works
    assert repo.talosconfig.read_bytes() == before_talosconfig
    assert tailscale.EXTENSION in settings.schematics["metal"].extensions
    assert not settings.schematics["metal"].is_current  # resolved by the next image show / plan / upgrade
    assert not tailscale.is_locked_down(repo)
    patch = Context.open(repo).sops.decrypt(tailscale.extension_patch_path(repo))
    assert f"TS_AUTHKEY={KEY}".encode() in patch
    assert not any(KEY.encode() in p.read_bytes() for p in _plain_files(repo.root))
    assert any("upgrade talos" in n for n in notes)  # the repo has a kubeconfig → running cluster


def test_enable_note_tells_apply_before_upgrade(repo):
    """On a real node, upgrading Talos before `apply` hangs the boot waiting for the extension
    service config; `apply` (which Talos accepts before the extension exists) must run first."""
    notes = tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    note = next(n for n in notes if "running cluster" in n)
    assert note.index("talkub apply") < note.index("talkub upgrade talos")
    assert "talkub tailscale lockdown" in note
    assert "--use-ips" in note


def test_enable_twice_and_bad_input(repo):
    with pytest.raises(ValidationError):
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey="nope")
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    with pytest.raises(ValidationError) as exc:
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    assert "already enabled" in str(exc.value)


def test_enable_reports_interrupted_half_state(repo):
    ctx = Context.open(repo)
    patch = tailscale.extension_patch_path(repo)
    atomic_write(
        patch,
        ctx.sops.encrypt(b"stray: true\n", filename=patch.name, public_key=ctx.keyring.public_key()),
    )
    assert repo.load_settings().tailscale is None
    with pytest.raises(ValidationError) as exc:
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    assert "interrupted" in exc.value.hint
    assert str(patch.relative_to(repo.root)) in str(exc.value)


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
    assert "subnet: 10.0.0.10/32" in tailscale.firewall_patch_path(repo).read_text()
    assert any("talkub apply" in n for n in notes)
    assert any("w-01 (w-01.lan)" in n for n in notes)


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


def test_lockdown_leaves_repo_retryable_on_failure(repo):
    """firewall.yaml (the lockdown marker) is written last: a failure midway must not lock down."""
    ctx = Context.open(repo)
    public = ctx.keyring.public_key()
    bad_kubeconfig = b"apiVersion: v1\nkind: Config\n"  # no clusters
    atomic_write(
        repo.kubeconfig,
        ctx.sops.encrypt(bad_kubeconfig, filename=repo.kubeconfig.name, public_key=public),
    )
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    before_talosconfig = repo.talosconfig.read_bytes()
    before_settings = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError):
        tailscale.lockdown(Context.open(repo))
    assert not tailscale.firewall_patch_path(repo).exists()
    assert repo.talosconfig.read_bytes() == before_talosconfig
    assert repo.settings_file.read_bytes() == before_settings


def test_lockdown_write_failure_does_not_leave_a_locked_down_repo(repo, monkeypatch):
    """A disk failure during the talosconfig write (which happens before firewall.yaml) must
    not leave firewall.yaml behind — proves the marker is genuinely written last, not first."""
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    real_atomic_write = tailscale.atomic_write

    def failing_write(path, data, *args, **kwargs):
        if path == repo.talosconfig:
            raise OSError("disk full")
        return real_atomic_write(path, data, *args, **kwargs)

    monkeypatch.setattr(tailscale, "atomic_write", failing_write)
    with pytest.raises(OSError):
        tailscale.lockdown(Context.open(repo))
    assert not tailscale.firewall_patch_path(repo).exists()
    assert not tailscale.is_locked_down(repo)


def test_lockdown_firewall_lists_the_nodes_addresses(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    text = tailscale.firewall_patch_path(repo).read_text()
    assert "subnet: 10.0.0.10/32" in text and "subnet: 10.0.0.20/32" in text
    assert "/24" not in text


def test_lockdown_refuses_a_node_without_an_ip_address(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    settings = repo.load_settings()
    settings.nodes["w-02"] = NodeSpec(ip="w-02.lan", role="worker")
    repo.write_settings(settings)
    before = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError, match="w-02"):
        tailscale.lockdown(Context.open(repo))
    assert not tailscale.is_locked_down(repo)
    assert repo.settings_file.read_bytes() == before


def test_firewall_requires_lockdown(repo):
    with pytest.raises(ValidationError, match="not enabled"):
        tailscale.firewall(repo)
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    with pytest.raises(ValidationError) as exc:
        tailscale.firewall(repo)
    assert "lockdown" in exc.value.hint


def test_firewall_rewrites_the_patch_from_the_nodes(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    path = tailscale.firewall_patch_path(repo)
    path.write_text("# hand edit\n")
    settings = repo.load_settings()
    settings.nodes["w-02"] = NodeSpec(ip="10.0.0.30", role="worker", mgmt="w-02.t.ts.net")
    repo.write_settings(settings)
    notes = tailscale.firewall(repo)
    text = path.read_text()
    assert "hand edit" not in text
    assert "subnet: 10.0.0.30/32" in text and "subnet: 10.0.0.10/32" in text
    assert any("10.0.0.30/32" in n for n in notes)
    assert any("talkub apply" in n for n in notes)


def test_lockdown_probes_every_node_via_the_tailnet_first(repo, talosctl):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    probes = [call for call in talosctl.calls if "version" in call["args"]]
    targets = {call["args"][call["args"].index("-n") + 1] for call in probes}
    assert targets == {"cp-01.t.ts.net", "cp-02.t.ts.net", "w-01.lan"}
    assert all(call["files"]["--talosconfig"] for call in probes)
    assert tailscale.is_locked_down(repo)


def test_lockdown_refuses_when_a_node_is_not_reachable_via_the_tailnet(repo, talosctl):
    talosctl.on("cp-02.t.ts.net", "version", rc=1, stderr="connection refused")
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    before_talosconfig = repo.talosconfig.read_bytes()
    before_settings = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo))
    assert "not reachable via the tailnet yet: cp-02 (cp-02.t.ts.net): connection refused" in str(exc.value)
    assert "cp-01" not in str(exc.value)
    assert "tailscale status" in exc.value.hint
    assert "--use-ips" not in exc.value.hint
    assert not tailscale.firewall_patch_path(repo).exists()
    assert repo.settings_file.read_bytes() == before_settings
    assert repo.talosconfig.read_bytes() == before_talosconfig


def test_lockdown_suggests_use_ips_when_talosctl_cannot_resolve_magicdns(repo, talosctl):
    # talosctl (Go resolver) may not see the MagicDNS names the OS resolver does, e.g. on macOS.
    stderr = (
        "error getting version from node cp-01.t.ts.net: rpc error: code = Unavailable "
        "desc = name resolver error: produced zero addresses"
    )
    talosctl.on("cp-01.t.ts.net", "version", rc=1, stderr=stderr)
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo))
    assert "produced zero addresses" in str(exc.value)
    assert "--use-ips" in exc.value.hint
    assert not tailscale.firewall_patch_path(repo).exists()


def test_lockdown_before_bootstrap_does_not_probe(repo, talosctl):
    repo.kubeconfig.unlink()
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    assert talosctl.calls == []


def _addresses_table(node_ip, tailnet_ip=None):
    lines = [
        "NODE              NAMESPACE   TYPE            ID                              VERSION   ADDRESS              LINK",
        f"{node_ip}   network     AddressStatus   eth0/{node_ip}/32           1         {node_ip}/32         eth0",
    ]
    if tailnet_ip:
        lines.append(
            f"{node_ip}   network     AddressStatus   tailscale0/{tailnet_ip}/32   1         {tailnet_ip}/32      tailscale0"
        )
    return "\n".join(lines) + "\n"


def test_lockdown_use_ips_sets_mgmt_to_the_nodes_tailnet_ipv4(repo, talosctl):
    talosctl.on(
        "10.0.0.10",
        "get",
        "addresses",
        stdout=_addresses_table("10.0.0.10", "100.100.148.20"),
    )
    talosctl.on(
        "10.0.0.12",
        "get",
        "addresses",
        stdout=_addresses_table("10.0.0.12", "100.100.148.21"),
    )
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    notes = tailscale.lockdown(Context.open(repo), use_ips=True)
    settings = repo.load_settings()
    assert settings.nodes["cp-01"].mgmt == "100.100.148.20"
    assert settings.nodes["cp-02"].mgmt == "100.100.148.21"
    assert settings.nodes["w-01"].mgmt == "w-01.lan"  # explicit mgmt is kept
    assert settings.tailscale.use_ips is True
    assert tailscale.is_locked_down(repo)
    non_tailnet_note = next(n for n in notes if "non-tailnet mgmt" in n)
    assert "cp-01" not in non_tailnet_note and "cp-02" not in non_tailnet_note  # tailnet IPs count as tailnet
    assert "w-01 (w-01.lan)" in non_tailnet_note  # explicit LAN mgmt is still flagged
    queries = [call for call in talosctl.calls if "addresses" in call["args"]]
    assert {call["args"][call["args"].index("-n") + 1] for call in queries} == {
        "10.0.0.10",
        "10.0.0.12",
    }


def test_lockdown_use_ips_requires_a_tailnet_ip_for_every_bare_node(repo, talosctl):
    talosctl.on(
        "10.0.0.10",
        "get",
        "addresses",
        stdout=_addresses_table("10.0.0.10", "100.100.148.20"),
    )
    talosctl.on("10.0.0.12", "get", "addresses", stdout=_addresses_table("10.0.0.12"))  # no tailnet address yet
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    before_settings = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo), use_ips=True)
    assert "cp-02" in str(exc.value)
    assert "cp-01" not in str(exc.value)
    assert "talkub apply" in exc.value.hint
    assert "upgrade talos" in exc.value.hint
    assert "tailscale status" in exc.value.hint
    assert not tailscale.firewall_patch_path(repo).exists()
    assert repo.settings_file.read_bytes() == before_settings


def test_lockdown_use_ips_reports_a_failed_probe_separately_from_no_tailnet_ip(repo, talosctl):
    talosctl.on(
        "10.0.0.10",
        "get",
        "addresses",
        stdout=_addresses_table("10.0.0.10", "100.100.148.20"),
    )
    talosctl.on(
        "10.0.0.12",
        "get",
        "addresses",
        rc=1,
        stderr="connection refused\nsecond line\n",
    )
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    before_settings = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo), use_ips=True)
    assert "cp-02 (talosctl exited 1: connection refused)" in str(exc.value)
    assert "second line" not in str(exc.value)
    assert "cp-01" not in str(exc.value)
    assert not tailscale.firewall_patch_path(repo).exists()
    assert repo.settings_file.read_bytes() == before_settings


def test_lockdown_without_talosconfig(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    repo.talosconfig.unlink()
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo))
    assert "does not exist" in str(exc.value)
    assert "talkub init" in exc.value.hint
    assert not tailscale.firewall_patch_path(repo).exists()
