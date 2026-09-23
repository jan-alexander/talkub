"""Locating a cluster repo and reading/writing its settings (.talkub.yaml)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import RepoNotFound, ValidationError
from talkub.fsutil import atomic_write

MARKER = ".talkub.yaml"
ROLES = ("controlplane", "worker")


@dataclass(frozen=True)
class NodeSpec:
    ip: str
    role: str = "controlplane"


@dataclass(frozen=True)
class ChartSpec:
    repo: str
    chart: str
    version: str


@dataclass
class Settings:
    name: str
    endpoint: str
    talos_version: str
    kubernetes_version: str
    install_image: str
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    charts: dict[str, ChartSpec] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "talosVersion": self.talos_version,
            "kubernetesVersion": self.kubernetes_version,
            "installImage": self.install_image,
            "nodes": {name: {"ip": n.ip, "role": n.role} for name, n in self.nodes.items()},
            "charts": {
                name: {"repo": c.repo, "chart": c.chart, "version": c.version}
                for name, c in self.charts.items()
            },
        }

    @classmethod
    def from_dict(cls, data: object) -> Settings:
        if not isinstance(data, Mapping):
            raise ValidationError(f"{MARKER} must be a mapping")

        def text(key: str) -> str:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                raise ValidationError(
                    f"{MARKER}: '{key}' must be a string", hint=f'quote it, e.g. {key}: "{value}"'
                )
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{MARKER}: '{key}' must be a non-empty string")
            return value.strip()

        endpoint = text("endpoint")
        if not endpoint.startswith("https://"):
            raise ValidationError(
                f"{MARKER}: 'endpoint' must start with https://", hint="e.g. https://10.0.0.10:6443"
            )

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
            nodes[str(name)] = NodeSpec(ip=spec["ip"], role=role)

        charts: dict[str, ChartSpec] = {}
        for name, spec in (data.get("charts") or {}).items():
            try:
                charts[str(name)] = ChartSpec(
                    repo=str(spec["repo"]), chart=str(spec["chart"]), version=str(spec["version"])
                )
            except (KeyError, TypeError):
                raise ValidationError(
                    f"{MARKER}: chart '{name}' needs repo, chart and version"
                ) from None

        return cls(
            name=text("name"),
            endpoint=endpoint,
            talos_version=text("talosVersion"),
            kubernetes_version=text("kubernetesVersion"),
            install_image=text("installImage"),
            nodes=nodes,
            charts=charts,
        )


@dataclass(frozen=True)
class Repo:
    root: Path

    @property
    def settings_file(self) -> Path:
        return self.root / MARKER

    @property
    def sops_config(self) -> Path:
        return self.root / ".sops.yaml"

    @property
    def keys_dir(self) -> Path:
        return self.root / "keys"

    @property
    def recipients_file(self) -> Path:
        return self.keys_dir / "recipients.txt"

    @property
    def master_age(self) -> Path:
        return self.keys_dir / "master.age"

    @property
    def master_next_age(self) -> Path:
        return self.keys_dir / "master.next.age"

    @property
    def master_pub(self) -> Path:
        return self.keys_dir / "master.pub"

    @property
    def talos_dir(self) -> Path:
        return self.root / "talos"

    @property
    def talos_secrets(self) -> Path:
        return self.talos_dir / "secrets.sops.yaml"

    @property
    def talosconfig(self) -> Path:
        return self.talos_dir / "talosconfig.sops.yaml"

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubernetes" / "kubeconfig.sops.yaml"

    @property
    def gitops_dir(self) -> Path:
        return self.root / "gitops"

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def load_settings(self) -> Settings:
        try:
            data = yamlio.load(self.settings_file.read_text())
        except FileNotFoundError:
            raise RepoNotFound(f"{self.settings_file} does not exist") from None
        except YAMLError as err:
            raise ValidationError(f"{MARKER} is not valid YAML: {err}") from err
        return Settings.from_dict(data)

    def write_settings(self, settings: Settings) -> None:
        atomic_write(self.settings_file, yamlio.dump(settings.to_dict()).encode())

    def sops_files(self) -> list[Path]:
        """All SOPS-encrypted files. `.sops.yaml` (the SOPS config) is not one of them."""
        return sorted(
            path
            for path in self.root.rglob("*.sops.yaml")
            if path.name != ".sops.yaml" and ".git" not in path.relative_to(self.root).parts
        )


def find_repo(start: Path, explicit: Path | None = None) -> Repo:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / MARKER).is_file():
            raise RepoNotFound(f"{root} is not a talkub repo (no {MARKER})")
        return Repo(root)
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / MARKER).is_file():
            return Repo(candidate)
    raise RepoNotFound(
        f"no {MARKER} found in {current} or any parent directory",
        hint="cd into a cluster repo, pass --repo, or create one with `talkub init`",
    )
