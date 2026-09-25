"""CoreDNS renderers: the Talos patch that disables Talos' own CoreDNS manifest, and the
Helm values for the `coredns` chart that replaces it, managed by Argo CD.

The default `servers` block mirrors Talos 1.14's own Corefile exactly, so moving from Talos
to the chart does not change DNS behaviour; `add_stub_zone` extends it with a tailnet stub
zone once the Tailscale operator's DNSConfig pins the tailnet nameserver's ClusterIP.

`takeover` moves CoreDNS on a running cluster from Talos to that Helm release without a DNS
outage: the new pods join the existing `kube-dns` Service before the old ones are deleted."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from talkub import apps, bootstrap, charts, lifecycle, yamlio
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.kube import KubeSession, kube_session
from talkub.repo import Repo
from talkub.runner import require_tool
from talkub.talos.inspect import endpoint_pod_names

CLUSTER_DNS_IP = "10.96.0.10"
TS_NAMESERVER_IP = "10.96.0.53"
DEPLOYMENT = "coredns-talkub"
RELEASE = "coredns"
NAMESPACE = "kube-system"

_VALUES_HEADER = "# Helm values for CoreDNS (chart coredns). Used by talkub and by Argo CD.\n"

TALOS_DEFAULT_PLUGINS = [
    {"name": "errors"},
    {"name": "health", "configBlock": "lameduck 5s"},
    {"name": "ready"},
    {"name": "log", "parameters": ".", "configBlock": "class error"},
    {"name": "prometheus", "parameters": ":9153"},
    {
        "name": "kubernetes",
        "parameters": "cluster.local in-addr.arpa ip6.arpa",
        "configBlock": "pods insecure\nfallthrough in-addr.arpa ip6.arpa\nttl 30",
    },
    {"name": "forward", "parameters": ". /etc/resolv.conf", "configBlock": "max_concurrent 1000"},
    {
        "name": "cache",
        "parameters": 30,
        "configBlock": "disable success cluster.local\ndisable denial cluster.local",
    },
    {"name": "loop"},
    {"name": "reload"},
    {"name": "loadbalance"},
]


def _stub_server(tailnet: str) -> dict:
    return {
        "zones": [{"zone": tailnet}],
        "port": 53,
        "plugins": [
            {"name": "errors"},
            {"name": "cache", "parameters": 30},
            {"name": "forward", "parameters": f". {TS_NAMESERVER_IP}"},
        ],
    }


def talos_patch() -> str:
    doc = {"apiVersion": "v1alpha1", "kind": "KubeCoreDNSConfig", "enabled": False}
    return yamlio.dump(doc)


def values(tailnet: str | None = None) -> str:
    servers = [{"zones": [{"zone": "."}], "port": 53, "plugins": TALOS_DEFAULT_PLUGINS}]
    if tailnet is not None:
        servers.append(_stub_server(tailnet))
    doc = {
        "fullnameOverride": DEPLOYMENT,
        "k8sAppLabelOverride": "kube-dns",
        "replicaCount": 2,
        "priorityClassName": "system-cluster-critical",
        "tolerations": [
            {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
        ],
        "service": {"name": "kube-dns", "clusterIP": CLUSTER_DNS_IP, "selector": {"k8s-app": "kube-dns"}},
        "servers": servers,
    }
    return _VALUES_HEADER + yamlio.dump(doc)


def add_stub_zone(values_text: str, tailnet: str) -> tuple[str, bool]:
    """Add the tailnet stub zone to an existing values document (round-trip via yamlio, so
    comments are kept). Unchanged when a server already serves that zone."""
    doc = yamlio.load(values_text)
    servers = doc.setdefault("servers", [])
    for server in servers:
        zones = server.get("zones") or []
        if any(zone.get("zone") == tailnet for zone in zones):
            return values_text, False
    servers.append(_stub_server(tailnet))
    return yamlio.dump(doc), True


QUESTION = "Move CoreDNS from Talos to Argo CD now? DNS keeps working during the switch."
# After the old pods are deleted the endpoints controller needs a moment to drop them.
ENDPOINTS_ATTEMPTS = 15
ENDPOINTS_INTERVAL = 2.0
_RESUME_HINT = (
    "old CoreDNS objects are deleted only after the new pods are ready; "
    "fix the cause, then run `talkub coredns takeover --resume`"
)


def app_path(repo: Repo) -> Path:
    return repo.gitops_dir / "apps" / "coredns.yaml"


def values_path(repo: Repo) -> Path:
    return repo.gitops_dir / "values" / "coredns.yaml"


def patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "controlplane-coredns.yaml"


def _step(text: str) -> None:
    print(f"== {text}", flush=True)


@contextmanager
def _failing_step(name: str) -> Iterator[None]:
    """Name the step in any CommandFailed so a partial takeover is easy to resume."""
    try:
        yield
    except CommandFailed as err:
        hint = f"{err.hint}; {_RESUME_HINT}" if err.hint else _RESUME_HINT
        raise CommandFailed(f"takeover step '{name}' failed: {err}", hint=hint) from None


def takeover(ctx: Context, *, resume: bool = False, confirm: Callable[[str], bool]) -> list[str]:
    """Move CoreDNS from Talos' bootstrap manifest to the Helm release `coredns` (Argo CD app)."""
    repo = ctx.repo
    require_tool("helm")
    if not lifecycle.is_bootstrapped(ctx):
        raise ValidationError("the cluster is not bootstrapped", hint="talkub bootstrap")
    settings = repo.load_settings()
    if settings.git is None:
        raise ValidationError("no git URL in .talkub.yaml", hint="talkub repo set-url <url>")
    if app_path(repo).exists() and not resume:
        raise ValidationError("CoreDNS is already managed by talkub", hint="--resume if the takeover was interrupted")
    if resume and not (app_path(repo).exists() and values_path(repo).exists() and "coredns" in settings.charts):
        raise ValidationError(
            "nothing to resume: gitops/apps/coredns.yaml, gitops/values/coredns.yaml or charts.coredns is missing",
            hint="run `talkub coredns takeover` without --resume",
        )
    with kube_session(ctx) as kube:
        cluster_ip = kube.kubectl(
            ["-n", NAMESPACE, "get", "svc", "kube-dns", "-o", "jsonpath={.spec.clusterIP}"]
        ).stdout.decode().strip()
    if cluster_ip != CLUSTER_DNS_IP:
        raise ValidationError(
            f"kube-dns has ClusterIP {cluster_ip or 'none'}, expected {CLUSTER_DNS_IP}",
            hint="the CoreDNS values pin the Service to the Talos default ClusterIP",
        )
    if not confirm(QUESTION):
        print("aborted, nothing was changed")
        return []

    if not resume:
        _write_repo_files(ctx)
    chart = repo.load_settings().charts["coredns"]

    _step("applying the Talos config (Talos stops managing CoreDNS)")
    with _failing_step("talos apply"):
        lifecycle.apply(ctx, [], confirm=lambda question: True)
    with kube_session(ctx) as kube:
        _warn_unless_old_pods_ready(kube)
        _step("adopting the kube-dns Service into the Helm release")
        with _failing_step("adopt kube-dns"):
            annotations = [f"meta.helm.sh/release-name={RELEASE}", f"meta.helm.sh/release-namespace={NAMESPACE}"]
            kube.kubectl(["-n", NAMESPACE, "annotate", "svc", "kube-dns", "--overwrite", *annotations])
            kube.kubectl(["-n", NAMESPACE, "label", "svc", "kube-dns", "--overwrite", "app.kubernetes.io/managed-by=Helm"])
        _step(f"installing {DEPLOYMENT} (Helm release {RELEASE})")
        with _failing_step("helm install"):
            bootstrap.helm_install(kube, repo, "coredns", RELEASE, chart, NAMESPACE, False)
        with _failing_step("rollout status"):
            kube.kubectl(["-n", NAMESPACE, "rollout", "status", f"deploy/{DEPLOYMENT}", "--timeout", "5m"])
        _step("deleting the Talos CoreDNS objects")
        with _failing_step("delete old CoreDNS"):
            for kind in ("deploy", "configmap", "serviceaccount"):
                cascade = ["--cascade=foreground"] if kind == "deploy" else []  # returns once the old pods are gone
                kube.kubectl(["-n", NAMESPACE, "delete", kind, "coredns", "--ignore-not-found", *cascade])
            for kind in ("clusterrolebinding", "clusterrole"):
                kube.kubectl(["delete", kind, "system:coredns", "--ignore-not-found"])
        _step("checking the kube-dns endpoints")
        _verify_endpoints(kube)
    return [
        f"CoreDNS now runs as {DEPLOYMENT} (Helm release {RELEASE})",
        "commit and push gitops/ and talos/ — Argo CD manages CoreDNS from now on",
    ]


def _write_repo_files(ctx: Context) -> None:
    repo = ctx.repo
    settings = repo.load_settings()
    chart = charts.resolve(ctx.runner, "coredns")
    settings.charts["coredns"] = chart
    repo.write_settings(settings)
    operator_app = repo.gitops_dir / "apps" / "tailscale-operator.yaml"
    tailnet = settings.tailscale.tailnet if settings.tailscale and operator_app.exists() else None
    values_file = repo.rel(values_path(repo))
    files = {
        patch_path(repo): talos_patch(),
        values_path(repo): values(tailnet),
        app_path(repo): apps.helm_app("coredns", chart, settings.git.url, values_file, NAMESPACE),
    }
    for path, text in files.items():
        atomic_write(path, text.encode())
        print(f"   wrote {repo.rel(path)}")


def _warn_unless_old_pods_ready(kube: KubeSession) -> None:
    result = kube.kubectl(
        ["-n", NAMESPACE, "get", "deploy", "coredns", "-o", "jsonpath={.status.readyReplicas}"], check=False
    )
    ready = result.stdout.decode().strip() if result.returncode == 0 else ""
    if ready in ("", "0"):
        print(
            "warning: the Talos CoreDNS deployment has no ready pods; continuing with the Helm release",
            file=sys.stderr,
            flush=True,
        )


def _verify_endpoints(kube: KubeSession) -> None:
    names: list[str] = []
    for attempt in range(ENDPOINTS_ATTEMPTS):
        if attempt:
            time.sleep(ENDPOINTS_INTERVAL)
        text = kube.kubectl(["-n", NAMESPACE, "get", "endpoints", "kube-dns", "-o", "json"]).stdout.decode()
        names = endpoint_pod_names(text)
        if names and all(name.startswith(f"{DEPLOYMENT}-") for name in names):
            return
    raise CommandFailed(
        f"kube-dns endpoints are not only {DEPLOYMENT} pods: {', '.join(names) or 'none'}",
        hint=f"check `talkub k -n {NAMESPACE} get pods -l k8s-app=kube-dns`; {_RESUME_HINT}",
    )
