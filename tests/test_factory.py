import hashlib

import pytest

from talkub.errors import CommandFailed
from talkub.factory import FactoryClient, boot_media_url
from talkub.schematic import schematic_document


def test_schematic_id_posts_the_document(factory_stub):
    body = schematic_document(["siderolabs/tailscale"], [])
    assert (
        FactoryClient().schematic_id(["siderolabs/tailscale"], [])
        == hashlib.sha256(body.encode()).hexdigest()
    )
    assert factory_stub.requests == [("POST", "/schematics", body.encode())]


def test_official_extensions_sorted(factory_stub):
    factory_stub.extensions = ["siderolabs/tailscale", "siderolabs/amdgpu"]
    assert FactoryClient().official_extensions("v1.14.0") == [
        "siderolabs/amdgpu",
        "siderolabs/tailscale",
    ]
    assert factory_stub.requests[0][:2] == (
        "GET",
        "/version/v1.14.0/extensions/official",
    )


def test_server_error_is_reported(factory_stub):
    factory_stub.fail = True
    with pytest.raises(CommandFailed) as exc:
        FactoryClient().schematic_id([], [])
    assert "offline" in exc.value.hint


def test_unreachable_factory(monkeypatch):
    with pytest.raises(CommandFailed):
        FactoryClient(base_url="http://127.0.0.1:9", timeout=2).official_extensions(
            "v1.14.0"
        )


def test_boot_media_urls():
    assert (
        boot_media_url("abc", "v1.14.0", "metal", "amd64")
        == "https://factory.talos.dev/image/abc/v1.14.0/metal-amd64.iso"
    )
    assert (
        boot_media_url("abc", "v1.14.0", "hcloud", "arm64")
        == "https://factory.talos.dev/image/abc/v1.14.0/hcloud-arm64.raw.xz"
    )
