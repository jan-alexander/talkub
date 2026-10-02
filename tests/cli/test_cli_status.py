import io

import pytest
from helpers import need
from rich.console import Console

from talkub.cli import status as status_cli
from talkub.cli.main import main
from talkub.config import Settings
from talkub.overview.status import KubeStatus, NodeStatus
from talkub.talos.inspect import ApplicationDetail

pytestmark = need("age", "age-keygen", "sops", "talosctl")

VERSION = "Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: v1.14.0\n"
NODES = '{"items": [{"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}}]}'
APPS = (
    '{"items": [{"metadata": {"name": "harbor"}, "status": {"sync": {"status": "OutOfSync", "revision": "1.18.0"}, '
    '"health": {"status": "Progressing"}, "operationState": {"phase": "Running"}}}]}'
)


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def _spies(spy):
    (
        spy("talosctl")
        .real("gen")
        .real("validate")
        .on("version", stdout=VERSION)
        .on("get", "extensions", stdout="{}")
        .on("--dry-run", stdout="Dry run summary:\nNo changes.\n")
    )
    spy("kubectl").on("get", "nodes", stdout=NODES).on("get", "applications.argoproj.io", stdout=APPS)


def test_status_without_flag_keeps_the_summary_only(talos_repo, spy, capfd):
    _spies(spy)
    assert _run(["--repo", str(talos_repo.root), "status"]) == 0
    out = capfd.readouterr().out
    assert "argocd: 1 OutOfSync/Progressing" in out
    assert "APP" not in out


def test_status_apps_prints_the_application_table(talos_repo, spy, capfd):
    _spies(spy)
    assert _run(["--repo", str(talos_repo.root), "status", "--apps"]) == 0
    out = capfd.readouterr().out
    assert "APP" in out and "SYNC" in out and "HEALTH" in out
    assert "harbor" in out and "OutOfSync" in out and "Progressing" in out and "Running" in out


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=True, width=120, color_system=None), buffer


def _render(console: Console, live: status_cli._LiveStatus) -> str:
    with console.capture() as capture:
        console.print(live._render())
    return capture.get()


def test_live_table_lists_nodes_up_front_and_fills_them_in(talos_repo):
    settings: Settings = talos_repo.load_settings()
    console, _ = _console()
    live = status_cli._LiveStatus(console, settings, ["cp-01"], apps=False)
    before = _render(console, live)
    assert "cp-01" in before and "…" in before
    live.on_step("cp-01", "talos version")
    assert "talos version" in _render(console, live)
    live.on_node(NodeStatus("cp-01", "10.0.0.1", reachable=True, talos="v1.14.0", config="in sync"))
    during = _render(console, live)
    assert "v1.14.0" in during and "in sync" in during
    assert "talos version" not in during
    assert "argocd" in during and "…" in during  # the kubelet and the Argo CD line still pending
    live.on_kube(KubeStatus("ok", kubelets={"cp-01": "1.37.0"}, applications={"Synced/Healthy": 2}))
    after = _render(console, live)
    assert "1.37.0" in after and "2 Synced/Healthy" in after and "…" not in after


def test_live_table_with_apps_shows_the_application_rows(talos_repo):
    settings: Settings = talos_repo.load_settings()
    console, _ = _console()
    live = status_cli._LiveStatus(console, settings, ["cp-01"], apps=True)
    live.on_kube(
        KubeStatus(
            "ok",
            applications={"OutOfSync/Progressing": 1},
            application_details=[
                ApplicationDetail(
                    "harbor", "OutOfSync", "Progressing", "Running", "1.18.0", [], ["Deployment/x: Progressing"]
                )
            ],
        )
    )
    text = _render(console, live)
    assert "APP" in text and "harbor" in text and "Deployment/x: Progressing" in text
