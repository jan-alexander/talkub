"""Tailscale as the only admin path: extension config, Talos firewall, client endpoints.

The cluster endpoint (used by the nodes themselves) is never changed: nodes run with
TS_ACCEPT_DNS=false and cannot resolve tailnet names. Only admin clients (talosctl,
kubectl) are pointed at the tailnet.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import images, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.lifecycle import is_bootstrapped, ordered_nodes
from talkub.repo import Repo, Settings, TailscaleSpec

EXTENSION = "siderolabs/tailscale"
TAILNET_V4 = "100.64.0.0/10"
TAILNET_V6 = "fd7a:115c:a1e0::/48"
POD_SUBNET = "10.244.0.0/16"
SERVICE_SUBNET = "10.96.0.0/12"

_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_TAILNET = re.compile(rf"{_LABEL}(\.{_LABEL})+")


def extension_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "tailscale.sops.yaml"


def firewall_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "firewall.yaml"


def is_locked_down(repo: Repo) -> bool:
    return firewall_patch_path(repo).is_file()


def validate_tailnet(tailnet: str) -> str:
    if not _TAILNET.fullmatch(tailnet):
        raise ValidationError(
            f"invalid tailnet {tailnet!r}", hint="e.g. tail1234.ts.net (Tailscale admin console → DNS)"
        )
    return tailnet


def validate_authkey(key: str) -> str:
    key = key.strip()
    if not key.startswith("tskey-"):
        raise ValidationError(
            "the Tailscale auth key must start with 'tskey-'",
            hint="create a tagged, reusable, pre-approved auth key in the Tailscale admin console",
        )
    return key


def validate_cidr(cidr: str) -> str:
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except ValueError:
        raise ValidationError(f"invalid --node-cidr {cidr!r}", hint="e.g. 10.0.0.0/24") from None


def controlplanes(settings: Settings) -> list[str]:
    return [name for name in ordered_nodes(settings, []) if settings.nodes[name].role == "controlplane"]


def default_node_cidr(settings: Settings) -> str:
    first = ordered_nodes(settings, [])[0]
    ip = settings.nodes[first].ip
    try:
        return str(ipaddress.ip_interface(f"{ip}/24").network)
    except ValueError:
        raise ValidationError(
            f"cannot derive the node network from {ip!r}", hint="pass --node-cidr, e.g. 10.0.0.0/24"
        ) from None


def admin_server(settings: Settings) -> str:
    """kubeconfig server for admins: the first control plane's management address."""
    cps = controlplanes(settings)
    if not cps:
        raise ValidationError("no controlplane node defined")
    return f"https://{settings.mgmt_address(cps[0])}:6443"


def render_extension_patch(authkey: str) -> bytes:
    doc = {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": [f"TS_AUTHKEY={authkey}", "TS_ACCEPT_DNS=false"],
    }
    return yamlio.dump(doc).encode()


def render_firewall_patch(node_cidr: str) -> str:
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
            "ingress": [{"subnet": node_cidr}, {"subnet": POD_SUBNET}, {"subnet": SERVICE_SUBNET}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "flannel-vxlan",
            "portSelector": {"ports": [4789], "protocol": "udp"},
            "ingress": [{"subnet": node_cidr}],
        },
    ]
    header = (
        "# Talos firewall written by `talkub tailscale lockdown`: from outside, the Talos and\n"
        "# Kubernetes APIs are reachable only via the tailnet; the node network keeps cluster ports.\n"
    )
    return header + "---\n".join(yamlio.dump(doc) for doc in docs)


def _load_mapping(data: bytes, what: str) -> dict:
    try:
        doc = yamlio.load(data.decode())
    except (YAMLError, UnicodeDecodeError):
        raise ValidationError(f"{what} is not valid YAML") from None
    if not isinstance(doc, Mapping):
        raise ValidationError(f"{what} is not a YAML mapping")
    return doc


def rewrite_talosconfig(data: bytes, endpoints: Sequence[str], node: str) -> bytes:
    doc = _load_mapping(data, "talosconfig")
    contexts = doc.get("contexts")
    name = doc.get("context")
    if not isinstance(contexts, Mapping) or not isinstance(contexts.get(name), Mapping):
        raise ValidationError("talosconfig has no current context")
    contexts[name]["endpoints"] = list(endpoints)
    contexts[name]["nodes"] = [node]
    return yamlio.dump(doc).encode()


def rewrite_kubeconfig_server(data: bytes, server: str) -> bytes:
    doc = _load_mapping(data, "kubeconfig")
    clusters = doc.get("clusters")
    entries = [c.get("cluster") for c in clusters if isinstance(c, Mapping)] if isinstance(clusters, list) else []
    targets = [cluster for cluster in entries if isinstance(cluster, Mapping)]
    if not targets:
        raise ValidationError("kubeconfig has no clusters to update")
    for cluster in targets:
        cluster["server"] = server
    return yamlio.dump(doc).encode()


def enable(ctx: Context, *, tailnet: str, authkey: str, node_cidr: str | None = None) -> list[str]:
    """Step 1: auth-key patch + extension in every used schematic. Addresses stay unchanged,
    so a running cluster can still be upgraded and configured via LAN."""
    repo = ctx.repo
    settings = repo.load_settings()
    if settings.tailscale is not None or extension_patch_path(repo).exists():
        raise ValidationError(
            "tailscale is already enabled",
            hint="change the auth key with `talkub sops edit talos/patches/tailscale.sops.yaml`",
        )
    validate_tailnet(tailnet)
    authkey = validate_authkey(authkey)
    cidr = validate_cidr(node_cidr) if node_cidr else default_node_cidr(settings)
    settings.tailscale = TailscaleSpec(tailnet=tailnet, node_cidr=cidr)
    for platform in images.used_platforms(settings):
        spec = settings.ensure_schematic(platform)
        if EXTENSION not in spec.extensions:
            spec.extensions = sorted([*spec.extensions, EXTENSION])
    patch = extension_patch_path(repo)
    encrypted = ctx.sops.encrypt(
        render_extension_patch(authkey), filename=patch.name, public_key=ctx.keyring.public_key()
    )
    atomic_write(patch, encrypted)
    repo.write_settings(settings)
    notes = [f"node network for the firewall rules: {cidr} (change with `talkub tailscale lockdown --node-cidr`)"]
    if is_bootstrapped(ctx):
        notes.append(
            "running cluster: `talkub upgrade talos` installs the extension, `talkub apply` configures it; "
            "once `tailscale status` lists every node, run `talkub tailscale lockdown`"
        )
    notes.append("use a tagged, reusable, pre-approved auth key and disable key expiry for these nodes")
    return notes


def lockdown(ctx: Context, *, node_cidr: str | None = None) -> list[str]:
    """Step 2: firewall patch, mgmt = <node>.<tailnet>, client configs pointed at the tailnet."""
    repo = ctx.repo
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if is_locked_down(repo):
        raise ValidationError(f"{repo.rel(firewall_patch_path(repo))} already exists", hint="already locked down")
    if node_cidr:
        settings.tailscale = TailscaleSpec(tailnet=settings.tailscale.tailnet, node_cidr=validate_cidr(node_cidr))
    tailnet = settings.tailscale.tailnet
    settings.nodes = {
        name: node if node.mgmt else dataclasses.replace(node, mgmt=f"{name}.{tailnet}")
        for name, node in settings.nodes.items()
    }
    endpoints = [settings.mgmt_address(name) for name in controlplanes(settings)]
    if not endpoints:
        raise ValidationError("no controlplane node defined")
    public = ctx.keyring.public_key()
    # Everything is computed in memory first; files are written only when all of it worked.
    talosconfig = ctx.sops.encrypt(
        rewrite_talosconfig(ctx.sops.decrypt(repo.talosconfig), endpoints, endpoints[0]),
        filename=repo.talosconfig.name, public_key=public,
    )
    kubeconfig = None
    if repo.kubeconfig.is_file():
        kubeconfig = ctx.sops.encrypt(
            rewrite_kubeconfig_server(ctx.sops.decrypt(repo.kubeconfig), admin_server(settings)),
            filename=repo.kubeconfig.name, public_key=public,
        )
    atomic_write(firewall_patch_path(repo), render_firewall_patch(settings.tailscale.node_cidr).encode())
    atomic_write(repo.talosconfig, talosconfig)
    if kubeconfig is not None:
        atomic_write(repo.kubeconfig, kubeconfig)
    repo.write_settings(settings)
    return [
        f"talkub now reaches the nodes via: {', '.join(settings.mgmt_address(n) for n in settings.nodes)}",
        "run `talkub apply` to activate the firewall; afterwards 50000/6443 are open only to the tailnet "
        f"and the node network {settings.tailscale.node_cidr}",
    ]
