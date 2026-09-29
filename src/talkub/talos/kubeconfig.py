"""The admin kubeconfig: fetched from a control plane via talosctl, stored SOPS-encrypted in the repo."""

from __future__ import annotations

from talkub.config import Repo, Settings
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos import tailscale
from talkub.talos.client import TalosSession, talos_session


def fetch(talos: TalosSession, repo: Repo, settings: Settings) -> bytes:
    """A fresh admin kubeconfig from the first control plane that answers; after `tailscale lockdown`
    its server points at the tailnet."""
    controlplanes = tailscale.controlplanes(settings)
    if not controlplanes:
        raise ValidationError("no controlplane node defined")
    errors: list[str] = []
    for name in controlplanes:
        address = settings.mgmt_address(name)
        proc = talos.run(address, ["kubeconfig", "-", "--merge=false"], check=False)
        if proc.returncode == 0:
            data = proc.stdout
            if tailscale.is_locked_down(repo):
                data = tailscale.rewrite_kubeconfig_server(data, tailscale.admin_server(settings))
            return data
        stderr = (proc.stderr or b"").decode(errors="replace").strip()
        errors.append(f"{name} ({address}): {stderr or f'exit code {proc.returncode}'}")
    raise CommandFailed(
        "no control plane returned a kubeconfig",
        hint="\n".join([*errors, "not bootstrapped yet? run `talkub bootstrap`"]),
    )


def store(ctx: Context, data: bytes) -> None:
    """Write `data` SOPS-encrypted to kubernetes/kubeconfig.sops.yaml, unless the file already holds it.

    Compared parsed: sops re-emits the YAML on encryption (e.g. list indentation), so the decrypted
    bytes differ from `data` even when nothing changed."""
    target = ctx.repo.kubeconfig
    if target.is_file() and yamlio.load(ctx.sops.decrypt(target).decode()) == yamlio.load(data.decode()):
        print("   kubeconfig unchanged")
        return
    atomic_write(
        target,
        ctx.sops.encrypt(data, filename=target.name, public_key=ctx.keyring.public_key()),
    )
    print(f"   wrote {ctx.repo.rel(target)}")


def refresh(ctx: Context) -> None:
    """Fetch a fresh kubeconfig and store it; touches nothing else in the cluster."""
    settings = ctx.repo.load_settings()
    with talos_session(ctx) as talos:
        data = fetch(talos, ctx.repo, settings)
    store(ctx, data)
