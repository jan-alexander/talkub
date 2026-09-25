"""Locating a cluster repo and reading/writing its settings (.talkub.yaml)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import RepoNotFound, ValidationError
from talkub.fsutil import atomic_write
from talkub.schematic import FACTORY_HOST, customization_hash

MARKER = ".talkub.yaml"
ROLES = ("controlplane", "worker")
PLATFORMS = ("metal", "hcloud")
ARCHES = ("amd64", "arm64")

_INSTALLER = re.compile(
    rf"^{re.escape(FACTORY_HOST)}/(?P<platform>[a-z0-9]+)-installer/(?P<id>[0-9a-f]+)$"
)

# DNS-1123 label: lowercase letters, digits and '-', max 63 characters, not starting/ending with '-'.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Kubernetes secret data key: alphanumerics, '-', '.', '_'.
_SECRET_KEY = re.compile(r"^[-._a-zA-Z0-9]+$")

_RUN_TOOLS = ("k", "kubectl", "t", "talosctl")
_SHORTCUT_KINDS = ("secret", "job", "run", "portForward")
_PORT_SPEC = re.compile(r"^(\d{1,5})(?::(\d{1,5}))?$")


def valid_label(value: object) -> bool:
    """True if `value` is a DNS-1123 label (used for shortcut/expose names, namespaces, services, ports)."""
    return isinstance(value, str) and len(value) <= 63 and bool(DNS_LABEL.match(value))


def _valid_port_spec(value: str) -> bool:
    match = _PORT_SPEC.match(value)
    if match is None:
        return False
    return all(1 <= int(group) <= 65535 for group in match.groups() if group is not None)


@dataclass(frozen=True)
class NodeSpec:
    ip: str
    role: str = "controlplane"
    mgmt: str | None = None
    platform: str | None = None
    arch: str | None = None


@dataclass(frozen=True)
class ChartSpec:
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
    node_cidr: str
    use_ips: bool = False


@dataclass(frozen=True)
class GitSpec:
    url: str


@dataclass(frozen=True)
class Shortcut:
    name: str
    kind: str  # "secret" | "job" | "run" | "portForward"
    description: str | None = None
    namespace: str | None = None  # secret, job, portForward
    secret: str | None = None  # secret: Secret name
    key: str | None = None  # secret
    cronjob: str | None = None  # job
    follow: bool = False  # job
    argv: tuple[str, ...] = ()  # run
    service: str | None = None  # portForward
    ports: tuple[str, ...] = ()  # portForward


def parse_installer_image(image: str) -> tuple[str, str]:
    """Split 'factory.talos.dev/<platform>-installer/<id>' (without tag) into (platform, id)."""
    match = _INSTALLER.match(image)
    if match is None:
        raise ValidationError(
            f"not an Image Factory installer image: {image!r}",
            hint=f"expected {FACTORY_HOST}/<platform>-installer/<schematic-id>",
        )
    return match["platform"], match["id"]


def _check_choice(value: object, choices: tuple[str, ...], where: str) -> str:
    if value not in choices:
        raise ValidationError(
            f"{MARKER}: {where} has invalid value {value!r}", hint=f"use one of: {', '.join(choices)}"
        )
    return str(value)


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{MARKER}: {where} must be a non-empty string")
    return value.strip()


def _text_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{MARKER}: {where} must be a list of strings")
    return [str(item) for item in value]


def _label(value: object, where: str) -> str:
    if not valid_label(value):
        raise ValidationError(f"{MARKER}: {where} must be a valid DNS label")
    return str(value)


def _shortcut_from_dict(name: str, spec: object) -> Shortcut:
    where = f"shortcuts.{name}"
    if not isinstance(spec, Mapping):
        raise ValidationError(f"{MARKER}: {where} must be a mapping")
    if not valid_label(name):
        raise ValidationError(f"{MARKER}: {where} is not a valid name")

    present = [kind for kind in _SHORTCUT_KINDS if kind in spec]
    if len(present) != 1:
        raise ValidationError(
            f"{MARKER}: {where} must have exactly one of: {', '.join(_SHORTCUT_KINDS)}"
        )
    kind = present[0]
    description = _optional_text(spec.get("description"), f"{where}.description")

    if kind == "secret":
        raw = spec["secret"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.secret must be a mapping")
        namespace = _label(raw.get("namespace"), f"{where}.secret.namespace")
        secret_name = _label(raw.get("name"), f"{where}.secret.name")
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip() or not _SECRET_KEY.match(key):
            raise ValidationError(f"{MARKER}: {where}.secret.key must be a valid secret key")
        return Shortcut(
            name=name, kind=kind, description=description, namespace=namespace, secret=secret_name, key=key
        )

    if kind == "job":
        raw = spec["job"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.job must be a mapping")
        namespace = _label(raw.get("namespace"), f"{where}.job.namespace")
        cronjob = _label(raw.get("cronjob"), f"{where}.job.cronjob")
        follow = raw.get("follow", False)
        if not isinstance(follow, bool):
            raise ValidationError(f"{MARKER}: {where}.job.follow must be a boolean")
        return Shortcut(
            name=name, kind=kind, description=description, namespace=namespace, cronjob=cronjob, follow=follow
        )

    if kind == "run":
        raw = spec["run"]
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValidationError(f"{MARKER}: {where}.run must be a non-empty list of strings")
        if raw[0] not in _RUN_TOOLS:
            raise ValidationError(
                f"{MARKER}: {where}.run must start with one of: {', '.join(_RUN_TOOLS)}"
            )
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw))

    raw = spec["portForward"]
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{MARKER}: {where}.portForward must be a mapping")
    namespace = _label(raw.get("namespace"), f"{where}.portForward.namespace")
    service = _label(raw.get("service"), f"{where}.portForward.service")
    ports = raw.get("ports")
    if not isinstance(ports, list) or not ports or not all(isinstance(item, str) for item in ports):
        raise ValidationError(f"{MARKER}: {where}.portForward.ports must be a non-empty list of strings")
    for port in ports:
        if not _valid_port_spec(port):
            raise ValidationError(f"{MARKER}: {where}.portForward.ports has invalid entry {port!r}")
    return Shortcut(
        name=name, kind=kind, description=description, namespace=namespace, service=service, ports=tuple(ports)
    )


def _shortcut_dict(shortcut: Shortcut) -> dict:
    data: dict = {}
    if shortcut.description is not None:
        data["description"] = shortcut.description
    if shortcut.kind == "secret":
        data["secret"] = {"namespace": shortcut.namespace, "name": shortcut.secret, "key": shortcut.key}
    elif shortcut.kind == "job":
        job: dict = {"namespace": shortcut.namespace, "cronjob": shortcut.cronjob}
        if shortcut.follow:
            job["follow"] = True
        data["job"] = job
    elif shortcut.kind == "run":
        data["run"] = list(shortcut.argv)
    else:
        data["portForward"] = {
            "namespace": shortcut.namespace, "service": shortcut.service, "ports": list(shortcut.ports)
        }
    return data


@dataclass
class Settings:
    name: str
    endpoint: str
    talos_version: str
    kubernetes_version: str
    platform: str = "metal"
    schematics: dict[str, SchematicSpec] = field(default_factory=dict)
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    charts: dict[str, ChartSpec] = field(default_factory=dict)
    tailscale: TailscaleSpec | None = None
    git: GitSpec | None = None
    shortcuts: dict[str, Shortcut] = field(default_factory=dict)

    def node(self, name: str) -> NodeSpec:
        try:
            return self.nodes[name]
        except KeyError:
            raise ValidationError(
                f"unknown node '{name}'", hint=f"known nodes: {', '.join(self.nodes) or 'none'}"
            ) from None

    def node_platform(self, name: str) -> str:
        return self.node(name).platform or self.platform

    def mgmt_address(self, name: str) -> str:
        node = self.node(name)
        return node.mgmt or node.ip

    def ensure_schematic(self, platform: str) -> SchematicSpec:
        _check_choice(platform, PLATFORMS, "platform")
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
            "schematics": {p: _schematic_dict(s) for p, s in self.schematics.items()},
            "nodes": {name: _node_dict(n) for name, n in self.nodes.items()},
            "charts": {
                name: {"repo": c.repo, "chart": c.chart, "version": c.version}
                for name, c in self.charts.items()
            },
        }
        if self.tailscale is not None:
            data["tailscale"] = {"tailnet": self.tailscale.tailnet, "nodeCidr": self.tailscale.node_cidr}
            if self.tailscale.use_ips:
                data["tailscale"]["useIps"] = True
        if self.git is not None:
            data["git"] = {"url": self.git.url}
        if self.shortcuts:
            data["shortcuts"] = {name: _shortcut_dict(s) for name, s in self.shortcuts.items()}
        return data

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

        platform = _check_choice(data.get("platform", "metal"), PLATFORMS, "'platform'")
        schematics: dict[str, SchematicSpec] = {}
        raw_schematics = data.get("schematics")
        if raw_schematics is not None:
            if not isinstance(raw_schematics, Mapping):
                raise ValidationError(f"{MARKER}: 'schematics' must be a mapping")
            for name, spec in raw_schematics.items():
                where = f"schematics.{name}"
                _check_choice(name, PLATFORMS, "schematics key")
                if not isinstance(spec, Mapping):
                    raise ValidationError(f"{MARKER}: {where} must be a mapping")
                schematics[str(name)] = SchematicSpec(
                    extensions=_text_list(spec.get("extensions"), f"{where}.extensions"),
                    kernel_args=_text_list(spec.get("kernelArgs"), f"{where}.kernelArgs"),
                    id=_optional_text(spec.get("id"), f"{where}.id"),
                    hash=_optional_text(spec.get("hash"), f"{where}.hash"),
                )
        elif "installImage" in data:
            # Phase-1 format: a single installer image without extension intent.
            legacy_platform, legacy_id = parse_installer_image(text("installImage"))
            _check_choice(legacy_platform, PLATFORMS, "installImage platform")
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
                _check_choice(node_platform, PLATFORMS, f"nodes.{name}.platform")
            arch = spec.get("arch")
            if arch is not None:
                _check_choice(arch, ARCHES, f"nodes.{name}.arch")
            nodes[str(name)] = NodeSpec(
                ip=spec["ip"],
                role=role,
                mgmt=_optional_text(spec.get("mgmt"), f"nodes.{name}.mgmt"),
                platform=node_platform,
                arch=arch,
            )

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

        tailscale = None
        raw_tailscale = data.get("tailscale")
        if raw_tailscale is not None:
            if not isinstance(raw_tailscale, Mapping):
                raise ValidationError(f"{MARKER}: 'tailscale' must be a mapping")
            tailnet = _optional_text(raw_tailscale.get("tailnet"), "tailscale.tailnet")
            node_cidr = _optional_text(raw_tailscale.get("nodeCidr"), "tailscale.nodeCidr")
            if tailnet is None or node_cidr is None:
                raise ValidationError(f"{MARKER}: 'tailscale' needs tailnet and nodeCidr")
            use_ips = raw_tailscale.get("useIps", False)
            if not isinstance(use_ips, bool):
                raise ValidationError(f"{MARKER}: 'tailscale.useIps' must be a boolean")
            tailscale = TailscaleSpec(tailnet=tailnet, node_cidr=node_cidr, use_ips=use_ips)

        git = None
        raw_git = data.get("git")
        if raw_git is not None:
            if not isinstance(raw_git, Mapping):
                raise ValidationError(f"{MARKER}: 'git' must be a mapping")
            url = _optional_text(raw_git.get("url"), "git.url")
            if url is None:
                raise ValidationError(f"{MARKER}: 'git' needs a url", hint="talkub repo set-url <url>")
            git = GitSpec(url=url)

        shortcuts: dict[str, Shortcut] = {}
        raw_shortcuts = data.get("shortcuts")
        if raw_shortcuts is not None:
            if not isinstance(raw_shortcuts, Mapping):
                raise ValidationError(f"{MARKER}: 'shortcuts' must be a mapping")
            for name, spec in raw_shortcuts.items():
                shortcuts[str(name)] = _shortcut_from_dict(str(name), spec)

        return cls(
            name=text("name"),
            endpoint=endpoint,
            talos_version=text("talosVersion"),
            kubernetes_version=text("kubernetesVersion"),
            platform=platform,
            schematics=schematics,
            nodes=nodes,
            charts=charts,
            tailscale=tailscale,
            git=git,
            shortcuts=shortcuts,
        )


def _schematic_dict(spec: SchematicSpec) -> dict:
    data: dict = {"extensions": list(spec.extensions), "kernelArgs": list(spec.kernel_args)}
    if spec.id is not None:
        data["id"] = spec.id
    if spec.hash is not None:
        data["hash"] = spec.hash
    return data


def _node_dict(node: NodeSpec) -> dict:
    data: dict = {"ip": node.ip, "role": node.role}
    for key in ("mgmt", "platform", "arch"):
        if (value := getattr(node, key)) is not None:
            data[key] = value
    return data


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
