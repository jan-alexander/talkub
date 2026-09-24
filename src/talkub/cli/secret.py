"""secret seal, secret backup-sealing-key."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

import typer

from talkub import sealing
from talkub.errors import ValidationError
from talkub.repo import Repo

app = typer.Typer(help="Sealed Secrets for manifests under gitops/.", no_args_is_help=True)


def _refuse_inside_repo(repo: Repo, source: str, path: Path, hint: str) -> None:
    if path.is_relative_to(repo.root):
        raise ValidationError(f"{source} is inside the cluster repo", hint=hint)


def _read_manifest(repo: Repo, source: str) -> bytes:
    if source == "-":
        return sys.stdin.buffer.read()
    path = Path(source).expanduser().resolve()
    _refuse_inside_repo(
        repo,
        source,
        path,
        hint="plaintext Secrets must not live in the repo; use --from-literal/--from-file "
        "or a manifest outside the repo",
    )
    try:
        return path.read_bytes()
    except OSError as err:
        raise ValidationError(f"cannot read {source}: {err.strerror}") from None


def _check_from_files(repo: Repo, files: list[str]) -> None:
    for item in files:
        _, sep, raw = item.partition("=")
        raw = raw if sep else item
        _refuse_inside_repo(
            repo,
            raw,
            Path(raw).expanduser().resolve(),
            hint="plaintext must not live in the repo; use --prompt or a path outside the repo",
        )


@app.command("seal")
def seal(
    ctx: typer.Context,
    source: str = typer.Argument(
        ..., help="Secret name (with --from-literal/--from-file), a Secret manifest file, or '-' for stdin."
    ),
    namespace: str = typer.Option(
        None, "-n", "--namespace", help="Namespace (with --from-literal/--from-file/--prompt)."
    ),
    literal: list[str] = typer.Option(
        None,
        "--from-literal",
        help="key=value (repeatable). Value is visible in ps/shell history; prefer --prompt or --from-file.",
    ),
    from_file: list[str] = typer.Option(None, "--from-file", help="[key=]path (repeatable)."),
    prompt: list[str] = typer.Option(
        None, "--prompt", help="Key to prompt for with hidden input (repeatable)."
    ),
    output: Path = typer.Option(..., "-o", "--output", help="Target file; must end with .sealed.yaml."),
) -> None:
    """Seal a Secret for gitops/; the plaintext never touches the repo."""
    context = ctx.obj.context()
    if literal or from_file or prompt:
        if not namespace:
            raise ValidationError("--namespace is required with --from-literal/--from-file/--prompt")
        if from_file:
            _check_from_files(context.repo, from_file)
        prompted = {key: getpass.getpass(f"{key}: ") for key in prompt or []}
        manifest = sealing.secret_manifest(source, namespace, literal or [], from_file or [], prompted=prompted)
    else:
        if namespace:
            raise ValidationError(
                "--namespace only applies with --from-literal/--from-file/--prompt",
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
