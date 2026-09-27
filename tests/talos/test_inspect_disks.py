import json

from talkub.talos import inspect
from talkub.talos.volumes.model import Discovered, Disk


def _stream(*docs):
    # talosctl -o json prints concatenated, indented objects, not an array
    return "".join(json.dumps(doc, indent=4) + "\n" for doc in docs)


def _disk(id, **spec):
    return {
        "metadata": {"namespace": "runtime", "type": "Disks.block.talos.dev", "id": id},
        "node": "10.0.0.5",
        "spec": spec,
    }


DISKS = _stream(
    _disk(
        "sda",
        dev_path="/dev/sda",
        size=4000787030016,
        pretty_size="4.0 TB",
        io_size=4096,
        sector_size=512,
        readonly=False,
        cdrom=False,
        model="WDC WD40EFRX  ",
        serial="WD-1",
        transport="sata",
        rotational=True,
    ),
    _disk(
        "nvme0n1",
        dev_path="/dev/nvme0n1",
        size=512110190592,
        pretty_size="512 GB",
        readonly=False,
        cdrom=False,
        transport="nvme",
        wwid="eui.1",
    ),
    _disk("sr0", dev_path="/dev/sr0", size=0, readonly=True, cdrom=True),
)


def test_disks_parses_and_defaults_omitted_fields():
    assert inspect.disks(DISKS) == [
        Disk("nvme0n1", "/dev/nvme0n1", 512110190592, transport="nvme", wwid="eui.1"),
        Disk(
            "sda", "/dev/sda", 4000787030016, transport="sata", rotational=True, model="WDC WD40EFRX  ", serial="WD-1"
        ),
    ]


def test_disks_skips_garbage_and_stops_at_invalid_json():
    text = _stream({"spec": "nope"}, _disk("vda", dev_path="/dev/vda", size=10, readonly=False)) + "{broken"
    assert [d.id for d in inspect.disks(text)] == ["vda"]
    assert inspect.disks("") == []


def test_size_true_is_not_an_int():
    assert inspect.disks(_stream(_disk("x", size=True))) == []


def test_system_disk():
    text = _stream({"metadata": {"id": "system-disk"}, "spec": {"diskID": "nvme0n1", "devPath": "/dev/nvme0n1"}})
    assert inspect.system_disk(text) == "nvme0n1"
    assert inspect.system_disk("") is None


def test_discovered_volumes():
    text = _stream(
        {
            "metadata": {"id": "nvme0n1"},
            "spec": {"dev_path": "/dev/nvme0n1", "type": "disk", "size": 512110190592, "name": "gpt"},
        },
        {
            "metadata": {"id": "nvme0n1p6"},
            "spec": {
                "dev_path": "/dev/nvme0n1p6",
                "type": "partition",
                "size": 100 * 1024**3,
                "offset": 2310012928,
                "name": "xfs",
                "parent_dev_path": "/dev/nvme0n1",
                "partition_label": "EPHEMERAL",
            },
        },
        {"metadata": {"id": "loop0"}, "spec": {"type": "disk"}},
    )
    assert inspect.discovered_volumes(text) == [
        Discovered("/dev/nvme0n1", "disk", 512110190592, name="gpt"),
        Discovered(
            "/dev/nvme0n1p6",
            "partition",
            100 * 1024**3,
            name="xfs",
            parent_dev_path="/dev/nvme0n1",
            offset=2310012928,
            label="EPHEMERAL",
        ),
    ]
