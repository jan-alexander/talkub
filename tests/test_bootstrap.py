import pytest

from helpers import need
from talkub import bootstrap, sealing
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import ChartSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")

KUBECONFIG = "apiVersion: v1\nkind: Config\nclusters: []\n"
KEY_LIST = (
    "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: Secret\n"
    "  metadata: {name: key1, namespace: kube-system}\n  data: {tls.key: a2V5}\n"
)
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


@pytest.fixture
def cluster(talos_repo, spy):
    """talos_repo prepared for bootstrap: charts, values, root app; no kubeconfig yet; spies."""
    settings = talos_repo.load_settings()
    settings.charts = {
        "sealed-secrets": ChartSpec("https://bitnami.github.io/sealed-secrets", "sealed-secrets", "2.20.0"),
        "argocd": ChartSpec("https://argoproj.github.io/argo-helm", "argo-cd", "10.9.2"),
    }
    talos_repo.write_settings(settings)
    for name in ("sealed-secrets.yaml", "argocd.yaml"):
        atomic_write(talos_repo.gitops_dir / "values" / name, b"{}\n")
    atomic_write(talos_repo.gitops_dir / "root.yaml", b"kind: Application\n")
    talos_repo.kubeconfig.unlink()
    spies = {
        "talosctl": spy("talosctl").real("gen").real("validate").on("kubeconfig", stdout=KUBECONFIG),
        "kubectl": spy("kubectl").on("get", "secret", stdout=KEY_LIST),
        "helm": spy("helm"),
        "kubeseal": spy("kubeseal").on("--fetch-cert", stdout=CERT),
    }
    return talos_repo, spies


def test_fresh_bootstrap(cluster):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    talos = spies["talosctl"].argvs
    assert [a[a.index("-e") + 2] for a in talos] == ["etcd", "bootstrap", "health", "kubeconfig"]
    assert "15m" in talos[2]
    ctx = Context.open(repo)
    assert ctx.sops.decrypt(repo.kubeconfig).decode() == KUBECONFIG
    helm = spies["helm"].argvs
    assert helm[0][:4] == ["upgrade", "--install", "sealed-secrets", "sealed-secrets"]
    assert "--create-namespace" not in helm[0]
    assert helm[1][:4] == ["upgrade", "--install", "argocd", "argo-cd"]
    assert "--create-namespace" in helm[1] and "argocd" in helm[1]
    assert all(call["kubeconfig"] and call["files"]["--values"] for call in spies["helm"].calls)
    kubectl = spies["kubectl"].argvs
    assert kubectl[0][:2] == ["get", "secret"]  # the new key is backed up right after sealed-secrets installs
    assert kubectl[1] == ["apply", "-f", str(repo.gitops_dir / "root.yaml")]
    assert sealing.key_backup_path(repo).exists()
    assert sealing.cert_path(repo).read_text() == CERT


def test_rerun_is_idempotent_and_restores_key_first(cluster):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    kubeconfig_bytes = repo.kubeconfig.read_bytes()
    spies["talosctl"].rules.insert(0, {"match": ["bootstrap"], "stdout": "", "rc": 1,
                                       "stderr": "error: rpc error: code = AlreadyExists desc = etcd data directory is not empty"})
    spies["talosctl"]._save()
    for fake in spies.values():
        fake.log.unlink(missing_ok=True)
    bootstrap.run(Context.open(repo))
    assert repo.kubeconfig.read_bytes() == kubeconfig_bytes  # unchanged content → not rewritten
    kubectl = spies["kubectl"].calls
    assert kubectl[0]["args"][:2] == ["apply", "-f"] and kubectl[0]["files"]["-f"]
    assert kubectl[0]["args"][2] != str(repo.gitops_dir / "root.yaml")  # the decrypted key comes first
    assert not any(a[:2] == ["get", "secret"] for a in spies["kubectl"].argvs)  # no new backup
    assert len(spies["helm"].argvs) == 2


ETCD_MEMBERS = (
    "NODE        ID                 HOSTNAME   PEER URLS               CLIENT URLS             LEARNER\n"
    "10.0.0.13   8e1f0c2d3b4a5968   cp-02      https://10.0.0.13:2380  https://10.0.0.13:2379  false\n"
)


def _add_controlplane(repo, name, ip):
    from talkub.nodes import node_patch_path, render_node_patch
    from talkub.repo import NodeSpec

    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip, role="controlplane")
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, name), render_node_patch(name).encode())


def test_etcd_members_are_checked_on_every_controlplane(cluster):
    repo, spies = cluster
    _add_controlplane(repo, "cp-02", "10.0.0.13")
    bootstrap.run(Context.open(repo))
    talos = spies["talosctl"].argvs
    members = [a[a.index("-n") + 1] for a in talos if a[a.index("-e") + 2:] == ["etcd", "members"]]
    assert members == ["10.0.0.10", "10.0.0.13"]
    [call] = [a for a in talos if "bootstrap" in a]
    assert call[call.index("-n") + 1] == "10.0.0.10"
    assert talos.index(call) > max(i for i, a in enumerate(talos) if "members" in a)


def test_existing_etcd_member_skips_bootstrap(cluster, capfd):
    repo, spies = cluster
    _add_controlplane(repo, "cp-02", "10.0.0.13")
    spies["talosctl"].rules.insert(
        0, {"match": ["-n", "10.0.0.13", "etcd", "members"], "stdout": ETCD_MEMBERS, "stderr": "", "rc": 0}
    )
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    assert not any("bootstrap" in a for a in spies["talosctl"].argvs)
    assert "etcd already has members on cp-02; skipping bootstrap" in capfd.readouterr().out
    assert Context.open(repo).sops.decrypt(repo.kubeconfig).decode() == KUBECONFIG  # the rest still runs


def test_failing_or_empty_etcd_answer_still_bootstraps(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(0, {"match": ["etcd", "members"], "stdout": ETCD_MEMBERS, "stderr": "", "rc": 1})
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    assert any("bootstrap" in a for a in spies["talosctl"].argvs)


def test_health_failure_stops_before_kubeconfig(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(0, {"match": ["health"], "stdout": "", "stderr": "", "rc": 1})
    spies["talosctl"]._save()
    with pytest.raises(CommandFailed) as exc:
        bootstrap.run(Context.open(repo))
    assert "healthy" in str(exc.value)
    assert not repo.kubeconfig.exists()
    assert spies["helm"].argvs == []


def test_bootstrap_error_is_reported(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(0, {"match": ["bootstrap"], "stdout": "", "stderr": "connection refused", "rc": 1})
    spies["talosctl"]._save()
    with pytest.raises(CommandFailed) as exc:
        bootstrap.run(Context.open(repo))
    assert "connection refused" in exc.value.hint


def test_missing_chart_settings(cluster):
    repo, _ = cluster
    settings = repo.load_settings()
    del settings.charts["argocd"]
    repo.write_settings(settings)
    with pytest.raises(ValidationError) as exc:
        bootstrap.run(Context.open(repo))
    assert "argocd" in str(exc.value)
