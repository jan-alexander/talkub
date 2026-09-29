"""talkub bootstrap: etcd → health → kubeconfig → sealing key → Helm releases → root app."""

from __future__ import annotations

from talkub.argocd.app_files import HelmSource, app_source
from talkub.config import Repo, Settings
from talkub.context import Context
from talkub.core.ephemeral import decrypted
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.runner import require_tool
from talkub.gitops import gitrepo
from talkub.helm.install import helm_install
from talkub.kube import sealing
from talkub.kube.session import KubeSession, kube_session
from talkub.talos import kubeconfig
from talkub.talos.client import TalosSession, talos_session
from talkub.talos.inspect import bootstrap_already_done, etcd_member_count
from talkub.talos.lifecycle import ordered_nodes

BOOTSTRAP_HEALTH_TIMEOUT = "15m"
# Installed in this order from gitops/apps/<key>.yaml; coredns only if its app file exists.
OPTIONAL_KEYS = ("coredns",)
REQUIRED_KEYS = ("sealed-secrets", "argocd")
# Argo CD's namespace does not exist yet; sealed-secrets and coredns live in kube-system.
CREATE_NAMESPACE = {"argocd"}


def _step(text: str) -> None:
    print(f"== {text}", flush=True)


def upgrade_argocd(ctx: Context, kube: KubeSession) -> None:
    """`helm upgrade` the argocd release with its values file (e.g. after configs.ssh.extraHosts changed)."""
    require_tool("helm")
    helm_install(kube, app_source(ctx.repo, "argocd"), create_namespace=True)


def _bootstrap_sources(repo: Repo) -> dict[str, HelmSource]:
    """The chart sources bootstrap installs, in install order (`ValidationError` if one is broken)."""
    keys = [key for key in OPTIONAL_KEYS if (repo.gitops_dir / "apps" / f"{key}.yaml").exists()]
    return {key: app_source(repo, key) for key in [*keys, *REQUIRED_KEYS]}


def run(ctx: Context) -> None:
    for tool in ("talosctl", "kubectl", "helm", "kubeseal"):
        require_tool(tool)
    settings = ctx.repo.load_settings()
    sources = _bootstrap_sources(ctx.repo)
    controlplanes = [n for n in ordered_nodes(settings, []) if settings.nodes[n].role == "controlplane"]
    if not controlplanes:
        raise ValidationError("bootstrap needs a controlplane node")
    first = controlplanes[0]
    address = settings.mgmt_address(first)

    with talos_session(ctx) as talos:
        _step(f"bootstrapping etcd on {first} ({address})")
        member = _etcd_member(talos, settings, controlplanes)
        if member is not None:
            print(
                f"   etcd already has members on {member}; skipping bootstrap",
                flush=True,
            )
        else:
            _bootstrap_etcd(talos, address)
        _step("waiting for the cluster to become healthy")
        code = talos.call(address, ["health", "--wait-timeout", BOOTSTRAP_HEALTH_TIMEOUT])
        if code != 0:
            raise CommandFailed(
                "the cluster did not become healthy",
                hint=f"talosctl health exited with {code}",
            )
        _step("fetching the kubeconfig")
        data = kubeconfig.fetch(talos, ctx.repo, settings)
    kubeconfig.store(ctx, data)

    backup = sealing.key_backup_path(ctx.repo)
    had_backup = backup.is_file()
    with kube_session(ctx) as kube:
        for key, source in sources.items():
            if key == "sealed-secrets" and had_backup:
                _step("restoring the sealed-secrets key")
                with decrypted(ctx.sops, {"sealing-key.yaml": backup}) as plain:
                    # server-side: no last-applied-configuration annotation carrying the private key
                    kube.kubectl(["apply", "--server-side", "-f", str(plain["sealing-key.yaml"])])
            _step(f"installing {source.release}")
            helm_install(kube, source, create_namespace=key in CREATE_NAMESPACE)
            if key != "sealed-secrets":
                continue
            if had_backup:
                _warn_if_restored_key_is_stale(ctx, kube)
            else:
                # kube's own session stays open; backup_sealing_key opens a second one to talk
                # to the just-installed controller (a second kubeconfig decryption, harmless).
                _step("backing up the new sealed-secrets key")
                for path in sealing.backup_sealing_key(ctx):
                    print(f"   wrote {ctx.repo.rel(path)}")
                print(
                    "   commit bootstrap/ now — without the key backup, a rebuilt cluster cannot decrypt "
                    "existing SealedSecrets",
                    flush=True,
                )
        _step("applying the repo credentials and gitops/root.yaml")
        if warning := gitrepo.placeholder_warning(ctx.repo):
            print(f"   warning: {warning}", flush=True)
        gitrepo.apply(ctx, kube)

    print("bootstrap complete — commit kubernetes/ and bootstrap/")


# Before bootstrap etcd is not running and `talosctl etcd members` hangs instead of failing.
ETCD_PROBE_TIMEOUT = 20.0


def _etcd_member(talos: TalosSession, settings: Settings, controlplanes: list[str]) -> str | None:
    """The first control plane whose etcd already has members (bootstrapping again would split etcd)."""
    for name in controlplanes:
        proc = talos.run(
            settings.mgmt_address(name),
            ["etcd", "members"],
            check=False,
            timeout=ETCD_PROBE_TIMEOUT,
        )
        if proc.returncode == 124:
            print(
                f"   etcd on {name} did not answer within {ETCD_PROBE_TIMEOUT:g}s; treating it as not bootstrapped",
                flush=True,
            )
            continue
        if proc.returncode == 0 and (etcd_member_count(proc.stdout.decode(errors="replace")) or 0) >= 1:
            return name
    return None


def _bootstrap_etcd(talos: TalosSession, address: str) -> None:
    proc = talos.run(address, ["bootstrap"], check=False)
    if proc.returncode == 0:
        return
    stderr = (proc.stderr or b"").decode()
    if bootstrap_already_done(stderr):
        print("   etcd is already bootstrapped", flush=True)
        return
    raise CommandFailed(
        "talosctl bootstrap failed",
        hint=stderr.strip() or f"exit code {proc.returncode}",
    )


def _warn_if_restored_key_is_stale(ctx: Context, kube: KubeSession) -> None:
    """The controller may have rotated its key since the backup; warn, but do not back up automatically."""
    proc = ctx.runner.run(["kubeseal", "--fetch-cert", *sealing.CONTROLLER], env=kube.env, check=False)
    if proc.returncode == 0 and b"BEGIN CERTIFICATE" in proc.stdout:
        sealing.warn_if_cert_changed(ctx.repo, proc.stdout)
