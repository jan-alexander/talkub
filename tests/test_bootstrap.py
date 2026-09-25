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
    # server-side apply: no last-applied-configuration annotation with the private key
    assert kubectl[0]["args"][:3] == ["apply", "--server-side", "-f"] and kubectl[0]["files"]["-f"]
    assert kubectl[0]["args"][3] != str(repo.gitops_dir / "root.yaml")  # the decrypted key comes first
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


def test_hanging_etcd_members_times_out_and_bootstraps(cluster, monkeypatch, capfd):
    # Before bootstrap etcd is not running and `talosctl etcd members` never returns (seen on a real node).
    repo, spies = cluster
    monkeypatch.setattr(bootstrap, "ETCD_PROBE_TIMEOUT", 0.5)
    spies["talosctl"].rules.insert(0, {"match": ["etcd", "members"], "sleep": 30})
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    assert any("bootstrap" in a for a in spies["talosctl"].argvs)
    assert "etcd on cp-01 did not answer within 0.5s" in capfd.readouterr().out


def test_failing_or_empty_etcd_answer_still_bootstraps(cluster):
    repo, spies = cluster
    spies["talosctl"].rules.insert(0, {"match": ["etcd", "members"], "stdout": ETCD_MEMBERS, "stderr": "", "rc": 1})
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    assert any("bootstrap" in a for a in spies["talosctl"].argvs)


def _rerun(repo, spies):
    spies["talosctl"].rules.insert(0, {"match": ["bootstrap"], "stdout": "", "rc": 1,
                                       "stderr": "error: rpc error: code = AlreadyExists desc = etcd data directory is not empty"})
    spies["talosctl"]._save()
    for fake in spies.values():
        fake.log.unlink(missing_ok=True)
    bootstrap.run(Context.open(repo))


def test_rerun_warns_when_restored_key_is_stale(cluster, capfd):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    capfd.readouterr()
    other = CERT.replace("MIIB", "MIIC")
    spies["kubeseal"].rules.insert(0, {"match": ["--fetch-cert"], "stdout": other, "stderr": "", "rc": 0})
    spies["kubeseal"]._save()
    _rerun(repo, spies)
    err = capfd.readouterr().err
    assert "differs from bootstrap/sealed-secrets.pem" in err
    assert "talkub secret backup-sealing-key" in err
    assert any("--fetch-cert" in a for a in spies["kubeseal"].argvs)
    assert all(call["kubeconfig"] for call in spies["kubeseal"].calls)
    assert not any(a[:2] == ["get", "secret"] for a in spies["kubectl"].argvs)  # still no automatic backup
    assert sealing.cert_path(repo).read_text() == CERT


def test_rerun_with_matching_cert_does_not_warn(cluster, capfd):
    repo, spies = cluster
    bootstrap.run(Context.open(repo))
    capfd.readouterr()
    _rerun(repo, spies)
    assert "differs from" not in capfd.readouterr().err


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


def test_commit_reminder_right_after_key_backup(cluster, capfd):
    repo, _ = cluster
    bootstrap.run(Context.open(repo))
    out = capfd.readouterr().out
    reminder = out.index("commit bootstrap/ now")
    assert out.index("backing up the new sealed-secrets key") < reminder < out.index("installing argocd")
    assert out.rstrip().endswith("bootstrap complete — commit kubernetes/ and bootstrap/")


def test_helm_install_oci_chart(tmp_path):
    from pathlib import Path

    from helpers import FakeRunner
    from talkub.kube import KubeSession
    from talkub.repo import Repo

    repo = Repo(tmp_path)
    atomic_write(repo.gitops_dir / "values" / "argocd.yaml", b"{}\n")
    runner = FakeRunner()
    chart = ChartSpec("oci://ghcr.io/argoproj/argo-helm/", "argo-cd", "10.9.2")
    bootstrap.helm_install(KubeSession(runner, Path("kc")), repo, "argocd", "argocd", chart, "argocd", True)
    [call] = runner.calls
    assert call.argv[:5] == ["helm", "upgrade", "--install", "argocd", "oci://ghcr.io/argoproj/argo-helm/argo-cd"]
    assert "--repo" not in call.argv
    assert call.argv[call.argv.index("--version") + 1] == "10.9.2"
    assert call.env == {"KUBECONFIG": "kc"}


def test_bootstrap_applies_repo_credentials_before_root(cluster):
    from talkub import gitrepo

    repo, spies = cluster
    ctx = Context.open(repo)
    gitrepo.write_credentials(ctx, gitrepo.build_secret("test", "git@github.com:me/x.git", ssh_private_key="K\n"))
    bootstrap.run(ctx)
    applies = [c for c in spies["kubectl"].calls if c["args"][0] == "apply"]
    root = [i for i, c in enumerate(applies) if c["args"] == ["apply", "-f", str(repo.gitops_dir / "root.yaml")]]
    creds = [i for i, c in enumerate(applies) if c["args"][:2] == ["apply", "--server-side"]]
    assert root and creds and max(creds) < root[0]


def test_bootstrap_after_lockdown_points_kubeconfig_at_tailnet(cluster):
    from talkub import tailscale, yamlio
    from talkub.repo import NodeSpec, TailscaleSpec

    repo, spies = cluster
    settings = repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    repo.write_settings(settings)
    tailscale.firewall_patch_path(repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(repo).write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    spies["talosctl"].rules.insert(0, {"match": ["kubeconfig"], "rc": 0, "stderr": "",
                                       "stdout": "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n"})
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    stored = yamlio.load(Context.open(repo).sops.decrypt(repo.kubeconfig).decode())
    assert stored["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
