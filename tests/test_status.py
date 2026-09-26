from contextlib import contextmanager

from helpers import DEFAULT_SCHEMATIC_ID, need

from talkub import status
from talkub.cli.main import main
from talkub.context import Context
from talkub.errors import TalkubError
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")

VERSION = "Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: v1.14.0\n"
EXT = f'{{"spec": {{"metadata": {{"name": "schematic", "version": "{DEFAULT_SCHEMATIC_ID}"}}}}}}'
NODES = '{"items": [{"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}}]}'
APPS = '{"items": [{"status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]}'


def _spies(spy, drift="Dry run summary:\nNo changes.\n"):
    talosctl = (
        spy("talosctl")
        .real("gen")
        .real("validate")
        .on("-n", "10.0.0.99", rc=1, stderr="connection refused")
        .on("version", stdout=VERSION)
        .on("get", "extensions", stdout=EXT)
        .on("--dry-run", stdout=drift)
    )
    kubectl = (
        spy("kubectl")
        .on("get", "nodes", stdout=NODES)
        .on("get", "applications.argoproj.io", stdout=APPS)
    )
    return talosctl, kubectl


def test_healthy_cluster(talos_repo, spy):
    _spies(spy)
    report = status.collect(Context.open(talos_repo))
    [node] = report.nodes
    assert (node.reachable, node.talos, node.schematic, node.config, node.kubelet) == (
        True,
        "v1.14.0",
        DEFAULT_SCHEMATIC_ID,
        "in sync",
        "1.37.0",
    )
    assert report.applications == {"Synced/Healthy": 1}
    text = status.render(report)
    assert "cp-01" in text and "in sync" in text and "1 Synced/Healthy" in text
    assert "want" not in text


def test_drift_and_version_mismatch_are_marked(talos_repo, spy):
    _spies(spy, drift="Config diff:\n+x\n")
    report = status.collect(Context.open(talos_repo))
    report.nodes[0].talos = "v1.13.0"
    text = status.render(report)
    assert "changes" in text
    assert "v1.13.0 (want v1.14.0)" in text


def test_unreachable_node_and_unparseable_output(talos_repo, spy):
    settings = talos_repo.load_settings()
    settings.nodes["w-99"] = NodeSpec(ip="10.0.0.99", role="worker")
    talos_repo.write_settings(settings)
    atomic_write(
        node_patch_path(talos_repo, "w-99"), render_node_patch("w-99").encode()
    )
    talosctl, _ = _spies(spy)
    # after the 10.0.0.99 rule (index 2), before the regular version rule
    talosctl.rules.insert(
        3, {"match": ["version"], "stdout": "garbage", "stderr": "", "rc": 0}
    )
    talosctl._save()
    report = status.collect(Context.open(talos_repo))
    by_name = {n.name: n for n in report.nodes}
    assert by_name["w-99"].reachable is False
    assert by_name["cp-01"].talos is None
    text = status.render(report)
    assert "unreachable" in text and "unknown" in text


def test_not_bootstrapped(talos_repo, spy):
    talos_repo.kubeconfig.unlink()
    _, kubectl = _spies(spy)
    report = status.collect(Context.open(talos_repo))
    assert report.kube == "not bootstrapped"
    assert kubectl.argvs == []
    assert "not bootstrapped" in status.render(report)


def test_outdated_schematic_does_not_touch_factory_or_write_settings(
    talos_repo, spy, monkeypatch, capfd
):
    """status is read-only and works offline even if a schematic ID is stale."""
    monkeypatch.setenv(
        "TALKUB_FACTORY_URL", "http://127.0.0.1:9"
    )  # nothing listens there
    settings = talos_repo.load_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    talos_repo.write_settings(settings)
    before = talos_repo.settings_file.read_bytes()
    _spies(spy)
    report = status.collect(Context.open(talos_repo))
    assert talos_repo.settings_file.read_bytes() == before
    [node] = report.nodes
    assert node.talos == "v1.14.0"
    assert node.config == "unknown"
    err = capfd.readouterr().err
    assert "cannot render machine configs" in err
    assert "config drift is unknown" in err


def test_current_schematic_makes_no_factory_request(talos_repo, spy, factory_stub):
    _spies(spy)
    status.collect(Context.open(talos_repo))
    assert factory_stub.requests == []


def test_status_command(talos_repo, spy, capfd):
    _spies(spy)
    try:
        main(["--repo", str(talos_repo.root), "status"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "cp-01" in capfd.readouterr().out


def test_kubectl_calls_have_a_request_timeout(talos_repo, spy):
    _, kubectl = _spies(spy)
    status.collect(Context.open(talos_repo))
    assert len(kubectl.argvs) == 2
    assert all("--request-timeout=10s" in argv for argv in kubectl.argvs)


def test_failing_kube_session_gives_unavailable(talos_repo, spy, monkeypatch):
    _spies(spy)

    @contextmanager
    def broken(ctx):
        raise TalkubError("kubectl is not installed")
        yield

    monkeypatch.setattr(status, "kube_session", broken)
    report = status.collect(Context.open(talos_repo))
    assert report.kube == "unavailable"
    assert report.nodes[0].talos == "v1.14.0"
    assert "kubernetes: unavailable" in status.render(report)


def test_undecryptable_kubeconfig_gives_unavailable(talos_repo, spy):
    _spies(spy)
    talos_repo.kubeconfig.write_bytes(b"not: sops\n")
    report = status.collect(Context.open(talos_repo))
    assert report.kube == "unavailable"
