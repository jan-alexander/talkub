"""talkub bootstrap: etcd → health → kubeconfig → sealing key → Helm releases → root app."""

from __future__ import annotations

from talkub import sealing
from talkub.context import Context
from talkub.ephemeral import decrypted
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import KubeSession, kube_session
from talkub.lifecycle import ordered_nodes
from talkub.repo import ChartSpec, Repo
from talkub.runner import require_tool
from talkub.talos.client import TalosSession, talos_session

BOOTSTRAP_HEALTH_TIMEOUT = "15m"
# chart key in .talkub.yaml -> (Helm release, namespace, create namespace)
RELEASES = {
    "sealed-secrets": ("sealed-secrets", "kube-system", False),
    "argocd": ("argocd", "argocd", True),
}


def _step(text: str) -> None:
    print(f"== {text}", flush=True)


def run(ctx: Context) -> None:
    for tool in ("talosctl", "kubectl", "helm", "kubeseal"):
        require_tool(tool)
    settings = ctx.repo.load_settings()
    missing = [key for key in RELEASES if key not in settings.charts]
    if missing:
        raise ValidationError(f"missing chart settings in .talkub.yaml: {', '.join(missing)}")
    first = ordered_nodes(settings, [])[0]
    if settings.node(first).role != "controlplane":
        raise ValidationError("bootstrap needs a controlplane node")
    address = settings.mgmt_address(first)

    with talos_session(ctx) as talos:
        _step(f"bootstrapping etcd on {first} ({address})")
        _bootstrap_etcd(talos, address)
        _step("waiting for the cluster to become healthy")
        code = talos.call(address, ["health", "--wait-timeout", BOOTSTRAP_HEALTH_TIMEOUT])
        if code != 0:
            raise CommandFailed("the cluster did not become healthy", hint=f"talosctl health exited with {code}")
        kubeconfig = talos.run(address, ["kubeconfig", "-", "--merge=false"]).stdout
    _store_kubeconfig(ctx, kubeconfig)

    backup = sealing.key_backup_path(ctx.repo)
    had_backup = backup.is_file()
    with kube_session(ctx) as kube:
        if had_backup:
            _step("restoring the sealed-secrets key")
            with decrypted(ctx.sops, {"sealing-key.yaml": backup}) as plain:
                kube.kubectl(["apply", "-f", str(plain["sealing-key.yaml"])])
        for key, (release, namespace, create) in RELEASES.items():
            _step(f"installing {release}")
            _helm_install(kube, ctx.repo, key, release, settings.charts[key], namespace, create)
        _step("applying gitops/root.yaml")
        kube.kubectl(["apply", "-f", str(ctx.repo.gitops_dir / "root.yaml")])

    if not had_backup:
        _step("backing up the new sealed-secrets key")
        sealing.backup_sealing_key(ctx)
    print("bootstrap complete — commit kubernetes/ and bootstrap/")


def _bootstrap_etcd(talos: TalosSession, address: str) -> None:
    proc = talos.run(address, ["bootstrap"], check=False)
    if proc.returncode == 0:
        return
    stderr = (proc.stderr or b"").decode()
    if "AlreadyExists" in stderr or "already bootstrapped" in stderr.lower():
        print("   etcd is already bootstrapped")
        return
    raise CommandFailed("talosctl bootstrap failed", hint=stderr.strip() or f"exit code {proc.returncode}")


def _store_kubeconfig(ctx: Context, kubeconfig: bytes) -> None:
    target = ctx.repo.kubeconfig
    if target.is_file() and ctx.sops.decrypt(target) == kubeconfig:
        print("   kubeconfig unchanged")
        return
    atomic_write(target, ctx.sops.encrypt(kubeconfig, filename=target.name, public_key=ctx.keyring.public_key()))
    print(f"   wrote {ctx.repo.rel(target)}")


def _helm_install(
    kube: KubeSession, repo: Repo, key: str, release: str, chart: ChartSpec, namespace: str, create: bool
) -> None:
    values = repo.gitops_dir / "values" / f"{key}.yaml"
    if not values.is_file():
        raise ValidationError(f"{repo.rel(values)} does not exist")
    if chart.repo.startswith("oci://"):
        ref = [f"{chart.repo.rstrip('/')}/{chart.chart}"]
    else:
        ref = [chart.chart, "--repo", chart.repo]
    argv = [
        "upgrade", "--install", release, *ref,
        "--version", chart.version,
        "--namespace", namespace,
        "--values", str(values),
        "--wait", "--timeout", "10m",
    ]
    if create:
        argv.append("--create-namespace")
    kube.helm(argv)
