import pytest

from talkub.talos.volumes.model import Disk, GiB
from talkub.talos.volumes.selector import Equals, Flag, Selector, SizeRange, SystemDisk, parse, suggest

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", model="Samsung 990", serial="S1")
NVME2 = Disk("nvme1n1", "/dev/nvme1n1", 2048 * GiB, transport="nvme", model="WD SN850", serial="W1")
SSD = Disk("sda", "/dev/sda", 1024 * GiB, transport="sata", model="Crucial", serial="C1")
HDD = Disk("sdb", "/dev/sdb", 4096 * GiB, transport="sata", rotational=True, model="WD Red", serial="R1")
STICK = Disk("sdc", "/dev/sdc", 32 * GiB, transport="usb", model="Cruzer", serial="U1")
TWIN_A = Disk("sdd", "/dev/sdd", 1024 * GiB, transport="sata", model="Same", serial="A")
TWIN_B = Disk("sde", "/dev/sde", 1024 * GiB, transport="sata", model="Same", serial="B")


def _matching(selector, disks, system_disk=None, install=False):
    return [d.id for d in disks if selector.matches(d, (not install) and d.id == system_disk)]


def test_to_cel_uses_uint_suffix_and_quotes():
    selector = Selector((Flag("readonly", False), Equals("transport", "usb", negate=True), SizeRange(90, 110)))
    assert selector.to_cel() == (
        '!disk.readonly && disk.transport != "usb" && disk.size >= 90u * GiB && disk.size <= 110u * GiB'
    )
    assert Equals("model", 'a"b\\c').cel() == 'disk.model == "a\\"b\\\\c"'


def test_install_prefers_transport_and_excludes_readonly_cdrom():
    options = suggest([NVME, SSD, HDD, STICK], NVME, None, install=True)
    assert options[0].to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "nvme"'
    assert "system_disk" not in " ".join(o.to_cel() for o in options)
    assert all(_matching(o, [NVME, SSD, HDD, STICK], install=True) == ["nvme0n1"] for o in options)


def test_rotational_stage_distinguishes_ssd_from_hdd():
    first = suggest([NVME, SSD, HDD], SSD, None, install=True)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "sata" && !disk.rotational'


def test_size_range_then_model_then_serial():
    first = suggest([NVME, NVME2], NVME2, None, install=True)[0]
    assert first.terms[-1] == SizeRange(1843, 2253)
    twins = suggest([TWIN_A, TWIN_B], TWIN_B, None, install=True)
    assert twins[0].to_cel().endswith('disk.serial == "B"')


def test_usb_target_drops_usb_exclusion():
    first = suggest([NVME, STICK], STICK, None, install=True)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "usb"'


def test_serial_stage_keeps_usb_exclusion():
    options = suggest([TWIN_A, TWIN_B], TWIN_A, None, install=True)
    assert options[0].to_cel() == '!disk.readonly && !disk.cdrom && disk.transport != "usb" && disk.serial == "A"'


def test_volume_selectors_pin_system_disk():
    first = suggest([NVME, NVME2], NVME2, "nvme0n1", install=False)[0]
    assert first.to_cel() == '!disk.readonly && !disk.cdrom && disk.transport == "nvme" && !system_disk'
    assert _matching(first, [NVME, NVME2], "nvme0n1") == ["nvme1n1"]
    on_system = suggest([NVME, NVME2], NVME, "nvme0n1", install=False)[0]
    assert on_system.terms[-1] == SystemDisk(True)


def test_no_unique_stage_returns_empty():
    clone = Disk("sdf", "/dev/sdf", TWIN_A.size, transport="sata", model="Same")
    clone2 = Disk("sdg", "/dev/sdg", TWIN_A.size, transport="sata", model="Same")
    assert suggest([clone, clone2], clone, None, install=True) == []


def test_readonly_and_cdrom_never_match():
    ro = Disk("sr0", "/dev/sr0", GiB, transport="sata", readonly=True, cdrom=True)
    assert _matching(suggest([ro, SSD], SSD, None, install=True)[0], [ro, SSD], install=True) == ["sda"]


@pytest.mark.parametrize("disk", [NVME, SSD, HDD, TWIN_A, Disk("x", "/dev/x", GiB, model='we"ird\\')])
def test_parse_round_trips_generated_selectors(disk):
    for option in suggest([disk, NVME2, STICK], disk, "nvme1n1", install=False):
        assert parse(option.to_cel()) == option


@pytest.mark.parametrize("text", ["disk.size > 100u * GB", 'glob("/dev/sd*", disk.dev_path)', "", "disk.readonlyX"])
def test_parse_keeps_foreign_expressions_raw(text):
    selector = parse(text)
    assert selector.raw == text and not selector.verifiable
    assert selector.to_cel() == text
    with pytest.raises(ValueError):
        selector.matches(NVME, False)
