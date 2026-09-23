# talkub Phase 2a (Nodes, Schematics, Render, Plan/Apply) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe nodes (platform, network, disk, ingress), manage Image Factory schematics, render and validate per-node Talos machine configs, and show/apply them (`plan`, `apply`, `apply --insecure`).

**Architecture:** `.talkub.yaml` gains `platform`, `schematics` (extension intent + cached factory ID) and per-node `mgmt`/`platform`/`arch`; Phase-1 `installImage` is migrated on load. `nodes.py` turns CLI options into a multi-document Talos patch. `talos/render.py` decrypts secrets and `*.sops.yaml` patches once, runs `talosctl gen config … -o -` per node and validates the result. `lifecycle.py` sends configs to nodes through a `talos_session` holding the ephemeral talosconfig.

**Tech Stack:** Python ≥ 3.12, uv, Typer ≥ 0.27, ruamel.yaml, pytest; stdlib `urllib`, `ipaddress`, `http.server` (test stub); binaries talosctl 1.14, age, sops.

**Spec:** `docs/superpowers/specs/2026-09-23-talkub-phase2-design.md` (§3, §4, §6, §7 rows node add/init/plan/apply/image). Phase-1 spec `docs/superpowers/specs/2026-09-23-talkub-design.md` still binds.

## Global Constraints

- All code, identifiers, comments, docstrings and CLI output in **English**.
- `runner.py` is the only module that starts subprocesses; argv lists only. HTTP to the Image Factory uses stdlib `urllib` only (no new dependency).
- Plaintext secrets (Talos `secrets.yaml`, decrypted `*.sops.yaml` patches, rendered machine configs, talosconfig) exist only inside `ephemeral.decrypted()` / `ephemeral.private_file()` / `private_tempdir()` (0700 dir, 0600 files).
- Installer image = `factory.talos.dev/<platform>-installer/<schematic id>:<talosVersion>`.
- Platforms: `metal`, `hcloud`. Arches: `amd64`, `arm64`. `talosctl validate --mode`: `metal` → `metal`, `hcloud` → `cloud`.
- Patch selection in `talos/patches/` (sorted by file name, `*.yaml` incl. `*.sops.yaml`): name starts with `controlplane` → `--config-patch-control-plane`; starts with `worker` → `--config-patch-worker`; otherwise `--config-patch`. `talos/nodes/<node>.yaml` last with `--config-patch`.
- Static link selector without `--mac`: `link.type == 1 && link.kind == ""`; with `--mac`: `mac(link.permanent_addr) == "<lowercase mac>"`. Link alias name `net0`.
- Schematic ID cache is valid iff `hash == sha256(json.dumps(customization, sort_keys=True, separators=(",", ":")))`.
- Factory base URL: `TALKUB_FACTORY_URL` env, default `https://factory.talos.dev`.
- Commits end with a Co-Authored-By trailer (the model that wrote the commit).
- Run tests with `uv run pytest`; integration tests skip when a binary is missing.

## Review Focus

1. **Offline use with cached schematic IDs:** `plan`/`apply` must not contact the Image Factory when the cached ID is current, and must work with the factory unreachable (test in Task 7).
2. **Static network typos:** a gateway outside the static subnet, a missing prefix length or a malformed MAC must be rejected before any file is written (tests in Task 4).
3. **Unreachable or failing node:** `apply` over several nodes must stop at the first failing node, name it, and leave later nodes untouched (test in Task 7).
4. **`apply --insecure` safety:** no install without confirmation; declining sends nothing; the shown installer image/disk come from the rendered config (tests in Tasks 7 and 8).
5. **Phase-1 repos:** a `.talkub.yaml` with only `installImage` keeps working and is rewritten in the new format (test in Task 1).

---

## File Structure

```
src/talkub/
├── schematic.py          # NEW: customization dict/hash/document, FACTORY_HOST
├── repo.py               # MODIFY: Settings (platform, schematics, tailscale, node fields), migration, helpers
├── factory.py            # NEW: FactoryClient (urllib), boot_media_url
├── images.py             # NEW: refresh / add / remove extensions, used_platforms
├── nodes.py              # NEW: NodeOptions, render_node_patch, node_patch_path, add_node
├── scaffold.py           # MODIFY: schematics instead of installImage; platform; node options; nodes.render_node_patch
├── templates/node.yaml.tmpl  # DELETE (replaced by nodes.render_node_patch)
├── talos/render.py       # NEW: PatchRef, select_patches, render_configs, install_summary
├── talos/client.py       # NEW: TalosSession, talos_session
├── lifecycle.py          # NEW: ordered_nodes, plan, apply, apply_insecure
└── cli/
    ├── image.py          # NEW: image extensions/add/remove/show
    ├── nodes.py          # NEW: node add
    ├── lifecycle.py      # NEW: plan, apply
    ├── setup.py          # MODIFY: init --platform + node options
    └── main.py           # MODIFY: register node/image/plan/apply
tests/
├── conftest.py           # MODIFY: proxy env isolation, factory_stub, talos_repo, talosctl_spy
├── helpers.py            # MODIFY: sample_settings with schematics
└── test_schematic.py, test_repo.py (extend), test_factory.py, test_images.py, test_cli_image.py,
    test_nodes.py, test_cli_nodes.py, test_render.py, test_lifecycle.py, test_cli_lifecycle.py,
    test_scaffold.py (extend)
```

---

### Task 1: Schematic helpers, extended settings model, migration

**Files:**
- Create: `src/talkub/schematic.py`, `tests/test_schematic.py`
- Modify: `src/talkub/repo.py` (replace whole file), `src/talkub/scaffold.py`, `tests/helpers.py`, `tests/test_repo.py`, `tests/test_scaffold.py`

**Interfaces:**
- Consumes: `yamlio.dump`, `ValidationError`, `RepoNotFound`, `atomic_write`, `talos.gen.Defaults.install_image`.
- Produces:
  - `schematic.FACTORY_HOST = "factory.talos.dev"`, `schematic.customization(extensions, kernel_args) -> dict`, `schematic.customization_hash(extensions, kernel_args) -> str`, `schematic.schematic_document(extensions, kernel_args) -> str`
  - `repo.PLATFORMS = ("metal", "hcloud")`, `repo.ARCHES = ("amd64", "arm64")`, `repo.ROLES` (unchanged)
  - `repo.NodeSpec(ip, role="controlplane", mgmt=None, platform=None, arch=None)` (frozen)
  - `repo.SchematicSpec(extensions=[], kernel_args=[], id=None, hash=None)` with property `is_current -> bool`
  - `repo.TailscaleSpec(tailnet: str, node_cidr: str)` (frozen)
  - `repo.Settings(name, endpoint, talos_version, kubernetes_version, platform="metal", schematics={}, nodes={}, charts={}, tailscale=None)` with `to_dict()`, `from_dict()`, `node(name) -> NodeSpec`, `node_platform(name) -> str`, `mgmt_address(name) -> str`, `ensure_schematic(platform) -> SchematicSpec`, `installer_image(platform) -> str`
  - `repo.parse_installer_image(image: str) -> tuple[str, str]` (platform, id)
  - `scaffold.InitRequest(..., platform: str = "metal")`
  - `helpers.DEFAULT_SCHEMATIC_ID = "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"`

- [ ] **Step 1: Write the failing tests**

`tests/test_schematic.py`:

```python
from talkub import yamlio
from talkub.schematic import customization, customization_hash, schematic_document


def test_empty_customization():
    assert customization([], []) == {}
    assert schematic_document([], []) == "customization: {}\n"


def test_customization_sorts_extensions_and_keeps_kernel_args():
    assert customization(["siderolabs/tailscale", "siderolabs/intel-ucode"], ["console=ttyS0"]) == {
        "extraKernelArgs": ["console=ttyS0"],
        "systemExtensions": {"officialExtensions": ["siderolabs/intel-ucode", "siderolabs/tailscale"]},
    }


def test_document_is_the_factory_request_body():
    doc = yamlio.load(schematic_document(["siderolabs/tailscale"], []))
    assert doc == {"customization": {"systemExtensions": {"officialExtensions": ["siderolabs/tailscale"]}}}


def test_hash_ignores_extension_order_but_not_content():
    a = customization_hash(["b", "a"], [])
    assert a == customization_hash(["a", "b"], [])
    assert a != customization_hash(["a"], [])
    assert len(a) == 64
```

Append to `tests/test_repo.py`:

```python
from helpers import DEFAULT_SCHEMATIC_ID
from talkub.repo import SchematicSpec, TailscaleSpec, parse_installer_image
from talkub.schematic import customization_hash


def test_full_settings_roundtrip(tmp_path):
    settings = sample_settings()
    settings.platform = "hcloud"
    settings.schematics["hcloud"] = SchematicSpec(
        extensions=["siderolabs/tailscale"], kernel_args=["console=ttyS0"], id="abc", hash="def"
    )
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.tail.ts.net", platform="metal", arch="arm64")
    settings.tailscale = TailscaleSpec(tailnet="tail.ts.net", node_cidr="10.0.0.0/24")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    text = repo.settings_file.read_text()
    assert "installImage" not in text
    assert "kernelArgs" in text and "nodeCidr" in text


def test_phase1_install_image_is_migrated():
    data = sample_settings().to_dict()
    del data["platform"]
    del data["schematics"]
    data["installImage"] = f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}"
    settings = Settings.from_dict(data)
    assert settings.platform == "metal"
    assert settings.schematics["metal"].id == DEFAULT_SCHEMATIC_ID
    assert settings.schematics["metal"].is_current
    assert "installImage" not in settings.to_dict()


def test_phase1_install_image_must_be_a_factory_image():
    data = sample_settings().to_dict()
    del data["schematics"]
    data["installImage"] = "ghcr.io/siderolabs/installer"
    with pytest.raises(ValidationError):
        Settings.from_dict(data)


def test_parse_installer_image():
    assert parse_installer_image("factory.talos.dev/hcloud-installer/abc123") == ("hcloud", "abc123")


def test_unknown_platform_and_arch_rejected():
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(platform="vmware"))
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(nodes={"n1": {"ip": "10.0.0.2", "arch": "riscv"}}))


def test_installer_image_and_node_helpers():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.ts", platform="hcloud")
    assert settings.installer_image("metal") == f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0"
    assert settings.node_platform("cp-01") == "metal"
    assert settings.node_platform("w-01") == "hcloud"
    assert settings.mgmt_address("cp-01") == "10.0.0.10"
    assert settings.mgmt_address("w-01") == "w-01.ts"
    with pytest.raises(ValidationError):
        settings.node("nope")


def test_installer_image_refuses_outdated_schematic():
    settings = sample_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    assert not settings.schematics["metal"].is_current
    with pytest.raises(ValidationError) as exc:
        settings.installer_image("metal")
    assert "outdated" in str(exc.value)


def test_ensure_schematic_adds_empty_entry():
    settings = sample_settings()
    spec = settings.ensure_schematic("hcloud")
    assert spec == SchematicSpec()
    assert settings.schematics["hcloud"] is spec
    with pytest.raises(ValidationError):
        settings.ensure_schematic("vmware")


def test_schematic_is_current_only_with_matching_hash():
    assert SchematicSpec(id="x", hash=customization_hash([], [])).is_current
    assert not SchematicSpec(id=None, hash=customization_hash([], [])).is_current
    assert not SchematicSpec(id="x", hash="other").is_current
```

In `tests/test_scaffold.py`, extend `_request` and add assertions/tests. Replace the `_request` helper with:

```python
def _request(target, public, platform="metal"):
    return InitRequest(
        target=target,
        name="demo",
        endpoint="https://10.0.0.10:6443",
        git_url="https://git.example.com/demo.git",
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
        recipients=[Recipient("me", public)],
        platform=platform,
    )
```

In `test_init_creates_complete_repo`, after `assert settings.talos_version.startswith("v")` add:

```python
    assert settings.platform == "metal"
    assert settings.schematics["metal"].is_current
    assert settings.installer_image("metal").startswith("factory.talos.dev/metal-installer/")
```

Add:

```python
def test_init_with_hcloud_platform(tmp_path, software_identity, fake_bin):
    fake_bin("helm", HELM)
    repo = init_cluster(_request(tmp_path / "demo", software_identity[1], platform="hcloud"), Runner())
    settings = repo.load_settings()
    assert settings.platform == "hcloud"
    assert settings.installer_image("hcloud").startswith("factory.talos.dev/hcloud-installer/")


def test_init_rejects_unknown_platform(tmp_path, software_identity):
    with pytest.raises(ValidationError):
        init_cluster(_request(tmp_path / "demo", software_identity[1], platform="vmware"), Runner())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schematic.py tests/test_repo.py tests/test_scaffold.py -q`
Expected: `ModuleNotFoundError: No module named 'talkub.schematic'` / import errors for `SchematicSpec`, `DEFAULT_SCHEMATIC_ID`.

- [ ] **Step 3: Implement `schematic.py`**

`src/talkub/schematic.py`:

```python
"""Talos Image Factory schematics: the customization talkub sends (pure data, no I/O)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from talkub import yamlio

FACTORY_HOST = "factory.talos.dev"


def customization(extensions: Sequence[str], kernel_args: Sequence[str]) -> dict:
    """The `customization` block of a schematic; empty lists are omitted like the factory does."""
    custom: dict = {}
    if kernel_args:
        custom["extraKernelArgs"] = list(kernel_args)
    if extensions:
        custom["systemExtensions"] = {"officialExtensions": sorted(extensions)}
    return custom


def customization_hash(extensions: Sequence[str], kernel_args: Sequence[str]) -> str:
    data = json.dumps(customization(extensions, kernel_args), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def schematic_document(extensions: Sequence[str], kernel_args: Sequence[str]) -> str:
    """Request body for POST /schematics."""
    return yamlio.dump({"customization": customization(extensions, kernel_args)})
```

- [ ] **Step 4: Replace `src/talkub/repo.py`**

```python
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
            tailscale = TailscaleSpec(tailnet=tailnet, node_cidr=node_cidr)

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
```

- [ ] **Step 5: Update `tests/helpers.py` `sample_settings`**

Replace the `sample_settings` function and add the constant:

```python
DEFAULT_SCHEMATIC_ID = "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"


def sample_settings():
    from talkub.repo import NodeSpec, SchematicSpec, Settings
    from talkub.schematic import customization_hash

    return Settings(
        name="test",
        endpoint="https://10.0.0.10:6443",
        talos_version="v1.14.0",
        kubernetes_version="1.37.0",
        platform="metal",
        schematics={"metal": SchematicSpec(id=DEFAULT_SCHEMATIC_ID, hash=customization_hash([], []))},
        nodes={"cp-01": NodeSpec(ip="10.0.0.10")},
    )
```

- [ ] **Step 6: Adapt `src/talkub/scaffold.py`**

Change the imports:

```python
from talkub.repo import PLATFORMS, NodeSpec, Repo, SchematicSpec, Settings, parse_installer_image
from talkub.schematic import customization_hash
```

Add the field to `InitRequest` (last field):

```python
    platform: str = "metal"
```

In `_validate`, add after the endpoint check:

```python
    if request.platform not in PLATFORMS:
        raise ValidationError(
            f"unknown platform {request.platform!r}", hint=f"use one of: {', '.join(PLATFORMS)}"
        )
```

In `_populate`, replace the `settings = Settings(...)` statement with:

```python
    # The schematic ID depends only on the customization, not on the platform, so the
    # default (empty) schematic from talosctl is valid for every platform.
    _, default_id = parse_installer_image(defaults.install_image)
    settings = Settings(
        name=request.name,
        endpoint=request.endpoint,
        talos_version=defaults.talos_version,
        kubernetes_version=defaults.kubernetes_version,
        platform=request.platform,
        schematics={request.platform: SchematicSpec(id=default_id, hash=customization_hash([], []))},
        nodes=dict(request.nodes),
        charts=chart_specs,
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all passed (the whole suite, since `Settings` changed shape).

- [ ] **Step 8: Commit**

```bash
git add src/talkub/schematic.py src/talkub/repo.py src/talkub/scaffold.py tests/helpers.py tests/test_schematic.py tests/test_repo.py tests/test_scaffold.py
git commit -m "feat: platforms and schematics in .talkub.yaml, migrate installImage"
```

---

### Task 2: Image Factory client and schematic refresh

**Files:**
- Create: `src/talkub/factory.py`, `src/talkub/images.py`, `tests/test_factory.py`, `tests/test_images.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `schematic.schematic_document`, `schematic.customization_hash`, `schematic.FACTORY_HOST`, `repo.Settings`, `repo.SchematicSpec`, `errors.CommandFailed`, `errors.ValidationError`.
- Produces:
  - `factory.DEFAULT_URL = "https://factory.talos.dev"`, `factory.FactoryClient(base_url: str | None = None, timeout: float = 30.0)` with `schematic_id(extensions, kernel_args) -> str`, `official_extensions(talos_version) -> list[str]`
  - `factory.boot_media_url(schematic_id: str, talos_version: str, platform: str, arch: str) -> str`
  - `images.used_platforms(settings) -> list[str]`, `images.refresh(settings, client, platforms) -> bool`, `images.add_extensions(settings, platform, names, client) -> list[str]`, `images.remove_extensions(settings, platform, names, client) -> list[str]`
  - conftest fixture `factory_stub` → object with `.requests: list[tuple[str, str, bytes]]`, `.extensions: list[str]`, `.fail: bool`

- [ ] **Step 1: Extend `tests/conftest.py`**

In `isolated_env`, extend the tuple of removed variables with the proxy variables so the local stub is reached directly:

```python
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
```

Append the fixture:

```python
@pytest.fixture
def factory_stub(monkeypatch):
    """Local stand-in for the Talos Image Factory; sets TALKUB_FACTORY_URL.

    POST /schematics answers {"id": sha256(body)}; GET .../extensions/official lists
    `state.extensions`. `state.fail = True` makes every request return HTTP 500.
    """
    import hashlib
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    state = SimpleNamespace(
        requests=[],
        extensions=["siderolabs/intel-ucode", "siderolabs/iscsi-tools", "siderolabs/tailscale"],
        fail=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append(("POST", self.path, body))
            if state.fail:
                return self._reply(500, {"error": "boom"})
            self._reply(201, {"id": hashlib.sha256(body).hexdigest()})

        def do_GET(self):
            state.requests.append(("GET", self.path, b""))
            if state.fail:
                return self._reply(500, {"error": "boom"})
            self._reply(200, [{"name": name, "ref": f"ghcr.io/{name}:1"} for name in state.extensions])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TALKUB_FACTORY_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield state
    server.shutdown()
    server.server_close()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_factory.py`:

```python
import hashlib

import pytest

from talkub.errors import CommandFailed
from talkub.factory import FactoryClient, boot_media_url
from talkub.schematic import schematic_document


def test_schematic_id_posts_the_document(factory_stub):
    body = schematic_document(["siderolabs/tailscale"], [])
    assert FactoryClient().schematic_id(["siderolabs/tailscale"], []) == hashlib.sha256(body.encode()).hexdigest()
    assert factory_stub.requests == [("POST", "/schematics", body.encode())]


def test_official_extensions_sorted(factory_stub):
    factory_stub.extensions = ["siderolabs/tailscale", "siderolabs/amdgpu"]
    assert FactoryClient().official_extensions("v1.14.0") == ["siderolabs/amdgpu", "siderolabs/tailscale"]
    assert factory_stub.requests[0][:2] == ("GET", "/version/v1.14.0/extensions/official")


def test_server_error_is_reported(factory_stub):
    factory_stub.fail = True
    with pytest.raises(CommandFailed) as exc:
        FactoryClient().schematic_id([], [])
    assert "offline" in exc.value.hint


def test_unreachable_factory(monkeypatch):
    with pytest.raises(CommandFailed):
        FactoryClient(base_url="http://127.0.0.1:9", timeout=2).official_extensions("v1.14.0")


def test_boot_media_urls():
    assert boot_media_url("abc", "v1.14.0", "metal", "amd64") == "https://factory.talos.dev/image/abc/v1.14.0/metal-amd64.iso"
    assert boot_media_url("abc", "v1.14.0", "hcloud", "arm64") == "https://factory.talos.dev/image/abc/v1.14.0/hcloud-arm64.raw.xz"
```

`tests/test_images.py`:

```python
import pytest

from helpers import DEFAULT_SCHEMATIC_ID, sample_settings
from talkub import images
from talkub.errors import ValidationError
from talkub.factory import FactoryClient
from talkub.repo import NodeSpec


def test_refresh_skips_current_schematic(factory_stub):
    settings = sample_settings()
    assert images.refresh(settings, FactoryClient(), ["metal"]) is False
    assert factory_stub.requests == []
    assert settings.schematics["metal"].id == DEFAULT_SCHEMATIC_ID


def test_refresh_resolves_outdated_and_missing(factory_stub):
    settings = sample_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    assert images.refresh(settings, FactoryClient(), ["metal", "hcloud"]) is True
    assert settings.schematics["metal"].is_current
    assert settings.schematics["hcloud"].is_current
    assert len(factory_stub.requests) == 2


def test_add_extensions_validates_names(factory_stub):
    settings = sample_settings()
    with pytest.raises(ValidationError) as exc:
        images.add_extensions(settings, "metal", ["siderolabs/nope"], FactoryClient())
    assert "siderolabs/nope" in str(exc.value)
    assert settings.schematics["metal"].extensions == []


def test_add_and_remove_extensions(factory_stub):
    settings = sample_settings()
    client = FactoryClient()
    assert images.add_extensions(settings, "metal", ["siderolabs/tailscale", "siderolabs/intel-ucode"], client) == [
        "siderolabs/intel-ucode",
        "siderolabs/tailscale",
    ]
    assert images.add_extensions(settings, "metal", ["siderolabs/tailscale"], client) == []
    assert settings.schematics["metal"].extensions == ["siderolabs/intel-ucode", "siderolabs/tailscale"]
    assert settings.schematics["metal"].is_current
    assert images.remove_extensions(settings, "metal", ["siderolabs/tailscale"], client) == ["siderolabs/tailscale"]
    assert settings.schematics["metal"].extensions == ["siderolabs/intel-ucode"]
    with pytest.raises(ValidationError):
        images.remove_extensions(settings, "metal", ["siderolabs/tailscale"], client)


def test_used_platforms():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    assert images.used_platforms(settings) == ["hcloud", "metal"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_factory.py tests/test_images.py -q`
Expected: `ModuleNotFoundError: No module named 'talkub.factory'`.

- [ ] **Step 4: Implement**

`src/talkub/factory.py`:

```python
"""Client for the Talos Image Factory (https://factory.talos.dev)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence

from talkub.errors import CommandFailed
from talkub.schematic import FACTORY_HOST, schematic_document

DEFAULT_URL = f"https://{FACTORY_HOST}"
_BOOT_MEDIA = {"metal": "iso", "hcloud": "raw.xz"}


class FactoryClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or os.environ.get("TALKUB_FACTORY_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None) -> object:
        headers = {"Content-Type": "application/yaml"} if body is not None else {}
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise CommandFailed(
                f"Talos Image Factory request failed: {method} {path}",
                hint=f"{err}; cached schematic IDs in .talkub.yaml keep working offline",
            ) from err

    def schematic_id(self, extensions: Sequence[str], kernel_args: Sequence[str]) -> str:
        data = self._request("POST", "/schematics", schematic_document(extensions, kernel_args).encode())
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise CommandFailed("unexpected answer from the Talos Image Factory", hint=str(data))
        return data["id"]

    def official_extensions(self, talos_version: str) -> list[str]:
        data = self._request("GET", f"/version/{talos_version}/extensions/official")
        if not isinstance(data, list):
            raise CommandFailed("unexpected answer from the Talos Image Factory", hint=str(data)[:200])
        return sorted(str(item["name"]) for item in data if isinstance(item, dict) and "name" in item)


def boot_media_url(schematic_id: str, talos_version: str, platform: str, arch: str) -> str:
    """Download URL for the boot medium: ISO for bare metal, raw.xz disk image for Hetzner."""
    return f"{DEFAULT_URL}/image/{schematic_id}/{talos_version}/{platform}-{arch}.{_BOOT_MEDIA[platform]}"
```

`src/talkub/images.py`:

```python
"""Schematic intent in .talkub.yaml: resolving IDs and editing extension lists."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from talkub.errors import ValidationError
from talkub.factory import FactoryClient
from talkub.repo import Settings
from talkub.schematic import customization_hash


def used_platforms(settings: Settings) -> list[str]:
    return sorted({settings.platform} | {settings.node_platform(name) for name in settings.nodes})


def refresh(settings: Settings, client: FactoryClient, platforms: Iterable[str]) -> bool:
    """Resolve schematic IDs whose cache is missing or outdated. Returns True if anything changed."""
    changed = False
    for platform in platforms:
        spec = settings.ensure_schematic(platform)
        if spec.is_current:
            continue
        spec.id = client.schematic_id(spec.extensions, spec.kernel_args)
        spec.hash = customization_hash(spec.extensions, spec.kernel_args)
        changed = True
    return changed


def add_extensions(
    settings: Settings, platform: str, names: Sequence[str], client: FactoryClient
) -> list[str]:
    official = set(client.official_extensions(settings.talos_version))
    unknown = sorted(set(names) - official)
    if unknown:
        raise ValidationError(
            f"unknown extension(s) for Talos {settings.talos_version}: {', '.join(unknown)}",
            hint="list them with `talkub image extensions`",
        )
    spec = settings.ensure_schematic(platform)
    added = sorted(set(names) - set(spec.extensions))
    spec.extensions = sorted(set(spec.extensions) | set(names))
    refresh(settings, client, [platform])
    return added


def remove_extensions(
    settings: Settings, platform: str, names: Sequence[str], client: FactoryClient
) -> list[str]:
    spec = settings.ensure_schematic(platform)
    missing = sorted(set(names) - set(spec.extensions))
    if missing:
        raise ValidationError(f"not in the {platform} schematic: {', '.join(missing)}")
    spec.extensions = sorted(set(spec.extensions) - set(names))
    refresh(settings, client, [platform])
    return sorted(set(names))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_factory.py tests/test_images.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/factory.py src/talkub/images.py tests/conftest.py tests/test_factory.py tests/test_images.py
git commit -m "feat: Image Factory client and schematic ID refresh"
```

---

### Task 3: `image` commands

**Files:**
- Create: `src/talkub/cli/image.py`, `tests/test_cli_image.py`
- Modify: `src/talkub/cli/main.py`

**Interfaces:**
- Consumes: `FactoryClient`, `boot_media_url`, `images.*`, `repo.ARCHES`, `State.repo()`.
- Produces: Typer sub-app `cli.image.app` registered as `talkub image` with `extensions`, `add`, `remove`, `show`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_image.py`:

```python
import pytest

from helpers import DEFAULT_SCHEMATIC_ID
from talkub.cli.main import main


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_extensions_marks_active(empty_repo, factory_stub, capfd):
    repo = str(empty_repo.root)
    assert _run(["--repo", repo, "image", "add", "siderolabs/tailscale"]) == 0
    capfd.readouterr()
    assert _run(["--repo", repo, "image", "extensions"]) == 0
    out = capfd.readouterr().out
    assert "* siderolabs/tailscale" in out
    assert "  siderolabs/intel-ucode" in out


def test_add_writes_settings_and_hints_upgrade(empty_repo, factory_stub, capfd):
    assert _run(["--repo", str(empty_repo.root), "image", "add", "siderolabs/intel-ucode"]) == 0
    settings = empty_repo.load_settings()
    assert settings.schematics["metal"].extensions == ["siderolabs/intel-ucode"]
    assert settings.schematics["metal"].is_current
    assert settings.schematics["metal"].id != DEFAULT_SCHEMATIC_ID
    assert "upgrade talos" in capfd.readouterr().out


def test_add_unknown_extension_fails(empty_repo, factory_stub, capfd):
    assert _run(["--repo", str(empty_repo.root), "image", "add", "siderolabs/nope"]) == 1
    assert "siderolabs/nope" in capfd.readouterr().err


def test_remove_for_other_platform(empty_repo, factory_stub):
    repo = str(empty_repo.root)
    assert _run(["--repo", repo, "image", "add", "--platform", "hcloud", "siderolabs/tailscale"]) == 0
    assert _run(["--repo", repo, "image", "remove", "--platform", "hcloud", "siderolabs/tailscale"]) == 0
    assert empty_repo.load_settings().schematics["hcloud"].extensions == []


def test_show_prints_installer_and_boot_media(empty_repo, factory_stub, capfd):
    assert _run(["--repo", str(empty_repo.root), "image", "show", "--platform", "hcloud", "--arch", "arm64"]) == 0
    out = capfd.readouterr().out
    assert "factory.talos.dev/hcloud-installer/" in out
    assert "hcloud-arm64.raw.xz" in out
    assert empty_repo.load_settings().schematics["hcloud"].is_current


def test_show_offline_with_current_cache(empty_repo, monkeypatch, capfd):
    monkeypatch.setenv("TALKUB_FACTORY_URL", "http://127.0.0.1:9")
    assert _run(["--repo", str(empty_repo.root), "image", "show"]) == 0
    assert f"metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0" in capfd.readouterr().out


def test_show_rejects_unknown_arch(empty_repo, capfd):
    assert _run(["--repo", str(empty_repo.root), "image", "show", "--arch", "riscv"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_image.py -q`
Expected: failures with exit code 2 (`No such command 'image'`).

- [ ] **Step 3: Implement**

`src/talkub/cli/image.py`:

```python
"""image extensions / add / remove / show."""

from __future__ import annotations

import typer

from talkub import images
from talkub.errors import ValidationError
from talkub.factory import FactoryClient, boot_media_url
from talkub.repo import ARCHES

app = typer.Typer(help="Manage Image Factory schematics (system extensions).", no_args_is_help=True)

PLATFORM = typer.Option(None, "--platform", help="metal or hcloud (default: the cluster platform).")


@app.command("extensions")
def extensions(ctx: typer.Context, platform: str = PLATFORM) -> None:
    """List official extensions for the target Talos version (* = active)."""
    settings = ctx.obj.repo().load_settings()
    active = set(settings.ensure_schematic(platform or settings.platform).extensions)
    for name in FactoryClient().official_extensions(settings.talos_version):
        print(f"{'*' if name in active else ' '} {name}")


@app.command("add")
def add(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., help="Extensions, e.g. siderolabs/tailscale."),
    platform: str = PLATFORM,
) -> None:
    """Add extensions to a platform's schematic."""
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    added = images.add_extensions(settings, target, names, FactoryClient())
    repo.write_settings(settings)
    print(f"{target}: added {', '.join(added) or 'nothing'}; schematic {settings.schematics[target].id}")
    print("roll the new image out to running nodes with `talkub upgrade talos`")


@app.command("remove")
def remove(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., help="Extensions to remove."),
    platform: str = PLATFORM,
) -> None:
    """Remove extensions from a platform's schematic."""
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    removed = images.remove_extensions(settings, target, names, FactoryClient())
    repo.write_settings(settings)
    print(f"{target}: removed {', '.join(removed)}; schematic {settings.schematics[target].id}")
    print("roll the new image out to running nodes with `talkub upgrade talos`")


@app.command("show")
def show(
    ctx: typer.Context,
    platform: str = PLATFORM,
    arch: str = typer.Option("amd64", "--arch", help="amd64 or arm64 (for the boot medium URL)."),
) -> None:
    """Show the installer image and the boot medium download URL."""
    if arch not in ARCHES:
        raise ValidationError(f"unknown arch {arch!r}", hint=f"use one of: {', '.join(ARCHES)}")
    repo = ctx.obj.repo()
    settings = repo.load_settings()
    target = platform or settings.platform
    if images.refresh(settings, FactoryClient(), [target]):
        repo.write_settings(settings)
    print(f"installer:  {settings.installer_image(target)}")
    print(f"boot media: {boot_media_url(settings.schematics[target].id, settings.talos_version, target, arch)}")
```

In `src/talkub/cli/main.py` change the import line to:

```python
from talkub.cli import admin, check, image, keys, session, setup
```

and add after `app.add_typer(sops_cli.app, name="sops")`:

```python
app.add_typer(image.app, name="image")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_image.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/cli/image.py src/talkub/cli/main.py tests/test_cli_image.py
git commit -m "feat: image commands for schematic extensions"
```

---

### Task 4: Node patches and `add_node`

**Files:**
- Create: `src/talkub/nodes.py`, `tests/test_nodes.py`
- Modify: `src/talkub/scaffold.py`
- Delete: `src/talkub/templates/node.yaml.tmpl`

**Interfaces:**
- Consumes: `repo.NodeSpec`, `repo.Repo`, `repo.Settings.from_dict/to_dict/ensure_schematic`, `yamlio`, `atomic_write`.
- Produces:
  - `nodes.LINK = "net0"`, `nodes.FIRST_ETHERNET = 'link.type == 1 && link.kind == ""'`
  - `nodes.NodeOptions(static=None, gateway=None, mac=None, dns=(), disk_selector=None, public_ingress=False)` (frozen) with `check() -> list[str]` (warnings; raises `ValidationError`)
  - `nodes.render_node_patch(node: str, options: NodeOptions = NodeOptions()) -> str`
  - `nodes.node_patch_path(repo, node) -> Path`
  - `nodes.validate_node_name(name) -> None`
  - `nodes.add_node(repo, name, spec: NodeSpec, options: NodeOptions) -> list[str]` (warnings)
  - `scaffold.InitRequest.node_options: dict[str, NodeOptions]` (default empty)

- [ ] **Step 1: Write the failing tests**

`tests/test_nodes.py`:

```python
import shutil
import subprocess

import pytest

from helpers import need
from talkub import yamlio
from talkub.errors import ValidationError
from talkub.nodes import FIRST_ETHERNET, NodeOptions, add_node, node_patch_path, render_node_patch
from talkub.repo import NodeSpec


def _docs(text):
    return [d for d in yamlio.load_all(text) if d]


def test_default_patch_is_hostname_only():
    docs = _docs(render_node_patch("cp-01"))
    assert docs == [{"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "off", "hostname": "cp-01"}]


def test_static_without_mac_uses_first_ethernet_and_warns():
    options = NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1")
    assert any("--mac" in w for w in options.check())
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["LinkAliasConfig"]["selector"]["match"] == FIRST_ETHERNET
    assert kinds["LinkConfig"]["addresses"] == [{"address": "10.0.0.10/24"}]
    assert kinds["LinkConfig"]["routes"] == [{"gateway": "10.0.0.1"}]
    assert "ResolverConfig" not in kinds


def test_static_with_mac_and_dns():
    options = NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="AA:BB:CC:DD:EE:FF", dns=("1.1.1.1",))
    assert options.check() == []
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["LinkAliasConfig"]["selector"]["match"] == 'mac(link.permanent_addr) == "aa:bb:cc:dd:ee:ff"'
    assert kinds["ResolverConfig"]["nameservers"] == [{"address": "1.1.1.1"}]


def test_disk_and_public_ingress_documents():
    options = NodeOptions(disk_selector='disk.transport == "nvme"', public_ingress=True)
    kinds = {d["kind"]: d for d in _docs(render_node_patch("cp-01", options))}
    assert kinds["UnattendedInstallConfig"]["provisioning"]["diskSelector"]["match"] == 'disk.transport == "nvme"'
    rule = kinds["NetworkRuleConfig"]
    assert rule["portSelector"] == {"ports": [80, 443], "protocol": "tcp"}
    assert rule["ingress"] == [{"subnet": "0.0.0.0/0"}, {"subnet": "::/0"}]


@pytest.mark.parametrize(
    "options, message",
    [
        (NodeOptions(static="10.0.0.10/24"), "together"),
        (NodeOptions(gateway="10.0.0.1"), "together"),
        (NodeOptions(static="10.0.0.10", gateway="10.0.0.1"), "prefix"),
        (NodeOptions(static="10.0.0.10/24", gateway="10.0.1.1"), "not in"),
        (NodeOptions(mac="aa:bb:cc:dd:ee:ff"), "--static"),
        (NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="aa:bb"), "MAC"),
        (NodeOptions(dns=("dns.example",)), "DNS"),
        (NodeOptions(static="10.0.0.300/24", gateway="10.0.0.1"), "invalid"),
    ],
)
def test_invalid_options(options, message):
    with pytest.raises(ValidationError) as exc:
        options.check()
    assert message in str(exc.value)


@pytest.fixture(scope="module")
def talos_secrets(tmp_path_factory):
    if shutil.which("talosctl") is None:
        pytest.skip("missing tools: talosctl")
    path = tmp_path_factory.mktemp("talos") / "secrets.yaml"
    subprocess.run(["talosctl", "gen", "secrets", "-o", str(path)], check=True, capture_output=True)
    return path


@need("talosctl")
@pytest.mark.parametrize(
    "options",
    [
        NodeOptions(),
        NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1"),
        NodeOptions(static="10.0.0.10/24", gateway="10.0.0.1", mac="aa:bb:cc:dd:ee:ff", dns=("1.1.1.1", "9.9.9.9")),
        NodeOptions(disk_selector='disk.transport == "nvme"', public_ingress=True),
    ],
)
def test_patches_validate_with_talosctl(tmp_path, talos_secrets, options):
    patch = tmp_path / "node.yaml"
    patch.write_text(render_node_patch("cp-01", options))
    out = tmp_path / "cp.yaml"
    subprocess.run(
        [
            "talosctl", "gen", "config", "demo", "https://10.0.0.10:6443",
            "--with-secrets", str(talos_secrets), "-t", "controlplane", "-o", str(out),
            "--with-docs=false", "--with-examples=false",
            "--install-image", "factory.talos.dev/metal-installer/abc:v1.14.0",
            "--config-patch", f"@{patch}",
        ],
        check=True, capture_output=True,
    )
    result = subprocess.run(["talosctl", "validate", "--config", str(out), "--mode", "metal"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hostname: cp-01" in out.read_text()
    assert "metal-installer/abc:v1.14.0" in out.read_text()


def test_add_node_writes_patch_and_settings(empty_repo):
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud"), NodeOptions())
    assert warnings == []
    settings = empty_repo.load_settings()
    assert settings.nodes["w-01"] == NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    assert "hcloud" in settings.schematics
    assert "hostname: w-01" in node_patch_path(empty_repo, "w-01").read_text()


def test_add_node_rejects_duplicates_and_bad_input(empty_repo):
    with pytest.raises(ValidationError):
        add_node(empty_repo, "cp-01", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "Bad_Name", NodeSpec(ip="10.0.0.12"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "n-02", NodeSpec(ip="10.0.0.12", role="master"), NodeOptions())
    with pytest.raises(ValidationError):
        add_node(empty_repo, "n-03", NodeSpec(ip="10.0.0.13"), NodeOptions(static="10.0.0.13/24", gateway="10.9.9.9"))
    assert set(empty_repo.load_settings().nodes) == {"cp-01"}
    assert not (empty_repo.talos_dir / "nodes").exists()


def test_public_ingress_without_firewall_warns(empty_repo):
    warnings = add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions(public_ingress=True))
    assert any("firewall" in w for w in warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_nodes.py -q`
Expected: `ModuleNotFoundError: No module named 'talkub.nodes'`.

- [ ] **Step 3: Implement `src/talkub/nodes.py`**

```python
"""Nodes: the per-node Talos patch and the `.talkub.yaml` entry."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import NodeSpec, Repo, Settings

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


def add_node(repo: Repo, name: str, spec: NodeSpec, options: NodeOptions) -> list[str]:
    """Write talos/nodes/<name>.yaml and the node entry. Returns warnings."""
    validate_node_name(name)
    warnings = options.check()
    settings = repo.load_settings()
    if name in settings.nodes:
        raise ValidationError(f"node '{name}' already exists")
    path = node_patch_path(repo, name)
    if path.exists():
        raise ValidationError(f"{repo.rel(path)} already exists", hint="remove it or choose another name")
    settings.nodes[name] = spec
    settings = Settings.from_dict(settings.to_dict())  # validates role, platform and arch
    if spec.platform is not None:
        settings.ensure_schematic(spec.platform)
    if options.public_ingress and not (repo.talos_dir / "patches" / "firewall.yaml").exists():
        warnings.append("--public-ingress has no effect without the Talos firewall (see `talkub tailscale enable`)")
    atomic_write(path, render_node_patch(name, options).encode())
    repo.write_settings(settings)
    return warnings
```

- [ ] **Step 4: Switch `scaffold.py` to `nodes.render_node_patch` and add node options**

In `src/talkub/scaffold.py`:
- add `from dataclasses import dataclass, field` (replace the existing `from dataclasses import dataclass`) and `from talkub.nodes import NodeOptions, render_node_patch, validate_node_name`
- delete the function `render_node_patch` defined in `scaffold.py`
- add the last field to `InitRequest`:

```python
    node_options: dict[str, NodeOptions] = field(default_factory=dict)
```

- in `_validate`, add at the end:

```python
    for node in request.nodes:
        validate_node_name(node)
    for options in request.node_options.values():
        options.check()
```

- in `_populate`, replace the node patch loop with:

```python
    for node in request.nodes:
        options = request.node_options.get(node, NodeOptions())
        atomic_write(repo.talos_dir / "nodes" / f"{node}.yaml", render_node_patch(node, options).encode())
```

Delete `src/talkub/templates/node.yaml.tmpl`:

```bash
git rm src/talkub/templates/node.yaml.tmpl
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_nodes.py tests/test_scaffold.py -q`
Expected: all passed.

- [ ] **Step 6: Run the whole suite and commit**

Run: `uv run pytest -q` — expected all passed.

```bash
git add src/talkub/nodes.py src/talkub/scaffold.py tests/test_nodes.py
git commit -m "feat: node patches with static network, disk selector and public ingress"
```

---

### Task 5: `node add` command and `init` node options

**Files:**
- Create: `src/talkub/cli/nodes.py`, `tests/test_cli_nodes.py`
- Modify: `src/talkub/cli/setup.py`, `src/talkub/cli/main.py`

**Interfaces:**
- Consumes: `nodes.NodeOptions`, `nodes.add_node`, `nodes.node_patch_path`, `repo.NodeSpec`, `scaffold.InitRequest(platform, node_options)`.
- Produces: `talkub node add …`; `talkub init … [--platform p] [--static … --gateway … --mac … --dns … --disk-selector … --public-ingress]`; helper `cli.nodes.node_options(static, gateway, mac, dns, disk_selector, public_ingress) -> NodeOptions`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_nodes.py`:

```python
import pytest

from helpers import need
from talkub.cli.main import main
from talkub.repo import NodeSpec

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_node_add_static(empty_repo, capfd):
    code = _run([
        "--repo", str(empty_repo.root), "node", "add", "w-01",
        "--ip", "10.0.0.11", "--role", "worker", "--mgmt", "w-01.tail.ts.net",
        "--static", "10.0.0.11/24", "--gateway", "10.0.0.1", "--dns", "1.1.1.1",
    ])
    assert code == 0
    captured = capfd.readouterr()
    assert "warning: no --mac given" in captured.err
    assert "talos/nodes/w-01.yaml" in captured.out
    assert "apply --insecure w-01" in captured.out
    assert empty_repo.load_settings().nodes["w-01"] == NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.tail.ts.net")
    assert "kind: LinkConfig" in (empty_repo.talos_dir / "nodes" / "w-01.yaml").read_text()


def test_node_add_rejects_bad_gateway(empty_repo, capfd):
    code = _run([
        "--repo", str(empty_repo.root), "node", "add", "w-01", "--ip", "10.0.0.11",
        "--static", "10.0.0.11/24", "--gateway", "192.168.1.1",
    ])
    assert code == 1
    assert "not in 10.0.0.0/24" in capfd.readouterr().err


def test_init_node_options_need_single_node(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run([
        "init", "demo", "--endpoint", "https://10.0.0.10:6443", "--git-url", "https://g",
        "--node", "cp-01=10.0.0.10", "--node", "cp-02=10.0.0.11", "--static", "10.0.0.10/24", "--gateway", "10.0.0.1",
    ])
    assert code == 1
    assert "exactly one --node" in capfd.readouterr().err


@need("age", "age-keygen", "sops", "talosctl")
def test_init_with_platform_and_static_node(tmp_path, software_identity, fake_bin, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    code = _run([
        "init", "demo", "--endpoint", "https://10.0.0.10:6443", "--git-url", "https://g",
        "--node", "cp-01=10.0.0.10", "--platform", "hcloud",
        "--static", "10.0.0.10/24", "--gateway", "10.0.0.1", "--mac", "aa:bb:cc:dd:ee:ff",
    ])
    assert code == 0
    patch = (tmp_path / "demo" / "talos" / "nodes" / "cp-01.yaml").read_text()
    assert 'mac(link.permanent_addr) == "aa:bb:cc:dd:ee:ff"' in patch
    assert "platform: hcloud" in (tmp_path / "demo" / ".talkub.yaml").read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_nodes.py -q`
Expected: failures (`No such command 'node'`, `No such option: --static`).

- [ ] **Step 3: Implement `src/talkub/cli/nodes.py`**

```python
"""node add."""

from __future__ import annotations

import sys

import typer

from talkub.nodes import NodeOptions, add_node, node_patch_path
from talkub.repo import NodeSpec

app = typer.Typer(help="Describe cluster nodes.", no_args_is_help=True)


def node_options(
    static: str | None,
    gateway: str | None,
    mac: str | None,
    dns: list[str] | None,
    disk_selector: str | None,
    public_ingress: bool,
) -> NodeOptions:
    return NodeOptions(
        static=static,
        gateway=gateway,
        mac=mac,
        dns=tuple(dns or ()),
        disk_selector=disk_selector,
        public_ingress=public_ingress,
    )


STATIC = typer.Option(None, "--static", help="Static address with prefix, e.g. 10.0.0.10/24 (default: DHCP).")
GATEWAY = typer.Option(None, "--gateway", help="Default gateway (with --static).")
MAC = typer.Option(None, "--mac", help="MAC address of the interface to configure (with --static).")
DNS = typer.Option(None, "--dns", help="DNS server (repeatable).")
DISK = typer.Option(None, "--disk-selector", help='CEL install disk selector, e.g. disk.transport == "nvme".')
PUBLIC = typer.Option(False, "--public-ingress", help="Open 80/443 from anywhere (with the Talos firewall).")


def print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


@app.command("add")
def add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node name; becomes the hostname."),
    ip: str = typer.Option(..., "--ip", help="Address for the first install (LAN or public)."),
    role: str = typer.Option("controlplane", "--role", help="controlplane or worker."),
    platform: str = typer.Option(None, "--platform", help="metal or hcloud (default: cluster platform)."),
    arch: str = typer.Option(None, "--arch", help="amd64 or arm64 (for boot media URLs)."),
    mgmt: str = typer.Option(None, "--mgmt", help="Address used after installation, e.g. a Tailscale name."),
    static: str = STATIC,
    gateway: str = GATEWAY,
    mac: str = MAC,
    dns: list[str] = DNS,
    disk_selector: str = DISK,
    public_ingress: bool = PUBLIC,
) -> None:
    """Add a node: writes talos/nodes/<name>.yaml and the entry in .talkub.yaml."""
    repo = ctx.obj.repo()
    spec = NodeSpec(ip=ip, role=role, mgmt=mgmt, platform=platform, arch=arch)
    options = node_options(static, gateway, mac, dns, disk_selector, public_ingress)
    print_warnings(add_node(repo, name, spec, options))
    print(f"added node {name}: {repo.rel(node_patch_path(repo, name))}")
    print(f"next: boot the node, then `talkub apply --insecure {name}`")
```

- [ ] **Step 4: Extend `init` in `src/talkub/cli/setup.py`**

Add imports:

```python
from talkub.cli.nodes import DISK, DNS, GATEWAY, MAC, PUBLIC, STATIC, node_options, print_warnings
from talkub.nodes import NodeOptions
```

Replace the `init` function with:

```python
def init(
    name: str = typer.Argument(..., help="Cluster name; also the directory name."),
    endpoint: str = typer.Option(..., "--endpoint", help="Kubernetes API endpoint, e.g. https://10.0.0.10:6443"),
    node: list[str] = typer.Option(..., "--node", help="Control plane node as name=ip (repeatable)."),
    git_url: str = typer.Option(..., "--git-url", help="Git URL ArgoCD pulls this repo from."),
    directory: Path = typer.Option(None, "--dir", help="Target directory (default: ./<name>)."),
    platform: str = typer.Option("metal", "--platform", help="Cluster platform: metal or hcloud."),
    static: str = STATIC,
    gateway: str = GATEWAY,
    mac: str = MAC,
    dns: list[str] = DNS,
    disk_selector: str = DISK,
    public_ingress: bool = PUBLIC,
) -> None:
    """Create a new cluster repo with keys, Talos secrets and the GitOps skeleton."""
    nodes = dict(_parse_node(value) for value in node)
    options = node_options(static, gateway, mac, dns, disk_selector, public_ingress)
    node_opts: dict[str, NodeOptions] = {}
    if options != NodeOptions():
        if len(nodes) != 1:
            raise ValidationError(
                "node options (--static, --mac, --dns, --disk-selector, --public-ingress) need exactly one --node",
                hint="add further nodes with `talkub node add`",
            )
        print_warnings(options.check())
        node_opts = {next(iter(nodes)): options}
    request = InitRequest(
        target=(directory or Path.cwd() / name),
        name=name,
        endpoint=endpoint,
        git_url=git_url,
        nodes=nodes,
        recipients=own_recipients(os.environ, getpass.getuser()),
        platform=platform,
        node_options=node_opts,
    )
    repo = init_cluster(request, Runner())
    print(f"created {repo.root}")
    print("next: add a second admin or backup key (talkub admin add), then git add/commit")
```

In `src/talkub/cli/main.py` change the import line to:

```python
from talkub.cli import admin, check, image, keys, session, setup
from talkub.cli import nodes as nodes_cli
```

and add after `app.add_typer(image.app, name="image")`:

```python
app.add_typer(nodes_cli.app, name="node")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_nodes.py tests/test_cli_manage.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/cli/nodes.py src/talkub/cli/setup.py src/talkub/cli/main.py tests/test_cli_nodes.py
git commit -m "feat: node add command and node options for init"
```

---

### Task 6: Rendering machine configs

**Files:**
- Create: `src/talkub/talos/render.py`, `tests/test_render.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `Context` (`repo`, `runner`, `sops`), `ephemeral.decrypted`, `ephemeral.private_file`, `Settings.node/node_platform/installer_image`, `nodes.node_patch_path`, `nodes.render_node_patch`, `talos.gen.gen_secrets`, `yamlio`.
- Produces:
  - `render.VALIDATE_MODE = {"metal": "metal", "hcloud": "cloud"}`
  - `render.PatchRef(flag: str, path: Path)` (frozen)
  - `render.select_patches(repo, role) -> list[PatchRef]`
  - `render.render_configs(ctx, settings, names) -> dict[str, bytes]`
  - `render.install_summary(config: bytes) -> tuple[str, str]` (installer image, disk selector)
  - conftest fixture `talos_repo` (repo_with_configs + real encrypted Talos secrets + `talos/nodes/cp-01.yaml`)

- [ ] **Step 1: Add the `talos_repo` fixture to `tests/conftest.py`**

```python
@pytest.fixture
def talos_repo(repo_with_configs):
    """repo_with_configs plus real SOPS-encrypted Talos secrets and the cp-01 node patch."""
    _require("talosctl")
    from talkub.context import Context
    from talkub.ephemeral import private_tempdir
    from talkub.fsutil import atomic_write
    from talkub.nodes import node_patch_path, render_node_patch
    from talkub.runner import Runner
    from talkub.talos import gen

    ctx = Context.open(repo_with_configs)
    public = ctx.keyring.public_key()
    with private_tempdir() as work:
        secrets = gen.gen_secrets(Runner(), work)
        atomic_write(
            repo_with_configs.talos_secrets,
            ctx.sops.encrypt(secrets.read_bytes(), filename="secrets.sops.yaml", public_key=public),
        )
    atomic_write(node_patch_path(repo_with_configs, "cp-01"), render_node_patch("cp-01").encode())
    return repo_with_configs
```

- [ ] **Step 2: Write the failing tests**

`tests/test_render.py`:

```python
import pytest

from helpers import DEFAULT_SCHEMATIC_ID, need
from talkub import yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec, SchematicSpec
from talkub.schematic import customization_hash
from talkub.talos.render import PatchRef, install_summary, render_configs, select_patches


def test_select_patches_by_role(empty_repo):
    patches = empty_repo.talos_dir / "patches"
    patches.mkdir(parents=True)
    for name in ("worker-extra.yaml", "common.yaml", "controlplane.yaml", "dns.sops.yaml", "notes.txt"):
        (patches / name).write_text("{}\n")
    assert select_patches(empty_repo, "controlplane") == [
        PatchRef("--config-patch", patches / "common.yaml"),
        PatchRef("--config-patch-control-plane", patches / "controlplane.yaml"),
        PatchRef("--config-patch", patches / "dns.sops.yaml"),
    ]
    assert select_patches(empty_repo, "worker") == [
        PatchRef("--config-patch", patches / "common.yaml"),
        PatchRef("--config-patch", patches / "dns.sops.yaml"),
        PatchRef("--config-patch-worker", patches / "worker-extra.yaml"),
    ]


def test_select_patches_without_directory(empty_repo):
    assert select_patches(empty_repo, "controlplane") == []


pytestmark_bins = need("age", "age-keygen", "sops", "talosctl")


def _docs(config: bytes):
    return [d for d in yamlio.load_all(config.decode()) if d]


@pytestmark_bins
def test_render_controlplane(talos_repo):
    settings = talos_repo.load_settings()
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01"])
    text = configs["cp-01"].decode()
    assert "type: controlplane" in text
    assert "hostname: cp-01" in text
    assert f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0" in text
    assert install_summary(configs["cp-01"]) == (
        f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0",
        'disk.dev_path == "/dev/sda"',
    )


@pytestmark_bins
def test_render_applies_encrypted_patch(talos_repo):
    ctx = Context.open(talos_repo)
    patch = talos_repo.talos_dir / "patches" / "dns.sops.yaml"
    plain = b"apiVersion: v1alpha1\nkind: ResolverConfig\nnameservers:\n  - address: 9.9.9.9\n"
    atomic_write(patch, ctx.sops.encrypt(plain, filename=patch.name, public_key=ctx.keyring.public_key()))
    configs = render_configs(ctx, talos_repo.load_settings(), ["cp-01"])
    assert "9.9.9.9" in configs["cp-01"].decode()


@pytestmark_bins
def test_render_hcloud_worker_and_sans(talos_repo):
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    settings.schematics["hcloud"] = SchematicSpec(id=DEFAULT_SCHEMATIC_ID, hash=customization_hash([], []))
    atomic_write(node_patch_path(talos_repo, "w-01"), render_node_patch("w-01").encode())
    configs = render_configs(Context.open(talos_repo), settings, ["cp-01", "w-01"])
    assert "cp-01.tail.ts.net" in configs["cp-01"].decode()
    worker = configs["w-01"].decode()
    assert "type: worker" in worker
    assert "hcloud-installer" in worker


@pytestmark_bins
def test_invalid_patch_is_reported_with_node_name(talos_repo):
    patches = talos_repo.talos_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / "common.yaml").write_text("machine:\n  network:\n    hostname: clash\n")
    with pytest.raises(ValidationError) as exc:
        render_configs(Context.open(talos_repo), talos_repo.load_settings(), ["cp-01"])
    assert "cp-01" in str(exc.value)
    assert "hostname" in exc.value.hint


@pytestmark_bins
def test_missing_node_patch(talos_repo):
    node_patch_path(talos_repo, "cp-01").unlink()
    with pytest.raises(ValidationError) as exc:
        render_configs(Context.open(talos_repo), talos_repo.load_settings(), ["cp-01"])
    assert "node add" in exc.value.hint


def test_outdated_schematic_is_not_rendered(empty_repo):
    settings = empty_repo.load_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    with pytest.raises(ValidationError):
        render_configs(Context.open(empty_repo), settings, ["cp-01"])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_render.py -q`
Expected: `ModuleNotFoundError: No module named 'talkub.talos.render'`.

- [ ] **Step 4: Implement `src/talkub/talos/render.py`**

```python
"""Render per-node machine configs from secrets + patches and validate them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub import yamlio
from talkub.context import Context
from talkub.ephemeral import decrypted, private_file
from talkub.errors import ValidationError
from talkub.nodes import node_patch_path
from talkub.repo import Repo, Settings
from talkub.runner import Runner

VALIDATE_MODE = {"metal": "metal", "hcloud": "cloud"}
_SECRETS = "secrets.yaml"


@dataclass(frozen=True)
class PatchRef:
    flag: str
    path: Path  # as stored in the repo (possibly *.sops.yaml)


def select_patches(repo: Repo, role: str) -> list[PatchRef]:
    """talos/patches/*.yaml by name: controlplane* / worker* are role-specific, the rest apply to all."""
    directory = repo.talos_dir / "patches"
    if not directory.is_dir():
        return []
    refs: list[PatchRef] = []
    for path in sorted(p for p in directory.glob("*.yaml") if p.is_file()):
        if path.name.startswith("controlplane"):
            if role == "controlplane":
                refs.append(PatchRef("--config-patch-control-plane", path))
        elif path.name.startswith("worker"):
            if role == "worker":
                refs.append(PatchRef("--config-patch-worker", path))
        else:
            refs.append(PatchRef("--config-patch", path))
    return refs


def _plain_key(path: Path) -> str:
    return f"patch-{path.name}"


def render_configs(ctx: Context, settings: Settings, names: Sequence[str]) -> dict[str, bytes]:
    """Render and validate the machine config of each node. Decrypts secrets and patches once."""
    refs: dict[str, list[PatchRef]] = {}
    images: dict[str, str] = {}
    for name in names:
        node = settings.node(name)
        images[name] = settings.installer_image(settings.node_platform(name))
        node_patch = node_patch_path(ctx.repo, name)
        if not node_patch.is_file():
            raise ValidationError(
                f"{ctx.repo.rel(node_patch)} does not exist", hint=f"create it with `talkub node add {name} …`"
            )
        refs[name] = [*select_patches(ctx.repo, node.role), PatchRef("--config-patch", node_patch)]

    encrypted = {_SECRETS: ctx.repo.talos_secrets}
    for ref in (r for node_refs in refs.values() for r in node_refs):
        if ref.path.name.endswith(".sops.yaml"):
            encrypted[_plain_key(ref.path)] = ref.path

    with decrypted(ctx.sops, encrypted) as plain:
        return {name: _render(ctx.runner, settings, name, images[name], refs[name], plain) for name in names}


def _render(
    runner: Runner,
    settings: Settings,
    name: str,
    image: str,
    refs: Sequence[PatchRef],
    plain: Mapping[str, Path],
) -> bytes:
    node = settings.node(name)
    argv = [
        "talosctl", "gen", "config", settings.name, settings.endpoint,
        "--with-secrets", str(plain[_SECRETS]),
        "--talos-version", settings.talos_version,
        "--kubernetes-version", settings.kubernetes_version,
        "--install-image", image,
        "-t", node.role,
        "-o", "-",
        "--with-docs=false",
        "--with-examples=false",
    ]
    sans = [n.mgmt for n in settings.nodes.values() if n.role == "controlplane" and n.mgmt]
    if sans:
        argv += ["--additional-sans", ",".join(sans)]
    for ref in refs:
        source = plain[_plain_key(ref.path)] if ref.path.name.endswith(".sops.yaml") else ref.path
        argv += [ref.flag, f"@{source}"]
    config = runner.run(argv).stdout
    _validate(runner, name, config, VALIDATE_MODE[settings.node_platform(name)])
    return config


def _validate(runner: Runner, name: str, config: bytes, mode: str) -> None:
    with private_file(f"{name}.yaml", config) as path:
        proc = runner.run(["talosctl", "validate", "--config", str(path), "--mode", mode], check=False)
    if proc.returncode != 0:
        output = (proc.stdout or b"") + (proc.stderr or b"")
        raise ValidationError(f"rendered config for node '{name}' is invalid", hint=output.decode().strip())


def install_summary(config: bytes) -> tuple[str, str]:
    """(installer image, disk selector) of a rendered config, for the install confirmation."""
    image = disk = "unknown"
    for doc in yamlio.load_all(config.decode()):
        if not isinstance(doc, Mapping):
            continue
        if doc.get("kind") == "UnattendedInstallConfig":
            image = (doc.get("installer") or {}).get("image", image)
            disk = ((doc.get("provisioning") or {}).get("diskSelector") or {}).get("match", disk)
        elif doc.get("version") == "v1alpha1":
            install = (doc.get("machine") or {}).get("install") or {}
            image = install.get("image", image)
            disk = install.get("disk", disk)
    return str(image), str(disk)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_render.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/talos/render.py tests/test_render.py tests/conftest.py
git commit -m "feat: render and validate per-node machine configs"
```

---

### Task 7: Talos session and plan/apply flows

**Files:**
- Create: `src/talkub/talos/client.py`, `src/talkub/lifecycle.py`, `tests/test_lifecycle.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `render.render_configs`, `render.install_summary`, `images.refresh`, `FactoryClient`, `ephemeral.decrypted/private_file`, `runner.require_tool`, `Context`, errors.
- Produces:
  - `client.TalosSession(runner, talosconfig: Path)` with `argv(node, args) -> list[str]`, `call(node, args) -> int`, `run(node, args, *, check=True)`
  - `client.talos_session(ctx) -> ContextManager[TalosSession]`
  - `lifecycle.MODES = ("auto", "no-reboot", "staged", "try")`
  - `lifecycle.ordered_nodes(settings, names) -> list[str]`
  - `lifecycle.plan(ctx, names) -> None`
  - `lifecycle.apply(ctx, names, *, mode="auto", confirm) -> list[str]` (applied node names)
  - `lifecycle.apply_insecure(ctx, name, *, confirm) -> bool`
  - conftest fixture `talosctl_spy` → path of a log file; the fake `talosctl` forwards `gen`/`validate` to the real binary, logs every other call, exits 97 if `--talosconfig` points to a missing file, exits 1 when an argument equals `10.0.0.99`

- [ ] **Step 1: Add the `talosctl_spy` fixture to `tests/conftest.py`**

```python
@pytest.fixture
def talosctl_spy(fake_bin, tmp_path):
    """Fake talosctl: real binary for gen/validate, logged no-op for node calls."""
    _require("talosctl")
    real = shutil.which("talosctl")
    log = tmp_path / "talosctl.log"
    fake_bin(
        "talosctl",
        f"""case "$1" in gen|validate) exec "{real}" "$@";; esac
printf 'call %s\\n' "$*" >> "{log}"
if [ "$1" = "--talosconfig" ] && [ ! -f "$2" ]; then exit 97; fi
prev=""
for a in "$@"; do
  if [ "$prev" = "--file" ] && [ -f "$a" ]; then printf 'file %s\\n' "$(grep -c 'hostname:' "$a")" >> "{log}"; fi
  [ "$a" = "10.0.0.99" ] && exit 1
  prev="$a"
done
exit 0
""",
    )
    return log
```

- [ ] **Step 2: Write the failing tests**

`tests/test_lifecycle.py`:

```python
import pytest

from helpers import need
from talkub import lifecycle
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _calls(log):
    return [line[5:] for line in log.read_text().splitlines() if line.startswith("call ")] if log.exists() else []


def _add_node(repo, name, ip, role="worker", mgmt=None):
    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip, role=role, mgmt=mgmt)
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, name), render_node_patch(name).encode())


def test_plan_runs_dry_run_with_ephemeral_talosconfig(talos_repo, talosctl_spy, capfd):
    lifecycle.plan(Context.open(talos_repo), [])
    calls = _calls(talosctl_spy)
    assert len(calls) == 1
    assert " -n 10.0.0.10 -e 10.0.0.10 apply-config --file " in calls[0]
    assert calls[0].endswith("--dry-run")
    assert "file 1" in talosctl_spy.read_text()  # rendered config existed and had a hostname
    assert "== cp-01 (10.0.0.10)" in capfd.readouterr().out


def test_plan_uses_mgmt_and_orders_controlplanes_first(talos_repo, talosctl_spy):
    _add_node(talos_repo, "a-worker", "10.0.0.11")
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net")
    talos_repo.write_settings(settings)
    lifecycle.plan(Context.open(talos_repo), [])
    calls = _calls(talosctl_spy)
    assert "-n cp-01.tail.ts.net" in calls[0]
    assert "-n 10.0.0.11" in calls[1]


def test_apply_confirmed_and_declined(talos_repo, talosctl_spy):
    assert lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01"]
    assert _calls(talosctl_spy)[-1].endswith("--mode auto")
    talosctl_spy.unlink()
    assert lifecycle.apply(Context.open(talos_repo), [], mode="no-reboot", confirm=lambda q: False) == []
    calls = _calls(talosctl_spy)
    assert len(calls) == 1 and calls[0].endswith("--dry-run")


def test_apply_stops_at_failing_node(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-bad", "10.0.0.99")
    _add_node(talos_repo, "w-ok", "10.0.0.12")
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "w-bad" in str(exc.value)
    assert not any("10.0.0.12" in call for call in _calls(talosctl_spy))


def test_apply_rejects_unknown_mode_and_node(talos_repo, talosctl_spy):
    with pytest.raises(ValidationError):
        lifecycle.apply(Context.open(talos_repo), [], mode="reboot", confirm=lambda q: True)
    with pytest.raises(ValidationError):
        lifecycle.plan(Context.open(talos_repo), ["nope"])
    assert _calls(talosctl_spy) == []


def test_apply_insecure(talos_repo, talosctl_spy, capfd):
    assert lifecycle.apply_insecure(Context.open(talos_repo), "cp-01", confirm=lambda q: True) is True
    calls = _calls(talosctl_spy)
    assert calls == [calls[0]]
    assert calls[0].startswith("apply-config --insecure -n 10.0.0.10 --file ")
    assert "--talosconfig" not in calls[0]
    out = capfd.readouterr().out
    assert "metal-installer/" in out
    assert 'disk.dev_path == "/dev/sda"' in out


def test_apply_insecure_declined_sends_nothing(talos_repo, talosctl_spy):
    assert lifecycle.apply_insecure(Context.open(talos_repo), "cp-01", confirm=lambda q: False) is False
    assert _calls(talosctl_spy) == []


def test_outdated_schematic_is_refreshed_before_render(talos_repo, talosctl_spy, factory_stub, capfd):
    settings = talos_repo.load_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    talos_repo.write_settings(settings)
    lifecycle.plan(Context.open(talos_repo), [])
    assert [r[0] for r in factory_stub.requests] == ["POST"]
    assert talos_repo.load_settings().schematics["metal"].is_current
    assert "updated schematic IDs" in capfd.readouterr().err


def test_current_schematic_needs_no_factory(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setenv("TALKUB_FACTORY_URL", "http://127.0.0.1:9")
    lifecycle.plan(Context.open(talos_repo), [])
    assert len(_calls(talosctl_spy)) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_lifecycle.py -q`
Expected: `ImportError: cannot import name 'lifecycle'`.

- [ ] **Step 4: Implement**

`src/talkub/talos/client.py`:

```python
"""talosctl against running nodes, with the talosconfig decrypted for the session only."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import ValidationError
from talkub.runner import Runner, require_tool


@dataclass(frozen=True)
class TalosSession:
    runner: Runner
    talosconfig: Path

    def argv(self, node: str, args: Sequence[str]) -> list[str]:
        return ["talosctl", "--talosconfig", str(self.talosconfig), "-n", node, "-e", node, *args]

    def call(self, node: str, args: Sequence[str]) -> int:
        return self.runner.call(self.argv(node, args))

    def run(self, node: str, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return self.runner.run(self.argv(node, args), check=check)


@contextmanager
def talos_session(ctx: Context) -> Iterator[TalosSession]:
    require_tool("talosctl")
    source = ctx.repo.talosconfig
    if not source.is_file():
        raise ValidationError(f"{ctx.repo.rel(source)} does not exist", hint="it is created by `talkub init`")
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        yield TalosSession(ctx.runner, paths["talosconfig"])
```

`src/talkub/lifecycle.py`:

```python
"""plan / apply: send rendered machine configs to nodes."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from talkub import images
from talkub.context import Context
from talkub.ephemeral import private_file
from talkub.errors import CommandFailed, ValidationError
from talkub.factory import FactoryClient
from talkub.repo import Settings
from talkub.runner import require_tool
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.render import install_summary, render_configs

MODES = ("auto", "no-reboot", "staged", "try")
Confirm = Callable[[str], bool]


def ordered_nodes(settings: Settings, names: Sequence[str]) -> list[str]:
    """The requested nodes (default: all), control planes first."""
    for name in names:
        settings.node(name)
    chosen = list(dict.fromkeys(names)) or list(settings.nodes)
    if not chosen:
        raise ValidationError("no nodes defined", hint="add one with `talkub node add`")
    return sorted(chosen, key=lambda n: (settings.nodes[n].role != "controlplane", n))


def _prepare(ctx: Context, names: Sequence[str]) -> tuple[Settings, list[str], dict[str, bytes]]:
    settings = ctx.repo.load_settings()
    order = ordered_nodes(settings, names)
    platforms = sorted({settings.node_platform(name) for name in order})
    if images.refresh(settings, FactoryClient(), platforms):
        ctx.repo.write_settings(settings)
        print("talkub: updated schematic IDs in .talkub.yaml", file=sys.stderr)
    return settings, order, render_configs(ctx, settings, order)


def _dry_run(talos: TalosSession, settings: Settings, name: str, config: bytes) -> None:
    address = settings.mgmt_address(name)
    print(f"== {name} ({address})", flush=True)
    with private_file(f"{name}.yaml", config) as path:
        code = talos.call(address, ["apply-config", "--file", str(path), "--dry-run"])
    if code != 0:
        raise CommandFailed(
            f"dry-run failed for node '{name}'", hint=f"talosctl exited with {code}; is {address} reachable?"
        )


def plan(ctx: Context, names: Sequence[str]) -> None:
    settings, order, configs = _prepare(ctx, names)
    with talos_session(ctx) as talos:
        for name in order:
            _dry_run(talos, settings, name, configs[name])


def apply(ctx: Context, names: Sequence[str], *, mode: str = "auto", confirm: Confirm) -> list[str]:
    if mode not in MODES:
        raise ValidationError(f"unknown mode {mode!r}", hint=f"use one of: {', '.join(MODES)}")
    settings, order, configs = _prepare(ctx, names)
    applied: list[str] = []
    with talos_session(ctx) as talos:
        for name in order:
            _dry_run(talos, settings, name, configs[name])
            if not confirm(f"apply this configuration to {name}?"):
                print(f"skipped {name}")
                continue
            address = settings.mgmt_address(name)
            with private_file(f"{name}.yaml", configs[name]) as path:
                code = talos.call(address, ["apply-config", "--file", str(path), "--mode", mode])
            if code != 0:
                raise CommandFailed(
                    f"apply failed for node '{name}'",
                    hint=f"talosctl exited with {code}; the remaining nodes were not touched",
                )
            applied.append(name)
    return applied


def apply_insecure(ctx: Context, name: str, *, confirm: Confirm) -> bool:
    """First install of a node booted into maintenance mode (no talosconfig, node's `ip`)."""
    settings, _, configs = _prepare(ctx, [name])
    node = settings.node(name)
    image, disk = install_summary(configs[name])
    print(f"node:      {name} at {node.ip} (maintenance mode)")
    print(f"installer: {image}")
    print(f"disk:      {disk}")
    if not confirm(f"install Talos on {name}? The selected disk will be overwritten"):
        print("aborted, nothing was sent")
        return False
    require_tool("talosctl")
    with private_file(f"{name}.yaml", configs[name]) as path:
        code = ctx.runner.call(["talosctl", "apply-config", "--insecure", "-n", node.ip, "--file", str(path)])
    if code != 0:
        raise CommandFailed(
            f"install failed for node '{name}'",
            hint=f"talosctl exited with {code}; is the node booted into maintenance mode at {node.ip}?",
        )
    print(f"next: once {name} has rebooted, check it with `talkub plan {name}`")
    return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_lifecycle.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/talos/client.py src/talkub/lifecycle.py tests/test_lifecycle.py tests/conftest.py
git commit -m "feat: plan and apply machine configs, first install via --insecure"
```

---

### Task 8: `plan` / `apply` commands and README

**Files:**
- Create: `src/talkub/cli/lifecycle.py`, `tests/test_cli_lifecycle.py`
- Modify: `src/talkub/cli/main.py`, `README.md`

**Interfaces:**
- Consumes: `lifecycle.plan`, `lifecycle.apply`, `lifecycle.apply_insecure`, `State.context()`.
- Produces: `talkub plan [NODES…]`, `talkub apply [NODES…] [--mode m] [--yes] [--insecure]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_lifecycle.py`:

```python
import io
import sys

import pytest

from helpers import need
from talkub.cli.main import main

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def _calls(log):
    return [l for l in log.read_text().splitlines() if l.startswith("call ")] if log.exists() else []


def test_plan_command(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "plan"]) == 0
    assert len(_calls(talosctl_spy)) == 1


def test_apply_yes(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "apply", "cp-01", "--yes", "--mode", "staged"]) == 0
    assert _calls(talosctl_spy)[-1].endswith("--mode staged")


def test_apply_declined_on_prompt(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    _run(["--repo", str(talos_repo.root), "apply"])
    calls = _calls(talosctl_spy)
    assert len(calls) == 1 and calls[0].endswith("--dry-run")


def test_insecure_needs_exactly_one_node(talos_repo, talosctl_spy, capfd):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "--yes"]) == 1
    assert "exactly one node" in capfd.readouterr().err


def test_insecure_declined_exits_nonzero(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01"]) == 1
    assert _calls(talosctl_spy) == []


def test_insecure_yes(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01", "--yes"]) == 0
    assert "--insecure" in _calls(talosctl_spy)[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_lifecycle.py -q`
Expected: failures (`No such command 'plan'`).

- [ ] **Step 3: Implement `src/talkub/cli/lifecycle.py`**

```python
"""plan, apply."""

from __future__ import annotations

import typer

from talkub import lifecycle
from talkub.errors import ValidationError

NODES = typer.Argument(None, help="Nodes (default: all, control planes first).")


def plan(ctx: typer.Context, nodes: list[str] = NODES) -> None:
    """Render configs and show what `apply` would change (Talos dry-run)."""
    lifecycle.plan(ctx.obj.context(), nodes or [])


def apply(
    ctx: typer.Context,
    nodes: list[str] = NODES,
    mode: str = typer.Option("auto", "--mode", help="auto, no-reboot, staged or try."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask before applying."),
    insecure: bool = typer.Option(False, "--insecure", help="First install of ONE node in maintenance mode."),
) -> None:
    """Apply rendered configs to nodes (asks per node unless --yes)."""
    context = ctx.obj.context()

    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    if insecure:
        if len(nodes or []) != 1:
            raise ValidationError("--insecure needs exactly one node", hint="e.g. talkub apply --insecure cp-01")
        if not lifecycle.apply_insecure(context, nodes[0], confirm=confirm):
            raise typer.Exit(1)
        return
    lifecycle.apply(context, nodes or [], mode=mode, confirm=confirm)
```

In `src/talkub/cli/main.py` add the import:

```python
from talkub.cli import lifecycle as lifecycle_cli
```

and after `app.add_typer(nodes_cli.app, name="node")`:

```python
app.command("plan")(lifecycle_cli.plan)
app.command("apply")(lifecycle_cli.apply)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_lifecycle.py -q`
Expected: all passed. If `typer.confirm` does not read the patched `sys.stdin`, report it (DONE_WITH_CONCERNS) instead of changing the confirm mechanism.

- [ ] **Step 5: Extend `README.md`**

Insert before the `## Admin changes` section:

````markdown
## Nodes and machine configs

    talkub node add w-01 --ip 10.0.0.11 --role worker \
        --static 10.0.0.11/24 --gateway 10.0.0.1 --mac aa:bb:cc:dd:ee:ff
    talkub image add siderolabs/intel-ucode      # extensions per platform (metal, hcloud)
    talkub image show --platform hcloud --arch arm64   # installer image + boot medium URL
    talkub apply --insecure w-01                  # first install (maintenance mode, node's --ip)
    talkub plan                                   # Talos dry-run for all nodes
    talkub apply --mode no-reboot                 # asks per node; --yes to skip

Machine configs are rendered on the fly from `talos/secrets.sops.yaml`, `talos/patches/*.yaml`
(`controlplane*` / `worker*` are role-specific, `*.sops.yaml` patches are decrypted) and
`talos/nodes/<node>.yaml`; they are never written to the repo.
````

- [ ] **Step 6: Run the whole suite and commit**

Run: `uv run pytest -q` — expected all passed.

```bash
git add src/talkub/cli/lifecycle.py src/talkub/cli/main.py tests/test_cli_lifecycle.py README.md
git commit -m "feat: plan and apply commands"
```

---

## Out of scope for this plan (Phase 2b/2c)

2b: `bootstrap`, `upgrade talos|k8s`, `status`, `secret seal`, `secret backup-sealing-key`.
2c: `init --tailscale`, `tailscale enable` (writes `talos/patches/tailscale.sops.yaml` and `firewall.yaml`,
sets `mgmt`), Phase-1 `sops edit` follow-ups.
