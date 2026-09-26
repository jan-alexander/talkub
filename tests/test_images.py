import pytest
from helpers import DEFAULT_SCHEMATIC_ID, sample_settings

from talkub import images
from talkub.config import NodeSpec
from talkub.core.errors import ValidationError
from talkub.factory import FactoryClient


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
    assert images.add_extensions(
        settings, "metal", ["siderolabs/tailscale", "siderolabs/intel-ucode"], client
    ) == [
        "siderolabs/intel-ucode",
        "siderolabs/tailscale",
    ]
    assert (
        images.add_extensions(settings, "metal", ["siderolabs/tailscale"], client) == []
    )
    assert settings.schematics["metal"].extensions == [
        "siderolabs/intel-ucode",
        "siderolabs/tailscale",
    ]
    assert settings.schematics["metal"].is_current
    assert images.remove_extensions(
        settings, "metal", ["siderolabs/tailscale"], client
    ) == ["siderolabs/tailscale"]
    assert settings.schematics["metal"].extensions == ["siderolabs/intel-ucode"]
    with pytest.raises(ValidationError):
        images.remove_extensions(settings, "metal", ["siderolabs/tailscale"], client)


def test_used_platforms():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", platform="hcloud")
    assert images.used_platforms(settings) == ["hcloud", "metal"]
