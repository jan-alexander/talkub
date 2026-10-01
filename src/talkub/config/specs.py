"""Value objects in .talkub.yaml: nodes, charts, schematics, Tailscale and git settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from talkub.config.schematic import FACTORY_HOST, customization_hash
from talkub.core.errors import ValidationError

ROLES = ("controlplane", "worker")


PLATFORMS = ("metal", "hcloud")


ARCHES = ("amd64", "arm64")


_INSTALLER = re.compile(rf"^{re.escape(FACTORY_HOST)}/(?P<platform>[a-z0-9]+)-installer/(?P<id>[0-9a-f]+)$")


@dataclass(frozen=True)
class NodeSpec:
    ip: str
    role: str = "controlplane"
    mgmt: str | None = None
    platform: str | None = None
    arch: str | None = None


@dataclass(frozen=True)
class ChartSpec:
    """A Helm chart at one version (for rendering new Argo CD apps and naming the chart to helm)."""

    repo: str
    chart: str
    version: str


@dataclass
class SchematicSpec:
    """Extension intent for one platform plus the factory ID cached for exactly that intent."""

    extensions: list[str] = field(default_factory=list)
    kernel_args: list[str] = field(default_factory=list)
    id: str | None = None
    hash: str | None = None

    @property
    def is_current(self) -> bool:
        return self.id is not None and self.hash == customization_hash(self.extensions, self.kernel_args)


@dataclass(frozen=True)
class TailscaleSpec:
    tailnet: str
    use_ips: bool = False


@dataclass(frozen=True)
class GitSpec:
    url: str


def parse_installer_image(image: str) -> tuple[str, str]:
    """Split 'factory.talos.dev/<platform>-installer/<id>' (without tag) into (platform, id)."""
    match = _INSTALLER.match(image)
    if match is None:
        raise ValidationError(
            f"not an Image Factory installer image: {image!r}",
            hint=f"expected {FACTORY_HOST}/<platform>-installer/<schematic-id>",
        )
    return match["platform"], match["id"]


def schematic_to_dict(spec: SchematicSpec) -> dict:
    data: dict = {
        "extensions": list(spec.extensions),
        "kernelArgs": list(spec.kernel_args),
    }
    if spec.id is not None:
        data["id"] = spec.id
    if spec.hash is not None:
        data["hash"] = spec.hash
    return data


def node_to_dict(node: NodeSpec) -> dict:
    data: dict = {"ip": node.ip, "role": node.role}
    for key in ("mgmt", "platform", "arch"):
        if (value := getattr(node, key)) is not None:
            data[key] = value
    return data
