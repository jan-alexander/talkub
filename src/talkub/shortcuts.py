"""Run the shortcuts defined in .talkub.yaml (`talkub run <name>`).

A secret's value is decoded in memory only: it goes to the clipboard tool on stdin (or to stdout
with --print) and never into argv, env, the terminal or a file.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from datetime import UTC, datetime

from talkub import access, clipboard
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.kube import kube_session
from talkub.repo import MARKER, Settings, Shortcut

_KUBECTL = ("k", "kubectl")


def listing(settings: Settings) -> list[str]:
    """One aligned line per shortcut: name, kind, description."""
    if not settings.shortcuts:
        return []
    width = max(len(name) for name in settings.shortcuts)
    kind_width = max(len(s.kind) for s in settings.shortcuts.values())
    return [
        f"{name:<{width}}  {s.kind:<{kind_width}}  {s.description or ''}".rstrip()
        for name, s in sorted(settings.shortcuts.items())
    ]


def job_name(cronjob: str, now: datetime) -> str:
    """Name for a manual run of `cronjob` (timestamp in UTC), a DNS-1123 label of at most 63 characters."""
    return f"{cronjob}-manual-{now:%Y%m%d%H%M%S}"[:63].rstrip("-")


def run(ctx: Context, name: str, extra: list[str], *, print_value: bool = False, follow: bool = False) -> int:
    settings = ctx.repo.load_settings()
    shortcut = settings.shortcuts.get(name)
    if shortcut is None:
        known = ", ".join(sorted(settings.shortcuts))
        hint = f"known: {known}" if known else f"define shortcuts in {MARKER}"
        raise ValidationError(f"unknown shortcut {name!r}", hint=hint)
    if extra and shortcut.kind != "run":
        raise ValidationError(f"shortcut {name} ({shortcut.kind}) takes no extra arguments")
    if print_value and shortcut.kind != "secret":
        raise ValidationError(f"--print only applies to secret shortcuts, {name} is {shortcut.kind}")
    if follow and shortcut.kind != "job":
        raise ValidationError(f"--follow only applies to job shortcuts, {name} is {shortcut.kind}")

    if shortcut.kind == "secret":
        return _secret(ctx, shortcut, print_value)
    if shortcut.kind == "job":
        return _job(ctx, shortcut, follow or shortcut.follow)
    if shortcut.kind == "run":
        tool, *args = shortcut.argv
        passthrough = access.kubectl if tool in _KUBECTL else access.talosctl
        return passthrough(ctx, [*args, *extra])
    return _port_forward(ctx, shortcut)


def _secret(ctx: Context, shortcut: Shortcut, print_value: bool) -> int:
    ns, secret, key = shortcut.namespace, shortcut.secret, shortcut.key
    with kube_session(ctx) as kube:
        raw = kube.kubectl(["-n", ns, "get", "secret", secret, "-o", "json"]).stdout
    try:
        data = json.loads(raw).get("data") or {}
    except (json.JSONDecodeError, AttributeError):
        raise ValidationError(f"kubectl returned no valid JSON for secret {ns}/{secret}") from None
    if key not in data:
        available = ", ".join(sorted(data)) or "none"
        raise ValidationError(f"secret {ns}/{secret} has no key {key!r}", hint=f"available keys: {available}")
    try:
        value = base64.b64decode(data[key], validate=True)
    except (binascii.Error, TypeError):
        raise ValidationError(f"key {key!r} of secret {ns}/{secret} is not valid base64") from None

    if print_value:
        sys.stdout.flush()
        sys.stdout.buffer.write(value + (b"\n" if sys.stdout.isatty() else b""))
        sys.stdout.buffer.flush()
        return 0
    clipboard.copy(ctx.runner, value)
    clipboard.schedule_clear(ctx.runner, value)
    print(f"copied {shortcut.name} ({len(value)} chars); the clipboard is cleared in {clipboard.clear_seconds():g} s")
    return 0


def _job(ctx: Context, shortcut: Shortcut, follow: bool) -> int:
    ns, cronjob = shortcut.namespace, shortcut.cronjob
    name = job_name(cronjob, datetime.now(UTC))
    with kube_session(ctx) as kube:
        kube.kubectl(["-n", ns, "create", "job", name, f"--from=cronjob/{cronjob}"])
        print(f"created job {ns}/{name}")
        if not follow:
            return 0
        # Tolerated: the pod may already have finished (or not be Ready yet); the logs follow anyway.
        kube.kubectl(
            ["-n", ns, "wait", "--for=condition=Ready", "pod", "-l", f"job-name={name}", "--timeout=5m"],
            check=False,
        )
        ctx.runner.call(["kubectl", "-n", ns, "logs", "-f", f"job/{name}"], env=kube.env)
        raw = kube.kubectl(["-n", ns, "get", "job", name, "-o", "json"]).stdout
    try:
        succeeded = int(json.loads(raw).get("status", {}).get("succeeded") or 0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        succeeded = 0
    if succeeded >= 1:
        return 0
    print(f"job {ns}/{name} has not succeeded", file=sys.stderr)
    print(f"hint: inspect with: talkub k -n {ns} describe job {name}", file=sys.stderr)
    return 1


def _port_forward(ctx: Context, shortcut: Shortcut) -> int:
    with kube_session(ctx) as kube:
        return ctx.runner.call(
            ["kubectl", "-n", shortcut.namespace, "port-forward", f"svc/{shortcut.service}", *shortcut.ports],
            env=kube.env,
        )
