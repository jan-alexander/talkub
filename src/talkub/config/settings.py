"""`Settings`: the parsed .talkub.yaml as a whole."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from talkub.config.fields import (
    MARKER,
    check_choice,
    optional_text,
    text_list,
)
from talkub.config.schematic import FACTORY_HOST, customization_hash
from talkub.config.shortcuts import Shortcut, shortcut_from_dict, shortcut_to_dict
from talkub.config.specs import (
    ARCHES,
    PLATFORMS,
    ROLES,
    GitSpec,
    NodeSpec,
    SchematicSpec,
    TailscaleSpec,
    node_to_dict,
    parse_installer_image,
    schematic_to_dict,
)
from talkub.core.errors import ValidationError


@dataclass
class Settings:
    name: str
    endpoint: str
    talos_version: str
    kubernetes_version: str
    platform: str = "metal"
    schematics: dict[str, SchematicSpec] = field(default_factory=dict)
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    tailscale: TailscaleSpec | None = None
    git: GitSpec | None = None
    shortcuts: dict[str, Shortcut] = field(default_factory=dict)

    def node(self, name: str) -> NodeSpec:
        try:
            return self.nodes[name]
        except KeyError:
            raise ValidationError(
                f"unknown node '{name}'",
                hint=f"known nodes: {', '.join(self.nodes) or 'none'}",
            ) from None

    def node_platform(self, name: str) -> str:
        return self.node(name).platform or self.platform

    def mgmt_address(self, name: str) -> str:
        node = self.node(name)
        return node.mgmt or node.ip

    def ensure_schematic(self, platform: str) -> SchematicSpec:
        check_choice(platform, PLATFORMS, "platform")
        return self.schematics.setdefault(platform, SchematicSpec())

    def installer_image(self, platform: str) -> str:
        spec = self.schematics.get(platform)
        if spec is None or not spec.is_current:
            raise ValidationError(
                f"the schematic ID for platform '{platform}' is missing or outdated",
                hint="`talkub image show`, `plan` and `apply` resolve it (needs network access)",
            )
        return f"{FACTORY_HOST}/{platform}-installer/{spec.id}:{self.talos_version}"

    def to_dict(self) -> dict:
        data: dict = {
            "name": self.name,
            "endpoint": self.endpoint,
            "talosVersion": self.talos_version,
            "kubernetesVersion": self.kubernetes_version,
            "platform": self.platform,
            "schematics": {p: schematic_to_dict(s) for p, s in self.schematics.items()},
            "nodes": {name: node_to_dict(n) for name, n in self.nodes.items()},
        }
        if self.tailscale is not None:
            data["tailscale"] = {"tailnet": self.tailscale.tailnet}
            if self.tailscale.use_ips:
                data["tailscale"]["useIps"] = True
        if self.git is not None:
            data["git"] = {"url": self.git.url}
        if self.shortcuts:
            data["shortcuts"] = {name: shortcut_to_dict(s) for name, s in self.shortcuts.items()}
        return data

    @classmethod
    def from_dict(cls, data: object) -> Settings:
        if not isinstance(data, Mapping):
            raise ValidationError(f"{MARKER} must be a mapping")

        def text(key: str) -> str:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                raise ValidationError(
                    f"{MARKER}: '{key}' must be a string",
                    hint=f'quote it, e.g. {key}: "{value}"',
                )
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{MARKER}: '{key}' must be a non-empty string")
            return value.strip()

        endpoint = text("endpoint")
        if not endpoint.startswith("https://"):
            raise ValidationError(
                f"{MARKER}: 'endpoint' must start with https://",
                hint="e.g. https://10.0.0.10:6443",
            )

        platform = check_choice(data.get("platform", "metal"), PLATFORMS, "'platform'")
        schematics: dict[str, SchematicSpec] = {}
        raw_schematics = data.get("schematics")
        if raw_schematics is not None:
            if not isinstance(raw_schematics, Mapping):
                raise ValidationError(f"{MARKER}: 'schematics' must be a mapping")
            for name, spec in raw_schematics.items():
                where = f"schematics.{name}"
                check_choice(name, PLATFORMS, "schematics key")
                if not isinstance(spec, Mapping):
                    raise ValidationError(f"{MARKER}: {where} must be a mapping")
                schematics[str(name)] = SchematicSpec(
                    extensions=text_list(spec.get("extensions"), f"{where}.extensions"),
                    kernel_args=text_list(spec.get("kernelArgs"), f"{where}.kernelArgs"),
                    id=optional_text(spec.get("id"), f"{where}.id"),
                    hash=optional_text(spec.get("hash"), f"{where}.hash"),
                )
        elif "installImage" in data:
            # Phase-1 format: a single installer image without extension intent.
            legacy_platform, legacy_id = parse_installer_image(text("installImage"))
            check_choice(legacy_platform, PLATFORMS, "installImage platform")
            schematics[legacy_platform] = SchematicSpec(id=legacy_id, hash=customization_hash([], []))
            if "platform" not in data:
                platform = legacy_platform

        nodes: dict[str, NodeSpec] = {}
        for name, spec in (data.get("nodes") or {}).items():
            if not isinstance(spec, Mapping) or not isinstance(spec.get("ip"), str):
                raise ValidationError(f"{MARKER}: node '{name}' needs an 'ip'")
            role = spec.get("role", "controlplane")
            if role not in ROLES:
                raise ValidationError(
                    f"{MARKER}: node '{name}' has invalid role '{role}'",
                    hint=f"use one of: {', '.join(ROLES)}",
                )
            node_platform = spec.get("platform")
            if node_platform is not None:
                check_choice(node_platform, PLATFORMS, f"nodes.{name}.platform")
            arch = spec.get("arch")
            if arch is not None:
                check_choice(arch, ARCHES, f"nodes.{name}.arch")
            nodes[str(name)] = NodeSpec(
                ip=spec["ip"],
                role=role,
                mgmt=optional_text(spec.get("mgmt"), f"nodes.{name}.mgmt"),
                platform=node_platform,
                arch=arch,
            )

        # A legacy `charts` block is ignored: the Argo CD apps under gitops/apps/ are the only
        # source of chart versions, and the block disappears on the next write.

        tailscale = None
        raw_tailscale = data.get("tailscale")
        if raw_tailscale is not None:
            if not isinstance(raw_tailscale, Mapping):
                raise ValidationError(f"{MARKER}: 'tailscale' must be a mapping")
            tailnet = optional_text(raw_tailscale.get("tailnet"), "tailscale.tailnet")
            if tailnet is None:
                raise ValidationError(f"{MARKER}: 'tailscale' needs tailnet")
            # A legacy `nodeCidr` (talkub <= 0.1.7) is ignored: the firewall rules list the nodes'
            # own addresses (`talkub tailscale firewall`); the key disappears on the next write.
            use_ips = raw_tailscale.get("useIps", False)
            if not isinstance(use_ips, bool):
                raise ValidationError(f"{MARKER}: 'tailscale.useIps' must be a boolean")
            tailscale = TailscaleSpec(tailnet=tailnet, use_ips=use_ips)

        git = None
        raw_git = data.get("git")
        if raw_git is not None:
            if not isinstance(raw_git, Mapping):
                raise ValidationError(f"{MARKER}: 'git' must be a mapping")
            url = optional_text(raw_git.get("url"), "git.url")
            if url is None:
                raise ValidationError(f"{MARKER}: 'git' needs a url", hint="talkub repo set-url <url>")
            git = GitSpec(url=url)

        shortcuts: dict[str, Shortcut] = {}
        raw_shortcuts = data.get("shortcuts")
        if raw_shortcuts is not None:
            if not isinstance(raw_shortcuts, Mapping):
                raise ValidationError(f"{MARKER}: 'shortcuts' must be a mapping")
            for name, spec in raw_shortcuts.items():
                shortcuts[str(name)] = shortcut_from_dict(str(name), spec)

        return cls(
            name=text("name"),
            endpoint=endpoint,
            talos_version=text("talosVersion"),
            kubernetes_version=text("kubernetesVersion"),
            platform=platform,
            schematics=schematics,
            nodes=nodes,
            tailscale=tailscale,
            git=git,
            shortcuts=shortcuts,
        )
