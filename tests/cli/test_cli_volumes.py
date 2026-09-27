import io
import sys

import pytest

from talkub.cli.main import main
from talkub.talos import disks
from talkub.talos.nodes import read_node_documents, render_node_patch
from talkub.talos.volumes.model import Disk, GiB, Inventory

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", serial="S1")
HDD = Disk("sda", "/dev/sda", 4096 * GiB, transport="sata", rotational=True, serial="H1")
STICK = Disk("sdb", "/dev/sdb", 32 * GiB, transport="usb", serial="U1")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def read_calls():
    return []


@pytest.fixture
def node_repo(empty_repo, monkeypatch, read_calls):
    path = empty_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_node_patch("cp-01"))

    def fake_read(ctx, name, mode):
        read_calls.append(mode)
        return Inventory((NVME, HDD, STICK), (), None, installed=False)

    monkeypatch.setattr(disks, "read", fake_read)
    return empty_repo


def _answers(monkeypatch, *lines):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))


def _kinds(repo):
    return {(d["kind"], d.get("name")): d for d in read_node_documents(repo.talos_dir / "nodes" / "cp-01.yaml")}


def test_full_dialog_writes_install_ephemeral_and_volumes(node_repo, read_calls, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "2",  # EPHEMERAL: limit
        "1",  # fixed size
        "40GiB",
        "n",  # do not encrypt EPHEMERAL
        "y",  # add a volume
        "local",
        "1",  # partition with filesystem
        "1",  # disk nvme0n1
        "1",  # recommended selector
        "1",  # fixed size
        "100GiB",
        "1",  # xfs
        "y",  # encrypt
        "y",  # add another volume
        "ceph",
        "2",  # raw
        "2",  # disk sda
        "1",
        "2",  # rest of the disk
        "n",  # no grow
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "/dev/nvme0n1 ★" in out and "u-local 100.0 GiB" in out
    docs = _kinds(node_repo)
    assert docs[("UnattendedInstallConfig", None)]["provisioning"]["diskSelector"]["match"] == (
        '!disk.readonly && !disk.cdrom && disk.transport == "nvme"'
    )
    assert docs[("VolumeConfig", "EPHEMERAL")]["provisioning"] == {
        "minSize": "40GiB",
        "maxSize": "40GiB",
        "grow": False,
    }
    local = docs[("UserVolumeConfig", "local")]
    assert local["provisioning"]["diskSelector"]["match"].endswith("system_disk")
    assert local["encryption"]["keys"][0]["nodeID"] == {}
    assert docs[("RawVolumeConfig", "ceph")]["provisioning"]["minSize"] == "1GiB"
    assert ("HostnameConfig", None) in docs
    assert read_calls == ["auto"]


def test_volume_on_unlimited_system_disk_is_rejected_and_not_written(node_repo, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "1",  # EPHEMERAL: whole system disk
        "y",  # add a volume on the system disk
        "local",
        "1",  # partition with filesystem
        "1",  # disk nvme0n1
        "1",  # recommended selector
        "1",  # fixed size
        "100GiB",
        "1",  # xfs
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "✗ u-local" in out and "limit EPHEMERAL" in out and "volume not added" in out
    assert ("UserVolumeConfig", "local") not in _kinds(node_repo)


def test_usb_install_disk_warns(node_repo, monkeypatch, capfd):
    _answers(monkeypatch, "3", "1", "1", "n", "y")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "USB" in capfd.readouterr().out
    match = _kinds(node_repo)[("UnattendedInstallConfig", None)]["provisioning"]["diskSelector"]["match"]
    assert match == '!disk.readonly && !disk.cdrom && disk.transport == "usb"'


def test_ephemeral_limit_that_cannot_be_provisioned_is_asked_again(node_repo, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "2",  # EPHEMERAL: limit
        "1",  # fixed size
        "600GiB",  # larger than the 512 GiB disk
        "n",  # do not encrypt EPHEMERAL
        "1",  # asked again: whole system disk
        "n",  # no volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "✗ EPHEMERAL" in out and "EPHEMERAL limit not used" in out
    assert ("VolumeConfig", "EPHEMERAL") not in _kinds(node_repo)


def test_declining_the_write_changes_nothing(node_repo, monkeypatch):
    before = (node_repo.talos_dir / "nodes" / "cp-01.yaml").read_text()
    _answers(monkeypatch, "1", "1", "1", "n", "n")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 1
    assert (node_repo.talos_dir / "nodes" / "cp-01.yaml").read_text() == before


def test_duplicate_volume_name_is_asked_again(node_repo, monkeypatch, capfd):
    path = node_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.write_text(
        render_node_patch("cp-01")
        + "---\napiVersion: v1alpha1\nkind: UserVolumeConfig\nname: data\nprovisioning:\n"
        + '  diskSelector:\n    match: disk.transport == "sata"\n  minSize: 1GiB\n'
    )
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "2",  # EPHEMERAL: limit
        "1",  # fixed size
        "40GiB",
        "n",  # do not encrypt EPHEMERAL
        "y",  # add a volume
        "data",  # rejected: exists
        "data2",
        "1",  # partition with filesystem
        "1",  # disk nvme0n1, next to EPHEMERAL
        "1",  # recommended selector
        "1",  # fixed size
        "10GiB",
        "1",  # xfs
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "already exists" in capfd.readouterr().out
    names = [name for kind, name in _kinds(node_repo) if kind == "UserVolumeConfig"]
    assert names == ["data", "data2"]


def test_flags_exclude_each_other(node_repo, capfd):
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01", "--insecure", "--installed"]) == 1
    assert "exclude each other" in capfd.readouterr().err


def test_unknown_node(node_repo, capfd):
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "nope"]) == 1


def _set_version(repo, version):
    settings = repo.load_settings()
    settings.talos_version = version
    repo.write_settings(settings)


def _inventory(monkeypatch, *disk_list):
    monkeypatch.setattr(disks, "read", lambda ctx, name, mode: Inventory(disk_list, (), None, installed=False))


def test_without_install_step_the_system_disk_is_unknown_and_volumes_still_work(node_repo, monkeypatch, capfd):
    _set_version(node_repo, "v1.13.0")
    _answers(
        monkeypatch,
        "y",  # add a volume (no install and no EPHEMERAL question before v1.14)
        "data",
        "1",  # partition with filesystem
        "2",  # disk sda
        "1",  # recommended selector
        "1",  # fixed size
        "100GiB",
        "1",  # xfs
        "n",  # no encryption; the plan-wide "system disk unknown" warning asks nothing
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "no UnattendedInstallConfig" in out and "EPHEMERAL is not configured" in out
    assert "system disk unknown: EPHEMERAL" in out and "volume not added" not in out
    docs = _kinds(node_repo)
    assert ("UnattendedInstallConfig", None) not in docs and ("VolumeConfig", "EPHEMERAL") not in docs
    assert docs[("UserVolumeConfig", "data")]["provisioning"]["maxSize"] == "100GiB"


def test_talos_before_1_11_is_rejected_before_reading_disks(node_repo, read_calls, capfd):
    _set_version(node_repo, "v1.10.5")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 1
    assert "needs Talos v1.11 or newer (talosVersion is v1.10.5)" in capfd.readouterr().err
    assert read_calls == []


def test_names_of_unparsed_volume_documents_are_taken(node_repo, monkeypatch, capfd):
    path = node_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.write_text(
        render_node_patch("cp-01")
        + "---\napiVersion: v1alpha1\nkind: UserVolumeConfig\nname: scratch\nvolumeType: directory\n"
    )
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "1",  # EPHEMERAL: whole system disk
        "y",  # add a volume
        "scratch",  # rejected: a directory volume of that name exists
        "bulk",
        "1",  # partition with filesystem
        "2",  # disk sda
        "1",  # recommended selector
        "1",  # fixed size
        "10GiB",
        "1",  # xfs
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "volume 'scratch' already exists" in capfd.readouterr().out
    docs = _kinds(node_repo)
    assert docs[("UserVolumeConfig", "scratch")]["volumeType"] == "directory"
    assert ("UserVolumeConfig", "bulk") in docs


def test_usb_volume_disk_warns(node_repo, monkeypatch, capfd):
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "1",  # EPHEMERAL: whole system disk
        "y",  # add a volume
        "stick",
        "1",  # partition with filesystem
        "3",  # disk sdb (USB)
        "1",  # recommended selector
        "1",  # fixed size
        "10GiB",
        "1",  # xfs
        "n",  # no encryption
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "warning: /dev/sdb is a USB disk" in capfd.readouterr().out
    match = _kinds(node_repo)[("UserVolumeConfig", "stick")]["provisioning"]["diskSelector"]["match"]
    assert 'disk.transport != "usb"' not in match


def test_custom_selector_hint_and_keep_prompt_for_the_new_volume(node_repo, monkeypatch, capfd):
    twin_a = Disk("sdc", "/dev/sdc", 1024 * GiB, transport="sata")
    twin_b = Disk("sdd", "/dev/sdd", 1024 * GiB, transport="sata")
    _inventory(monkeypatch, NVME, twin_a, twin_b)
    _answers(
        monkeypatch,
        "1",  # install disk: nvme0n1
        "1",  # recommended selector
        "1",  # EPHEMERAL: whole system disk
        "y",  # add a volume
        "twin",
        "1",  # partition with filesystem
        "2",  # disk sdc: no unique selector
        "1",  # own CEL expression (the only option)
        'disk.dev_path == "/dev/sdc"',
        "1",  # fixed size
        "10GiB",
        "1",  # xfs
        "n",  # no encryption
        "n",  # "custom selector, not simulated" concerns this volume: do not keep it
        "n",  # no more volumes
        "y",  # write
    )
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    out = capfd.readouterr().out
    assert "no selector matches only /dev/sdc; the disk has no serial or WWID" in out
    assert "keep this volume?" in out
    assert ("UserVolumeConfig", "twin") not in _kinds(node_repo)


def test_no_usable_disk(node_repo, monkeypatch, capfd):
    _inventory(monkeypatch, Disk("sr0", "/dev/sr0", GiB, transport="sata", readonly=True, cdrom=True))
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 1
    assert "no usable disk on cp-01" in capfd.readouterr().err


def test_non_mapping_documents_in_the_node_patch_are_ignored(node_repo, monkeypatch):
    path = node_repo.talos_dir / "nodes" / "cp-01.yaml"
    path.write_text(render_node_patch("cp-01") + "---\n- op: remove\n  path: /machine/install\n")
    _answers(monkeypatch, "1", "1", "1", "n", "y")
    assert _run(["--repo", str(node_repo.root), "node", "volumes", "cp-01"]) == 0
    assert "- op: remove" in path.read_text()
