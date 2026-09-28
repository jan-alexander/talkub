"""`talkub init`: create a new cluster repo from the packaged templates."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template

from talkub.config import (
    PLATFORMS,
    ChartSpec,
    GitSpec,
    NodeSpec,
    Repo,
    SchematicSpec,
    Settings,
    parse_installer_image,
)
from talkub.config.schematic import customization_hash
from talkub.core.ephemeral import private_tempdir
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.core.runner import Runner
from talkub.gitops import coredns
from talkub.gitops.gitrepo import classify_url, enclosing_repo
from talkub.helm import charts
from talkub.quality.lint import DEFAULT_YAMLLINT
from talkub.secrets.keyring import Keyring
from talkub.secrets.recipients import Recipient
from talkub.secrets.sops import Sops, render_sops_config
from talkub.talos import gen
from talkub.talos.nodes import NodeOptions, render_node_patch, validate_node_name

TEMPLATES = resources.files("talkub") / "templates"
ALLOWED_EXISTING = {".git", ".DS_Store"}
_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


@dataclass(frozen=True)
class InitRequest:
    target: Path
    name: str
    endpoint: str
    git_url: str
    nodes: dict[str, NodeSpec]
    recipients: list[Recipient]
    platform: str = "metal"
    node_options: dict[str, NodeOptions] = field(default_factory=dict)


def init_cluster(request: InitRequest, runner: Runner) -> Repo:
    _validate(request)
    target = request.target
    existed = target.exists()
    if existed and not target.is_dir():
        raise ValidationError(f"{target} exists and is not a directory", hint="choose a new directory")
    if existed and {p.name for p in target.iterdir()} - ALLOWED_EXISTING:
        raise ValidationError(f"{target} is not empty", hint="use a new directory or a fresh git clone")
    enclosing = enclosing_repo(target, runner)
    if enclosing is not None and enclosing != target.resolve():
        raise ValidationError(
            f"{target} is inside the git repository {enclosing}",
            hint="the cluster repo must be the root of its own git repository (Argo CD reads "
            "gitops/apps relative to it); use a new directory outside it or a fresh git clone",
        )

    gen.check_talosctl(runner)
    # Network lookups first: nothing is written if they fail.
    chart_specs = charts.resolve_defaults(runner) | {"coredns": charts.resolve(runner, "coredns")}
    target.mkdir(parents=True, exist_ok=True)
    try:
        return _populate(Repo(target.resolve()), request, chart_specs, runner)
    except BaseException:
        _cleanup(target, existed)
        raise


def _validate(request: InitRequest) -> None:
    if not _NAME.fullmatch(request.name):
        raise ValidationError(
            f"invalid cluster name {request.name!r}",
            hint="lowercase letters, digits and '-'",
        )
    if not request.endpoint.startswith("https://"):
        raise ValidationError("endpoint must start with https://", hint="e.g. https://10.0.0.10:6443")
    if request.platform not in PLATFORMS:
        raise ValidationError(
            f"unknown platform {request.platform!r}",
            hint=f"use one of: {', '.join(PLATFORMS)}",
        )
    classify_url(request.git_url)
    if not any(n.role == "controlplane" for n in request.nodes.values()):
        raise ValidationError("at least one controlplane node is required")
    if not request.recipients:
        raise ValidationError("at least one recipient is required")
    for node in request.nodes:
        validate_node_name(node)
    for options in request.node_options.values():
        options.check()


def _cleanup(target: Path, existed: bool) -> None:
    if not existed:
        shutil.rmtree(target, ignore_errors=True)
        return
    for child in target.iterdir():
        if child.name in ALLOWED_EXISTING:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _populate(repo: Repo, request: InitRequest, chart_specs: dict[str, ChartSpec], runner: Runner) -> Repo:
    keyring = Keyring(repo, runner, env={})
    keyring.create(request.recipients)
    public = keyring.public_key()
    sops = Sops(runner, keyring.master_key)
    atomic_write(repo.sops_config, render_sops_config(public).encode())

    controlplane_ips = [n.ip for n in request.nodes.values() if n.role == "controlplane"]
    with private_tempdir() as work:
        secrets = gen.gen_secrets(runner, work)
        defaults = gen.detect_defaults(runner, request.name, request.endpoint, secrets)
        talosconfig = gen.gen_talosconfig(
            runner,
            request.name,
            request.endpoint,
            secrets,
            work,
            endpoints=controlplane_ips,
            node=controlplane_ips[0],
        )
        for source, target in (
            (secrets, repo.talos_secrets),
            (talosconfig, repo.talosconfig),
        ):
            atomic_write(
                target,
                sops.encrypt(source.read_bytes(), filename=target.name, public_key=public),
            )

    # The schematic ID depends only on the customization, not on the platform, so the
    # default (empty) schematic from talosctl is valid for every platform.
    _, default_id = parse_installer_image(defaults.install_image)
    git_url = request.git_url.strip()
    settings = Settings(
        name=request.name,
        endpoint=request.endpoint,
        talos_version=defaults.talos_version,
        kubernetes_version=defaults.kubernetes_version,
        platform=request.platform,
        schematics={request.platform: SchematicSpec(id=default_id, hash=customization_hash([], []))},
        nodes=dict(request.nodes),
        git=GitSpec(url=git_url),
    )
    repo.write_settings(settings)
    _write_tree(
        TEMPLATES / "repo",
        repo.root,
        _template_values(settings.name, git_url, chart_specs),
    )
    atomic_write(repo.root / ".yamllint", DEFAULT_YAMLLINT.encode())
    coredns.write_files(repo, chart_specs["coredns"], git_url)
    for node in request.nodes:
        options = request.node_options.get(node, NodeOptions())
        atomic_write(
            repo.talos_dir / "nodes" / f"{node}.yaml",
            render_node_patch(node, options).encode(),
        )
    return repo


def _template_values(name: str, git_url: str, chart_specs: dict[str, ChartSpec]) -> dict[str, str]:
    values = {"name": name, "git_url": git_url}
    for key, spec in chart_specs.items():
        prefix = key.replace("-", "_")
        values |= {
            f"{prefix}_repo": spec.repo,
            f"{prefix}_chart": spec.chart,
            f"{prefix}_version": spec.version,
        }
    return values


def _write_tree(source: Traversable, destination: Path, values: dict[str, str]) -> None:
    """Copy templates; 'dot-x' becomes '.x', '*.tmpl' files are rendered with string.Template."""
    for entry in source.iterdir():
        name = "." + entry.name[4:] if entry.name.startswith("dot-") else entry.name
        if entry.is_dir():
            _write_tree(entry, destination / name, values)
        elif name.endswith(".tmpl"):
            text = Template(entry.read_text()).substitute(values)
            atomic_write(destination / name.removesuffix(".tmpl"), text.encode())
        else:
            atomic_write(destination / name, entry.read_bytes())
