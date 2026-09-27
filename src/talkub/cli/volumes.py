"""node volumes: interactive assistant for the install disk, EPHEMERAL and user volumes."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import typer

from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.talos import disks
from talkub.talos.nodes import node_patch_path, read_node_documents, upsert_documents
from talkub.talos.volumes import render, report
from talkub.talos.volumes.model import Disk, Inventory, Kind, Size, SizeKind, VolumeSpec
from talkub.talos.volumes.selector import Selector, parse, suggest
from talkub.talos.volumes.simulate import DEFAULT_EPHEMERAL, simulate
from talkub.talos.volumes.sizes import Intent, format_bytes, parse_size, to_fields
from talkub.talos.volumes.validate import NAME, check_volume, supports

SIZE_HELP = (
    "Talos semantics: minSize only picks the disk; without maxSize a volume takes the largest free\n"
    "region; percent and negative sizes are relative to that free region, not to the disk."
)


def _menu(title: str, options: Sequence[tuple[str, str]], default: int = 1) -> str:
    """Numbered choice; returns the key of the chosen option."""
    print(title)
    for number, (_, label) in enumerate(options, start=1):
        print(f"  {number}) {label}")
    while True:
        choice = typer.prompt("choice", default=str(default))
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        print(f"enter 1-{len(options)}")


def _pick_disk(title: str, inventory: Inventory) -> Disk:
    while True:
        choice = typer.prompt(f"{title} (# from the table)")
        if choice.isdigit() and 1 <= int(choice) <= len(inventory.disks):
            disk = inventory.disks[int(choice) - 1]
            if disk.readonly or disk.cdrom:
                print(f"{disk.dev_path} is read-only or a CD-ROM")
                continue
            if disk.transport == "usb":
                print(f"warning: {disk.dev_path} is a USB disk; the selector cannot exclude USB devices")
            return disk
        print(f"enter 1-{len(inventory.disks)}")


def _pick_selector(options: list[Selector], disk: Disk, *, allow_custom: bool) -> Selector:
    choices = [(str(i), s.to_cel() + ("  (recommended)" if i == 0 else "")) for i, s in enumerate(options)]
    if allow_custom:
        choices.append(("custom", "own CEL expression (not verified against the disks)"))
    if not options and allow_custom:
        print(f"no selector matches only {disk.dev_path}; the disk has no serial or WWID - write your own expression")
    if not choices:
        raise ValidationError(f"no selector matches only {disk.dev_path}", hint="the disk has no serial or WWID")
    key = _menu(f"selector for {disk.dev_path}:", choices)
    if key == "custom":
        return parse(typer.prompt("CEL expression"))
    return options[int(key)]


def _ask_size(version: str, kind: Kind) -> tuple[Size | None, Size | None, bool]:
    print(SIZE_HELP)
    intents = [(Intent.FIXED.value, "fixed size (minSize = maxSize)")]
    if kind is not Kind.EPHEMERAL:
        intents.append((Intent.REST.value, "rest of the disk (largest free region)"))
    if supports(version, "percent"):
        intents.append((Intent.PERCENT.value, "percent of the free region"))
    if supports(version, "negative") and kind is not Kind.RAW:
        intents.append((Intent.ALL_BUT.value, "all of the free region but X"))
    intent = Intent(_menu("size:", intents))
    while True:
        try:
            amount = None
            if intent is Intent.FIXED:
                amount = parse_size(typer.prompt("size, e.g. 100GiB or 100GB"))
            elif intent is Intent.PERCENT:
                amount = parse_size(typer.prompt("percent, e.g. 50%").rstrip("%") + "%")
            elif intent is Intent.ALL_BUT:
                amount = parse_size("-" + typer.prompt("leave free, e.g. 20GiB").lstrip("-"))
            if amount is not None and amount.kind is not SizeKind.PERCENT:
                print(f"  {amount.text} = {format_bytes(amount.value)}")
            grow = False
            if intent is Intent.REST:
                grow = typer.confirm("grow later into free space right behind it?", default=False)
            return to_fields(intent, amount, grow=grow)
        except ValidationError as err:
            print(f"error: {err}")


def _ask_encryption(installed: bool) -> bool:
    if installed:
        print("note: only newly created, empty volumes are encrypted")
    return typer.confirm(
        "encrypt (LUKS2, key bound to this node and its STATE; unreadable after a reset)?", default=False
    )


def _choose_install(inventory: Inventory) -> tuple[Disk, Selector]:
    print("\nInstall disk (becomes the system disk; the install wipes it)")
    disk = _pick_disk("install disk", inventory)
    return disk, _pick_selector(suggest(inventory.disks, disk, None, install=True), disk, allow_custom=True)


def _ask_ephemeral(version: str) -> VolumeSpec | None:
    print("\nEPHEMERAL (/var: images, logs, emptyDir) takes the rest of the system disk unless limited.")
    print("A limit only works before the first boot from disk.")
    if _menu("EPHEMERAL:", [("whole", "whole system disk"), ("limit", "limit it")]) == "whole":
        return None
    min_size, max_size, _ = _ask_size(version, Kind.EPHEMERAL)
    spec = VolumeSpec("EPHEMERAL", Kind.EPHEMERAL, None, min_size, max_size, False, encrypted=_ask_encryption(False))
    check_volume(spec, version)
    return spec


def _ask_volume(inventory: Inventory, system: str | None, version: str, taken: set[str]) -> VolumeSpec:
    while True:
        name = typer.prompt("volume name (mounted at /var/mnt/<name>)")
        if not NAME.fullmatch(name):
            print("1 to 34 characters: letters, digits and '-'")
        elif name in taken:
            print(f"volume '{name}' already exists")
        else:
            break
    kinds = [("partition", "partition with filesystem"), ("raw", "raw partition (no filesystem, e.g. Ceph)")]
    if supports(version, "disk"):
        kinds.append(("disk", "whole disk (must be empty)"))
    kind = Kind(_menu("type:", kinds))
    disk = _pick_disk("disk", inventory)
    options = suggest(inventory.disks, disk, system, install=False)
    selector = _pick_selector(options, disk, allow_custom=kind is not Kind.DISK)
    min_size = max_size = None
    grow = False
    if kind is not Kind.DISK:
        min_size, max_size, grow = _ask_size(version, kind)
    filesystem = "xfs"
    if kind is not Kind.RAW:
        systems = [("xfs", "xfs"), ("ext4", "ext4")] + ([("btrfs", "btrfs")] if supports(version, "btrfs") else [])
        filesystem = _menu("filesystem:", systems)
    spec = VolumeSpec(name, kind, selector, min_size, max_size, grow, filesystem, _ask_encryption(inventory.installed))
    check_volume(spec, version)
    return spec


def volumes(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node name."),
    insecure: bool = typer.Option(False, "--insecure", help="Read disks via the maintenance API (not installed)."),
    installed: bool = typer.Option(False, "--installed", help="Read disks via the talosconfig (installed node)."),
) -> None:
    """Choose install disk, EPHEMERAL size and volumes from the node's disks; writes talos/nodes/<name>.yaml."""
    if insecure and installed:
        raise ValidationError("--insecure and --installed exclude each other")
    context = ctx.obj.context()
    settings = context.repo.load_settings()
    settings.node(name)
    version = settings.talos_version
    if not supports(version, "volumes"):
        raise ValidationError(
            f"node volumes needs Talos v1.11 or newer (talosVersion is {version})",
            hint="raise talosVersion in .talkub.yaml",
        )
    path = node_patch_path(context.repo, name)
    if not path.is_file():
        raise ValidationError(
            f"{context.repo.rel(path)} does not exist", hint=f"create it with `talkub node add {name} …`"
        )
    mode = "insecure" if insecure else "installed" if installed else "auto"
    inventory = disks.read(context, name, mode)
    if not any(not d.readonly and not d.cdrom for d in inventory.disks):
        raise ValidationError(f"no usable disk on {name}")
    print(f"disks of {name} ({'installed' if inventory.installed else 'maintenance mode'}):")
    print(report.disk_table(inventory, inventory.system_disk))

    # Non-mapping documents (e.g. JSON6902 patches) are not ours; upsert_documents keeps them.
    existing_docs = [d for d in read_node_documents(path) if isinstance(d, dict)]
    taken = {
        str(d.get("name"))
        for d in existing_docs
        if d.get("kind") in ("UserVolumeConfig", "RawVolumeConfig", "ExistingVolumeConfig")
    }
    new_docs: list[dict[str, object]] = []
    system, install_disk = inventory.system_disk, None
    if not inventory.installed:
        if supports(version, "install"):
            disk, selector = _choose_install(inventory)
            system = install_disk = disk.id
            current = next((d for d in existing_docs if d.get("kind") == "UnattendedInstallConfig"), None)
            new_docs.append(render.install_document(selector, current))
        else:
            print(f"note: talosVersion {version} has no UnattendedInstallConfig; the install disk is not chosen here")

    specs = [s for s in (render.spec_from_document(d) for d in existing_docs) if s is not None]
    ephemeral = next((s for s in specs if s.kind is Kind.EPHEMERAL), None)
    others = [s for s in specs if s.kind is not Kind.EPHEMERAL]
    if inventory.installed:
        print("note: EPHEMERAL is already provisioned; resizing it needs a wipe of EPHEMERAL")
    elif ephemeral is None and system is None:
        print("note: the system disk is unknown here, so EPHEMERAL is not configured")
    elif ephemeral is None:
        while (ephemeral := _ask_ephemeral(version)) is not None:
            plan = simulate(inventory, [ephemeral, *others], system_disk=system, install_disk=install_disk)
            if "EPHEMERAL" not in plan.errors:
                break
            print(report.plan_text(plan, inventory, system))
            print("EPHEMERAL limit not used")
        if ephemeral is not None:
            new_docs.append(render.volume_document(ephemeral))

    added: list[VolumeSpec] = []

    def planned(extra: list[VolumeSpec]):
        return simulate(
            inventory,
            [ephemeral or DEFAULT_EPHEMERAL, *others, *added, *extra],
            system_disk=system,
            install_disk=install_disk,
        )

    print("\n" + report.plan_text(planned([]), inventory, system))
    while typer.confirm("\nadd a volume?", default=False):
        try:
            spec = _ask_volume(inventory, system, version, taken | {s.name for s in added})
        except ValidationError as err:
            print(f"error: {err}" + (f" ({err.hint})" if err.hint else ""))
            continue
        plan = planned([spec])
        print(report.plan_text(plan, inventory, system))
        if spec.volume_id in plan.errors:
            print("volume not added")
            continue
        own = [w for w in plan.warnings if w.startswith(f"{spec.volume_id}:")]
        if own and not typer.confirm("keep this volume?", default=True):
            continue
        added.append(spec)
    new_docs += [render.volume_document(spec) for spec in added]

    if not new_docs:
        print("nothing to write")
        return
    print("\n---\n".join(yamlio.dump(doc) for doc in new_docs))
    if not typer.confirm(f"write these documents to {context.repo.rel(path)}?", default=True):
        print("aborted, nothing written", file=sys.stderr)
        raise typer.Exit(1)
    upsert_documents(path, new_docs)
    print(f"written: {context.repo.rel(path)}")
    step = f"talkub plan {name}" if inventory.installed else f"talkub apply --insecure {name}"
    print(f"next: `{step}`")
