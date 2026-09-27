import pytest

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import GiB, Kind, MiB, Size, SizeKind, VolumeSpec
from talkub.talos.volumes.sizes import Intent, format_bytes, parse_bytes, parse_size, to_fields


@pytest.mark.parametrize(
    "text, expected",
    [
        ("100GB", 100 * 1000**3),
        ("100 GiB", 100 * GiB),
        ("1.5GiB", int(1.5 * GiB)),
        ("512mib", 512 * MiB),
        ("4096", 4096),
        ("2TB", 2 * 1000**4),
    ],
)
def test_parse_bytes(text, expected):
    assert parse_bytes(text) == expected


@pytest.mark.parametrize("text", ["", "GB", "10 parsecs", "-5GB", "1e3GB"])
def test_parse_bytes_rejects(text):
    with pytest.raises(ValidationError):
        parse_bytes(text)


def test_parse_size_kinds():
    assert parse_size("100GiB") == Size(SizeKind.BYTES, 100 * GiB, "100GiB")
    assert parse_size(" 50 % ") == Size(SizeKind.PERCENT, 50, "50%")
    assert parse_size("33.9%") == Size(SizeKind.PERCENT, 33, "33%")
    assert parse_size("-10 GiB") == Size(SizeKind.NEGATIVE, 10 * GiB, "-10GiB")


@pytest.mark.parametrize("text", ["101%", "-1%", "abc%"])
def test_parse_size_rejects_bad_percent(text):
    with pytest.raises(ValidationError):
        parse_size(text)


def test_format_bytes():
    assert format_bytes(100 * 1000**3) == "93.1 GiB"
    assert format_bytes(512 * MiB) == "512.0 MiB"
    assert format_bytes(3 * 1024**4) == "3.0 TiB"
    assert format_bytes(10) == "10 B"


def test_to_fields_fixed_sets_min_equal_max():
    size = parse_size("100GiB")
    assert to_fields(Intent.FIXED, size) == (size, size, False)


def test_to_fields_rest_keeps_grow_and_has_no_max():
    min_size, max_size, grow = to_fields(Intent.REST, None, grow=True)
    assert min_size == Size(SizeKind.BYTES, GiB, "1GiB")
    assert max_size is None and grow is True


def test_to_fields_percent_and_all_but_never_grow():
    percent = parse_size("50%")
    assert to_fields(Intent.PERCENT, percent, grow=True) == (Size(SizeKind.BYTES, 100 * MiB, "100MiB"), percent, False)
    negative = parse_size("-10GiB")
    assert to_fields(Intent.ALL_BUT, negative)[1] == negative


def test_to_fields_rejects_wrong_amount_kind():
    with pytest.raises(ValidationError):
        to_fields(Intent.FIXED, parse_size("50%"))
    with pytest.raises(ValidationError):
        to_fields(Intent.PERCENT, parse_size("50GiB"))


def test_volume_ids():
    assert VolumeSpec("data", Kind.PARTITION, None).volume_id == "u-data"
    assert VolumeSpec("data", Kind.DISK, None).volume_id == "u-data"
    assert VolumeSpec("ceph", Kind.RAW, None).volume_id == "r-ceph"
    assert VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None).volume_id == "EPHEMERAL"
