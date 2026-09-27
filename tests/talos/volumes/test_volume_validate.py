import pytest

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import Kind, VolumeSpec
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.sizes import parse_size
from talkub.talos.volumes.validate import check_volume, supports, version_tuple

SEL = parse('disk.transport == "nvme"')


def _spec(**kw):
    base = {"name": "data", "kind": Kind.PARTITION, "selector": SEL, "min_size": parse_size("10GiB"), "max_size": None}
    base.update(kw)
    return VolumeSpec(**base)


def test_version_tuple():
    assert version_tuple("v1.14.0") == (1, 14)
    assert version_tuple("1.12") == (1, 12)
    with pytest.raises(ValidationError):
        version_tuple("latest")


@pytest.mark.parametrize(
    "feature, version, expected",
    [
        ("disk", "v1.11.5", False),
        ("disk", "v1.12.0", True),
        ("percent", "v1.12.0", True),
        ("negative", "v1.12.9", False),
        ("negative", "v1.13.0", True),
        ("install", "v1.13.4", False),
        ("install", "v1.14.0", True),
        ("btrfs", "v1.14.0", True),
    ],
)
def test_supports(feature, version, expected):
    assert supports(version, feature) is expected


def test_valid_partition_passes():
    check_volume(_spec(), "v1.14.0")


@pytest.mark.parametrize(
    "spec, version, message",
    [
        (_spec(name="x" * 35), "v1.14.0", "name"),
        (_spec(name="da_ta"), "v1.14.0", "name"),
        (_spec(min_size=None, max_size=None), "v1.14.0", "min size or max size"),
        (_spec(min_size=parse_size("20GiB"), max_size=parse_size("10GiB")), "v1.14.0", "larger"),
        (_spec(max_size=parse_size("50%")), "v1.11.0", "v1.12"),
        (_spec(max_size=parse_size("-5GiB")), "v1.12.0", "v1.13"),
        (_spec(kind=Kind.RAW, max_size=parse_size("-5GiB")), "v1.14.0", "raw"),
        (_spec(max_size=parse_size("50%"), grow=True), "v1.14.0", "absolute"),
        (_spec(kind=Kind.DISK, min_size=None), "v1.11.0", "v1.12"),
        (_spec(kind=Kind.DISK), "v1.14.0", "whole-disk"),
        (_spec(filesystem="btrfs"), "v1.13.0", "v1.14"),
        (_spec(filesystem="zfs"), "v1.14.0", "filesystem"),
        (_spec(), "v1.10.0", "v1.11"),
        (_spec(min_size=parse_size("50%")), "v1.14.0", "min size"),
    ],
)
def test_rejects(spec, version, message):
    with pytest.raises(ValidationError, match=message):
        check_volume(spec, version)


def test_disk_volume_without_sizes_passes():
    check_volume(_spec(kind=Kind.DISK, min_size=None), "v1.12.0")


def test_ephemeral_skips_name_rule():
    check_volume(VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, max_size=parse_size("40GiB")), "v1.11.0")
