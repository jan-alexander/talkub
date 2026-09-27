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


def test_negative_max_size_orders_like_bytes_not_infinite():
    # Talos' CompareVolumeConfigs orders by the raw MaxSize field; a negative max keeps its absolute
    # value there (NegativeMaxSize is a separate flag), so it is not treated as "no max" (= infinite).
    disk = Disk("sdz", "/dev/sdz", 1024 * GiB, transport="scsi")
    free = 1024 * GiB - 2 * MiB
    plan = simulate(
        _inv(disk),
        [
            _vol("db", 'disk.transport == "scsi"', min_size="100GiB", max_size="100GiB"),
            _vol("most", 'disk.transport == "scsi"', min_size="100MiB", max_size="-24GiB"),
        ],
        system_disk=None,
        install_disk=None,
    )
    assert [a.volume for a in plan.allocations] == ["u-most"]
    assert _sizes(plan)["u-most"] == ("sdz", free - 24 * GiB)
    assert "not enough space" in plan.errors["u-db"]


def test_partition_carved_from_smallest_sufficient_region_not_the_largest():
    # Talos sizes the partition from the largest free region (LargestContiguousAllocatable), but
    # AllocatePartition then carves it from the smallest region that still fits it (best fit).
    part_a_size = 10 * GiB
    gap1 = 60 * GiB  # small hole up front, big enough for u-a
    part_b_offset = MiB + part_a_size + gap1
    gap2 = 800 * GiB  # the largest free region, at the end of the disk
    part_b_size = (1024 * GiB - MiB - gap2) - part_b_offset
    discovered = [
        Discovered("/dev/sda", "disk", SSD.size, name="gpt"),
        Discovered("/dev/sda1", "partition", part_a_size, name="xfs", parent_dev_path="/dev/sda", offset=MiB),
        Discovered("/dev/sda2", "partition", part_b_size, name="xfs", parent_dev_path="/dev/sda", offset=part_b_offset),
    ]
    plan = simulate(
        _inv(SSD, discovered=discovered),
        [
            _vol("a", 'disk.transport == "sata"', min_size="50GiB", max_size="50GiB"),
            _vol("b", 'disk.transport == "sata"', min_size="1GiB", grow=True),
        ],
        system_disk=None,
        install_disk=None,
    )
    assert _sizes(plan)["u-a"] == ("sda", 50 * GiB)  # carved from the 60 GiB hole, not the 800 GiB tail
    assert _sizes(plan)["u-b"] == ("sda", gap2)  # u-b still gets the largest region
    assert plan.free["sda"] == gap1 - 50 * GiB  # the 10 GiB left over in the hole


def test_existing_volume_is_located_even_before_installation():
    discovered = [
        Discovered("/dev/sdb", "disk", SSD2.size, name="gpt"),
        Discovered(
            "/dev/sdb1", "partition", 300 * GiB, name="xfs", parent_dev_path="/dev/sdb", offset=MiB, label="u-data"
        ),
    ]
    plan = simulate(
        _inv(NVME, SSD2, discovered=discovered, installed=False),
        [_vol("data", 'disk.model == "gone"', min_size="1GiB")],
        system_disk=None,
        install_disk="nvme0n1",
    )
    assert plan.allocations[0].existing and _sizes(plan)["u-data"] == ("sdb", 300 * GiB)


def test_existing_volume_on_the_install_disk_is_ignored_because_it_will_be_wiped():
    discovered = [
        Discovered("/dev/nvme0n1", "disk", NVME.size, name="gpt"),
        Discovered(
            "/dev/nvme0n1p1",
            "partition",
            300 * GiB,
            name="xfs",
            parent_dev_path="/dev/nvme0n1",
            offset=MiB,
            label="u-data",
        ),
    ]
    plan = simulate(
        _inv(NVME, discovered=discovered, installed=False),
        [_vol("data", 'disk.transport == "nvme"', min_size="1GiB")],
        system_disk=None,
        install_disk="nvme0n1",
    )
    assert not plan.allocations[0].existing
    assert _sizes(plan)["u-data"][0] == "nvme0n1"


def test_whole_disk_volumes_are_grown_last_and_existing_whole_disk_needs_a_content_match():
    sda_full_foreign = [Discovered("/dev/sda", "disk", SSD.size, name="ntfs")]
    sdb = Disk("sdb2", "/dev/sdb2", 1024 * GiB, transport="sata", serial="arc")
    plan = simulate(
        _inv(SSD, sdb, discovered=sda_full_foreign),
        [
            _vol("archive", 'disk.serial == "arc"', kind=Kind.DISK),
            _vol("logs", 'disk.transport == "sata"', min_size="1GiB"),
        ],
        system_disk=None,
        install_disk=None,
    )
    # u-logs (grow=False) must sort before u-archive (a disk volume, forced grow=True) even though
    # "archive" < "logs" alphabetically, so u-logs claims sdb first.
    assert [a.volume for a in plan.allocations] == ["u-logs"]
    assert _sizes(plan)["u-logs"][0] == "sdb2"
    assert "not empty" in plan.errors["u-archive"]


def test_existing_whole_disk_volume_recognized_by_matching_filesystem():
    disk = Disk("sdc", "/dev/sdc", 500 * GiB, transport="sata")
    discovered = [Discovered("/dev/sdc", "disk", disk.size, name="xfs")]
    plan = simulate(
        _inv(disk, discovered=discovered),
        [_vol("bulk", 'disk.transport == "sata"', kind=Kind.DISK)],
        system_disk=None,
        install_disk=None,
    )
    assert plan.allocations[0].existing
    assert _sizes(plan)["u-bulk"] == ("sdc", disk.size)


def test_wave_zero_waits_when_ephemeral_cannot_be_provisioned():
    plan = simulate(
        _inv(SSD),
        [DEFAULT_EPHEMERAL, _vol("data", 'disk.transport == "sata"', min_size="1GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert plan.errors["EPHEMERAL"] == "system disk unknown"
    assert plan.errors["u-data"] == "waits for EPHEMERAL, which cannot be provisioned"
    assert plan.allocations == []


def test_gpt_tail_reserves_33_sectors_not_a_full_mib():
    size = 1000204886016  # a common 1 TB SSD's raw byte count, not a MiB multiple
    disk = Disk("sdd", "/dev/sdd", size, transport="sata")
    old_usable = ((size - MiB) // MiB) * MiB - MiB  # previous formula: 1 MiB head + 1 MiB tail
    new_usable = ((size - 33 * 512) // MiB) * MiB - MiB  # 1 MiB head + 33 sectors (512 B) tail
    assert new_usable - old_usable == MiB  # the extra MiB the old fixed-tail formula wasted
    plan = simulate(
        _inv(disk), [_vol("x", 'disk.transport == "sata"', min_size="1GiB")], system_disk=None, install_disk=None
    )
    assert _sizes(plan)["u-x"] == ("sdd", new_usable)


def test_non_gpt_candidate_produces_a_warning_even_when_another_disk_is_used():
    discovered = [Discovered("/dev/sda", "disk", SSD.size, name="ntfs")]
    plan = simulate(
        _inv(SSD, SSD2, discovered=discovered),
        [_vol("x", 'disk.transport == "sata"', min_size="1GiB")],
        system_disk=None,
        install_disk=None,
    )
    assert _sizes(plan)["u-x"][0] == "sdb"
    assert any("u-x: sda has ntfs content and is skipped" in w for w in plan.warnings)
