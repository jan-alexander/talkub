import subprocess

import pytest

from helpers import need
from talkub import coredns, yamlio

TAILNET = "tail1234.ts.net"


def test_talos_patch_parses_to_three_keys():
    doc = yamlio.load(coredns.talos_patch())
    assert doc == {"apiVersion": "v1alpha1", "kind": "KubeCoreDNSConfig", "enabled": False}


def test_values_without_tailnet_has_leading_comment_and_one_server():
    text = coredns.values()
    assert text.startswith("# Helm values for CoreDNS (chart coredns). Used by talkub and by Argo CD.\n")
    doc = yamlio.load(text)
    assert doc["fullnameOverride"] == "coredns-talkub"
    assert doc["k8sAppLabelOverride"] == "kube-dns"
    assert doc["replicaCount"] == 2
    assert doc["priorityClassName"] == "system-cluster-critical"
    assert doc["tolerations"] == [
        {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
    ]
    assert doc["service"] == {"name": "kube-dns", "clusterIP": "10.96.0.10", "selector": {"k8s-app": "kube-dns"}}
    assert len(doc["servers"]) == 1
    default_server = doc["servers"][0]
    assert default_server["zones"] == [{"zone": ".", "use_tcp": True}]
    assert doc["serviceAccount"] == {"create": True}
    assert default_server["port"] == 53
    names = [p["name"] for p in default_server["plugins"]]
    assert names == [
        "errors", "health", "ready", "log", "prometheus", "kubernetes", "forward", "cache", "loop",
        "reload", "loadbalance",
    ]
    forward = next(p for p in default_server["plugins"] if p["name"] == "forward")
    assert forward["parameters"] == ". /etc/resolv.conf"
    health = next(p for p in default_server["plugins"] if p["name"] == "health")
    assert health["configBlock"] == "lameduck 5s"


def test_values_with_tailnet_adds_stub_block_exactly_once():
    text = coredns.values(tailnet=TAILNET)
    doc = yamlio.load(text)
    assert len(doc["servers"]) == 2
    stub = doc["servers"][1]
    assert stub["zones"] == [{"zone": TAILNET, "use_tcp": True}]
    assert stub["port"] == 53
    names = [p["name"] for p in stub["plugins"]]
    assert names == ["errors", "cache", "forward"]
    forward = next(p for p in stub["plugins"] if p["name"] == "forward")
    assert forward["parameters"] == f". {coredns.TS_NAMESERVER_IP}"
    assert coredns.TS_NAMESERVER_IP == "10.96.0.53"


def test_add_stub_zone_adds_server_and_keeps_comment():
    base = coredns.values()
    text, changed = coredns.add_stub_zone(base, TAILNET)
    assert changed is True
    assert text.startswith("# Helm values for CoreDNS (chart coredns). Used by talkub and by Argo CD.\n")
    doc = yamlio.load(text)
    assert len(doc["servers"]) == 2
    assert doc["servers"][1]["zones"] == [{"zone": TAILNET, "use_tcp": True}]


def test_add_stub_zone_idempotent():
    base = coredns.values()
    once, changed1 = coredns.add_stub_zone(base, TAILNET)
    twice, changed2 = coredns.add_stub_zone(once, TAILNET)
    assert changed1 is True
    assert changed2 is False
    assert yamlio.load(twice) == yamlio.load(once)


@need("helm")
def test_real_chart_renders_expected_shape(tmp_path):
    values_path = tmp_path / "values.yaml"
    values_path.write_text(coredns.values())
    argv = [
        "helm", "template", "coredns", "coredns", "--repo", "https://coredns.github.io/helm",
        "--version", "1.47.1", "-f", str(values_path),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as err:
        pytest.skip(f"helm template failed (network?): {err}")
    if result.returncode != 0:
        pytest.skip(f"helm template failed (network?): {result.stderr.decode(errors='replace')}")
    rendered = result.stdout.decode()
    assert "lameduck 5s" in rendered
    assert "forward . /etc/resolv.conf" in rendered

    docs = [d for d in yamlio.load_all(rendered) if d]
    service = next(d for d in docs if d.get("kind") == "Service")
    assert service["spec"]["clusterIP"] == "10.96.0.10"
    assert service["spec"]["selector"] == {"k8s-app": "kube-dns"}
    ports = {(p["name"], p["port"], p["protocol"]) for p in service["spec"]["ports"]}
    assert len(service["spec"]["ports"]) == 2
    assert ports == {("udp-53", 53, "UDP"), ("tcp-53", 53, "TCP")}
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    assert deployment["metadata"]["name"] == "coredns-talkub"
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] not in ("default", None)


@need("helm")
def test_real_chart_renders_stub_forward(tmp_path):
    values_path = tmp_path / "values.yaml"
    values_path.write_text(coredns.values(tailnet=TAILNET))
    argv = [
        "helm", "template", "coredns", "coredns", "--repo", "https://coredns.github.io/helm",
        "--version", "1.47.1", "-f", str(values_path),
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as err:
        pytest.skip(f"helm template failed (network?): {err}")
    if result.returncode != 0:
        pytest.skip(f"helm template failed (network?): {result.stderr.decode(errors='replace')}")
    rendered = result.stdout.decode()
    assert "forward . 10.96.0.53" in rendered
