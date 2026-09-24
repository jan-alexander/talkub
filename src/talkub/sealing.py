"""Sealed Secrets: build Secrets in memory, seal them, back up the controller key."""

from __future__ import annotations

import base64
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.context import Context
from talkub.ephemeral import private_file
from talkub.errors import TalkubError, ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import kube_session
from talkub.repo import Repo
from talkub.runner import require_tool

CONTROLLER = ["--controller-name", "sealed-secrets-controller", "--controller-namespace", "kube-system"]
KEY_LABEL = "sealedsecrets.bitnami.com/sealed-secrets-key"
RUNTIME_FIELDS = (
    "resourceVersion", "uid", "creationTimestamp", "managedFields", "ownerReferences", "generation", "selfLink",
)
_KEY = re.compile(r"[-._a-zA-Z0-9]+")


def cert_path(repo: Repo) -> Path:
    return repo.root / "bootstrap" / "sealed-secrets.pem"


def key_backup_path(repo: Repo) -> Path:
    return repo.root / "bootstrap" / "sealed-secrets-key.sops.yaml"


def _add(data: dict[str, str], key: str, value: bytes) -> None:
    if not _KEY.fullmatch(key):
        raise ValidationError(f"invalid secret key {key!r}", hint="use letters, digits, '-', '_' and '.'")
    if key in data:
        raise ValidationError(f"duplicate secret key {key!r}")
    data[key] = base64.b64encode(value).decode()


def secret_manifest(name: str, namespace: str, literals: Sequence[str], files: Sequence[str]) -> bytes:
    """A Secret manifest built in memory, so values never appear on a command line."""
    if not literals and not files:
        raise ValidationError("give at least one --from-literal or --from-file")
    data: dict[str, str] = {}
    for item in literals:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValidationError(f"invalid --from-literal {item!r}", hint="use key=value")
        _add(data, key, value.encode())
    for item in files:
        key, sep, path = item.partition("=")
        if not sep:
            key, path = Path(item).name, item
        try:
            content = Path(path).expanduser().read_bytes()
        except OSError as err:
            raise ValidationError(f"cannot read {path}: {err.strerror}") from None
        _add(data, key, content)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }
    return yamlio.dump(manifest).encode()


def check_secret_manifest(manifest: bytes) -> None:
    try:
        docs = [doc for doc in yamlio.load_all(manifest.decode()) if doc]
    except (YAMLError, UnicodeDecodeError):
        raise ValidationError("input is not valid YAML") from None
    if len(docs) != 1 or not isinstance(docs[0], Mapping) or docs[0].get("kind") != "Secret":
        raise ValidationError("input must be exactly one `kind: Secret` manifest")
    if not (docs[0].get("metadata") or {}).get("namespace"):
        raise ValidationError("the Secret needs metadata.namespace", hint="sealed secrets are bound to their namespace")


def sealing_cert(ctx: Context) -> bytes:
    """The controller's live certificate; bootstrap/sealed-secrets.pem when the cluster is unreachable."""
    reason = "the cluster is not bootstrapped yet"
    if ctx.repo.kubeconfig.is_file():
        try:
            with kube_session(ctx) as kube:
                proc = ctx.runner.run(["kubeseal", "--fetch-cert", *CONTROLLER], env=kube.env, check=False)
        except TalkubError as err:
            reason = str(err)
        else:
            if proc.returncode == 0 and b"BEGIN CERTIFICATE" in proc.stdout:
                return proc.stdout
            reason = (proc.stderr or b"").decode().strip() or f"kubeseal exited with {proc.returncode}"
    fallback = cert_path(ctx.repo)
    if fallback.is_file():
        print(f"talkub: using {ctx.repo.rel(fallback)} ({reason})", file=sys.stderr)
        return fallback.read_bytes()
    raise ValidationError(
        "no sealing certificate available",
        hint=f"{reason}; {ctx.repo.rel(fallback)} is missing too — "
        "run `talkub secret backup-sealing-key` once the cluster is up",
    )


def seal(ctx: Context, manifest: bytes) -> bytes:
    require_tool("kubeseal")
    check_secret_manifest(manifest)
    cert = sealing_cert(ctx)
    with private_file("sealing-cert.pem", cert) as cert_file:
        # Offline sealing against the certificate: kubeseal must not talk to any cluster here.
        argv = ["kubeseal", "--format", "yaml", "--cert", str(cert_file)]
        return ctx.runner.run(argv, input=manifest, env={"KUBECONFIG": os.devnull}).stdout


def write_sealed(sealed: bytes, output: Path) -> None:
    if not output.name.endswith(".sealed.yaml"):
        raise ValidationError(f"{output} must end with .sealed.yaml", hint="`talkub check` relies on the name")
    atomic_write(output, sealed)


def backup_sealing_key(ctx: Context) -> list[Path]:
    """Store the controller key(s) SOPS-encrypted and the public certificate in bootstrap/."""
    require_tool("kubeseal")
    with kube_session(ctx) as kube:
        listing = kube.kubectl(["get", "secret", "-n", "kube-system", "-l", KEY_LABEL, "-o", "yaml"]).stdout
        cert = ctx.runner.run(["kubeseal", "--fetch-cert", *CONTROLLER], env=kube.env).stdout
    items = (yamlio.load(listing.decode()) or {}).get("items") or []
    if not items:
        raise ValidationError(
            "no sealed-secrets key found in kube-system", hint="is the sealed-secrets controller running?"
        )
    for item in items:
        metadata = item.get("metadata") or {}
        for key in RUNTIME_FIELDS:
            metadata.pop(key, None)
        (metadata.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    plain = yamlio.dump({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    backup = key_backup_path(ctx.repo)
    atomic_write(backup, ctx.sops.encrypt(plain, filename=backup.name, public_key=ctx.keyring.public_key()))
    atomic_write(cert_path(ctx.repo), cert)
    return [backup, cert_path(ctx.repo)]
