from talkub.talos.volumes.model import Discovered, Disk, GiB, Inventory
from talkub.talos.volumes.report import disk_table, plan_text
from talkub.talos.volumes.simulate import SYSTEM_LAYOUT, Allocation, Plan

NVME = Disk("nvme0n1", "/dev/nvme0n1", 512 * GiB, transport="nvme", model="Samsung 990  ", serial="S1")
HDD = Disk("sda", "/dev/sda", 4096 * GiB, transport="sata", rotational=True)
CD = Disk("sr0", "/dev/sr0", GiB, readonly=True, cdrom=True)


def test_disk_table():
    inventory = Inventory(
        (NVME, HDD, CD),
        (
            Discovered("/dev/sda", "disk", HDD.size, name="gpt"),
            Discovered("/dev/sda1", "partition", GiB, name="ext4", parent_dev_path="/dev/sda", label="data"),
        ),
        None,
        False,
    )
    lines = disk_table(inventory, system_disk="nvme0n1").splitlines()
    assert lines[0].split() == ["#", "DEVICE", "SIZE", "TYPE", "TRANSPORT", "MODEL", "SERIAL", "CONTENT"]
    assert lines[1].split()[:5] == ["1", "/dev/nvme0n1", "512.0", "GiB", "SSD"]
    assert "Samsung 990" in lines[1] and "system" in lines[1]
    assert "HDD" in lines[2] and "gpt: data (ext4)" in lines[2]
    assert "readonly, cdrom" in lines[3]


def test_inventory_content():
    """Test Inventory.content() returns disk's discovered entry and partitions."""
    top_entry = Discovered("/dev/sda", "disk", HDD.size, name="gpt")
    partition = Discovered("/dev/sda1", "partition", GiB, name="ext4", parent_dev_path="/dev/sda", label="data")
    inventory = Inventory((NVME, HDD, CD), (top_entry, partition), None, False)

    # HDD has discovered entry and partition
    top, parts = inventory.content(HDD)
    assert top == top_entry
    assert parts == [partition]

    # NVME has no discovered entry
    top, parts = inventory.content(NVME)
    assert top is None
    assert parts == []

    # CD has no partitions (only disks in discovered)
    cd_entry = Discovered("/dev/sr0", "disk", GiB, name="iso9660")
    inventory2 = Inventory((NVME, HDD, CD), (cd_entry,), None, False)
    top, parts = inventory2.content(CD)
    assert top == cd_entry
    assert parts == []


def test_plan_text():
    plan = Plan(
        allocations=[
            Allocation("EPHEMERAL", "nvme0n1", 40 * GiB),
            Allocation("u-data", "sda", 100 * GiB, existing=True),
        ],
        errors={"u-x": "selector matches no disk"},
        warnings=["u-y: selector matches sda, sdb; Talos takes the first with enough space"],
        free={"nvme0n1": 400 * GiB, "sda": 3996 * GiB, "sr0": 0},
        reserved={"nvme0n1": SYSTEM_LAYOUT},
        inexact=True,
    )
    text = plan_text(plan, Inventory((NVME, HDD, CD), (), "nvme0n1", False), "nvme0n1")
    assert "/dev/nvme0n1 ★" in text
    assert "system ≈2.2 GiB │ EPHEMERAL 40.0 GiB │ free 400.0 GiB" in text
    assert "u-data 100.0 GiB (exists)" in text
    assert "sr0" not in text
    assert "✗ u-x: selector matches no disk" in text
    assert "⚠ u-y:" in text
    assert "approximate" in text and "★ = system disk" in text
