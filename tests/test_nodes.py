import shutil
import subprocess

import pytest

from helpers import need
from talkub import yamlio
from talkub.errors import ValidationError
from talkub.nodes import FIRST_ETHERNET, NodeOptions, add_node, node_patch_path, render_node_patch
from talkub.repo import NodeSpec


def _docs(text):
    return [d for d in yamlio.load_all(text) if d]


def test_default_patch_is_hostname_only():
    docs = _docs(render_node_patch("cp-01"))
    assert docs == [{"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "off", "hostname": "cp-01"}]


def test_static_without_mac_uses_first_ethernet_and_warns():
    options = NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1")
    assert any("--mac" in w for w in options.check())
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["LinkAliasConfig"]["selector"]["match"] == FIRST_ETHERNET
    assert kinds["LinkConfig"]["addresses"] == [{"address": "10.0.0.10/24"}]
    assert kinds["LinkConfig"]["routes"] == [{"gateway": "10.0.0.1"}]
    assert "ResolverConfig" not in kinds


def test_static_with_mac_and_dns():
    options = NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="AA:BB:CC:DD:EE:FF", dns=("1.1.1.1",))
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
        NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="aa:bb:cc:dd:ee:ff", dns=("1.1.1.1", "9.9.9.9")),
        NodeOptions(disk_selector='disk.transport == "nvme"', public_ingress=True),
    ],
)
def test_patches_validate_with_talosctl(tmp_path, talos_secrets, options):
    patch = tmp_path / "node.yaml"
    patch.write_text(render_node_patch("cp-01", options))
    out = tmp_path / "cp.yaml"
    subprocess.run(
        [
            "talosctl", "gen", "config", "demo", "https://10.0.0.10:6443",
            "--with-secrets", str(talos_secrets), "-t", "controlplane", "-o", str(out),
            "--with-docs=false", "--with-examples=false",
            "--install-image", "factory.talos.dev/metal-installer/abc:v1.14.0",
            "--config-patch", f"@{patch}",
        ],
        check=True, capture_output=True,
    )
    result = subprocess.run(["talosctl", "validate", "--config", str(out), "--mode", "metal"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hostname: cp-01" in out.read_text()
    assert "metal-installer/abc:v1.14.0" in out.read_text()


def test_add_node_writes_patch_and_settings(empty_repo):
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud"), NodeOptions())
    assert warnings == []
    settings = empty_repo.load_settings()
    assert settings.nodes["w-01"] == NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    assert "hcloud" in settings.schematics
    assert "hostname: w-01" in node_patch_path(empty_repo, "w-01").read_text()


def test_add_node_rejects_duplicates_and_bad_input(empty_repo):
    with pytest.raises(ValidationError):
        add_node(empty_repo, "cp-01", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "Bad_Name", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "n-02", NodeSpec(ip="10.0.0.12", role="master"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "n-03", NodeSpec(ip="10.0.0.13"), NodeOptions(static="10.0.0.13/24", gateway="10.9.9.9"))
    assert set(empty_repo.load_settings().nodes) == {"cp-01"}
    assert not (empty_repo.talos_dir / "nodes").exists()


def test_public_ingress_without_firewall_warns(empty_repo):
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions(public_ingress=True))
    assert any("firewall" in w for w in warnings)
