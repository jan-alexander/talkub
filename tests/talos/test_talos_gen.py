import re

import pytest
from helpers import need

from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner
from talkub.talos import gen

MULTIDOC = """version: v1alpha1
machine:
    type: controlplane
---
apiVersion: v1alpha1
kind: UnattendedInstallConfig
installer:
    image: factory.talos.dev/metal-installer/376567:v1.14.0
---
apiVersion: v1alpha1
kind: KubeletConfig
image: ghcr.io/siderolabs/kubelet:v1.37.0
"""

LEGACY = """version: v1alpha1
machine:
    install:
        image: ghcr.io/siderolabs/installer:v1.9.0
    kubelet:
        image: ghcr.io/siderolabs/kubelet:v1.32.0
"""


def test_parse_defaults_multidoc():
    assert gen.parse_defaults(MULTIDOC) == gen.Defaults(
        talos_version="v1.14.0",
        kubernetes_version="1.37.0",
        install_image="factory.talos.dev/metal-installer/376567",
    )


def test_parse_defaults_legacy_v1alpha1():
    assert gen.parse_defaults(LEGACY) == gen.Defaults(
        "v1.9.0", "1.32.0", "ghcr.io/siderolabs/installer"
    )


def test_parse_defaults_unknown_layout():
    with pytest.raises(ValidationError):
        gen.parse_defaults("version: v1alpha1\n")


@need("talosctl")
def test_detect_defaults_and_talosconfig(tmp_path):
    runner = Runner()
    secrets = gen.gen_secrets(runner, tmp_path)
    defaults = gen.detect_defaults(runner, "demo", "https://10.0.0.10:6443", secrets)
    assert re.fullmatch(r"v\d+\.\d+\.\d+", defaults.talos_version)
    assert re.fullmatch(r"\d+\.\d+\.\d+", defaults.kubernetes_version)
    config = gen.gen_talosconfig(
        runner,
        "demo",
        "https://10.0.0.10:6443",
        secrets,
        tmp_path,
        endpoints=["10.0.0.10"],
        node="10.0.0.10",
    )
    context = yamlio.load(config.read_text())["contexts"]["demo"]
    assert list(context["endpoints"]) == ["10.0.0.10"]
    assert list(context["nodes"]) == ["10.0.0.10"]
