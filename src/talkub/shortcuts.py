"""Run the shortcuts defined in .talkub.yaml (`talkub run <name>`).

A secret's value is decoded in memory only: it goes to the clipboard tool on stdin (or to stdout
with --print) and never into argv, env, the terminal or a file.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

from talkub import access, clipboard
from talkub.config import MARKER, Settings, Shortcut
from talkub.context import Context
from talkub.core.errors import CommandFailed, TalkubError, ValidationError
from talkub.kube import KubeSession, kube_session

_KUBECTL = ("k", "kubectl")
_JOB_NAME_MAX = 63
_OUTCOMES = ("Complete", "Failed")

# Job --follow polling (module constants so tests can shorten them).
POLL_INTERVAL = 2.0
POD_START_TIMEOUT = 300.0  # until the job's pod exists and is no longer Pending
JOB_SETTLE_TIMEOUT = (
    60.0  # after the logs end, until the job reports Complete or Failed
)


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
    """Name for a manual run of `cronjob` (timestamp in UTC), a DNS-1123 label of at most 63
    characters: the cronjob part is shortened so the timestamp always survives."""
    suffix = f"-manual-{now:%Y%m%d%H%M%S}"
    return cronjob[: _JOB_NAME_MAX - len(suffix)].rstrip("-") + suffix


def _json_object(text: bytes) -> dict:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def pod_phases(text: bytes) -> list[str]:
    """Phases of the pods in `kubectl get pods -o json` output (missing or invalid → none)."""
    items = _json_object(text).get("items")
    phases = []
    for item in items if isinstance(items, list) else []:
        status = item.get("status") if isinstance(item, dict) else None
        phase = status.get("phase") if isinstance(status, dict) else None
        phases.append(phase if isinstance(phase, str) else "Unknown")
    return phases


def job_outcome(text: bytes) -> str | None:
    """ "Complete" or "Failed" from `kubectl get job -o json` output; None while undecided."""
    status = _json_object(text).get("status")
    if not isinstance(status, dict):
        return None
    conditions = status.get("conditions")
    for condition in conditions if isinstance(conditions, list) else []:
        if (
            isinstance(condition, dict)
            and condition.get("status") == "True"
            and condition.get("type") in _OUTCOMES
        ):
            return condition["type"]
    succeeded = status.get("succeeded")
    if isinstance(succeeded, int) and succeeded >= 1:
        return "Complete"
    return None


def _poll(check: Callable[[], bool], timeout: float) -> bool:
    """Call `check` every POLL_INTERVAL seconds until it returns True or `timeout` has passed."""
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def run(
    ctx: Context,
    name: str,
    extra: list[str],
    *,
    print_value: bool = False,
    follow: bool = False,
) -> int:
    settings = ctx.repo.load_settings()
    shortcut = settings.shortcuts.get(name)
    if shortcut is None:
        known = ", ".join(sorted(settings.shortcuts))
        hint = f"known: {known}" if known else f"define shortcuts in {MARKER}"
        raise ValidationError(f"unknown shortcut {name!r}", hint=hint)
    if extra and shortcut.kind != "run":
        raise ValidationError(
            f"shortcut {name} ({shortcut.kind}) takes no extra arguments"
        )
    if print_value and shortcut.kind != "secret":
        raise ValidationError(
            f"--print only applies to secret shortcuts, {name} is {shortcut.kind}"
        )
    if follow and shortcut.kind != "job":
        raise ValidationError(
            f"--follow only applies to job shortcuts, {name} is {shortcut.kind}"
        )

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
        raise ValidationError(
            f"kubectl returned no valid JSON for secret {ns}/{secret}"
        ) from None
    if key not in data:
        available = ", ".join(sorted(data)) or "none"
        raise ValidationError(
            f"secret {ns}/{secret} has no key {key!r}",
            hint=f"available keys: {available}",
        )
    try:
        value = base64.b64decode(data[key], validate=True)
    except (binascii.Error, TypeError):
        raise ValidationError(
            f"key {key!r} of secret {ns}/{secret} is not valid base64"
        ) from None

    if print_value:
        sys.stdout.flush()
        sys.stdout.buffer.write(value + (b"\n" if sys.stdout.isatty() else b""))
        sys.stdout.buffer.flush()
        return 0
    clipboard.copy(ctx.runner, value)
    try:
        clipboard.schedule_clear(ctx.runner, value)
    except TalkubError:
        print(
            "warning: the clipboard will not be cleared automatically", file=sys.stderr
        )
    try:
        size = f"{len(value.decode())} chars"
    except UnicodeDecodeError:
        size = f"{len(value)} bytes"
    print(
        f"copied {shortcut.name} ({size}); the clipboard is cleared in {clipboard.clear_seconds():g} s"
    )
    return 0


def _job(ctx: Context, shortcut: Shortcut, follow: bool) -> int:
    ns, cronjob = shortcut.namespace, shortcut.cronjob
    name = job_name(cronjob, datetime.now(UTC))
    with kube_session(ctx) as kube:
        kube.kubectl(["-n", ns, "create", "job", name, f"--from=cronjob/{cronjob}"])
        print(f"created job {ns}/{name}")
        if not follow:
            return 0
        return _follow(ctx, kube, ns, name)


def _follow(ctx: Context, kube: KubeSession, ns: str, name: str) -> int:
    """Wait for the job's pod, stream its logs, then report the job's outcome."""
    describe = f"inspect with: talkub k -n {ns} describe job {name}"
    phases: list[str] = []

    def pod_started() -> bool:
        nonlocal phases
        phases = pod_phases(
            kube.kubectl(
                ["-n", ns, "get", "pods", "-l", f"job-name={name}", "-o", "json"]
            ).stdout
        )
        return any(phase != "Pending" for phase in phases)

    if not _poll(pod_started, POD_START_TIMEOUT):
        if not phases:
            raise CommandFailed("job pod did not start within 5 min", hint=describe)
        print(
            f"note: the pod of job {ns}/{name} is still pending; following its logs anyway",
            file=sys.stderr,
        )

    logs = ctx.runner.call(
        ["kubectl", "-n", ns, "logs", "-f", f"job/{name}"], env=kube.env
    )

    outcome: str | None = None

    def settled() -> bool:
        nonlocal outcome
        outcome = job_outcome(
            kube.kubectl(["-n", ns, "get", "job", name, "-o", "json"]).stdout
        )
        return outcome is not None

    # After an interrupted or failed `logs -f` (e.g. Ctrl-C), check once instead of waiting.
    _poll(settled, JOB_SETTLE_TIMEOUT if logs == 0 else 0)
    if outcome == "Complete":
        return 0
    if outcome == "Failed":
        print(f"job {ns}/{name} failed", file=sys.stderr)
    else:
        print(f"job {ns}/{name} is still running", file=sys.stderr)
    print(f"hint: {describe}", file=sys.stderr)
    return 1


def _port_forward(ctx: Context, shortcut: Shortcut) -> int:
    with kube_session(ctx) as kube:
        return ctx.runner.call(
            [
                "kubectl",
                "-n",
                shortcut.namespace,
                "port-forward",
                f"svc/{shortcut.service}",
                *shortcut.ports,
            ],
            env=kube.env,
        )
