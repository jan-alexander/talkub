"""CoreDNS renderers: the Talos patch that disables Talos' own CoreDNS manifest, and the
Helm values for the `coredns` chart that replaces it, managed by Argo CD.

The default `servers` block mirrors Talos 1.14's own Corefile exactly, so moving from Talos
to the chart does not change DNS behaviour; `add_stub_zone` extends it with a tailnet stub
zone once the Tailscale operator's DNSConfig pins the tailnet nameserver's ClusterIP."""

from __future__ import annotations

from talkub import yamlio

CLUSTER_DNS_IP = "10.96.0.10"
TS_NAMESERVER_IP = "10.96.0.53"
DEPLOYMENT = "coredns-talkub"
RELEASE = "coredns"
NAMESPACE = "kube-system"

_VALUES_HEADER = "# Helm values for CoreDNS (chart coredns). Used by talkub and by Argo CD.\n"

TALOS_DEFAULT_PLUGINS = [
    {"name": "errors"},
    {"name": "health", "configBlock": "lameduck 5s"},
    {"name": "ready"},
    {"name": "log", "parameters": ".", "configBlock": "class error"},
    {"name": "prometheus", "parameters": ":9153"},
    {
        "name": "kubernetes",
        "parameters": "cluster.local in-addr.arpa ip6.arpa",
        "configBlock": "pods insecure\nfallthrough in-addr.arpa ip6.arpa\nttl 30",
    },
    {"name": "forward", "parameters": ". /etc/resolv.conf", "configBlock": "max_concurrent 1000"},
    {
        "name": "cache",
        "parameters": 30,
        "configBlock": "disable success cluster.local\ndisable denial cluster.local",
    },
    {"name": "loop"},
    {"name": "reload"},
    {"name": "loadbalance"},
]


def _stub_server(tailnet: str) -> dict:
    return {
        "zones": [{"zone": tailnet}],
        "port": 53,
        "plugins": [
            {"name": "errors"},
            {"name": "cache", "parameters": 30},
            {"name": "forward", "parameters": f". {TS_NAMESERVER_IP}"},
        ],
    }


def talos_patch() -> str:
    doc = {"apiVersion": "v1alpha1", "kind": "KubeCoreDNSConfig", "enabled": False}
    return yamlio.dump(doc)


def values(tailnet: str | None = None) -> str:
    servers = [{"zones": [{"zone": "."}], "port": 53, "plugins": TALOS_DEFAULT_PLUGINS}]
    if tailnet is not None:
        servers.append(_stub_server(tailnet))
    doc = {
        "fullnameOverride": DEPLOYMENT,
        "k8sAppLabelOverride": "kube-dns",
        "replicaCount": 2,
        "priorityClassName": "system-cluster-critical",
        "tolerations": [
            {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
        ],
        "service": {"name": "kube-dns", "clusterIP": CLUSTER_DNS_IP, "selector": {"k8s-app": "kube-dns"}},
        "servers": servers,
    }
    return _VALUES_HEADER + yamlio.dump(doc)


def add_stub_zone(values_text: str, tailnet: str) -> tuple[str, bool]:
    """Add the tailnet stub zone to an existing values document (round-trip via yamlio, so
    comments are kept). Unchanged when a server already serves that zone."""
    doc = yamlio.load(values_text)
    servers = doc.setdefault("servers", [])
    for server in servers:
        zones = server.get("zones") or []
        if any(zone.get("zone") == tailnet for zone in zones):
            return values_text, False
    servers.append(_stub_server(tailnet))
    return yamlio.dump(doc), True
