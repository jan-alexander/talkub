"""talkub rotate-ca: rotate the Talos API CA and bring the repo in line."""

from __future__ import annotations

import base64
import binascii
import shutil
import sys
import tempfile
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.lifecycle import node_drift, prepare, wait_healthy
from talkub.repo import Repo, Settings
from talkub.tailscale import controlplanes, rewrite_talosconfig
from talkub.talos import inspect
from talkub.talos.client import talos_session


def _bundle_yaml() -> YAML:
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.width = 1 << 20  # no line wrapping: base64 PEM values can exceed 4096 chars
    return yaml


def replace_os_ca(bundle: bytes, crt: str, key: str) -> bytes:
    """The secrets bundle with certs.os replaced; everything else round-trips unchanged."""
    yaml = _bundle_yaml()
    doc = yaml.load(bundle.decode())
    os_certs = doc.get("certs", {}).get("os") if isinstance(doc, dict) else None
    if not isinstance(os_certs, dict):
        raise ValidationError("the Talos secrets bundle has no certs.os")
    os_certs["crt"] = crt
    os_certs["key"] = key
    buffer = StringIO()
    yaml.dump(doc, buffer)
    return buffer.getvalue().encode()


def previous_path(repo: Repo) -> Path:
    """Byte copy of the encrypted talosconfig from before the rotation; present = rotation in progress."""
    return repo.talos_dir / "talosconfig.previous.sops.yaml"


def _encrypt_to(ctx: Context, path: Path, data: bytes) -> None:
    atomic_write(path, ctx.sops.encrypt(data, filename=path.name, public_key=ctx.keyring.public_key()))


def _store_talosconfig(ctx: Context, settings: Settings, data: bytes) -> None:
    """Point the (new) talosconfig at the control planes' mgmt addresses and encrypt it into the repo."""
    endpoints = [settings.mgmt_address(name) for name in controlplanes(settings)]
    if not endpoints:
        raise ValidationError("no controlplane node defined")
    _encrypt_to(ctx, ctx.repo.talosconfig, rewrite_talosconfig(data, endpoints, endpoints[0]))


def _decodes_to(value: str, prefix: bytes) -> bool:
    try:
        return base64.b64decode(value, validate=True).startswith(prefix)
    except (binascii.Error, ValueError):
        return False


def rotate(ctx: Context, confirm: Callable[[str], bool]) -> list[str]:
    """Rotate the Talos API CA (`talosctl rotate-ca`), then bring the repo in line (`finish`)."""
    repo = ctx.repo
    if previous_path(repo).exists():
        raise ValidationError("a CA rotation is in progress", hint="talkub rotate-ca --finish")
    settings, order, configs = prepare(ctx, [], refresh=False)
    cps = [name for name in order if settings.nodes[name].role == "controlplane"]
    if not cps:
        raise ValidationError("no controlplane node defined")
    workers = [name for name in order if name not in cps]
    cp_addrs = [settings.mgmt_address(name) for name in cps]
    rotate_args = [
        "rotate-ca", "--talos=true", "--kubernetes=false", "--with-docs=false", "--with-examples=false",
        "--control-plane-nodes", ",".join(cp_addrs),
    ]
    if workers:
        rotate_args += ["--worker-nodes", ",".join(settings.mgmt_address(name) for name in workers)]
    with talos_session(ctx) as talos:
        wait_healthy(talos, cp_addrs[0])
        drifting = [name for name in order if node_drift(talos, settings, name, configs[name]) != "in sync"]
        if drifting:
            raise ValidationError(
                f"the repo is not in sync with: {', '.join(drifting)}",
                hint="run `talkub plan`/`talkub apply` first",
            )
        print("== rotate-ca dry-run", flush=True)
        if talos.call_global_redacted([*rotate_args, "--dry-run=true"], cp_addrs) != 0:
            raise CommandFailed("rotate-ca dry-run failed")
        if not confirm("Rotate the Talos CA now?"):
            raise ValidationError("aborted; nothing was changed")
        atomic_write(previous_path(repo), repo.talosconfig.read_bytes())
        # The only plaintext kept on disk on failure: losing the new talosconfig can lock the user out.
        work = Path(tempfile.mkdtemp(prefix="talkub-rotate-"))  # mode 0700
        new = work / "talosconfig"
        try:
            print("== rotate-ca", flush=True)
            code = talos.call_global_redacted([*rotate_args, "--dry-run=false", "-o", str(new)], cp_addrs)
            if code != 0 or not new.is_file():
                raise CommandFailed(f"rotate-ca failed (exit {code})")
            _store_talosconfig(ctx, settings, new.read_bytes())
        except BaseException:
            print(
                f"warning: the new talosconfig (if any) is kept in {work}; if the rotation completed, "
                f"only it works: `talkub rotate-ca --finish --talosconfig {new}`; delete {work} afterwards",
                file=sys.stderr, flush=True,
            )
            raise
        shutil.rmtree(work, ignore_errors=True)
    try:
        return finish(ctx)
    except BaseException:
        print(
            "warning: the CA was rotated and the repo already has the new talosconfig; "
            "resume with `talkub rotate-ca --finish`",
            file=sys.stderr, flush=True,
        )
        raise


def finish(ctx: Context, talosconfig: Path | None = None) -> list[str]:
    """Take the new CA from the running cluster into the secrets bundle and verify the repo is in sync."""
    repo = ctx.repo
    if not previous_path(repo).exists():
        raise ValidationError("no CA rotation in progress")
    settings = repo.load_settings()
    notes: list[str] = []
    if talosconfig is not None:
        _store_talosconfig(ctx, settings, talosconfig.read_bytes())
        notes.append(f"the new talosconfig is stored in the repo; delete {talosconfig} now")
    cp_addrs = [settings.mgmt_address(name) for name in controlplanes(settings)]
    if not cp_addrs:
        raise ValidationError("no controlplane node defined")
    with talos_session(ctx) as talos:
        out = talos.run(cp_addrs[0], ["get", "machineconfig", "-o", "yaml"]).stdout.decode(errors="replace")
    crt, key = inspect.machine_ca(out)
    if not _decodes_to(crt, b"-----BEGIN CERTIFICATE-----") or not _decodes_to(key, b"-----BEGIN"):
        raise ValidationError("the machine CA in the running machine config does not look like a PEM certificate/key")
    plain = ctx.sops.decrypt(repo.talos_secrets)
    _encrypt_to(ctx, repo.talos_secrets, replace_os_ca(plain, crt, key))

    settings, order, configs = prepare(ctx, [], refresh=False)
    with talos_session(ctx) as talos:
        for name in order:
            talos.run(settings.mgmt_address(name), ["version"])
        drifting = [name for name in order if node_drift(talos, settings, name, configs[name]) != "in sync"]
    if drifting:
        raise ValidationError(
            f"still not in sync: {', '.join(drifting)}",
            hint="inspect with `talkub plan`; the old talosconfig is in talos/talosconfig.previous.sops.yaml",
        )
    previous_path(repo).unlink()
    return [*notes, "the Talos CA was rotated", "commit talos/"]
