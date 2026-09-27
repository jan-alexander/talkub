from talkub.talos.volumes.model import Discovered, Disk, GiB, Inventory, Kind, MiB, VolumeSpec
from talkub.talos.volumes.selector import parse
from talkub.talos.volumes.simulate import DEFAULT_EPHEMERAL, SYSTEM_LAYOUT, simulate
from talkub.talos.volumes.sizes import parse_size

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme")
SSD = Disk("sda", "/dev/sda", 1024 * GiB, transport="sata")
SSD2 = Disk("sdb", "/dev/sdb", 1024 * GiB, transport="sata")
USABLE_NVME = 512 * GiB - 2 * MiB  # 1 MiB GPT head and tail


def _inv(*disks, discovered=(), system=None, installed=False):
    return Inventory(tuple(disks), tuple(discovered), system, installed)


def _vol(name, match, kind=Kind.PARTITION, min_size=None, max_size=None, grow=False):
    return VolumeSpec(
        name,
        kind,
        parse(match),
        parse_size(min_size) if min_size else None,
        parse_size(max_size) if max_size else None,
        grow,
    )


def _sizes(plan):
    return {a.volume: (a.disk, a.size) for a in plan.allocations}


def test_default_ephemeral_takes_the_whole_system_disk():
    plan = simulate(
        _inv(NVME),
        [DEFAULT_EPHEMERAL, _vol("local", "system_disk", min_size="100GiB", max_size="100GiB")],
        system_disk="nvme0n1",
        install_disk="nvme0n1",
    )
    assert _sizes(plan)["EPHEMERAL"] == ("nvme0n1", USABLE_NVME - SYSTEM_LAYOUT)
    assert "not enough space" in plan.errors["u-local"]
    assert "limit EPHEMERAL" in plan.errors["u-local"]
    assert plan.reserved == {"nvme0n1": SYSTEM_LAYOUT}


def test_limited_ephemeral_leaves_room():
    ephemeral = VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, parse_size("40GiB"), parse_size("40GiB"))
    plan = simulate(
        _inv(NVME),
        [ephemeral, _vol("local", "system_disk", min_size="100GiB", max_size="100GiB")],
        system_disk="nvme0n1",
        install_disk="nvme0n1",
    )
    assert _sizes(plan) == {"EPHEMERAL": ("nvme0n1", 40 * GiB), "u-local": ("nvme0n1", 100 * GiB)}
    assert plan.free["nvme0n1"] == USABLE_NVME - SYSTEM_LAYOUT - 140 * GiB
    assert plan.errors == {}


def test_percent_is_of_the_free_region_and_negative_subtracts():
    plan = simulate(
        _inv(SSD, SSD2),
        [
            _vol("half", 'disk.transport == "sata" && disk.serial == ""', min_size="100MiB", max_size="50%"),
        ],
        system_disk=None,
        install_disk=None,
    )
    free = 1024 * GiB - 2 * MiB
    assert _sizes(plan)["u-half"] == ("sda", (free * 50 // 100) // MiB * MiB)
    assert any("matches sda, sdb" in w for w in plan.warnings)
    negative = simulate(
        _inv(SSD),
        [_vol("most", "!system_disk", min_size="100MiB", max_size="-24GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert _sizes(negative)["u-most"] == ("sda", free - 24 * GiB)


def test_order_non_grow_and_smaller_max_first_then_falls_through():
    volumes = [
        _vol("big", 'disk.transport == "sata"', min_size="1GiB", grow=True),
        _vol("small", 'disk.transport == "sata"', min_size="600GiB", max_size="600GiB"),
        _vol("mid", 'disk.transport == "sata"', min_size="700GiB", max_size="700GiB"),
    ]
    plan = simulate(_inv(SSD, SSD2), volumes, system_disk=None, install_disk=None)
    assert [a.volume for a in plan.allocations] == ["u-small", "u-mid", "u-big"]
    # small fills sda to 600, mid does not fit next to it and falls through to sdb
    assert _sizes(plan)["u-mid"][0] == "sdb"
    assert _sizes(plan)["u-big"] == ("sda", 1024 * GiB - 2 * MiB - 600 * GiB)


def test_no_match_and_readonly():
    ro = Disk("sr0", "/dev/sr0", GiB, transport="sata", readonly=True)
    plan = simulate(
        _inv(ro), [_vol("x", 'disk.transport == "sata"', min_size="100MiB")], system_disk=None, install_disk=None
    )
    assert plan.errors["u-x"] == "selector matches no disk"


def test_whole_disk_needs_one_blank_match():
    several = simulate(
        _inv(SSD, SSD2), [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)], system_disk=None, install_disk=None
    )
    assert "several disks" in several.errors["u-d"]
    used = Discovered("/dev/sda", "disk", SSD.size, name="ntfs")
    plan = simulate(
        _inv(SSD, discovered=[used]),
        [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)],
        system_disk=None,
        install_disk=None,
    )
    assert "not empty" in plan.errors["u-d"]
    blank = simulate(
        _inv(SSD), [_vol("d", 'disk.transport == "sata"', kind=Kind.DISK)], system_disk=None, install_disk=None
    )
    assert _sizes(blank)["u-d"] == ("sda", SSD.size)


def test_existing_partitions_limit_free_space_and_foreign_format_is_rejected():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", 900 * GiB, name="ext4", parent_dev_path="/dev/sda", offset=MiB),
        Discovered("/dev/sdb", "disk", SSD2.size, name="ntfs"),
    ]
    plan = simulate(
        _inv(SSD, SSD2, discovered=discovered),
        [_vol("x", 'disk.transport == "sata"', min_size="200GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert "sda: not enough space" in plan.errors["u-x"]
    assert "sdb: not GPT (ntfs)" in plan.errors["u-x"]


def test_missing_offsets_mark_the_plan_inexact():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", 100 * GiB, parent_dev_path="/dev/sda"),
    ]
    plan = simulate(
        _inv(SSD, discovered=discovered),
        [_vol("x", "!system_disk", min_size="1GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert plan.inexact
    assert _sizes(plan)["u-x"] == ("sda", 1024 * GiB - 2 * MiB - 100 * GiB)


def test_existing_volume_is_located_by_label():
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered(
            "/dev/sda1", "partition", 300 * GiB, name="xfs", parent_dev_path="/dev/sda", offset=MiB, label="u-data"
        ),
    ]
    plan = simulate(
        _inv(SSD, discovered=discovered, system="nvme0n1", installed=True),
        [_vol("data", 'disk.model == "gone"', min_size="1GiB")],
        system_disk="nvme0n1",
        install_disk=None,
    )
    assert plan.allocations[0].existing and _sizes(plan)["u-data"] == ("sda", 300 * GiB)


def test_custom_selector_is_skipped_with_warning():
    plan = simulate(_inv(SSD), [_vol("x", "disk.size > 1u * GB", min_size="1GiB")], system_disk=None, install_disk=None)
    assert plan.allocations == [] and "not simulated" in plan.warnings[0]
