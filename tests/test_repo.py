import pytest
from helpers import DEFAULT_SCHEMATIC_ID, sample_settings

from talkub.errors import RepoNotFound, ValidationError
from talkub.repo import (
    MARKER,
    ChartSpec,
    NodeSpec,
    Repo,
    SchematicSpec,
    Settings,
    TailscaleSpec,
    find_repo,
    parse_installer_image,
)
from talkub.schematic import customization_hash


def test_settings_roundtrip(tmp_path):
    settings = sample_settings()
    settings.charts["argocd"] = ChartSpec("https://argoproj.github.io/argo-helm", "argo-cd", "10.9.2")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "talosVersion: v1.14.0" in repo.settings_file.read_text()


def test_find_repo_searches_upwards(tmp_path):
    Repo(tmp_path).write_settings(sample_settings())
    nested = tmp_path / "gitops" / "apps"
    nested.mkdir(parents=True)
    assert find_repo(nested).root == tmp_path.resolve()


def test_find_repo_explicit_path_must_be_repo(tmp_path):
    with pytest.raises(RepoNotFound):
        find_repo(tmp_path, explicit=tmp_path)


def test_find_repo_not_found(tmp_path):
    with pytest.raises(RepoNotFound) as exc:
        find_repo(tmp_path)
    assert "talkub init" in exc.value.hint


def _data(**overrides):
    data = sample_settings().to_dict()
    data.update(overrides)
    return data


def test_invalid_role_rejected():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(nodes={"n1": {"ip": "10.0.0.2", "role": "master"}}))
    assert "role" in str(exc.value)


def test_endpoint_must_be_https():
    with pytest.raises(ValidationError):
        Settings.from_dict(_data(endpoint="10.0.0.10:6443"))


def test_unquoted_numeric_version_gives_hint():
    with pytest.raises(ValidationError) as exc:
        Settings.from_dict(_data(kubernetesVersion=1.37))
    assert "quote" in exc.value.hint


def test_invalid_yaml_reported(tmp_path):
    (tmp_path / MARKER).write_text("name: [unclosed\n")
    with pytest.raises(ValidationError):
        Repo(tmp_path).load_settings()


def test_sops_files_excludes_sops_config_and_git(tmp_path):
    repo = Repo(tmp_path)
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n")
    (tmp_path / "talos").mkdir()
    (tmp_path / "talos" / "secrets.sops.yaml").write_text("a: b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.sops.yaml").write_text("a: b\n")
    assert repo.sops_files() == [tmp_path / "talos" / "secrets.sops.yaml"]


def test_node_defaults_to_controlplane():
    settings = Settings.from_dict(_data(nodes={"cp-01": {"ip": "10.0.0.10"}}))
    assert settings.nodes["cp-01"] == NodeSpec(ip="10.0.0.10", role="controlplane")


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
