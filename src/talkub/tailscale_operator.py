"""Tailscale operator (egress via ExternalName services; its API server proxy stays disabled):
Helm values, Argo CD apps, the DNSConfig that points CoreDNS' tailnet stub at the
operator's nameserver, and the SealedSecret for its OAuth client.

The OAuth client secret is never written in plaintext or passed on argv: it is built
into a Secret manifest in memory (`sealing.secret_manifest`) and sealed with kubeseal
before anything touches disk (`sealing.seal`)."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from talkub import apps, charts, coredns
from talkub.config import ChartSpec, Repo, Settings, valid_label
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write
from talkub.kube import sealing

NAMESPACE = "tailscale"
CHART_KEY = "tailscale-operator"
_VALUES_HEADER = "# Helm values for the Tailscale operator (chart tailscale-operator). Used by talkub and by Argo CD.\n"
_NAMESPACE_HEADER = "# Managed by talkub. The Tailscale proxies need privileged pods (sysctl, NET_ADMIN).\n"
_INGRESS_HEADER = "# Managed by talkub (talkub tailscale expose).\n"

_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

EGRESS_COMMIT_NOTE = "commit and push gitops/"

ACL_NOTE = 'add to the tailnet ACL: `"tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}`'
SCOPES_NOTE = "create the OAuth client with scopes Devices Core, Auth Keys, Services (write) and tag tag:k8s-operator"
COMMIT_NOTE = "commit and push gitops/ — Argo CD installs the operator"

INGRESS_BACKEND_NOTE = "the backend must speak HTTP — TLS ends at the Tailscale proxy"
INGRESS_CERT_NOTE = "HTTPS certificates must be enabled in the tailnet"
INGRESS_COMMIT_NOTE = "commit and push gitops/"


def app_path(repo: Repo) -> Path:
    return repo.gitops_dir / "apps" / "tailscale-operator.yaml"


def values_path(repo: Repo) -> Path:
    return repo.gitops_dir / "values" / "tailscale-operator.yaml"


def config_app_path(repo: Repo) -> Path:
    return repo.gitops_dir / "apps" / "tailscale-operator-config.yaml"


def manifests_dir(repo: Repo) -> Path:
    return repo.gitops_dir / "manifests" / "tailscale"


def dnsconfig_path(repo: Repo) -> Path:
    return manifests_dir(repo) / "dnsconfig.yaml"


def namespace_path(repo: Repo) -> Path:
    return manifests_dir(repo) / "namespace.yaml"


def secret_path(repo: Repo) -> Path:
    return manifests_dir(repo) / "operator-oauth.sealed.yaml"


def egress_dir(repo: Repo) -> Path:
    return manifests_dir(repo) / "egress"


def egress_path(repo: Repo, name: str) -> Path:
    return egress_dir(repo) / f"{name}.yaml"


def ingress_dir(repo: Repo) -> Path:
    return manifests_dir(repo) / "ingress"


def ingress_path(repo: Repo, name: str) -> Path:
    return ingress_dir(repo) / f"{name}.yaml"


def values(cluster: str) -> str:
    doc = {
        "installCRDs": True,
        "operatorConfig": {
            "hostname": f"{cluster}-operator",
            "defaultTags": ["tag:k8s-operator"],
        },
        "proxyConfig": {"defaultTags": "tag:k8s"},
        "apiServerProxyConfig": {"mode": "false"},
    }
    return _VALUES_HEADER + yamlio.dump(doc)


def dnsconfig() -> str:
    doc = {
        "apiVersion": "tailscale.com/v1alpha1",
        "kind": "DNSConfig",
        "metadata": {"name": "ts-dns"},
        "spec": {"nameserver": {"service": {"clusterIP": coredns.TS_NAMESERVER_IP}}},
    }
    return yamlio.dump(doc)


def namespace() -> str:
    """Talos enforces Pod Security `baseline`; the egress/ingress proxy pods need a privileged
    init container (sysctl) and NET_ADMIN, so this namespace is loosened."""
    doc = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
        },
    }
    return _NAMESPACE_HEADER + yamlio.dump(doc)


def _check_single_line(value: str, what: str) -> str:
    value = value.strip()
    if not value or "\n" in value:
        raise ValidationError(f"{what} must be a non-empty single line")
    return value


def check_can_enable(repo: Repo, *, rotate_credentials: bool = False) -> Settings:
    """Refuse `enable` when a precondition is missing, or when it is already enabled and
    `--rotate-credentials` was not given. Called before the OAuth client secret is asked for."""
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError(
            "tailscale is not enabled", hint="run `talkub tailscale enable` first"
        )
    if not coredns.app_path(repo).exists():
        raise ValidationError(
            "CoreDNS is not managed by talkub yet", hint="talkub coredns takeover"
        )
    if settings.git is None:
        raise ValidationError(
            "no git URL in .talkub.yaml", hint="talkub repo set-url <url>"
        )
    if app_path(repo).exists() and not rotate_credentials:
        raise ValidationError(
            "the Tailscale operator is already enabled",
            hint="--rotate-credentials to replace the OAuth client",
        )
    return settings


def write_files(repo: Repo, chart: ChartSpec, git_url: str, cluster: str) -> None:
    values_file = repo.rel(values_path(repo))
    files = {
        values_path(repo): values(cluster),
        app_path(repo): apps.helm_app(
            "tailscale-operator",
            chart,
            git_url,
            values_file,
            NAMESPACE,
            create_namespace=True,
            sync_wave="0",
        ),
        # Wave 1: the manifests need the operator's CRDs. The operator rewrites spec.externalName
        # of egress Services (tailscale/tailscale#15098); selfHeal must leave that field alone.
        config_app_path(repo): apps.directory_app(
            "tailscale-operator-config",
            git_url,
            repo.rel(manifests_dir(repo)),
            NAMESPACE,
            create_namespace=True,
            skip_dry_run_on_missing=True,
            ignore_differences=[
                {"group": "", "kind": "Service", "jsonPointers": ["/spec/externalName"]}
            ],
            sync_wave="1",
        ),
        dnsconfig_path(repo): dnsconfig(),
        namespace_path(repo): namespace(),
    }
    for path, text in files.items():
        atomic_write(path, text.encode())
        print(f"   wrote {repo.rel(path)}")


def _add_coredns_stub(repo: Repo, tailnet: str) -> None:
    path = coredns.values_path(repo)
    text = path.read_text() if path.exists() else coredns.values()
    new_text, changed = coredns.add_stub_zone(text, tailnet)
    if changed:
        atomic_write(path, new_text.encode())
        print(f"   wrote {repo.rel(path)}")


def enable(
    ctx: Context,
    *,
    client_id: str,
    client_secret: str,
    rotate_credentials: bool = False,
) -> list[str]:
    """Enable (or, with `rotate_credentials`, re-seal the OAuth client of) the Tailscale operator."""
    repo = ctx.repo
    settings = check_can_enable(repo, rotate_credentials=rotate_credentials)
    client_id = _check_single_line(client_id, "the OAuth client ID")
    client_secret = _check_single_line(client_secret, "the OAuth client secret")
    manifest = sealing.secret_manifest(
        "operator-oauth",
        NAMESPACE,
        [],
        [],
        {"client_id": client_id, "client_secret": client_secret},
    )
    sealed = sealing.seal(ctx, manifest)
    sealing.write_sealed(sealed, secret_path(repo))
    print(f"   wrote {repo.rel(secret_path(repo))}")

    if app_path(repo).exists():
        if not namespace_path(repo).exists():
            atomic_write(namespace_path(repo), namespace().encode())
            print(
                f"   wrote {repo.rel(namespace_path(repo))} (added: privileged pod security for this namespace)"
            )
        return ["re-sealed the OAuth client secret", COMMIT_NOTE]

    chart = charts.resolve(ctx.runner, CHART_KEY)
    write_files(repo, chart, settings.git.url, settings.name)
    _add_coredns_stub(repo, settings.tailscale.tailnet)
    return [ACL_NOTE, SCOPES_NOTE, COMMIT_NOTE]


def _tailnet(settings: Settings) -> str:
    if settings.tailscale is None:
        raise ValidationError(
            "tailscale is not enabled", hint="talkub tailscale enable"
        )
    return settings.tailscale.tailnet


def _check_ingress_name(name: str) -> str:
    if not valid_label(name):
        raise ValidationError(
            f"invalid name {name!r}",
            hint="lowercase letters, digits and '-', max 63 characters (it becomes the tailnet device name)",
        )
    return name


def _check_egress_name(name: str) -> str:
    if not valid_label(name):
        raise ValidationError(
            f"invalid egress name {name!r}",
            hint="lowercase letters, digits and '-', max 63 characters (it becomes the Service name)",
        )
    return name


def _check_fqdn(fqdn: str, tailnet: str) -> str:
    if not fqdn.lower().endswith(f".{tailnet.lower()}"):
        raise ValidationError(
            f"--fqdn must end with .{tailnet}", hint=f"e.g. host.{tailnet}"
        )
    return fqdn


def _check_ip(ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        raise ValidationError(f"invalid --ip {ip!r}") from None
    if address not in _TAILNET_V4 and address not in _TAILNET_V6:
        raise ValidationError(
            f"--ip {ip!r} is not a tailnet address",
            hint="use an address in 100.64.0.0/10 or fd7a:115c:a1e0::/48",
        )
    return ip


def egress_service(name: str, namespace: str, annotations: dict[str, str]) -> str:
    doc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "annotations": annotations},
        "spec": {"type": "ExternalName", "externalName": "placeholder"},
    }
    return yamlio.dump(doc)


def egress_add(
    ctx: Context,
    name: str,
    *,
    fqdn: str | None = None,
    ip: str | None = None,
    namespace: str = NAMESPACE,
) -> Path:
    """Write a Service in `gitops/manifests/tailscale/egress/<name>.yaml` that lets pods reach a
    tailnet host (`fqdn`) or address (`ip`) via an ExternalName Service that the operator points
    at the tailnet target."""
    repo = ctx.repo
    if not app_path(repo).exists():
        raise ValidationError(
            "the Tailscale operator is not enabled",
            hint="talkub tailscale operator enable",
        )
    settings = repo.load_settings()
    _check_egress_name(name)
    if (fqdn is None) == (ip is None):
        raise ValidationError("give exactly one of --fqdn or --ip")
    if fqdn is not None:
        annotations = {
            "tailscale.com/tailnet-fqdn": _check_fqdn(fqdn, _tailnet(settings))
        }
    else:
        annotations = {"tailscale.com/tailnet-ip": _check_ip(ip)}
    path = egress_path(repo, name)
    if path.exists():
        raise ValidationError(
            f"{repo.rel(path)} already exists",
            hint="talkub tailscale egress remove first, or choose another name",
        )
    atomic_write(path, egress_service(name, namespace, annotations).encode())
    print(f"   wrote {repo.rel(path)}")
    if namespace != NAMESPACE:
        print(f"note: the namespace {namespace!r} must exist")
    return path


def egress_remove(ctx: Context, name: str) -> Path:
    """Delete the egress Service manifest written by `egress_add`."""
    repo = ctx.repo
    path = egress_path(repo, _check_egress_name(name))
    if not path.exists():
        raise ValidationError(f"{repo.rel(path)} does not exist")
    path.unlink()
    print(f"   removed {repo.rel(path)}")
    return path


def _check_service(service: str) -> tuple[str, str]:
    parts = service.split("/")
    if len(parts) != 2 or not valid_label(parts[0]) or not valid_label(parts[1]):
        raise ValidationError(
            f"invalid --service {service!r}",
            hint="expected <namespace>/<service>, both DNS-1123 labels",
        )
    return parts[0], parts[1]


def _check_port(port: str) -> int | str:
    if port.isdigit():
        number = int(port)
        if not (1 <= number <= 65535):
            raise ValidationError(
                f"invalid --port {port!r}",
                hint="1-65535, or a port name (lowercase letters, digits and '-')",
            )
        return number
    if not valid_label(port):
        raise ValidationError(
            f"invalid --port {port!r}",
            hint="1-65535, or a port name (lowercase letters, digits and '-')",
        )
    return port


def _check_path(path: str) -> str:
    if not path.startswith("/"):
        raise ValidationError(f"--path must start with '/': {path!r}")
    return path


def ingress(
    name: str, namespace: str, service: str, port: int | str, path: str | None = None
) -> str:
    """Render a Tailscale-Ingress manifest exposing `service` (in `namespace`) at
    `https://<name>.<tailnet>`, either as a `defaultBackend` or, with `path`, a single
    path-based rule."""
    port_field = {"number": port} if isinstance(port, int) else {"name": port}
    backend = {"service": {"name": service, "port": port_field}}
    spec: dict = {"ingressClassName": "tailscale"}
    if path is None:
        spec["defaultBackend"] = backend
    else:
        spec["rules"] = [
            {
                "http": {
                    "paths": [{"path": path, "pathType": "Prefix", "backend": backend}]
                }
            }
        ]
    spec["tls"] = [{"hosts": [name]}]
    doc = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": f"tailscale-{name}", "namespace": namespace},
        "spec": spec,
    }
    return _INGRESS_HEADER + yamlio.dump(doc)


def expose(
    ctx: Context, name: str, *, service: str, port: str, path: str | None = None
) -> Path:
    """Write an Ingress in `gitops/manifests/tailscale/ingress/<name>.yaml` that exposes a
    Service in the tailnet at `https://<name>.<tailnet>`. The backend must speak HTTP: the
    Tailscale proxy terminates TLS."""
    repo = ctx.repo
    if not app_path(repo).exists():
        raise ValidationError(
            "the Tailscale operator is not enabled",
            hint="talkub tailscale operator enable",
        )
    tailnet = _tailnet(repo.load_settings())
    _check_ingress_name(name)
    namespace, svc = _check_service(service)
    port_value = _check_port(port)
    if path is not None:
        path = _check_path(path)
    target = ingress_path(repo, name)
    if target.exists():
        raise ValidationError(
            f"{repo.rel(target)} already exists",
            hint=f"talkub tailscale unexpose {name}",
        )
    atomic_write(target, ingress(name, namespace, svc, port_value, path).encode())
    print(f"   wrote {repo.rel(target)}")
    print(f"URL: https://{name}.{tailnet}")
    return target


def unexpose(ctx: Context, name: str) -> Path:
    """Delete the Ingress manifest written by `expose`."""
    repo = ctx.repo
    path = ingress_path(repo, _check_ingress_name(name))
    if not path.exists():
        raise ValidationError(f"{repo.rel(path)} does not exist")
    path.unlink()
    print(f"   removed {repo.rel(path)}")
    return path
