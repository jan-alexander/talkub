"""Tailscale operator (Kubernetes API server proxy, egress via ExternalName services):
Helm values, Argo CD apps, the DNSConfig that points CoreDNS' tailnet stub at the
operator's nameserver, and the SealedSecret for its OAuth client.

The OAuth client secret is never written in plaintext or passed on argv: it is built
into a Secret manifest in memory (`sealing.secret_manifest`) and sealed with kubeseal
before anything touches disk (`sealing.seal`)."""

from __future__ import annotations

from pathlib import Path

from talkub import apps, charts, coredns, sealing, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import ChartSpec, Repo, Settings

NAMESPACE = "tailscale"
CHART_KEY = "tailscale-operator"
_VALUES_HEADER = "# Helm values for the Tailscale operator (chart tailscale-operator). Used by talkub and by Argo CD.\n"

ACL_NOTE = 'add to the tailnet ACL: `"tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}`'
SCOPES_NOTE = (
    "create the OAuth client with scopes Devices Core, Auth Keys, Services (write) and tag tag:k8s-operator"
)
COMMIT_NOTE = "commit and push gitops/ — Argo CD installs the operator"


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


def secret_path(repo: Repo) -> Path:
    return manifests_dir(repo) / "operator-oauth.sealed.yaml"


def values(cluster: str) -> str:
    doc = {
        "installCRDs": True,
        "operatorConfig": {"hostname": f"{cluster}-operator", "defaultTags": ["tag:k8s-operator"]},
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
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if not coredns.app_path(repo).exists():
        raise ValidationError("CoreDNS is not managed by talkub yet", hint="talkub coredns takeover")
    if settings.git is None:
        raise ValidationError("no git URL in .talkub.yaml", hint="talkub repo set-url <url>")
    if app_path(repo).exists() and not rotate_credentials:
        raise ValidationError(
            "the Tailscale operator is already enabled", hint="--rotate-credentials to replace the OAuth client"
        )
    return settings


def write_files(repo: Repo, chart: ChartSpec, git_url: str, cluster: str) -> None:
    values_file = repo.rel(values_path(repo))
    files = {
        values_path(repo): values(cluster),
        app_path(repo): apps.helm_app(
            "tailscale-operator", chart, git_url, values_file, NAMESPACE, create_namespace=True
        ),
        config_app_path(repo): apps.directory_app(
            "tailscale-operator-config", git_url, repo.rel(manifests_dir(repo)), NAMESPACE,
            skip_dry_run_on_missing=True,
        ),
        dnsconfig_path(repo): dnsconfig(),
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


def enable(ctx: Context, *, client_id: str, client_secret: str, rotate_credentials: bool = False) -> list[str]:
    """Enable (or, with `rotate_credentials`, re-seal the OAuth client of) the Tailscale operator."""
    repo = ctx.repo
    settings = check_can_enable(repo, rotate_credentials=rotate_credentials)
    client_id = _check_single_line(client_id, "the OAuth client ID")
    client_secret = _check_single_line(client_secret, "the OAuth client secret")
    manifest = sealing.secret_manifest(
        "operator-oauth", NAMESPACE, [], [], {"client_id": client_id, "client_secret": client_secret}
    )
    sealed = sealing.seal(ctx, manifest)
    sealing.write_sealed(sealed, secret_path(repo))
    print(f"   wrote {repo.rel(secret_path(repo))}")

    if app_path(repo).exists():
        return ["re-sealed the OAuth client secret", COMMIT_NOTE]

    chart = charts.resolve(ctx.runner, CHART_KEY)
    settings.charts[CHART_KEY] = chart
    repo.write_settings(settings)
    write_files(repo, chart, settings.git.url, settings.name)
    _add_coredns_stub(repo, settings.tailscale.tailnet)
    return [ACL_NOTE, SCOPES_NOTE, COMMIT_NOTE]
