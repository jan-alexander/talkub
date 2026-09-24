import shutil
import subprocess

import pytest

from helpers import need, sample_settings
from talkub import tailscale, yamlio
from talkub.errors import ValidationError
from talkub.repo import NodeSpec

TALOSCONFIG = b"""context: test
contexts:
    test:
        endpoints:
            - 10.0.0.10
        nodes:
            - 10.0.0.10
        ca: Y2E=
        crt: Y3J0
        key: a2V5
"""

KUBECONFIG = b"""apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: https://10.0.0.10:6443
    certificate-authority-data: Y2E=
users: []
"""


def test_validators():
    assert tailscale.validate_tailnet("tail1234.ts.net") == "tail1234.ts.net"
    assert tailscale.validate_authkey("  tskey-auth-abc  \n") == "tskey-auth-abc"
    assert tailscale.validate_cidr("10.0.0.0/24") == "10.0.0.0/24"
    assert tailscale.validate_cidr("10.0.0.7/24") == "10.0.0.0/24"
    for bad in ("tail", "-x.ts.net", "Tail.TS.net", "a b.ts.net"):
        with pytest.raises(ValidationError):
            tailscale.validate_tailnet(bad)
    with pytest.raises(ValidationError):
        tailscale.validate_authkey("abc")
    with pytest.raises(ValidationError):
        tailscale.validate_cidr("10.0.0.0/33")


def test_default_node_cidr():
    settings = sample_settings()
    assert tailscale.default_node_cidr(settings) == "10.0.0.0/24"
    settings.nodes = {"cp-01": NodeSpec(ip="cp.example.com")}
    with pytest.raises(ValidationError) as exc:
        tailscale.default_node_cidr(settings)
    assert "--node-cidr" in exc.value.hint


def test_admin_server_uses_first_controlplane_mgmt():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.t.ts.net")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    assert tailscale.controlplanes(settings) == ["cp-01"]
    assert tailscale.admin_server(settings) == "https://cp-01.t.ts.net:6443"


def test_extension_patch():
    doc = yamlio.load(tailscale.render_extension_patch("tskey-auth-abc").decode())
    assert doc == {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": ["TS_AUTHKEY=tskey-auth-abc", "TS_ACCEPT_DNS=false"],
    }


def test_firewall_patch():
    docs = [d for d in yamlio.load_all(tailscale.render_firewall_patch("10.0.0.0/24")) if d]
    kinds = [(d["kind"], d.get("name")) for d in docs]
    assert kinds == [
        ("NetworkDefaultActionConfig", None),
        ("NetworkRuleConfig", "admin-via-tailnet"),
        ("NetworkRuleConfig", "cluster-internal"),
        ("NetworkRuleConfig", "flannel-vxlan"),
    ]
    assert docs[0]["ingress"] == "block"
    assert docs[1]["portSelector"] == {"ports": [50000, 6443], "protocol": "tcp"}
    assert docs[1]["ingress"] == [{"subnet": "100.64.0.0/10"}, {"subnet": "fd7a:115c:a1e0::/48"}]
    assert docs[2]["portSelector"]["ports"] == [50000, 50001, 6443, "2379-2380", 10250]
    assert docs[2]["ingress"] == [{"subnet": "10.0.0.0/24"}, {"subnet": "10.244.0.0/16"}, {"subnet": "10.96.0.0/12"}]
    assert docs[3]["portSelector"] == {"ports": [4789], "protocol": "udp"}


def test_rewrite_talosconfig():
    doc = yamlio.load(tailscale.rewrite_talosconfig(TALOSCONFIG, ["cp-01.t.ts.net", "cp-02.t.ts.net"], "cp-01.t.ts.net").decode())
    context = doc["contexts"]["test"]
    assert list(context["endpoints"]) == ["cp-01.t.ts.net", "cp-02.t.ts.net"]
    assert list(context["nodes"]) == ["cp-01.t.ts.net"]
    assert context["key"] == "a2V5"
    with pytest.raises(ValidationError):
        tailscale.rewrite_talosconfig(b"context: nope\ncontexts: {}\n", ["x"], "x")


def test_rewrite_kubeconfig_server():
    doc = yamlio.load(tailscale.rewrite_kubeconfig_server(KUBECONFIG, "https://cp-01.t.ts.net:6443").decode())
    assert doc["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
    assert doc["clusters"][0]["cluster"]["certificate-authority-data"] == "Y2E="
    with pytest.raises(ValidationError):
        tailscale.rewrite_kubeconfig_server(b"kind: Config\nclusters: []\n", "https://x:6443")


@need("talosctl")
def test_patches_validate_with_talosctl(tmp_path):
    secrets = tmp_path / "secrets.yaml"
    subprocess.run(["talosctl", "gen", "secrets", "-o", str(secrets)], check=True, capture_output=True)
    (tmp_path / "ts.yaml").write_bytes(tailscale.render_extension_patch("tskey-auth-abc"))
    (tmp_path / "fw.yaml").write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    out = tmp_path / "cp.yaml"
    subprocess.run(
        [
            "talosctl", "gen", "config", "demo", "https://10.0.0.10:6443", "--with-secrets", str(secrets),
            "-t", "controlplane", "-o", str(out), "--with-docs=false", "--with-examples=false",
            "--config-patch", f"@{tmp_path / 'ts.yaml'}", "--config-patch", f"@{tmp_path / 'fw.yaml'}",
        ],
        check=True, capture_output=True,
    )
    result = subprocess.run(["talosctl", "validate", "--config", str(out), "--mode", "metal"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_is_locked_down(empty_repo):
    assert tailscale.is_locked_down(empty_repo) is False
    tailscale.firewall_patch_path(empty_repo).parent.mkdir(parents=True)
    tailscale.firewall_patch_path(empty_repo).write_text("x")
    assert tailscale.is_locked_down(empty_repo) is True
