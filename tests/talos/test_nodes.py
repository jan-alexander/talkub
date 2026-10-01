import shutil
import subprocess

import pytest
from helpers import need

from talkub.config import NodeSpec, SchematicSpec
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.talos.nodes import (
    FIRST_ETHERNET,
    NodeOptions,
    add_node,
    node_patch_path,
    render_node_patch,
)


def _docs(text):
    return [d for d in yamlio.load_all(text) if d]


def test_default_patch_is_hostname_only():
    docs = _docs(render_node_patch("cp-01"))
    assert docs == [
        {
            "apiVersion": "v1alpha1",
            "kind": "HostnameConfig",
            "auto": "off",
            "hostname": "cp-01",
        }
    ]


def test_static_without_mac_uses_first_ethernet_and_warns():
    options = NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1")
    assert any("--mac" in w for w in options.check())
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["LinkAliasConfig"]["selector"]["match"] == FIRST_ETHERNET
    assert kinds["LinkConfig"]["addresses"] == [{"address": "10.0.0.10/24"}]
    assert kinds["LinkConfig"]["routes"] == [{"gateway": "10.0.0.1"}]
    assert "ResolverConfig" not in kinds


def test_static_with_mac_and_dns():
    options = NodeOptions(
        static="10.0.0.10/24",
        gateway="10.0.0.1",
        mac="AA:BB:CC:DD:EE:FF",
        dns=("1.1.1.1",),
    )
    assert options.check() == []
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["LinkAliasConfig"]["selector"]["match"] == 'mac(link.permanent_addr) == "aa:bb:cc:dd:ee:ff"'
    assert kinds["ResolverConfig"]["nameservers"] == [{"address": "1.1.1.1"}]


def test_disk_and_public_ingress_documents():
    options = NodeOptions(disk_selector='disk.transport == "nvme"', public_ingress=True)
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["UnattendedInstallConfig"]["provisioning"]["diskSelector"]["match"] == 'disk.transport == "nvme"'
    rule = kinds["NetworkRuleConfig"]
    assert rule["portSelector"] == {"ports": [80, 443], "protocol": "tcp"}
    assert rule["ingress"] == [{"subnet": "0.0.0.0/0"}, {"subnet": "::/0"}]


@pytest.mark.parametrize(
    "options, message",
    [
        (NodeOptions(static="10.0.0.10/24"), "together"),
        (NodeOptions(gateway="10.0.0.1"), "together"),
        (NodeOptions(static="10.0.0.10", gateway="10.0.0.1"), "prefix"),
        (NodeOptions(static="10.0.0.10/24", gateway="10.0.1.1"), "not in"),
        (NodeOptions(mac="aa:bb:cc:dd:ee:ff"), "--static"),
        (NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="aa:bb"), "MAC"),
        (NodeOptions(dns=("dns.example",)), "DNS"),
        (NodeOptions(static="10.0.0.300/24", gateway="10.0.0.1"), "invalid"),
    ],
)
def test_invalid_options(options, message):
    with pytest.raises(ValidationError) as exc:
        options.check()
    assert message in str(exc.value)


@pytest.fixture(scope="module")
def talos_secrets(tmp_path_factory):
    if shutil.which("talosctl") is None:
        pytest.skip("missing tools: talosctl")
    path = tmp_path_factory.mktemp("talos") / "secrets.yaml"
    subprocess.run(["talosctl", "gen", "secrets", "-o", str(path)], check=True, capture_output=True)
    return path


@need("talosctl")
@pytest.mark.parametrize(
    "options",
    [
        NodeOptions(),
        NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1"),
        NodeOptions(
            static="10.0.0.10/24",
            gateway="10.0.0.1",
            mac="aa:bb:cc:dd:ee:ff",
            dns=("1.1.1.1", "9.9.9.9"),
        ),
        NodeOptions(disk_selector='disk.transport == "nvme"', public_ingress=True),
    ],
)
def test_patches_validate_with_talosctl(tmp_path, talos_secrets, options):
    patch = tmp_path / "node.yaml"
    patch.write_text(render_node_patch("cp-01", options))
    out = tmp_path / "cp.yaml"
    subprocess.run(
        [
            "talosctl",
            "gen",
            "config",
            "demo",
            "https://10.0.0.10:6443",
            "--with-secrets",
            str(talos_secrets),
            "-t",
            "controlplane",
            "-o",
            str(out),
            "--with-docs=false",
            "--with-examples=false",
            "--install-image",
            "factory.talos.dev/metal-installer/abc:v1.14.0",
            "--config-patch",
            f"@{patch}",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["talosctl", "validate", "--config", str(out), "--mode", "metal"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hostname: cp-01" in out.read_text()
    assert "metal-installer/abc:v1.14.0" in out.read_text()


def test_add_node_writes_patch_and_settings(empty_repo):
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions())
    assert warnings == []
    settings = empty_repo.load_settings()
    assert settings.nodes["w-01"] == NodeSpec(ip="10.0.0.11", role="worker")
    assert "hostname: w-01" in node_patch_path(empty_repo, "w-01").read_text()


def test_add_node_new_platform_copies_cluster_schematic(empty_repo):
    settings = empty_repo.load_settings()
    settings.schematics["metal"].extensions = ["siderolabs/intel-ucode"]
    settings.schematics["metal"].kernel_args = ["console=ttyS0"]
    empty_repo.write_settings(settings)
    warnings = add_node(
        empty_repo,
        "h-01",
        NodeSpec(ip="10.0.0.20", role="worker", platform="hcloud"),
        NodeOptions(),
    )
    assert warnings == [
        (
            "platform 'hcloud' is new: copied extensions from 'metal' (siderolabs/intel-ucode); "
            "adjust with talkub image add/remove --platform hcloud"
        )
    ]
    hcloud = empty_repo.load_settings().schematics["hcloud"]
    assert hcloud == SchematicSpec(extensions=["siderolabs/intel-ucode"], kernel_args=["console=ttyS0"])
    # a second node on the now-known platform neither copies nor warns
    assert (
        add_node(
            empty_repo,
            "h-02",
            NodeSpec(ip="10.0.0.21", role="worker", platform="hcloud"),
            NodeOptions(),
        )
        == []
    )


def test_add_node_new_platform_without_extensions_says_none(empty_repo):
    warnings = add_node(empty_repo, "h-01", NodeSpec(ip="10.0.0.20", platform="hcloud"), NodeOptions())
    assert warnings == [
        (
            "platform 'hcloud' is new: copied extensions from 'metal' (none); "
            "adjust with talkub image add/remove --platform hcloud"
        )
    ]


def test_add_node_rejects_duplicates_and_bad_input(empty_repo):
    with pytest.raises(ValidationError):
        add_node(empty_repo, "cp-01", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "Bad_Name", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "n-02", NodeSpec(ip="10.0.0.12", role="master"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(
            empty_repo,
            "n-03",
            NodeSpec(ip="10.0.0.13"),
            NodeOptions(static="10.0.0.13/24", gateway="10.9.9.9"),
        )
    assert set(empty_repo.load_settings().nodes) == {"cp-01"}
    assert not (empty_repo.talos_dir / "nodes").exists()


def test_public_ingress_without_firewall_warns(empty_repo):
    warnings = add_node(
        empty_repo,
        "w-01",
        NodeSpec(ip="10.0.0.11", role="worker"),
        NodeOptions(public_ingress=True),
    )
    assert any("firewall" in w and "talkub tailscale lockdown" in w for w in warnings)


def test_static_address_differs_from_ip_without_mgmt_warns(empty_repo):
    options = NodeOptions(static="10.0.0.50/24", gateway="10.0.0.1", mac="aa:bb:cc:dd:ee:ff")
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="192.168.1.50", role="worker"), options)
    assert warnings == ["after install the node is reachable at 10.0.0.50; add --mgmt 10.0.0.50 so plan/apply use it"]
    same = NodeOptions(static="10.0.0.51/24", gateway="10.0.0.1", mac="aa:bb:cc:dd:ee:ff")
    assert add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.51", role="worker"), same) == []
    with_mgmt = NodeSpec(ip="192.168.1.52", role="worker", mgmt="10.0.0.52")
    assert (
        add_node(
            empty_repo,
            "w-03",
            with_mgmt,
            NodeOptions(static="10.0.0.52/24", gateway="10.0.0.1", mac="aa:bb:cc:dd:ee:ff"),
        )
        == []
    )


@pytest.mark.parametrize(
    "spec, flag",
    [
        (NodeSpec(ip=""), "--ip"),
        (NodeSpec(ip="-n"), "--ip"),
        (NodeSpec(ip="10.0.0.1 10.0.0.2"), "--ip"),
        (NodeSpec(ip="10.0.0.11/24"), "--ip"),
        (NodeSpec(ip="10.0.0.11", mgmt=""), "--mgmt"),
        (NodeSpec(ip="10.0.0.11", mgmt="--talosconfig"), "--mgmt"),
        (NodeSpec(ip="10.0.0.11", mgmt="w-01.tail\tts.net"), "--mgmt"),
    ],
)
def test_add_node_rejects_bad_addresses(empty_repo, spec, flag):
    with pytest.raises(ValidationError) as exc:
        add_node(empty_repo, "w-01", spec, NodeOptions())
    assert flag in str(exc.value)
    assert "w-01" not in empty_repo.load_settings().nodes


def test_add_node_after_lockdown_uses_tailnet_mgmt(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    empty_repo.write_settings(settings)
    add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions())
    assert empty_repo.load_settings().nodes["w-01"].mgmt is None  # enabled, not locked down
    tailscale.firewall_patch_path(empty_repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(empty_repo).write_text("x")
    add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.12", role="worker"), NodeOptions())
    add_node(
        empty_repo,
        "w-03",
        NodeSpec(ip="10.0.0.13", role="worker", mgmt="w3.lan"),
        NodeOptions(),
    )
    nodes = empty_repo.load_settings().nodes
    assert nodes["w-02"].mgmt == "w-02.t.ts.net"
    assert nodes["w-03"].mgmt == "w3.lan"
    assert "subnet: 10.0.0.12/32" in tailscale.firewall_patch_path(empty_repo).read_text()


def test_add_node_after_lockdown_use_ips_keeps_lan_ip_and_warns(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", use_ips=True)
    empty_repo.write_settings(settings)
    tailscale.firewall_patch_path(empty_repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(empty_repo).write_text("x")
    warnings = add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.12", role="worker"), NodeOptions())
    node = empty_repo.load_settings().nodes["w-02"]
    assert node.mgmt is None  # keeps the LAN ip (mgmt_address falls back to it)
    assert node.ip == "10.0.0.12"
    assert any("w-02" in w and "tailnet IP" in w for w in warnings)
    # explicit --mgmt is still honored; only the firewall rewrite is reported
    warnings = add_node(
        empty_repo,
        "w-03",
        NodeSpec(ip="10.0.0.13", role="worker", mgmt="w3.lan"),
        NodeOptions(),
    )
    assert [w for w in warnings if "firewall.yaml" not in w] == []
    assert empty_repo.load_settings().nodes["w-03"].mgmt == "w3.lan"


def test_add_node_after_lockdown_rewrites_the_firewall(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    empty_repo.write_settings(settings)
    # enabled, not locked down: no firewall is created
    assert add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions()) == []
    assert not tailscale.firewall_patch_path(empty_repo).exists()
    tailscale.write_firewall_patch(empty_repo, empty_repo.load_settings())
    warnings = add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.12", role="worker"), NodeOptions())
    text = tailscale.firewall_patch_path(empty_repo).read_text()
    assert "subnet: 10.0.0.12/32" in text and "subnet: 10.0.0.11/32" in text
    assert len(warnings) == 1
    assert "10.0.0.12" in warnings[0] and "talkub apply" in warnings[0]


def test_add_node_after_lockdown_needs_an_ip_address(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    empty_repo.write_settings(settings)
    tailscale.write_firewall_patch(empty_repo, settings)
    before = tailscale.firewall_patch_path(empty_repo).read_text()
    with pytest.raises(ValidationError, match="w-04"):
        add_node(empty_repo, "w-04", NodeSpec(ip="w-04.lan", role="worker"), NodeOptions())
    assert "w-04" not in empty_repo.load_settings().nodes
    assert not node_patch_path(empty_repo, "w-04").exists()
    assert tailscale.firewall_patch_path(empty_repo).read_text() == before


def test_upsert_documents_replaces_by_kind_and_name_and_keeps_the_rest(tmp_path):
    from talkub.talos.nodes import read_node_documents, upsert_documents

    path = tmp_path / "cp-01.yaml"
    path.write_text(render_node_patch("cp-01", NodeOptions(disk_selector="old", dns=("1.1.1.1",))))
    upsert_documents(
        path,
        [
            {
                "apiVersion": "v1alpha1",
                "kind": "UnattendedInstallConfig",
                "provisioning": {"diskSelector": {"match": "new"}},
            },
            {"apiVersion": "v1alpha1", "kind": "UserVolumeConfig", "name": "data"},
        ],
    )
    text = path.read_text()
    assert text.startswith("# Talos patch for node cp-01")
    assert text.count("# Talos patch for node cp-01") == 1
    docs = read_node_documents(path)
    assert [d["kind"] for d in docs] == [
        "HostnameConfig",
        "ResolverConfig",
        "UnattendedInstallConfig",
        "UserVolumeConfig",
    ]
    assert docs[2]["provisioning"]["diskSelector"]["match"] == "new"
    upsert_documents(path, [{"apiVersion": "v1alpha1", "kind": "UserVolumeConfig", "name": "data", "x": 1}])
    assert [d.get("x") for d in read_node_documents(path) if d["kind"] == "UserVolumeConfig"] == [1]


def test_upsert_documents_keeps_non_mapping_documents(tmp_path):
    from talkub.talos.nodes import read_node_documents, upsert_documents

    path = tmp_path / "cp-01.yaml"
    json6902 = [{"op": "add", "path": "/machine/network/hostname", "value": "cp-01"}]
    path.write_text(render_node_patch("cp-01") + "---\n- op: add\n  path: /machine/network/hostname\n  value: cp-01\n")
    volume = {"apiVersion": "v1alpha1", "kind": "UserVolumeConfig", "name": "data"}
    upsert_documents(path, [volume])
    assert read_node_documents(path)[-2:] == [json6902, volume]
