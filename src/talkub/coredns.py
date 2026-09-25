"""CoreDNS renderers: the Talos patch that disables Talos' own CoreDNS manifest, and the
Helm values for the `coredns` chart that replaces it, managed by Argo CD.

The default `servers` block mirrors Talos 1.14's own Corefile exactly, so moving from Talos
to the chart does not change DNS behaviour; `add_stub_zone` extends it with a tailnet stub
zone once the Tailscale operator's DNSConfig pins the tailnet nameserver's ClusterIP.

`takeover` moves CoreDNS on a running cluster from Talos to that Helm release without a DNS
outage: the new pods join the existing `kube-dns` Service before the old ones are deleted."""

from __future__ import annotations

import json
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
from talkub.repo import ChartSpec, Repo
from talkub.runner import require_tool
from talkub.talos.inspect import endpointslice_pod_names, service_spec

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
        "zones": [{"zone": tailnet, "use_tcp": True}],
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
    servers = [{"zones": [{"zone": ".", "use_tcp": True}], "port": 53, "plugins": TALOS_DEFAULT_PLUGINS}]
    if tailnet is not None:
        servers.append(_stub_server(tailnet))
    doc = {
        "fullnameOverride": DEPLOYMENT,
        "k8sAppLabelOverride": "kube-dns",
        "replicaCount": 2,
        "priorityClassName": "system-cluster-critical",
        "serviceAccount": {"create": True},
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
        result = kube.kubectl(
            ["-n", NAMESPACE, "get", "svc", "kube-dns", "-o", "jsonpath={.spec.clusterIP}"], check=False
        )
    stderr = result.stderr.decode(errors="replace").strip()
    # On --resume the Service may be gone (e.g. pruned); Helm then creates it with the pinned ClusterIP.
    service_missing = result.returncode != 0 and "NotFound" in stderr
    if result.returncode != 0 and not (resume and service_missing):
        raise ValidationError(f"cannot read the kube-dns Service: {stderr or 'kubectl failed'}")
    cluster_ip = result.stdout.decode().strip()
    if not service_missing and cluster_ip != CLUSTER_DNS_IP:
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
        if service_missing:
            print("   the kube-dns Service does not exist; the Helm release creates it", flush=True)
            with _failing_step("render service"):
                ports = _rendered_ports(kube, repo, chart)
        else:
            _step("adopting the kube-dns Service into the Helm release")
            with _failing_step("adopt kube-dns"):
                ports = _adopt_service(kube, repo, chart)
        _step(f"installing {DEPLOYMENT} (Helm release {RELEASE})")
        with _failing_step("helm install"):
            bootstrap.helm_install(kube, repo, "coredns", RELEASE, chart, NAMESPACE, False)
        with _failing_step("rollout status"):
            kube.kubectl(["-n", NAMESPACE, "rollout", "status", f"deploy/{DEPLOYMENT}", "--timeout", "5m"])
        with _failing_step("verify service"):
            _verify_service(kube, ports)
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


def write_files(repo: Repo, chart: ChartSpec, git_url: str, tailnet: str | None = None) -> None:
    """Write the Talos patch, Helm values and Argo CD app for CoreDNS. Shared by `init_cluster`
    (a fresh cluster, no stub zone yet) and `takeover` (a running cluster, possibly with the
    Tailscale operator already configured)."""
    values_file = repo.rel(values_path(repo))
    files = {
        patch_path(repo): talos_patch(),
        values_path(repo): values(tailnet),
        app_path(repo): apps.helm_app("coredns", chart, git_url, values_file, NAMESPACE),
    }
    for path, text in files.items():
        atomic_write(path, text.encode())
        print(f"   wrote {repo.rel(path)}")


def _write_repo_files(ctx: Context) -> None:
    repo = ctx.repo
    settings = repo.load_settings()
    chart = charts.resolve(ctx.runner, "coredns")
    settings.charts["coredns"] = chart
    repo.write_settings(settings)
    operator_app = repo.gitops_dir / "apps" / "tailscale-operator.yaml"
    tailnet = settings.tailscale.tailnet if settings.tailscale and operator_app.exists() else None
    write_files(repo, chart, settings.git.url, tailnet)


def _rendered_ports(kube: KubeSession, repo: Repo, chart: ChartSpec) -> list[dict]:
    """The chart's kube-dns Service ports for our values (`helm template`), so names cannot drift."""
    argv = [
        "template", RELEASE, *bootstrap.chart_ref(chart),
        "--version", chart.version,
        "--namespace", NAMESPACE,
        "--values", str(values_path(repo)),
        "--show-only", "templates/service.yaml",
    ]
    ports = service_spec(kube.helm(argv).stdout.decode()).get("ports")
    if not isinstance(ports, list) or not ports:
        raise CommandFailed("helm template rendered no Service ports for kube-dns")
    return ports


def _adopt_service(kube: KubeSession, repo: Repo, chart: ChartSpec) -> list[dict]:
    """Hand the Talos-applied kube-dns Service to Helm without a gap in DNS.

    Talos' ports (dns, dns-tcp, metrics) differ from the chart's; neither Helm 3's strategic merge
    (keyed by port only, two port-53 entries) nor Helm 4's server-side apply (field conflicts with
    Talos' manager) can reconcile them, so the list is replaced up front and the field managers are
    reset. targetPort stays 53, so the old pods keep serving until they are deleted."""
    annotations = [f"meta.helm.sh/release-name={RELEASE}", f"meta.helm.sh/release-namespace={NAMESPACE}"]
    kube.kubectl(["-n", NAMESPACE, "annotate", "svc", "kube-dns", "--overwrite", *annotations])
    kube.kubectl(["-n", NAMESPACE, "label", "svc", "kube-dns", "--overwrite", "app.kubernetes.io/managed-by=Helm"])
    ports = _rendered_ports(kube, repo, chart)
    patch = json.dumps([{"op": "replace", "path": "/spec/ports", "value": ports}])
    kube.kubectl(["-n", NAMESPACE, "patch", "svc", "kube-dns", "--type=json", "-p", patch])
    reset = json.dumps({"metadata": {"managedFields": [{}]}})
    kube.kubectl(["-n", NAMESPACE, "patch", "svc", "kube-dns", "--type=merge", "-p", reset])
    return ports


def _port_keys(ports: object) -> list[tuple]:
    entries = ports if isinstance(ports, list) else []
    return [(p.get("name"), p.get("port"), p.get("protocol")) for p in entries if isinstance(p, dict)]


def _verify_service(kube: KubeSession, ports: list[dict]) -> None:
    spec = service_spec(kube.kubectl(["-n", NAMESPACE, "get", "svc", "kube-dns", "-o", "json"]).stdout.decode())
    live, expected = _port_keys(spec.get("ports")), _port_keys(ports)
    if spec.get("clusterIP") != CLUSTER_DNS_IP or live != expected:
        raise CommandFailed(
            f"kube-dns has ClusterIP {spec.get('clusterIP') or 'none'} and ports {live}, "
            f"expected {CLUSTER_DNS_IP} and {expected}"
        )


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
        selector = "kubernetes.io/service-name=kube-dns"
        text = kube.kubectl(["-n", NAMESPACE, "get", "endpointslices", "-l", selector, "-o", "json"]).stdout.decode()
        names = endpointslice_pod_names(text)
        if names and all(name.startswith(f"{DEPLOYMENT}-") for name in names):
            return
    raise CommandFailed(
        f"kube-dns endpoints are not only {DEPLOYMENT} pods: {', '.join(names) or 'none'}",
        hint=f"check `talkub k -n {NAMESPACE} get pods -l k8s-app=kube-dns`; {_RESUME_HINT}",
    )
