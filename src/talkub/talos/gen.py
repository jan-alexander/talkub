"""talosctl gen helpers used by `init` (and later by rendering in phase 2)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner


@dataclass(frozen=True)
class Defaults:
    talos_version: str
    kubernetes_version: str
    install_image: str


def gen_secrets(runner: Runner, workdir: Path) -> Path:
    path = workdir / "secrets.yaml"
    runner.run(["talosctl", "gen", "secrets", "-o", str(path)])
    return path


def _image_tag(reference: str) -> tuple[str, str]:
    image, _, tag = reference.rpartition(":")
    return image, tag


def parse_defaults(text: str) -> Defaults:
    """Read installer image and kubelet version from a generated controlplane config.

    Talos >= 1.12 emits separate documents (UnattendedInstallConfig, KubeletConfig);
    older versions keep both inside the v1alpha1 document.
    """
    docs = [doc for doc in yamlio.load_all(text) if isinstance(doc, Mapping)]
    installer = kubelet = None
    for doc in docs:
        kind = doc.get("kind")
        if kind == "UnattendedInstallConfig":
            installer = (doc.get("installer") or {}).get("image")
        elif kind == "KubeletConfig":
            kubelet = doc.get("image")
        elif doc.get("version") == "v1alpha1" and isinstance(
            doc.get("machine"), Mapping
        ):
            machine = doc["machine"]
            installer = installer or (machine.get("install") or {}).get("image")
            kubelet = kubelet or (machine.get("kubelet") or {}).get("image")
    if not installer or not kubelet:
        raise ValidationError(
            "could not detect installer image and Kubernetes version from talosctl output",
            hint="check `talosctl version --client`; this talosctl may be unsupported",
        )
    image, talos_version = _image_tag(installer)
    _, kubelet_tag = _image_tag(kubelet)
    return Defaults(
        talos_version=talos_version,
        kubernetes_version=kubelet_tag.lstrip("v"),
        install_image=image,
    )


def detect_defaults(
    runner: Runner, name: str, endpoint: str, secrets: Path
) -> Defaults:
    argv = [
        "talosctl",
        "gen",
        "config",
        name,
        endpoint,
        "--with-secrets",
        str(secrets),
        "-t",
        "controlplane",
        "-o",
        "-",
        "--with-docs=false",
        "--with-examples=false",
    ]
    return parse_defaults(runner.run(argv).stdout.decode())


def gen_talosconfig(
    runner: Runner,
    name: str,
    endpoint: str,
    secrets: Path,
    workdir: Path,
    *,
    endpoints: Sequence[str],
    node: str,
) -> Path:
    path = workdir / "talosconfig"
    runner.run(
        [
            "talosctl",
            "gen",
            "config",
            name,
            endpoint,
            "--with-secrets",
            str(secrets),
            "-t",
            "talosconfig",
            "-o",
            str(path),
        ]
    )
    runner.run(
        ["talosctl", "--talosconfig", str(path), "config", "endpoint", *endpoints]
    )
    runner.run(["talosctl", "--talosconfig", str(path), "config", "node", node])
    return path
