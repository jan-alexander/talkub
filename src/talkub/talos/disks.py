"""Read a node's disks: via the maintenance API (`--insecure`) before installation, via the
talosconfig afterwards."""

from __future__ import annotations

from talkub.context import Context
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner, require_tool
from talkub.talos import inspect
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.volumes.model import Inventory

PROBE_TIMEOUT = 15.0
MODES = ("auto", "insecure", "installed")


def _require_disks(inventory: Inventory, address: str) -> Inventory:
    if not inventory.disks:
        raise ValidationError(f"no disks found on {address}", hint="is the node booted and reachable?")
    return inventory


def read_insecure(runner: Runner, address: str) -> Inventory:
    require_tool("talosctl")

    def get(resource: str, *, check: bool) -> str:
        argv = ["talosctl", "get", resource, "--insecure", "-n", address, "-o", "json"]
        return runner.run(argv, check=check, timeout=PROBE_TIMEOUT).stdout.decode(errors="replace")

    disks = inspect.disks(get("disks", check=True))
    discovered = inspect.discovered_volumes(get("discoveredvolumes", check=False))
    return _require_disks(Inventory(tuple(disks), tuple(discovered), None, installed=False), address)


def read_installed(talos: TalosSession, address: str) -> Inventory:
    def get(resource: str, *, check: bool) -> str:
        result = talos.run(address, ["get", resource, "-o", "json"], check=check, timeout=PROBE_TIMEOUT)
        return result.stdout.decode(errors="replace")

    disks = inspect.disks(get("disks", check=True))
    system = inspect.system_disk(get("systemdisk", check=False))
    discovered = inspect.discovered_volumes(get("discoveredvolumes", check=False))
    return _require_disks(Inventory(tuple(disks), tuple(discovered), system, installed=True), address)


def read(ctx: Context, name: str, mode: str) -> Inventory:
    """Disks of node `name`. `auto` uses the talosconfig if the node answers with it (installed),
    else the maintenance API at the node's `ip`."""
    if mode not in MODES:
        raise ValueError(mode)
    settings = ctx.repo.load_settings()
    node = settings.node(name)
    address = settings.mgmt_address(name)
    if mode == "insecure":
        return read_insecure(ctx.runner, node.ip)
    if mode == "installed":
        with talos_session(ctx) as talos:
            return read_installed(talos, address)
    if ctx.repo.talosconfig.is_file():
        with talos_session(ctx) as talos:
            if talos.run(address, ["version"], check=False, timeout=PROBE_TIMEOUT).returncode == 0:
                return read_installed(talos, address)
    return read_insecure(ctx.runner, node.ip)
