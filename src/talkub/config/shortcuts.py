"""`shortcuts:` entries in .talkub.yaml: parsing and serializing them."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from talkub.config.fields import (
    MARKER,
    label,
    optional_text,
    valid_label,
)
from talkub.core.errors import ValidationError

# Kubernetes secret data key: alphanumerics, '-', '.', '_'.
_SECRET_KEY = re.compile(r"^[-._a-zA-Z0-9]+$")


_RUN_TOOLS = ("k", "kubectl", "t", "talosctl")


_SHORTCUT_KINDS = ("secret", "token", "job", "run", "portForward", "kubernetes")


_PORT_SPEC = re.compile(r"^(\d{1,5})(?::(\d{1,5}))?$")


def _valid_port_spec(value: str) -> bool:
    match = _PORT_SPEC.match(value)
    if match is None:
        return False
    return all(1 <= int(group) <= 65535 for group in match.groups() if group is not None)


# `kubectl create token --duration`: Go duration restricted to h/m/s blocks in that order, e.g. 8h or 1h30m.
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _valid_duration(value: str) -> bool:
    match = _DURATION.match(value)
    return match is not None and any(group is not None and int(group) > 0 for group in match.groups())


# `{node}` or `{node.<field>}` in a run/kubernetes argument; filled in at call time from the chosen node.
# Anything else in braces (kubectl jsonpath, Go templates) is left alone.
NODE_FIELDS = ("addr", "name", "ip", "mgmt", "role")
NODE_PLACEHOLDER = re.compile(r"\{node(?:\.([a-z]+))?\}")
# Wider than NODE_PLACEHOLDER, for load-time checks only: a typo like {node.mgmt_ip} is an error, not a literal.
_NODE_FIELD_LIKE = re.compile(r"\{node\.([^{}]*)\}")


def _check_placeholders(argv: list[str], where: str) -> None:
    for arg in argv:
        for match in _NODE_FIELD_LIKE.finditer(arg):
            if match.group(1) not in NODE_FIELDS:
                known = ", ".join(["{node}", *(f"{{node.{name}}}" for name in NODE_FIELDS)])
                raise ValidationError(
                    f"{MARKER}: {where}: unknown placeholder {match.group(0)}",
                    hint=f"one of {known}",
                )


# workstation.access replaces it in the first stage only; spelled out here because config sits below workstation.
_KUBECONFIG_PLACEHOLDER = "{kubeconfig}"


def _pipe(spec: Mapping, where: str) -> tuple[tuple[str, ...], ...]:
    raw = spec.get("pipe")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"{MARKER}: {where}.pipe must be a non-empty list of stages")
    stages: list[tuple[str, ...]] = []
    for index, stage in enumerate(raw):
        at = f"{where}.pipe[{index}]"
        if (
            not isinstance(stage, list)
            or not stage
            or not all(isinstance(item, str) for item in stage)
            or not stage[0].strip()
        ):
            raise ValidationError(f"{MARKER}: {at} must be a non-empty list of strings")
        if any(_KUBECONFIG_PLACEHOLDER in arg for arg in stage):
            raise ValidationError(f"{MARKER}: {at}: {_KUBECONFIG_PLACEHOLDER} is only available in the first stage")
        _check_placeholders(stage, at)
        stages.append(tuple(stage))
    return tuple(stages)


@dataclass(frozen=True)
class Shortcut:
    name: str
    kind: str  # "secret" | "token" | "job" | "run" | "portForward" | "kubernetes"
    description: str | None = None
    namespace: str | None = None  # secret, token, job, portForward
    secret: str | None = None  # secret: Secret name
    key: str | None = None  # secret
    service_account: str | None = None  # token
    duration: str | None = None  # token: passed as --duration if set
    cronjob: str | None = None  # job
    follow: bool = False  # job
    argv: tuple[str, ...] = ()  # run, kubernetes
    service: str | None = None  # portForward
    ports: tuple[str, ...] = ()  # portForward
    pipe: tuple[tuple[str, ...], ...] = ()  # run, kubernetes: further stages fed by the first one's stdout


def needs_node(shortcut: Shortcut) -> bool:
    """Whether running the shortcut needs a node: a run/kubernetes argument has a {node…} placeholder."""
    args = [*shortcut.argv, *(arg for stage in shortcut.pipe for arg in stage)]
    return shortcut.kind in ("run", "kubernetes") and any(NODE_PLACEHOLDER.search(arg) for arg in args)


def shortcut_from_dict(name: str, spec: object) -> Shortcut:
    where = f"shortcuts.{name}"
    if not isinstance(spec, Mapping):
        raise ValidationError(f"{MARKER}: {where} must be a mapping")
    if not valid_label(name):
        raise ValidationError(f"{MARKER}: {where} is not a valid name")

    present = [kind for kind in _SHORTCUT_KINDS if kind in spec]
    if len(present) != 1:
        raise ValidationError(f"{MARKER}: {where} must have exactly one of: {', '.join(_SHORTCUT_KINDS)}")
    kind = present[0]
    description = optional_text(spec.get("description"), f"{where}.description")
    if "pipe" in spec and kind not in ("run", "kubernetes"):
        raise ValidationError(f"{MARKER}: {where}.pipe is only allowed with run or kubernetes")

    if kind == "secret":
        raw = spec["secret"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.secret must be a mapping")
        namespace = label(raw.get("namespace"), f"{where}.secret.namespace")
        secret_name = label(raw.get("name"), f"{where}.secret.name")
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip() or not _SECRET_KEY.match(key):
            raise ValidationError(f"{MARKER}: {where}.secret.key must be a valid secret key")
        return Shortcut(
            name=name,
            kind=kind,
            description=description,
            namespace=namespace,
            secret=secret_name,
            key=key,
        )

    if kind == "token":
        raw = spec["token"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.token must be a mapping")
        namespace = label(raw.get("namespace"), f"{where}.token.namespace")
        service_account = label(raw.get("serviceAccount"), f"{where}.token.serviceAccount")
        duration = raw.get("duration")
        if duration is not None and (not isinstance(duration, str) or not _valid_duration(duration)):
            raise ValidationError(f"{MARKER}: {where}.token.duration must be a duration like 8h or 1h30m")
        return Shortcut(
            name=name,
            kind=kind,
            description=description,
            namespace=namespace,
            service_account=service_account,
            duration=duration,
        )

    if kind == "job":
        raw = spec["job"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.job must be a mapping")
        namespace = label(raw.get("namespace"), f"{where}.job.namespace")
        cronjob = label(raw.get("cronjob"), f"{where}.job.cronjob")
        follow = raw.get("follow", False)
        if not isinstance(follow, bool):
            raise ValidationError(f"{MARKER}: {where}.job.follow must be a boolean")
        return Shortcut(
            name=name,
            kind=kind,
            description=description,
            namespace=namespace,
            cronjob=cronjob,
            follow=follow,
        )

    if kind == "run":
        raw = spec["run"]
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValidationError(f"{MARKER}: {where}.run must be a non-empty list of strings")
        if raw[0] not in _RUN_TOOLS:
            raise ValidationError(f"{MARKER}: {where}.run must start with one of: {', '.join(_RUN_TOOLS)}")
        _check_placeholders(raw, f"{where}.run")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw), pipe=_pipe(spec, where))

    if kind == "kubernetes":
        raw = spec["kubernetes"]
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValidationError(f"{MARKER}: {where}.kubernetes must be a non-empty list of strings")
        if not raw[0].strip():
            raise ValidationError(f"{MARKER}: {where}.kubernetes must start with a program")
        _check_placeholders(raw, f"{where}.kubernetes")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw), pipe=_pipe(spec, where))

    raw = spec["portForward"]
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{MARKER}: {where}.portForward must be a mapping")
    namespace = label(raw.get("namespace"), f"{where}.portForward.namespace")
    service = label(raw.get("service"), f"{where}.portForward.service")
    ports = raw.get("ports")
    if not isinstance(ports, list) or not ports or not all(isinstance(item, str) for item in ports):
        raise ValidationError(f"{MARKER}: {where}.portForward.ports must be a non-empty list of strings")
    for port in ports:
        if not _valid_port_spec(port):
            raise ValidationError(f"{MARKER}: {where}.portForward.ports has invalid entry {port!r}")
    return Shortcut(
        name=name,
        kind=kind,
        description=description,
        namespace=namespace,
        service=service,
        ports=tuple(ports),
    )


def shortcut_to_dict(shortcut: Shortcut) -> dict:
    data: dict = {}
    if shortcut.description is not None:
        data["description"] = shortcut.description
    if shortcut.kind == "secret":
        data["secret"] = {
            "namespace": shortcut.namespace,
            "name": shortcut.secret,
            "key": shortcut.key,
        }
    elif shortcut.kind == "token":
        token: dict = {"namespace": shortcut.namespace, "serviceAccount": shortcut.service_account}
        if shortcut.duration is not None:
            token["duration"] = shortcut.duration
        data["token"] = token
    elif shortcut.kind == "job":
        job: dict = {"namespace": shortcut.namespace, "cronjob": shortcut.cronjob}
        if shortcut.follow:
            job["follow"] = True
        data["job"] = job
    elif shortcut.kind == "run":
        data["run"] = list(shortcut.argv)
    elif shortcut.kind == "kubernetes":
        data["kubernetes"] = list(shortcut.argv)
    else:
        data["portForward"] = {
            "namespace": shortcut.namespace,
            "service": shortcut.service,
            "ports": list(shortcut.ports),
        }
    if shortcut.pipe:
        data["pipe"] = [list(stage) for stage in shortcut.pipe]
    return data
