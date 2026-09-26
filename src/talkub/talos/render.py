"""Render per-node machine configs from secrets + patches and validate them."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub.config import Repo, Settings
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.ephemeral import decrypted, private_file
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner
from talkub.nodes import node_patch_path

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


def render_configs(
    ctx: Context, settings: Settings, names: Sequence[str]
) -> dict[str, bytes]:
    """Render and validate the machine config of each node. Decrypts secrets and patches once."""
    refs: dict[str, list[PatchRef]] = {}
    images: dict[str, str] = {}
    for name in names:
        node = settings.node(name)
        images[name] = settings.installer_image(settings.node_platform(name))
        node_patch = node_patch_path(ctx.repo, name)
        if not node_patch.is_file():
            raise ValidationError(
                f"{ctx.repo.rel(node_patch)} does not exist",
                hint=f"create it with `talkub node add {name} …`",
            )
        refs[name] = [
            *select_patches(ctx.repo, node.role),
            PatchRef("--config-patch", node_patch),
        ]

    encrypted = {_SECRETS: ctx.repo.talos_secrets}
    for ref in (r for node_refs in refs.values() for r in node_refs):
        if ref.path.name.endswith(".sops.yaml"):
            encrypted[_plain_key(ref.path)] = ref.path

    with decrypted(ctx.sops, encrypted) as plain:
        return {
            name: _render(ctx.runner, settings, name, images[name], refs[name], plain)
            for name in names
        }


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
        "talosctl",
        "gen",
        "config",
        settings.name,
        settings.endpoint,
        "--with-secrets",
        str(plain[_SECRETS]),
        "--talos-version",
        settings.talos_version,
        "--kubernetes-version",
        settings.kubernetes_version,
        "--install-image",
        image,
        "-t",
        node.role,
        "-o",
        "-",
        "--with-docs=false",
        "--with-examples=false",
    ]
    sans = _cert_sans(settings, name)
    if sans:
        argv += ["--additional-sans", ",".join(sans)]
    for ref in refs:
        source = (
            plain[_plain_key(ref.path)]
            if ref.path.name.endswith(".sops.yaml")
            else ref.path
        )
        argv += [ref.flag, f"@{source}"]
    config = runner.run(argv).stdout
    _validate(runner, name, config, VALIDATE_MODE[settings.node_platform(name)])
    return config


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _cert_sans(settings: Settings, name: str) -> list[str]:
    """Extra certificate names for node `name`: the control planes' names (the Kubernetes
    API and talosctl endpoints) plus the node's own names, since talkub connects to every
    node by its management address (`-n/-e <mgmt>`), after lockdown `<node>.<tailnet>`."""
    tailnet = settings.tailscale.tailnet if settings.tailscale is not None else None
    sans = [
        n.mgmt for n in settings.nodes.values() if n.role == "controlplane" and n.mgmt
    ]
    if tailnet is not None:
        # So the LAN `apply` recommended between `tailscale enable` and `tailscale lockdown`
        # already issues certificates for the tailnet names lockdown will switch clients to.
        sans += [
            f"{cp_name}.{tailnet}"
            for cp_name, cp in settings.nodes.items()
            if cp.role == "controlplane"
        ]
    own = settings.node(name).mgmt
    if own and not _is_ip(own):
        sans.append(own)
    if tailnet is not None:
        sans.append(f"{name}.{tailnet}")
    return list(dict.fromkeys(sans))


def _validate(runner: Runner, name: str, config: bytes, mode: str) -> None:
    with private_file(f"{name}.yaml", config) as path:
        proc = runner.run(
            ["talosctl", "validate", "--config", str(path), "--mode", mode], check=False
        )
    if proc.returncode != 0:
        output = (proc.stdout or b"") + (proc.stderr or b"")
        raise ValidationError(
            f"rendered config for node '{name}' is invalid",
            hint=output.decode().strip(),
        )


def install_summary(config: bytes) -> tuple[str, str]:
    """(installer image, disk selector) of a rendered config, for the install confirmation."""
    image = disk = "unknown"
    for doc in yamlio.load_all(config.decode()):
        if not isinstance(doc, Mapping):
            continue
        if doc.get("kind") == "UnattendedInstallConfig":
            image = (doc.get("installer") or {}).get("image", image)
            disk = ((doc.get("provisioning") or {}).get("diskSelector") or {}).get(
                "match", disk
            )
        elif doc.get("version") == "v1alpha1":
            install = (doc.get("machine") or {}).get("install") or {}
            image = install.get("image", image)
            disk = install.get("disk", disk)
    return str(image), str(disk)
