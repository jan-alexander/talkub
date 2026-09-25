import base64

import pytest

from helpers import need
from talkub import coredns, tailscale_operator, yamlio
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
        "operatorConfig": {"hostname": "mycluster-operator", "defaultTags": ["tag:k8s-operator"]},
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
    notes = tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert notes == [tailscale_operator.ACL_NOTE, tailscale_operator.SCOPES_NOTE, tailscale_operator.COMMIT_NOTE]

    values_doc = yamlio.load(tailscale_operator.values_path(repo).read_text())
    assert values_doc == {
        "installCRDs": True,
        "operatorConfig": {"hostname": "test-operator", "defaultTags": ["tag:k8s-operator"]},
        "proxyConfig": {"defaultTags": "tag:k8s"},
        "apiServerProxyConfig": {"mode": "false"},
    }

    app_doc = yamlio.load(tailscale_operator.app_path(repo).read_text())
    assert app_doc["spec"]["destination"]["namespace"] == "tailscale"
    assert app_doc["spec"]["syncPolicy"]["syncOptions"] == ["CreateNamespace=true", "ServerSideApply=true"]
    assert app_doc["spec"]["sources"][0]["repoURL"] == "https://pkgs.tailscale.com/helmcharts"
    assert app_doc["spec"]["sources"][0]["chart"] == "tailscale-operator"
    assert app_doc["spec"]["sources"][0]["targetRevision"] == "1.2.3"
    assert app_doc["spec"]["sources"][1]["repoURL"] == GIT_URL

    config_doc = yamlio.load(tailscale_operator.config_app_path(repo).read_text())
    assert config_doc["metadata"]["name"] == "tailscale-operator-config"
    assert config_doc["spec"]["source"]["path"] == "gitops/manifests/tailscale"
    assert config_doc["spec"]["source"]["repoURL"] == GIT_URL
    assert config_doc["spec"]["destination"]["namespace"] == "tailscale"
    assert config_doc["spec"]["syncPolicy"]["syncOptions"] == [
        "CreateNamespace=true", "ServerSideApply=true", "SkipDryRunOnMissingResource=true",
        "RespectIgnoreDifferences=true",
    ]
    # the operator rewrites spec.externalName of egress Services (tailscale/tailscale#15098)
    assert config_doc["spec"]["ignoreDifferences"] == [
        {"group": "", "kind": "Service", "jsonPointers": ["/spec/externalName"]}
    ]
    assert app_doc["metadata"]["annotations"] == {"argocd.argoproj.io/sync-wave": "0"}
    assert config_doc["metadata"]["annotations"] == {"argocd.argoproj.io/sync-wave": "1"}

    dns_doc = yamlio.load(tailscale_operator.dnsconfig_path(repo).read_text())
    assert dns_doc == {
        "apiVersion": "tailscale.com/v1alpha1",
        "kind": "DNSConfig",
        "metadata": {"name": "ts-dns"},
        "spec": {"nameserver": {"service": {"clusterIP": "10.96.0.53"}}},
    }

    assert b"kind: SealedSecret" in tailscale_operator.secret_path(repo).read_bytes()
    settings = repo.load_settings()
    assert settings.charts["tailscale-operator"].version == "1.2.3"
    assert settings.charts["tailscale-operator"].repo == "https://pkgs.tailscale.com/helmcharts"


def test_coredns_stub_added_exactly_once(repo, kubeseal_spy):
    ctx = Context.open(repo)
    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    doc = yamlio.load(coredns.values_path(repo).read_text())
    assert len(doc["servers"]) == 2
    assert doc["servers"][1]["zones"] == [{"zone": TAILNET, "use_tcp": True}]

    tailscale_operator.enable(ctx, client_id=CLIENT_ID, client_secret="tskey-client-rotated", rotate_credentials=True)
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
        ctx, client_id=CLIENT_ID, client_secret="tskey-client-rotated", rotate_credentials=True
    )
    assert notes == ["re-sealed the OAuth client secret", tailscale_operator.COMMIT_NOTE]
    assert tailscale_operator.values_path(repo).read_text() == values_before
    assert tailscale_operator.app_path(repo).read_text() == app_before
    assert tailscale_operator.dnsconfig_path(repo).read_text() == dns_before
    assert len([c for c in kubeseal_spy.calls if "--cert" in c["args"]]) == 2


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


def test_cli_operator_enable_with_secret_file_outside_repo(repo, tmp_path, kubeseal_spy, capfd):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(CLIENT_SECRET + "\n")
    code = _run(
        [
            "--repo", str(repo.root), "tailscale", "operator", "enable",
            "--client-id", CLIENT_ID, "--client-secret-file", str(secret_file),
        ]
    )
    assert code == 0
    err = capfd.readouterr().err
    assert "must not be committed" not in err
    assert tailscale_operator.app_path(repo).exists()
    assert CLIENT_SECRET.encode() not in tailscale_operator.secret_path(repo).read_bytes()


def test_cli_operator_enable_prompts_for_client_id(repo, tmp_path, kubeseal_spy, monkeypatch):
    import typer

    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(CLIENT_SECRET)
    asked = []
    monkeypatch.setattr(typer, "prompt", lambda text: asked.append(text) or CLIENT_ID)
    code = _run(
        ["--repo", str(repo.root), "tailscale", "operator", "enable", "--client-secret-file", str(secret_file)]
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
    assert path == tailscale_operator.manifests_dir(operator_repo) / "egress" / "db.yaml"
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
    path = tailscale_operator.egress_add(ctx, "db", fqdn="db." + TAILNET, namespace="app")
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
            "--repo", str(operator_repo.root), "tailscale", "egress", "add", "db",
            "--fqdn", "db." + TAILNET,
        ]
    )
    assert code == 0
    path = tailscale_operator.manifests_dir(operator_repo) / "egress" / "db.yaml"
    assert path.exists()
    out = capfd.readouterr().out
    assert "commit and push gitops/" in out

    code = _run(["--repo", str(operator_repo.root), "tailscale", "egress", "remove", "db"])
    assert code == 0
    assert not path.exists()
    out = capfd.readouterr().out
    assert "commit and push gitops/" in out
