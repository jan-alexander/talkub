import pytest
from helpers import need

from talkub.talos.volumes.model import Kind, VolumeSpec
from talkub.talos.volumes.render import ENCRYPTION, install_document, spec_from_document, volume_document
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.sizes import parse_size

NVME = parse('!disk.readonly && !disk.cdrom && disk.transport == "nvme" && !system_disk')


def test_partition_document():
    spec = VolumeSpec("data", Kind.PARTITION, NVME, parse_size("100GiB"), parse_size("100GiB"), encrypted=True)
    assert volume_document(spec) == {
        "apiVersion": "v1alpha1",
        "kind": "UserVolumeConfig",
        "name": "data",
        "provisioning": {"diskSelector": {"match": NVME.to_cel()}, "minSize": "100GiB", "maxSize": "100GiB"},
        "filesystem": {"type": "xfs"},
        "encryption": ENCRYPTION,
    }
    assert ENCRYPTION == {"provider": "luks2", "keys": [{"slot": 0, "nodeID": {}, "lockToState": True}]}


def test_raw_disk_and_ephemeral_documents():
    raw = volume_document(VolumeSpec("ceph", Kind.RAW, NVME, parse_size("1GiB"), grow=True))
    assert raw["kind"] == "RawVolumeConfig" and "filesystem" not in raw and raw["provisioning"]["grow"] is True
    disk = volume_document(VolumeSpec("lvm", Kind.DISK, NVME, filesystem="ext4"))
    assert disk["volumeType"] == "disk" and disk["provisioning"] == {"diskSelector": {"match": NVME.to_cel()}}
    ephemeral = volume_document(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("100MiB"), parse_size("40%")))
    assert ephemeral == {
        "apiVersion": "v1alpha1",
        "kind": "VolumeConfig",
        "name": "EPHEMERAL",
        "provisioning": {"minSize": "100MiB", "maxSize": "40%", "grow": False},
    }


def test_install_document_keeps_installer_and_wipe():
    existing = {
        "apiVersion": "v1alpha1",
        "kind": "UnattendedInstallConfig",
        "installer": {"image": "x"},
        "provisioning": {"wipe": False, "diskSelector": {"match": "old"}},
    }
    doc = install_document(NVME, existing)
    assert doc["installer"] == {"image": "x"}
    assert doc["provisioning"] == {"wipe": False, "diskSelector": {"match": NVME.to_cel()}}
    assert existing["provisioning"]["diskSelector"]["match"] == "old"
    assert install_document(NVME, None)["kind"] == "UnattendedInstallConfig"


@pytest.mark.parametrize(
    "spec",
    [
        VolumeSpec("data", Kind.PARTITION, NVME, parse_size("10GiB"), parse_size("10GiB"), filesystem="ext4"),
        VolumeSpec("ceph", Kind.RAW, NVME, parse_size("1GiB"), encrypted=True),
        VolumeSpec("lvm", Kind.DISK, NVME),
        VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB")),
    ],
)
def test_spec_round_trips(spec):
    assert spec_from_document(volume_document(spec)) == spec


def test_spec_from_foreign_documents():
    assert spec_from_document({"kind": "HostnameConfig"}) is None
    assert spec_from_document({"kind": "UserVolumeConfig", "name": "d", "volumeType": "directory"}) is None
    custom = spec_from_document(
        {
            "kind": "UserVolumeConfig",
            "name": "d",
            "provisioning": {"diskSelector": {"match": "disk.size > 1u * GB"}, "minSize": "2GB"},
        }
    )
    assert custom is not None and not custom.selector.verifiable


@pytest.mark.parametrize(
    "doc",
    [
        {"kind": "UserVolumeConfig", "name": "d", "provisioning": "not-a-mapping"},
        {"kind": "UserVolumeConfig", "name": "d", "provisioning": {"diskSelector": "not-a-mapping"}},
        {"kind": "UserVolumeConfig", "name": "d", "filesystem": "xfs"},
    ],
)
def test_spec_from_document_tolerates_malformed_nested_fields(doc):
    """Node patches are hand-editable; a nested field that is not a mapping must not crash."""
    spec = spec_from_document(doc)
    assert spec == VolumeSpec("d", Kind.PARTITION, None, filesystem="xfs")


@need("talosctl")
def test_documents_pass_talosctl_validate(tmp_path):
    import subprocess

    from talkub.core import yamlio

    docs = [
        volume_document(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB"))),
        volume_document(VolumeSpec("data", Kind.PARTITION, NVME, parse_size("10GiB"), parse_size("50%"))),
        volume_document(
            VolumeSpec(
                "ceph",
                Kind.RAW,
                parse("disk.size >= 90u * GiB && disk.size <= 110u * GiB"),
                parse_size("1GiB"),
                encrypted=True,
            )
        ),
        volume_document(VolumeSpec("lvm", Kind.DISK, parse('disk.serial == "a\\"b"'), filesystem="btrfs")),
    ]
    patch = tmp_path / "patch.yaml"
    patch.write_text("---\n".join(yamlio.dump(d) for d in docs))
    config = subprocess.run(
        [
            "talosctl",
            "gen",
            "config",
            "t",
            "https://10.0.0.1:6443",
            "-t",
            "controlplane",
            "-o",
            "-",
            "--talos-version",
            "v1.14.0",
            "--with-docs=false",
            "--with-examples=false",
            "--config-patch",
            f"@{patch}",
        ],
        capture_output=True,
        check=True,
    ).stdout
    rendered = tmp_path / "cp.yaml"
    rendered.write_bytes(config)
    result = subprocess.run(["talosctl", "validate", "--config", str(rendered), "--mode", "metal"], capture_output=True)
    assert result.returncode == 0, result.stderr.decode() + result.stdout.decode()
