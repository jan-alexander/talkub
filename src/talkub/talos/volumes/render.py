"""Talos documents for volume specs, and specs back from documents already in a node patch."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from talkub.core.errors import ValidationError
from talkub.talos.volumes.model import Kind, Size, VolumeSpec
from talkub.talos.volumes.selector import Selector, parse
from talkub.talos.volumes.sizes import parse_size

ENCRYPTION = {"provider": "luks2", "keys": [{"slot": 0, "nodeID": {}, "lockToState": True}]}
_KIND = {Kind.PARTITION: "UserVolumeConfig", Kind.DISK: "UserVolumeConfig", Kind.RAW: "RawVolumeConfig"}


def volume_document(spec: VolumeSpec) -> dict:
    provisioning: dict = {}
    if spec.selector is not None:
        provisioning["diskSelector"] = {"match": spec.selector.to_cel()}
    if spec.min_size is not None:
        provisioning["minSize"] = spec.min_size.text
    if spec.max_size is not None:
        provisioning["maxSize"] = spec.max_size.text
    if spec.kind is Kind.EPHEMERAL:
        provisioning["grow"] = spec.grow  # explicit: Talos defaults EPHEMERAL to grow
    elif spec.grow:
        provisioning["grow"] = True
    if spec.kind is Kind.EPHEMERAL:
        doc: dict = {"apiVersion": "v1alpha1", "kind": "VolumeConfig", "name": "EPHEMERAL"}
    else:
        doc = {"apiVersion": "v1alpha1", "kind": _KIND[spec.kind], "name": spec.name}
        if spec.kind is Kind.DISK:
            doc["volumeType"] = "disk"
    doc["provisioning"] = provisioning
    if spec.kind in (Kind.PARTITION, Kind.DISK):
        doc["filesystem"] = {"type": spec.filesystem}
    if spec.encrypted:
        doc["encryption"] = copy.deepcopy(ENCRYPTION)
    return doc


def install_document(selector: Selector, existing: Mapping | None) -> dict:
    """The node's UnattendedInstallConfig with a new disk selector; other fields are kept."""
    doc = copy.deepcopy(dict(existing)) if existing else {"apiVersion": "v1alpha1", "kind": "UnattendedInstallConfig"}
    provisioning = dict(doc.get("provisioning") or {})
    provisioning["diskSelector"] = {"match": selector.to_cel()}
    doc["provisioning"] = provisioning
    return doc


def _size(value: object) -> Size | None:
    if value is None:
        return None
    try:
        return parse_size(str(value))
    except ValidationError:
        return None


def _mapping(value: object) -> Mapping:
    """`value` if it is a mapping, else empty: node patches are hand-editable, so a nested field
    that is not a mapping (e.g. `provisioning: not-a-mapping`) must not crash, just yield defaults."""
    return value if isinstance(value, Mapping) else {}


def spec_from_document(doc: object) -> VolumeSpec | None:
    """VolumeSpec for a volume document already in the patch (to simulate it); None otherwise."""
    if not isinstance(doc, Mapping):
        return None
    kind_name, name = doc.get("kind"), str(doc.get("name", ""))
    if kind_name == "VolumeConfig" and name == "EPHEMERAL":
        kind = Kind.EPHEMERAL
    elif kind_name == "RawVolumeConfig":
        kind = Kind.RAW
    elif kind_name == "UserVolumeConfig" and doc.get("volumeType", "partition") in ("partition", "disk"):
        kind = Kind.DISK if doc.get("volumeType") == "disk" else Kind.PARTITION
    else:
        return None
    provisioning = _mapping(doc.get("provisioning"))
    match = _mapping(provisioning.get("diskSelector")).get("match")
    return VolumeSpec(
        name=name,
        kind=kind,
        selector=parse(str(match)) if match is not None else None,
        min_size=_size(provisioning.get("minSize")),
        max_size=_size(provisioning.get("maxSize")),
        grow=provisioning.get("grow") is True,
        filesystem=str(_mapping(doc.get("filesystem")).get("type", "xfs")),
        encrypted="encryption" in doc,
    )
