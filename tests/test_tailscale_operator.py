import base64

import pytest
from helpers import need

from talkub import coredns, tailscale_operator, yamlio
from talkub.checks import run_checks
from talkub.cli.main import main
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import GitSpec, TailscaleSpec

pytestmark = need("age", "age-keygen", "sops")

TAILNET = "tail1234.ts.net"
GIT_URL = "git@github.com:me/cluster.git"
HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"
CLIENT_ID = "k8s-abc123CNTRL"
CLIENT_SECRET = "tskey-client-super-secret-value"  # gitleaks:allow (test fixture, never sealed for real)
CERT = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
SEALED_STDOUT = (
    "apiVersion: bitnami.com/v1alpha1\nkind: SealedSecret\n"
    "metadata:\n  name: operator-oauth\n  namespace: tailscale\nspec:\n  encryptedData: {}\n"
)


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def _enable_prereqs(repo):
    settings = repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet=TAILNET, node_cidr="10.0.0.0/24")
    settings.git = GitSpec(GIT_URL)
    repo.write_settings(settings)
    atomic_write(coredns.app_path(repo), b"placeholder\n")
    atomic_write(coredns.values_path(repo), coredns.values().encode())
    return repo


@pytest.fixture
def repo(repo_with_configs):
    return _enable_prereqs(repo_with_configs)


@pytest.fixture
def operator_repo(repo):
    """`repo` plus a placeholder tailscale-operator app (as if `operator enable` already ran)."""
    atomic_write(tailscale_operator.app_path(repo), b"placeholder\n")
    return repo


@pytest.fixture
def kubeseal_spy(spy, fake_bin):
    """Fakes kubectl (only presence is required), kubeseal (--fetch-cert, --cert) and helm (chart resolve)."""
    spy("kubectl")
    fake_bin("helm", HELM)
    kubeseal = spy("kubeseal")
    kubeseal.on("--fetch-cert", stdout=CERT)
    kubeseal.on("--cert", stdout=SEALED_STDOUT, read_stdin=True)
    return kubeseal


# --- renderers -----------------------------------------------------------------------------


def test_values_renderer():
    doc = yamlio.load(tailscale_operator.values("mycluster"))
    assert doc == {
        "installCRDs": True,
        "operatorConfig": {
            "hostname": "mycluster-operator",
            "defaultTags": ["tag:k8s-operator"],
        },
        "proxyConfig": {"defaultTags": "tag:k8s"},
        "apiServerProxyConfig": {"mode": "false"},
    }


def test_dnsconfig_renderer():
    doc = yamlio.load(tailscale_operator.dnsconfig())
    assert doc == {
        "apiVersion": "tailscale.com/v1alpha1",
        "kind": "DNSConfig",
        "metadata": {"name": "ts-dns"},
        "spec": {"nameserver": {"service": {"clusterIP": "10.96.0.53"}}},
    }
    assert coredns.TS_NAMESERVER_IP == "10.96.0.53"


def test_namespace_renderer():
    doc = yamlio.load(tailscale_operator.namespace())
    assert doc == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "tailscale",
            "labels": {"pod-security.kubernetes.io/enforce": "privileged"},
        },
    }


# --- preconditions ---------------------------------------------------------------------------


def test_check_can_enable_requires_tailscale(repo_with_configs):
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.check_can_enable(repo_with_configs)
    assert "tailscale is not enabled" in str(exc.value)


def test_check_can_enable_requires_coredns(repo_with_configs):
    settings = repo_with_configs.load_settings()
    settings.tailscale = TailscaleSpec(tailnet=TAILNET, node_cidr="10.0.0.0/24")
    repo_with_configs.write_settings(settings)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.check_can_enable(repo_with_configs)
    assert "CoreDNS" in str(exc.value)
    assert "coredns takeover" in exc.value.hint


def test_check_can_enable_requires_git_url(repo_with_configs):
    settings = repo_with_configs.load_settings()
    settings.tailscale = TailscaleSpec(tailnet=TAILNET, node_cidr="10.0.0.0/24")
    repo_with_configs.write_settings(settings)
    atomic_write(coredns.app_path(repo_with_configs), b"placeholder\n")
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.check_can_enable(repo_with_configs)
    assert "git URL" in str(exc.value)


# --- enable: files, notes, chart pinning ------------------------------------------------------


def test_enable_writes_repo_files_and_notes(repo, kubeseal_spy):
    ctx = Context.open(repo)
    notes = tailscale_operator.enable(
        ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )
    assert notes == [
        tailscale_operator.ACL_NOTE,
        tailscale_operator.SCOPES_NOTE,
        tailscale_operator.COMMIT_NOTE,
    ]

    values_doc = yamlio.load(tailscale_operator.values_path(repo).read_text())
    assert values_doc == {
        "installCRDs": True,
        "operatorConfig": {
            "hostname": "test-operator",
            "defaultTags": ["tag:k8s-operator"],
        },
        "proxyConfig": {"defaultTags": "tag:k8s"},
        "apiServerProxyConfig": {"mode": "false"},
    }

    app_doc = yamlio.load(tailscale_operator.app_path(repo).read_text())
    assert app_doc["spec"]["destination"]["namespace"] == "tailscale"
    assert app_doc["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true",
        "ServerSideApply=true",
    ]
    assert (
        app_doc["spec"]["sources"][0]["repoURL"]
        == "https://pkgs.tailscale.com/helmcharts"
    )
    assert app_doc["spec"]["sources"][0]["chart"] == "tailscale-operator"
    assert app_doc["spec"]["sources"][0]["targetRevision"] == "1.2.3"
    assert app_doc["spec"]["sources"][1]["repoURL"] == GIT_URL

    config_doc = yamlio.load(tailscale_operator.config_app_path(repo).read_text())
    assert config_doc["metadata"]["name"] == "tailscale-operator-config"
    assert config_doc["spec"]["source"]["path"] == "gitops/manifests/tailscale"
    assert config_doc["spec"]["source"]["repoURL"] == GIT_URL
    assert config_doc["spec"]["destination"]["namespace"] == "tailscale"
    assert config_doc["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true",
        "ServerSideApply=true",
        "SkipDryRunOnMissingResource=true",
        "RespectIgnoreDifferences=true",
    ]
    # the operator rewrites spec.externalName of egress Services (tailscale/tailscale#15098)
    assert config_doc["spec"]["ignoreDifferences"] == [
        {"group": "", "kind": "Service", "jsonPointers": ["/spec/externalName"]}
    ]
    assert app_doc["metadata"]["annotations"] == {"argocd.argoproj.io/sync-wave": "0"}
    assert config_doc["metadata"]["annotations"] == {
        "argocd.argoproj.io/sync-wave": "1"
    }

    dns_doc = yamlio.load(tailscale_operator.dnsconfig_path(repo).read_text())
    assert dns_doc == {
        "apiVersion": "tailscale.com/v1alpha1",
        "kind": "DNSConfig",
        "metadata": {"name": "ts-dns"},
        "spec": {"nameserver": {"service": {"clusterIP": "10.96.0.53"}}},
    }

    assert b"kind: SealedSecret" in tailscale_operator.secret_path(repo).read_bytes()
    assert "charts" not in repo.settings_file.read_text()


def test_enable_writes_namespace_file(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    doc = yamlio.load(tailscale_operator.namespace_path(repo).read_text())
    assert doc["metadata"]["name"] == "tailscale"
    assert doc["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "privileged"
    }


def test_check_stays_clean_after_enable(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert [f.message for f in run_checks(repo) if f.level == "error"] == []


def test_coredns_stub_added_exactly_once(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    doc = yamlio.load(coredns.values_path(repo).read_text())
    assert len(doc["servers"]) == 2
    assert doc["servers"][1]["zones"] == [{"zone": TAILNET, "use_tcp": True}]

    tailscale_operator.enable(
        ctx,
        client_id=CLIENT_ID,
        client_secret="tskey-client-rotated",
        rotate_credentials=True,
    )
    doc2 = yamlio.load(coredns.values_path(repo).read_text())
    assert len(doc2["servers"]) == 2  # rotate does not touch CoreDNS


# --- already enabled / rotate ------------------------------------------------------------------


def test_enable_twice_without_rotate_errors(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert "already enabled" in str(exc.value)
    assert "--rotate-credentials" in exc.value.hint


def test_rotate_credentials_only_reseals(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    values_before = tailscale_operator.values_path(repo).read_text()
    app_before = tailscale_operator.app_path(repo).read_text()
    dns_before = tailscale_operator.dnsconfig_path(repo).read_text()

    notes = tailscale_operator.enable(
        ctx,
        client_id=CLIENT_ID,
        client_secret="tskey-client-rotated",
        rotate_credentials=True,
    )
    assert notes == [
        "re-sealed the OAuth client secret",
        tailscale_operator.COMMIT_NOTE,
    ]
    assert tailscale_operator.values_path(repo).read_text() == values_before
    assert tailscale_operator.app_path(repo).read_text() == app_before
    assert tailscale_operator.dnsconfig_path(repo).read_text() == dns_before
    assert len([c for c in kubeseal_spy.calls if "--cert" in c["args"]]) == 2


def test_rotate_credentials_adds_missing_namespace_file(repo, kubeseal_spy):
    """Repos where the operator was enabled before this fix are missing the namespace manifest."""
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    tailscale_operator.namespace_path(repo).unlink()
    values_before = tailscale_operator.values_path(repo).read_text()
    app_before = tailscale_operator.app_path(repo).read_text()
    dns_before = tailscale_operator.dnsconfig_path(repo).read_text()

    tailscale_operator.enable(
        ctx,
        client_id=CLIENT_ID,
        client_secret="tskey-client-rotated",
        rotate_credentials=True,
    )

    doc = yamlio.load(tailscale_operator.namespace_path(repo).read_text())
    assert doc["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "privileged"
    }
    assert tailscale_operator.values_path(repo).read_text() == values_before
    assert tailscale_operator.app_path(repo).read_text() == app_before
    assert tailscale_operator.dnsconfig_path(repo).read_text() == dns_before


# --- the secret only ever touches kubeseal's stdin ----------------------------------------------


def test_secret_appears_only_in_kubeseal_stdin(repo, kubeseal_spy, capfd):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    out, err = capfd.readouterr()

    encoded = base64.b64encode(CLIENT_SECRET.encode()).decode()
    seal_calls = [c for c in kubeseal_spy.calls if "--cert" in c["args"]]
    assert len(seal_calls) == 1
    assert encoded in seal_calls[0]["stdin"]

    for call in kubeseal_spy.calls:
        joined = " ".join(call["args"])
        assert CLIENT_SECRET not in joined
        if call is not seal_calls[0]:
            assert encoded not in joined

    assert CLIENT_SECRET not in out and encoded not in out
    assert CLIENT_SECRET not in err and encoded not in err

    for path in repo.root.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert CLIENT_SECRET.encode() not in content
            assert encoded.encode() not in content


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_operator_enable_with_secret_file_outside_repo(
    repo, tmp_path, kubeseal_spy, capfd
):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(CLIENT_SECRET + "\n")
    code = _run(
        [
            "--repo",
            str(repo.root),
            "tailscale",
            "operator",
            "enable",
            "--client-id",
            CLIENT_ID,
            "--client-secret-file",
            str(secret_file),
        ]
    )
    assert code == 0
    err = capfd.readouterr().err
    assert "must not be committed" not in err
    assert tailscale_operator.app_path(repo).exists()
    assert (
        CLIENT_SECRET.encode() not in tailscale_operator.secret_path(repo).read_bytes()
    )


def test_cli_operator_enable_prompts_for_client_id(
    repo, tmp_path, kubeseal_spy, monkeypatch
):
    import typer

    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(CLIENT_SECRET)
    asked = []
    monkeypatch.setattr(typer, "prompt", lambda text: asked.append(text) or CLIENT_ID)
    code = _run(
        [
            "--repo",
            str(repo.root),
            "tailscale",
            "operator",
            "enable",
            "--client-secret-file",
            str(secret_file),
        ]
    )
    assert code == 0
    assert asked == ["OAuth client ID"]


# --- egress add/remove ---------------------------------------------------------------------------


def test_egress_add_requires_operator_enabled(repo):
    ctx = Context.open(repo)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET)
    assert "operator" in str(exc.value)
    assert "operator enable" in exc.value.hint


@pytest.mark.parametrize("name", ["DB", "-db", "db-", "has_underscore", "a b", ""])
def test_egress_add_rejects_invalid_name(operator_repo, name):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, name, fqdn="db." + TAILNET)


def test_egress_add_rejects_name_over_63_chars(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, "a" * 64, fqdn="db." + TAILNET)


def test_egress_add_requires_exactly_one_of_fqdn_or_ip(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, "db")
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET, ip="100.64.0.1")


def test_egress_add_rejects_fqdn_outside_tailnet(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.egress_add(ctx, "db", fqdn="db.example.com")
    assert TAILNET in str(exc.value)


def test_egress_add_accepts_fqdn_case_insensitively(operator_repo):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.egress_add(ctx, "db", fqdn="DB." + TAILNET.upper())
    assert path.exists()


@pytest.mark.parametrize("ip", ["8.8.8.8", "10.0.0.1", "not-an-ip", "::1"])
def test_egress_add_rejects_non_tailnet_ip(operator_repo, ip):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, "db", ip=ip)


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1"])
def test_egress_add_accepts_tailnet_ip(operator_repo, ip):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.egress_add(ctx, "db", ip=ip)
    doc = yamlio.load(path.read_text())
    assert doc["metadata"]["annotations"] == {"tailscale.com/tailnet-ip": ip}


def test_egress_add_writes_fqdn_service(operator_repo):
    ctx = Context.open(operator_repo)
    fqdn = "db." + TAILNET
    path = tailscale_operator.egress_add(ctx, "db", fqdn=fqdn)
    assert (
        path == tailscale_operator.manifests_dir(operator_repo) / "egress" / "db.yaml"
    )
    doc = yamlio.load(path.read_text())
    assert doc == {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "db",
            "namespace": "tailscale",
            "annotations": {"tailscale.com/tailnet-fqdn": fqdn},
        },
        "spec": {"type": "ExternalName", "externalName": "placeholder"},
    }


def test_egress_add_custom_namespace(operator_repo, capfd):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.egress_add(
        ctx, "db", fqdn="db." + TAILNET, namespace="app"
    )
    doc = yamlio.load(path.read_text())
    assert doc["metadata"]["namespace"] == "app"
    out = capfd.readouterr().out
    assert "namespace" in out and "exist" in out


def test_egress_add_existing_file_errors(operator_repo):
    ctx = Context.open(operator_repo)
    tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET)


def test_egress_remove_missing_errors(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.egress_remove(ctx, "db")


def test_egress_remove_deletes_file(operator_repo):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET)
    assert path.exists()
    result = tailscale_operator.egress_remove(ctx, "db")
    assert result == path
    assert not path.exists()


def test_cli_egress_add_and_remove(operator_repo, capfd):
    code = _run(
        [
            "--repo",
            str(operator_repo.root),
            "tailscale",
            "egress",
            "add",
            "db",
            "--fqdn",
            "db." + TAILNET,
        ]
    )
    assert code == 0
    path = tailscale_operator.manifests_dir(operator_repo) / "egress" / "db.yaml"
    assert path.exists()
    out = capfd.readouterr().out
    assert "commit and push gitops/" in out

    code = _run(
        ["--repo", str(operator_repo.root), "tailscale", "egress", "remove", "db"]
    )
    assert code == 0
    assert not path.exists()
    out = capfd.readouterr().out
    assert "commit and push gitops/" in out


# --- expose/unexpose ------------------------------------------------------------------------------


def test_ingress_renders_default_backend_with_numeric_port():
    text = tailscale_operator.ingress("web", "app", "web", 80)
    assert text.startswith("# Managed by talkub (talkub tailscale expose).\n")
    doc = yamlio.load(text)
    assert doc == {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "tailscale-web", "namespace": "app"},
        "spec": {
            "ingressClassName": "tailscale",
            "defaultBackend": {"service": {"name": "web", "port": {"number": 80}}},
            "tls": [{"hosts": ["web"]}],
        },
    }


def test_ingress_renders_default_backend_with_named_port():
    doc = yamlio.load(tailscale_operator.ingress("web", "app", "web", "http"))
    assert doc["spec"]["defaultBackend"]["service"]["port"] == {"name": "http"}


def test_ingress_renders_path_rule_instead_of_default_backend():
    doc = yamlio.load(tailscale_operator.ingress("web", "app", "web", 80, path="/api"))
    assert "defaultBackend" not in doc["spec"]
    assert doc["spec"]["rules"] == [
        {
            "http": {
                "paths": [
                    {
                        "path": "/api",
                        "pathType": "Prefix",
                        "backend": {"service": {"name": "web", "port": {"number": 80}}},
                    }
                ]
            }
        }
    ]
    assert doc["spec"]["tls"] == [{"hosts": ["web"]}]


def test_expose_requires_operator_enabled(repo):
    ctx = Context.open(repo)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    assert "operator" in str(exc.value)
    assert "operator enable" in exc.value.hint


@pytest.mark.parametrize(
    "name", ["WEB", "-web", "web-", "has_underscore", "a b", "", "a" * 64]
)
def test_expose_rejects_invalid_name(operator_repo, name):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.expose(ctx, name, service="app/web", port="80")


@pytest.mark.parametrize(
    "service", ["web", "app/web/extra", "APP/web", "app/WEB", "app/", "/web"]
)
def test_expose_rejects_invalid_service(operator_repo, service):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.expose(ctx, "web", service=service, port="80")


@pytest.mark.parametrize("port", ["0", "65536", "-1", "HTTP", "has_underscore", ""])
def test_expose_rejects_invalid_port(operator_repo, port):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.expose(ctx, "web", service="app/web", port=port)


def test_expose_accepts_named_port(operator_repo):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.expose(ctx, "web", service="app/web", port="http")
    doc = yamlio.load(path.read_text())
    assert doc["spec"]["defaultBackend"]["service"]["port"] == {"name": "http"}


def test_expose_rejects_path_without_leading_slash(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.expose(ctx, "web", service="app/web", port="80", path="api")


def test_expose_existing_file_errors_with_unexpose_hint(operator_repo):
    ctx = Context.open(operator_repo)
    tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    assert "unexpose web" in exc.value.hint


def test_expose_writes_ingress_and_prints_url(operator_repo, capfd):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    assert (
        path == tailscale_operator.manifests_dir(operator_repo) / "ingress" / "web.yaml"
    )
    doc = yamlio.load(path.read_text())
    assert doc["metadata"] == {"name": "tailscale-web", "namespace": "app"}
    out = capfd.readouterr().out
    assert f"wrote {operator_repo.rel(path)}" in out
    assert f"URL: https://web.{TAILNET}" in out


def test_expose_with_path_writes_rules_variant(operator_repo):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.expose(
        ctx, "web", service="app/web", port="80", path="/api"
    )
    doc = yamlio.load(path.read_text())
    assert "defaultBackend" not in doc["spec"]
    assert doc["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/api"


def test_unexpose_missing_errors(operator_repo):
    ctx = Context.open(operator_repo)
    with pytest.raises(ValidationError):
        tailscale_operator.unexpose(ctx, "web")


def test_unexpose_deletes_file(operator_repo):
    ctx = Context.open(operator_repo)
    path = tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    assert path.exists()
    result = tailscale_operator.unexpose(ctx, "web")
    assert result == path
    assert not path.exists()


def test_check_stays_clean_after_expose(operator_repo):
    ctx = Context.open(operator_repo)
    tailscale_operator.expose(ctx, "web", service="app/web", port="80")
    assert [f.message for f in run_checks(operator_repo) if f.level == "error"] == []


def test_cli_expose_and_unexpose(operator_repo, capfd):
    code = _run(
        [
            "--repo",
            str(operator_repo.root),
            "tailscale",
            "expose",
            "web",
            "--service",
            "app/web",
            "--port",
            "80",
        ]
    )
    assert code == 0
    path = tailscale_operator.manifests_dir(operator_repo) / "ingress" / "web.yaml"
    assert path.exists()
    out = capfd.readouterr().out
    assert f"URL: https://web.{TAILNET}" in out
    assert "TLS ends at the Tailscale proxy" in out or "speak HTTP" in out
    assert "certificates" in out
    assert "commit and push gitops/" in out

    code = _run(["--repo", str(operator_repo.root), "tailscale", "unexpose", "web"])
    assert code == 0
    assert not path.exists()
    out = capfd.readouterr().out
    assert "commit and push gitops/" in out


@pytest.mark.parametrize(
    "remove", [tailscale_operator.unexpose, tailscale_operator.egress_remove]
)
@pytest.mark.parametrize("name", ["../victim", "..", "a/b", "WEB"])
def test_remove_rejects_invalid_names_without_touching_files(
    operator_repo, remove, name
):
    victim = tailscale_operator.manifests_dir(operator_repo) / "victim.yaml"
    atomic_write(victim, b"keep me\n")
    with pytest.raises(ValidationError) as exc:
        remove(Context.open(operator_repo), name)
    assert "invalid" in str(exc.value)
    assert victim.read_bytes() == b"keep me\n"


def _disable_tailscale(repo):
    settings = repo.load_settings()
    settings.tailscale = None
    repo.write_settings(settings)


def test_expose_without_tailscale_enabled(operator_repo):
    _disable_tailscale(operator_repo)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.expose(
            Context.open(operator_repo), "web", service="app/web", port="80"
        )
    assert str(exc.value) == "tailscale is not enabled"
    assert exc.value.hint == "talkub tailscale enable"


def test_egress_add_fqdn_without_tailscale_enabled(operator_repo):
    _disable_tailscale(operator_repo)
    with pytest.raises(ValidationError) as exc:
        tailscale_operator.egress_add(
            Context.open(operator_repo), "db", fqdn="db.tail1234.ts.net"
        )
    assert str(exc.value) == "tailscale is not enabled"


def test_operator_help_mentions_expose(capfd, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    assert _run(["tailscale", "operator", "--help"]) == 0
    out = capfd.readouterr().out
    assert "talkub tailscale expose" in out
    assert "your own manifests" not in out
