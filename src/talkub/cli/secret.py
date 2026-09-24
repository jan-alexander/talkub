"""secret seal, secret backup-sealing-key."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from talkub import sealing
from talkub.errors import ValidationError
from talkub.repo import Repo

app = typer.Typer(help="Sealed Secrets for manifests under gitops/.", no_args_is_help=True)


def _read_manifest(repo: Repo, source: str) -> bytes:
    if source == "-":
        return sys.stdin.buffer.read()
    path = Path(source).expanduser().resolve()
    if path.is_relative_to(repo.root):
        raise ValidationError(
            f"{source} is inside the cluster repo",
            hint="plaintext Secrets must not live in the repo; use --from-literal/--from-file "
            "or a manifest outside the repo",
        )
    try:
        return path.read_bytes()
    except OSError as err:
        raise ValidationError(f"cannot read {source}: {err.strerror}") from None


@app.command("seal")
def seal(
    ctx: typer.Context,
    source: str = typer.Argument(
        ..., help="Secret name (with --from-literal/--from-file), a Secret manifest file, or '-' for stdin."
    ),
    namespace: str = typer.Option(None, "-n", "--namespace", help="Namespace (with --from-literal/--from-file)."),
    literal: list[str] = typer.Option(None, "--from-literal", help="key=value (repeatable)."),
    from_file: list[str] = typer.Option(None, "--from-file", help="[key=]path (repeatable)."),
    output: Path = typer.Option(..., "-o", "--output", help="Target file; must end with .sealed.yaml."),
) -> None:
    """Seal a Secret for gitops/; the plaintext never touches the repo."""
    context = ctx.obj.context()
    if literal or from_file:
        if not namespace:
            raise ValidationError("--namespace is required with --from-literal/--from-file")
        manifest = sealing.secret_manifest(source, namespace, literal or [], from_file or [])
    else:
        if namespace:
            raise ValidationError(
                "--namespace only applies with --from-literal/--from-file",
                hint="the manifest's metadata.namespace is used",
            )
        manifest = _read_manifest(context.repo, source)
    if not output.name.endswith(".sealed.yaml"):
        raise ValidationError(f"{output} must end with .sealed.yaml", hint="`talkub check` relies on the name")
    sealing.write_sealed(sealing.seal(context, manifest), output)
    print(f"wrote {output}")


@app.command("backup-sealing-key")
def backup_sealing_key(ctx: typer.Context) -> None:
    """Back up the controller key (SOPS) and certificate into bootstrap/."""
    context = ctx.obj.context()
    for path in sealing.backup_sealing_key(context):
        print(f"wrote {context.repo.rel(path)}")
    print("commit bootstrap/ so a rebuilt cluster can still decrypt existing SealedSecrets")
