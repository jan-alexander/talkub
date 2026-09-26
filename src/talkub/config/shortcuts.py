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


_SHORTCUT_KINDS = ("secret", "job", "run", "portForward")


_PORT_SPEC = re.compile(r"^(\d{1,5})(?::(\d{1,5}))?$")


def _valid_port_spec(value: str) -> bool:
    match = _PORT_SPEC.match(value)
    if match is None:
        return False
    return all(
        1 <= int(group) <= 65535 for group in match.groups() if group is not None
    )


@dataclass(frozen=True)
class Shortcut:
    name: str
    kind: str  # "secret" | "job" | "run" | "portForward"
    description: str | None = None
    namespace: str | None = None  # secret, job, portForward
    secret: str | None = None  # secret: Secret name
    key: str | None = None  # secret
    cronjob: str | None = None  # job
    follow: bool = False  # job
    argv: tuple[str, ...] = ()  # run
    service: str | None = None  # portForward
    ports: tuple[str, ...] = ()  # portForward


def shortcut_from_dict(name: str, spec: object) -> Shortcut:
    where = f"shortcuts.{name}"
    if not isinstance(spec, Mapping):
        raise ValidationError(f"{MARKER}: {where} must be a mapping")
    if not valid_label(name):
        raise ValidationError(f"{MARKER}: {where} is not a valid name")

    present = [kind for kind in _SHORTCUT_KINDS if kind in spec]
    if len(present) != 1:
        raise ValidationError(
            f"{MARKER}: {where} must have exactly one of: {', '.join(_SHORTCUT_KINDS)}"
        )
    kind = present[0]
    description = optional_text(spec.get("description"), f"{where}.description")

    if kind == "secret":
        raw = spec["secret"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.secret must be a mapping")
        namespace = label(raw.get("namespace"), f"{where}.secret.namespace")
        secret_name = label(raw.get("name"), f"{where}.secret.name")
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip() or not _SECRET_KEY.match(key):
            raise ValidationError(
                f"{MARKER}: {where}.secret.key must be a valid secret key"
            )
        return Shortcut(
            name=name,
            kind=kind,
            description=description,
            namespace=namespace,
            secret=secret_name,
            key=key,
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
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) for item in raw)
        ):
            raise ValidationError(
                f"{MARKER}: {where}.run must be a non-empty list of strings"
            )
        if raw[0] not in _RUN_TOOLS:
            raise ValidationError(
                f"{MARKER}: {where}.run must start with one of: {', '.join(_RUN_TOOLS)}"
            )
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw))

    raw = spec["portForward"]
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{MARKER}: {where}.portForward must be a mapping")
    namespace = label(raw.get("namespace"), f"{where}.portForward.namespace")
    service = label(raw.get("service"), f"{where}.portForward.service")
    ports = raw.get("ports")
    if (
        not isinstance(ports, list)
        or not ports
        or not all(isinstance(item, str) for item in ports)
    ):
        raise ValidationError(
            f"{MARKER}: {where}.portForward.ports must be a non-empty list of strings"
        )
    for port in ports:
        if not _valid_port_spec(port):
            raise ValidationError(
                f"{MARKER}: {where}.portForward.ports has invalid entry {port!r}"
            )
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
    elif shortcut.kind == "job":
        job: dict = {"namespace": shortcut.namespace, "cronjob": shortcut.cronjob}
        if shortcut.follow:
            job["follow"] = True
        data["job"] = job
    elif shortcut.kind == "run":
        data["run"] = list(shortcut.argv)
    else:
        data["portForward"] = {
            "namespace": shortcut.namespace,
            "service": shortcut.service,
            "ports": list(shortcut.ports),
        }
    return data
