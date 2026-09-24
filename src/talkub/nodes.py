"""Nodes: the per-node Talos patch and the `.talkub.yaml` entry."""

from __future__ import annotations

import dataclasses
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import NodeSpec, Repo, SchematicSpec, Settings

LINK = "net0"
# ARPHRD_ETHER and no link kind (not a VLAN, bond or bridge): the physical Ethernet port.
FIRST_ETHERNET = 'link.type == 1 && link.kind == ""'

_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


@dataclass(frozen=True)
class NodeOptions:
    static: str | None = None  # address with prefix, e.g. 10.0.0.10/24; None = DHCP
    gateway: str | None = None
    mac: str | None = None
    dns: tuple[str, ...] = ()
    disk_selector: str | None = None  # CEL, e.g. disk.transport == "nvme"
    public_ingress: bool = False

    def check(self) -> list[str]:
        """Validate the combination. Returns warnings for the user."""
        warnings: list[str] = []
        if (self.static is None) != (self.gateway is None):
            raise ValidationError("--static and --gateway must be given together")
        if self.mac is not None and self.static is None:
            raise ValidationError("--mac only applies together with --static")
        if self.static is not None:
            if "/" not in self.static:
                raise ValidationError(
                    f"--static needs a prefix length: {self.static!r}", hint="e.g. 10.0.0.10/24"
                )
            try:
                interface = ipaddress.ip_interface(self.static)
                gateway = ipaddress.ip_address(self.gateway)
            except ValueError as err:
                raise ValidationError(f"invalid address: {err}") from None
            if gateway not in interface.network:
                raise ValidationError(f"gateway {gateway} is not in {interface.network}")
            if self.mac is None:
                warnings.append(
                    "no --mac given: the first physical Ethernet interface is configured; "
                    "nodes with several NICs need --mac"
                )
        if self.mac is not None and not _MAC.fullmatch(self.mac.lower()):
            raise ValidationError(f"invalid MAC address {self.mac!r}", hint="e.g. aa:bb:cc:dd:ee:ff")
        for server in self.dns:
            try:
                ipaddress.ip_address(server)
            except ValueError:
                raise ValidationError(f"invalid DNS server {server!r}", hint="use an IP address") from None
        return warnings


def render_node_patch(node: str, options: NodeOptions = NodeOptions()) -> str:
    docs: list[dict] = [{"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "off", "hostname": node}]
    if options.static is not None:
        selector = (
            f'mac(link.permanent_addr) == "{options.mac.lower()}"' if options.mac else FIRST_ETHERNET
        )
        docs.append(
            {"apiVersion": "v1alpha1", "kind": "LinkAliasConfig", "name": LINK, "selector": {"match": selector}}
        )
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "LinkConfig",
                "name": LINK,
                "up": True,
                "addresses": [{"address": options.static}],
                "routes": [{"gateway": options.gateway}],
            }
        )
    if options.dns:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "ResolverConfig",
                "nameservers": [{"address": server} for server in options.dns],
            }
        )
    if options.disk_selector is not None:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "UnattendedInstallConfig",
                "provisioning": {"diskSelector": {"match": options.disk_selector}},
            }
        )
    if options.public_ingress:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "NetworkRuleConfig",
                "name": "public-ingress",
                "portSelector": {"ports": [80, 443], "protocol": "tcp"},
                "ingress": [{"subnet": "0.0.0.0/0"}, {"subnet": "::/0"}],
            }
        )
    header = (
        f"# Talos patch for node {node}, applied after talos/patches/*.yaml.\n"
        "# Written by `talkub node add`; edit freely.\n"
    )
    return header + "---\n".join(yamlio.dump(doc) for doc in docs)


def node_patch_path(repo: Repo, node: str) -> Path:
    return repo.talos_dir / "nodes" / f"{node}.yaml"


def validate_node_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValidationError(
            f"invalid node name {name!r}", hint="lowercase letters, digits and '-' (it becomes the hostname)"
        )


def validate_address(value: str, flag: str) -> None:
    """An IP or host name that is passed to talosctl as `-n`/`-e`."""
    if not value or value.startswith("-") or "/" in value or any(char.isspace() for char in value):
        raise ValidationError(
            f"invalid address for {flag}: {value!r}", hint="use an IP address or host name without prefix length"
        )


def static_address_warning(spec: NodeSpec, options: NodeOptions, fix: str = "add --mgmt {address}") -> str | None:
    """Warn when the node moves to its static address but plan/apply would still use `ip`."""
    if options.static is None or spec.mgmt is not None:
        return None
    address = str(ipaddress.ip_interface(options.static).ip)
    if address == spec.ip:
        return None
    return f"after install the node is reachable at {address}; {fix.format(address=address)} so plan/apply use it"


def add_node(repo: Repo, name: str, spec: NodeSpec, options: NodeOptions) -> list[str]:
    """Write talos/nodes/<name>.yaml and the node entry. Returns warnings."""
    validate_node_name(name)
    validate_address(spec.ip, "--ip")
    if spec.mgmt is not None:
        validate_address(spec.mgmt, "--mgmt")
    warnings = options.check()
    moved = static_address_warning(spec, options)
    if moved is not None:
        warnings.append(moved)
    settings = repo.load_settings()
    firewall = repo.talos_dir / "patches" / "firewall.yaml"
    if spec.mgmt is None and settings.tailscale is not None and firewall.is_file():
        # Locked down: talkub reaches nodes only via the tailnet (see tailscale.lockdown).
        spec = dataclasses.replace(spec, mgmt=f"{name}.{settings.tailscale.tailnet}")
    if name in settings.nodes:
        raise ValidationError(f"node '{name}' already exists")
    path = node_patch_path(repo, name)
    if path.exists():
        raise ValidationError(f"{repo.rel(path)} already exists", hint="remove it or choose another name")
    settings.nodes[name] = spec
    settings = Settings.from_dict(settings.to_dict())  # validates role, platform and arch
    platform = settings.node_platform(name)
    if platform not in settings.schematics:
        source = settings.schematics.get(settings.platform, SchematicSpec())
        # ID and hash stay empty: resolved by the next `image show`, `plan` or `apply`.
        settings.schematics[platform] = SchematicSpec(
            extensions=list(source.extensions), kernel_args=list(source.kernel_args)
        )
        warnings.append(
            f"platform '{platform}' is new: copied extensions from '{settings.platform}' "
            f"({', '.join(source.extensions) or 'none'}); adjust with talkub image add/remove --platform {platform}"
        )
    if options.public_ingress and not (repo.talos_dir / "patches" / "firewall.yaml").exists():
        warnings.append("--public-ingress has no effect without the Talos firewall (see `talkub tailscale enable`)")
    atomic_write(path, render_node_patch(name, options).encode())
    repo.write_settings(settings)
    return warnings
