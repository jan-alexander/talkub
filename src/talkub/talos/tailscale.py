"""Tailscale on the Talos hosts: the only admin path (extension config, Talos firewall, client
endpoints). The in-cluster operator is talkub.gitops.tailscale_operator.

The cluster endpoint (used by the nodes themselves) is never changed: nodes run with
TS_ACCEPT_DNS=false and cannot resolve tailnet names. Only admin clients (talosctl,
kubectl) are pointed at the tailnet.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub.config import Repo, Settings, TailscaleSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos import images, inspect
from talkub.talos.client import talos_session
from talkub.talos.firewall import (
    TAILNET_V4,
    TAILNET_V6,
    firewall_patch_path,
    is_locked_down,
    node_sources,
    render_firewall_patch,
    write_firewall_patch,
)
from talkub.talos.lifecycle import is_bootstrapped, ordered_nodes
from talkub.talos.nodes import ensure_node_ip_pins

EXTENSION = "siderolabs/tailscale"
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_TAILNET = re.compile(rf"{_LABEL}(\.{_LABEL})+")


def extension_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "tailscale.sops.yaml"


def validate_tailnet(tailnet: str) -> str:
    if not _TAILNET.fullmatch(tailnet):
        raise ValidationError(
            f"invalid tailnet {tailnet!r}",
            hint="e.g. tail1234.ts.net (Tailscale admin console → DNS)",
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


def controlplanes(settings: Settings) -> list[str]:
    return [name for name in ordered_nodes(settings, []) if settings.nodes[name].role == "controlplane"]


def is_tailnet_address(address: str, tailnet: str) -> bool:
    """Whether `address` reaches a node via the tailnet: a `<name>.<tailnet>` MagicDNS name,
    or an IP inside the tailnet ranges (used with `lockdown --use-ips`)."""
    if address.endswith(f".{tailnet}"):
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip in ipaddress.ip_network(TAILNET_V4) or ip in ipaddress.ip_network(TAILNET_V6)


def _tailnet_ips_for_bare_nodes(ctx: Context, settings: Settings) -> dict[str, str]:
    """Nodes without an explicit `mgmt` -> their tailnet IPv4, queried via `get addresses` at
    each node's current (LAN/public) management address."""
    targets = [name for name, node in settings.nodes.items() if not node.mgmt]
    found: dict[str, str] = {}
    missing: list[str] = []
    if targets:
        with talos_session(ctx) as session:
            for name in targets:
                address = settings.mgmt_address(name)
                result = session.run(address, ["get", "addresses"], check=False)
                if result.returncode != 0:
                    missing.append(f"{name} (talosctl exited {result.returncode}: {_first_error_line(result)})")
                    continue
                ip = inspect.tailnet_ipv4(result.stdout.decode(errors="replace"))
                if ip is None:
                    missing.append(name)
                else:
                    found[name] = ip
    if missing:
        raise ValidationError(
            f"no tailnet IP found for: {', '.join(missing)}",
            hint="finish `talkub apply`/`upgrade talos` after `tailscale enable`, check `tailscale status`",
        )
    return found


def _first_error_line(result: subprocess.CompletedProcess[bytes]) -> str:
    stderr = result.stderr.decode(errors="replace").strip().splitlines()
    return stderr[0] if stderr else "no output"


def unreachable_via_tailnet(ctx: Context, settings: Settings) -> list[str]:
    """`name (mgmt): talosctl error` of the nodes that do not answer `talosctl version` at their
    management address, using the current talosconfig (the client certificates do not change)."""
    failed = []
    with talos_session(ctx) as session:
        for name in settings.nodes:
            address = settings.mgmt_address(name)
            result = session.run(address, ["version"], check=False)
            if result.returncode != 0:
                failed.append(f"{name} ({address}): {_first_error_line(result)}")
    return failed


# talosctl resolves names with Go's resolver, which can miss MagicDNS names the OS resolves
# (seen on macOS); the nodes' tailnet IPs avoid DNS entirely.
_RESOLVER_ERRORS = ("name resolver error", "no such host")


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


def firewall(repo: Repo) -> list[str]:
    """Rewrite the firewall patch from the current nodes (after changing a node's ip, or once
    after upgrading from a talkub that allowed a whole node network)."""
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if not is_locked_down(repo):
        raise ValidationError(
            f"{repo.rel(firewall_patch_path(repo))} does not exist",
            hint="run `talkub tailscale lockdown` first; it writes the firewall",
        )
    sources = write_firewall_patch(repo, settings)
    pinned, pin_warnings = ensure_node_ip_pins(repo, settings)
    return [
        *_pin_notes(pinned, pin_warnings),
        f"rewrote {repo.rel(firewall_patch_path(repo))}: cluster ports open to {', '.join(sources)}",
        "run `talkub apply` to activate it on the nodes",
    ]


def _pin_notes(pinned: Sequence[str], warnings: Sequence[str]) -> list[str]:
    notes = list(warnings)
    if pinned:
        notes.insert(
            0,
            f"pinned the Kubernetes node IP of {', '.join(pinned)} to its ip (KubeNodeConfig in talos/nodes/): "
            "API server endpoint and kubelet no longer depend on tailscale0",
        )
    return notes


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


def check_can_enable(repo: Repo) -> Settings:
    """Refuse `enable` when already enabled or half-enabled; called before the auth key is asked for."""
    settings = repo.load_settings()
    patch = extension_patch_path(repo)
    if settings.tailscale is not None:
        raise ValidationError(
            "tailscale is already enabled",
            hint="change the auth key with `talkub sops edit talos/patches/tailscale.sops.yaml`",
        )
    if patch.exists():
        raise ValidationError(
            f"{repo.rel(patch)} exists but tailscale is not enabled in .talkub.yaml",
            hint="a previous `tailscale enable` was interrupted; delete that file and run it again",
        )
    return settings


def enable(ctx: Context, *, tailnet: str, authkey: str) -> list[str]:
    """Step 1: auth-key patch + extension in every used schematic. Addresses stay unchanged,
    so a running cluster can still be upgraded and configured via LAN."""
    repo = ctx.repo
    settings = check_can_enable(repo)
    patch = extension_patch_path(repo)
    validate_tailnet(tailnet)
    authkey = validate_authkey(authkey)
    settings.tailscale = TailscaleSpec(tailnet=tailnet)
    for platform in images.used_platforms(settings):
        spec = settings.ensure_schematic(platform)
        if EXTENSION not in spec.extensions:
            spec.extensions = sorted([*spec.extensions, EXTENSION])
    encrypted = ctx.sops.encrypt(
        render_extension_patch(authkey),
        filename=patch.name,
        public_key=ctx.keyring.public_key(),
    )
    atomic_write(patch, encrypted)
    repo.write_settings(settings)
    notes = []
    if is_bootstrapped(ctx):
        notes.append(
            "running cluster: `talkub apply` configures the extension (Talos accepts it before the "
            "extension exists), then `talkub upgrade talos` installs it; once `tailscale status` lists "
            "every node, run `talkub tailscale lockdown` (`--use-ips` if your client has no MagicDNS)"
        )
    notes.append("use a tagged, reusable, pre-approved auth key and disable key expiry for these nodes")
    return notes


def lockdown(ctx: Context, *, use_ips: bool = False) -> list[str]:
    """Step 2: firewall patch, mgmt = <node>.<tailnet> (or the node's tailnet IP with
    `use_ips`, for clients without MagicDNS), client configs pointed at the tailnet."""
    repo = ctx.repo
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if is_locked_down(repo):
        raise ValidationError(
            f"{repo.rel(firewall_patch_path(repo))} already exists",
            hint="already locked down",
        )
    settings.tailscale = TailscaleSpec(tailnet=settings.tailscale.tailnet, use_ips=use_ips)
    sources = node_sources(settings)  # fails before anything is written
    if not repo.talosconfig.is_file():
        raise ValidationError(
            f"{repo.rel(repo.talosconfig)} does not exist",
            hint="it is created by `talkub init`",
        )
    tailnet = settings.tailscale.tailnet
    if use_ips:
        ips = _tailnet_ips_for_bare_nodes(ctx, settings)
        settings.nodes = {
            name: node if node.mgmt else dataclasses.replace(node, mgmt=ips[name])
            for name, node in settings.nodes.items()
        }
    else:
        settings.nodes = {
            name: node if node.mgmt else dataclasses.replace(node, mgmt=f"{name}.{tailnet}")
            for name, node in settings.nodes.items()
        }
    endpoints = [settings.mgmt_address(name) for name in controlplanes(settings)]
    if not endpoints:
        raise ValidationError("no controlplane node defined")
    if is_bootstrapped(ctx):
        # After `apply` the nodes answer only via the tailnet: refuse unless they already do.
        unreachable = unreachable_via_tailnet(ctx, settings)
        if unreachable:
            if not use_ips and any(e in line for line in unreachable for e in _RESOLVER_ERRORS):
                hint = (
                    "talosctl cannot resolve the MagicDNS names on this machine, even if `tailscale status` "
                    "and the OS resolver can; run `talkub tailscale lockdown --use-ips` to use the tailnet IPs"
                )
            else:
                hint = (
                    "finish `talkub upgrade talos` and `talkub apply` after `tailscale enable`, "
                    "check `tailscale status`, then run lockdown again"
                )
            raise ValidationError(f"not reachable via the tailnet yet: {'; '.join(unreachable)}", hint=hint)
    public = ctx.keyring.public_key()
    # Everything is computed in memory first; files are written only once all of it worked,
    # and the firewall.yaml marker (lockdown ⇔ its presence) is written last, so a failure
    # midway leaves a retryable, not-locked-down repo.
    talosconfig = ctx.sops.encrypt(
        rewrite_talosconfig(ctx.sops.decrypt(repo.talosconfig), endpoints, endpoints[0]),
        filename=repo.talosconfig.name,
        public_key=public,
    )
    kubeconfig = None
    if repo.kubeconfig.is_file():
        kubeconfig = ctx.sops.encrypt(
            rewrite_kubeconfig_server(ctx.sops.decrypt(repo.kubeconfig), admin_server(settings)),
            filename=repo.kubeconfig.name,
            public_key=public,
        )
    atomic_write(repo.talosconfig, talosconfig)
    if kubeconfig is not None:
        atomic_write(repo.kubeconfig, kubeconfig)
    repo.write_settings(settings)
    pinned, pin_warnings = ensure_node_ip_pins(repo, settings)
    atomic_write(firewall_patch_path(repo), render_firewall_patch(sources).encode())
    notes = [
        *_pin_notes(pinned, pin_warnings),
        f"talkub now reaches the nodes via: {', '.join(settings.mgmt_address(n) for n in settings.nodes)}",
        (
            "run `talkub apply` to activate the firewall; afterwards 50000/6443 are open only to the tailnet "
            f"and cluster ports only to the nodes' own addresses ({', '.join(sources)})"
        ),
    ]
    non_tailnet = [
        f"{name} ({node.mgmt})" for name, node in settings.nodes.items() if not is_tailnet_address(node.mgmt, tailnet)
    ]
    if non_tailnet:
        notes.append(
            "these nodes keep a non-tailnet mgmt and are reachable only from the other nodes after "
            f"`apply`: {', '.join(non_tailnet)}"
        )
    return notes
