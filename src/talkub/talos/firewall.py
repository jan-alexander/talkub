"""The Talos firewall patch: default-block, admin ports from the tailnet, cluster ports from the
nodes' own addresses. Written by `tailscale lockdown`, rewritten by `tailscale firewall` and
`node add`; both import this module, which depends only on config and core."""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from pathlib import Path

from talkub.config import Repo, Settings
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write

TAILNET_V4 = "100.64.0.0/10"
TAILNET_V6 = "fd7a:115c:a1e0::/48"
POD_SUBNET = "10.244.0.0/16"
SERVICE_SUBNET = "10.96.0.0/12"


def firewall_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "firewall.yaml"


def is_locked_down(repo: Repo) -> bool:
    return firewall_patch_path(repo).is_file()


def node_sources(settings: Settings) -> list[str]:
    """One host subnet per node (`/32`, `/128`) for the cluster-internal firewall rules."""
    sources = []
    for name, node in settings.nodes.items():
        try:
            address = ipaddress.ip_address(node.ip)
        except ValueError:
            raise ValidationError(
                f"node '{name}' has no IP address (ip: {node.ip!r}); the firewall rules need one",
                hint=f"set nodes.{name}.ip in .talkub.yaml to the node's IP address (mgmt may stay a name)",
            ) from None
        sources.append(f"{address}/{address.max_prefixlen}")
    return sources


FIREWALL_HEADER = (
    "# Talos firewall written by `talkub tailscale lockdown` and rewritten by `talkub tailscale firewall`\n"
    "# and `talkub node add`: from outside, the Talos and Kubernetes APIs are reachable only via the\n"
    "# tailnet; cluster ports are open only to the nodes' own addresses. Edits here are overwritten;\n"
    "# put extra rules into talos/nodes/<node>.yaml or another patch.\n"
)


def render_firewall_patch(sources: Sequence[str]) -> str:
    node_rules = [{"subnet": s} for s in sources]
    docs = [
        {"apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block"},
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "admin-via-tailnet",
            "portSelector": {"ports": [50000, 6443], "protocol": "tcp"},
            "ingress": [{"subnet": TAILNET_V4}, {"subnet": TAILNET_V6}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "cluster-internal",
            "portSelector": {"ports": [50000, 50001, 6443, "2379-2380", 10250], "protocol": "tcp"},
            "ingress": [*node_rules, {"subnet": POD_SUBNET}, {"subnet": SERVICE_SUBNET}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "flannel-vxlan",
            "portSelector": {"ports": [4789], "protocol": "udp"},
            "ingress": node_rules,
        },
    ]
    return FIREWALL_HEADER + "---\n".join(yamlio.dump(doc) for doc in docs)


def write_firewall_patch(repo: Repo, settings: Settings) -> list[str]:
    """Write talos/patches/firewall.yaml for the nodes in `settings`; returns their subnets."""
    sources = node_sources(settings)
    atomic_write(firewall_patch_path(repo), render_firewall_patch(sources).encode())
    return sources
