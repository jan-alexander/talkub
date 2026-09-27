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
    assert (
        _run(
            [
                "--repo",
                repo,
                "image",
                "add",
                "--platform",
                "hcloud",
                "siderolabs/tailscale",
            ]
        )
        == 0
    )
    assert (
        _run(
            [
                "--repo",
                repo,
                "image",
                "remove",
                "--platform",
                "hcloud",
                "siderolabs/tailscale",
            ]
        )
        == 0
    )
    assert empty_repo.load_settings().schematics["hcloud"].extensions == []


def test_show_prints_installer_and_boot_media(empty_repo, factory_stub, capfd):
    assert (
        _run(
            [
                "--repo",
                str(empty_repo.root),
                "image",
                "show",
                "--platform",
                "hcloud",
                "--arch",
                "arm64",
            ]
        )
        == 0
    )
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


@pytest.mark.parametrize(
    "argv",
    [
        ["image", "add", "--platform", "vmware", "x"],
        ["image", "remove", "--platform", "vmware", "x"],
        ["image", "extensions", "--platform", "vmware"],
        ["image", "show", "--platform", "vmware"],
    ],
)
def test_unknown_platform_fails_before_network(empty_repo, factory_stub, capfd, argv):
    assert _run(["--repo", str(empty_repo.root), *argv]) == 1
    err = capfd.readouterr().err
    assert "unknown platform 'vmware'" in err
    assert "metal, hcloud" in err
    assert factory_stub.requests == []
