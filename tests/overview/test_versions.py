from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from helpers import need, write_chart_app

from talkub.cli.main import main
from talkub.context import Context
from talkub.core.errors import ValidationError
from talkub.overview import versions

pytestmark = need("age", "age-keygen", "sops")

VERSION_OUT = "Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: v1.14.0\n"
NODES_JSON = '{"items": [{"metadata": {"name": "cp-01"}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}}]}'
CHART_OLD = "apiVersion: v2\nname: argo-cd\nversion: 10.9.2\nappVersion: v3.5.3\n"
CHART_NEW = "apiVersion: v2\nname: argo-cd\nversion: 10.10.0\nappVersion: v3.6.0\n"


@pytest.fixture
def version_http_stub(monkeypatch):
    """Local stand-in for the GitHub releases API and dl.k8s.io/release/stable.txt."""
    state = SimpleNamespace(talos_tag="v1.15.0", k8s_stable="v1.38.0\n", fail=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if state.fail:
                self.send_response(500)
                self.end_headers()
                return
            if self.path.endswith("/releases/latest"):
                body = json.dumps({"tag_name": state.talos_tag}).encode()
                content_type = "application/json"
            else:
                body = state.k8s_stable.encode()
                content_type = "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("TALKUB_GITHUB_API", base)
    monkeypatch.setenv("TALKUB_K8S_RELEASE_URL", f"{base}/release/stable.txt")
    yield state
    server.shutdown()
    server.server_close()


def _spy_talos_kube(spy, talos_stdout=VERSION_OUT, talos_rc=0, kube_stdout=NODES_JSON, kube_rc=0):
    talosctl = spy("talosctl").on("version", stdout=talos_stdout, rc=talos_rc)
    kubectl = spy("kubectl").on("get", "nodes", stdout=kube_stdout, rc=kube_rc)
    return talosctl, kubectl


# --- talos / kubernetes rows ---


def test_talos_and_kubernetes_rows(repo_with_configs, spy, version_http_stub):
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    talos = next(r for r in rows if r.component == "talos")
    k8s = next(r for r in rows if r.component == "kubernetes")
    assert (talos.target, talos.running, talos.latest) == (
        "v1.14.0",
        "v1.14.0",
        "v1.15.0",
    )
    assert (k8s.target, k8s.running, k8s.latest) == ("1.37.0", "1.37.0", "1.38.0")
    assert k8s.note == "check the Talos support matrix before upgrading"


def test_talos_running_is_question_mark_on_talosctl_failure(repo_with_configs, spy, version_http_stub):
    _spy_talos_kube(spy, talos_rc=1)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    talos = next(r for r in rows if r.component == "talos")
    assert talos.running == "?"


def test_talos_running_is_question_mark_without_talosconfig(empty_repo, spy, version_http_stub):
    spy("talosctl")
    spy("kubectl")
    spy("helm")
    rows = versions.collect(Context.open(empty_repo))
    talos = next(r for r in rows if r.component == "talos")
    assert talos.running == "?"


def test_kubernetes_running_is_question_mark_without_kubeconfig(repo_with_configs, spy, version_http_stub):
    repo_with_configs.kubeconfig.unlink()
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    k8s = next(r for r in rows if r.component == "kubernetes")
    assert k8s.running == "?"


def test_multiple_distinct_talos_versions_are_comma_joined(repo_with_configs, spy, version_http_stub):
    from talkub.config import NodeSpec
    from talkub.core.fsutil import atomic_write
    from talkub.talos.nodes import node_patch_path, render_node_patch

    settings = repo_with_configs.load_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker")
    repo_with_configs.write_settings(settings)
    atomic_write(node_patch_path(repo_with_configs, "w-01"), render_node_patch("w-01").encode())

    (
        spy("talosctl")
        .on("-n", "10.0.0.11", "version", stdout="Server:\n\tNODE: y\n\tTag: v1.13.9\n")
        .on("version", stdout=VERSION_OUT)
    )
    spy("kubectl").on("get", "nodes", stdout=NODES_JSON)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    talos = next(r for r in rows if r.component == "talos")
    assert talos.running in ("v1.14.0, v1.13.9", "v1.13.9, v1.14.0")


def test_offline_sets_latest_dash_everywhere_and_makes_no_http_request(repo_with_configs, spy, monkeypatch):
    monkeypatch.setenv("TALKUB_GITHUB_API", "http://127.0.0.1:1")  # nothing listens there
    monkeypatch.setenv("TALKUB_K8S_RELEASE_URL", "http://127.0.0.1:1/x")
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs), offline=True)
    assert all(row.latest == "-" for row in rows)


def test_talos_latest_is_question_mark_on_http_failure(repo_with_configs, spy, version_http_stub):
    version_http_stub.fail = True
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    talos = next(r for r in rows if r.component == "talos")
    k8s = next(r for r in rows if r.component == "kubernetes")
    assert talos.latest == "?"
    assert k8s.latest == "?"
    assert k8s.note == ""


def test_no_note_when_kubernetes_target_is_already_latest(repo_with_configs, spy, version_http_stub):
    version_http_stub.k8s_stable = "v1.37.0\n"
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    k8s = next(r for r in rows if r.component == "kubernetes")
    assert k8s.note == ""


# --- chart app rows ---


def _write_argocd_app(repo):
    write_chart_app(
        repo,
        "argocd",
        "https://argoproj.github.io/argo-helm",
        "argo-cd",
        "10.9.2",
        "argocd",
    )


APP_SYNCED_JSON = '{"status": {"sync": {"revisions": ["10.9.2", "HEAD"]}}}'


def test_chart_row_running_latest_and_appversion_note(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    (
        spy("helm")
        .on("--version", "10.9.2", stdout=CHART_OLD)
        .on("--version", "10.10.0", stdout=CHART_NEW)
        .on("show", "chart", stdout=CHART_NEW)
    )
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert (argocd.target, argocd.running, argocd.latest) == (
        "10.9.2",
        "10.9.2",
        "10.10.0",
    )
    assert argocd.note == "appVersion v3.5.3 → v3.6.0"


def test_chart_row_no_note_when_already_at_latest(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    spy("helm").on("show", "chart", stdout=CHART_OLD)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.latest == "10.9.2"
    assert argocd.note == ""


def test_chart_row_running_is_question_mark_on_kubectl_failure(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout="", stderr="Unable to connect to the server", rc=1)
    spy("helm").on("show", "chart", stdout=CHART_NEW)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.running == "?"


def test_chart_row_running_is_not_deployed_when_app_missing(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on(
        "get",
        "application",
        "argocd",
        stderr='Error from server (NotFound): applications.argoproj.io "argocd" not found',
        rc=1,
    )
    spy("helm").on("show", "chart", stdout=CHART_NEW)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.running == "not deployed"


def test_chart_row_running_is_pending_before_the_first_sync(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout='{"status": {"sync": {"status": "Unknown"}}}')
    spy("helm").on("show", "chart", stdout=CHART_NEW)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.running == "pending"


def test_chart_row_latest_is_question_mark_on_helm_failure(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    spy("helm").on("show", "chart", stdout="", rc=1)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.latest == "?"
    assert argocd.note == ""


def test_singular_source_app_running_uses_sync_revision(repo_with_configs, spy, version_http_stub):
    """An app with `spec.source` (not `spec.sources`) has no multi-source revision index:
    running comes from `status.sync.revision`, not `status.sync.revisions[i]`."""
    apps_dir = repo_with_configs.gitops_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / "argocd.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: argocd\n"
        "spec:\n"
        "  source:\n"
        "    repoURL: https://argoproj.github.io/argo-helm\n"
        "    chart: argo-cd\n"
        "    targetRevision: 10.9.2\n"
        "  destination:\n"
        "    namespace: argocd\n"
    )
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on(
        "get",
        "application",
        "argocd",
        stdout='{"status": {"sync": {"revision": "10.9.2"}}}',
    )
    spy("helm").on("show", "chart", stdout=CHART_OLD)
    rows = versions.collect(Context.open(repo_with_configs))
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.running == "10.9.2"


def test_chart_row_offline_is_dash_and_makes_no_helm_call(repo_with_configs, spy):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    helm = spy("helm")
    rows = versions.collect(Context.open(repo_with_configs), offline=True)
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.latest == "-"
    assert helm.argvs == []


def _write_app_yaml(repo, stem, name, target_revision_line, *, namespace="argocd"):
    """A single-source Argo CD Application at `gitops/apps/<stem>.yaml`, with `targetRevision`
    given as a raw YAML line (e.g. `"targetRevision:"` for null, `"targetRevision: 1.10"` for an
    unquoted float) — `write_chart_app` always writes a valid quoted `str`, which can't reproduce
    these malformed-input cases."""
    apps_dir = repo.gitops_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / f"{stem}.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  source:\n"
        "    repoURL: https://argoproj.github.io/argo-helm\n"
        "    chart: argo-cd\n"
        f"    {target_revision_line}\n"
        "  destination:\n"
        f"    namespace: {namespace}\n"
    )


def test_chart_row_target_is_question_mark_for_null_target_revision(repo_with_configs, spy):
    _write_app_yaml(repo_with_configs, "argocd", "argocd", "targetRevision:")
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    rows = versions.collect(Context.open(repo_with_configs), offline=True)  # must not crash
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.target == "?"


def test_chart_row_target_is_question_mark_for_float_target_revision(repo_with_configs, spy):
    _write_app_yaml(repo_with_configs, "argocd", "argocd", "targetRevision: 1.10")
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    rows = versions.collect(Context.open(repo_with_configs), offline=True)  # must not crash
    argocd = next(r for r in rows if r.component == "argocd")
    assert argocd.target == "?"


def test_chart_row_uses_file_stem_not_metadata_name(repo_with_configs, spy, version_http_stub):
    """The Application is named `renamed-argocd`; the row (and the `kubectl get application`
    lookup for the running revision) must key off the file stem `argocd`/the real resource name
    respectively — not conflate the two."""
    _write_app_yaml(repo_with_configs, "argocd", "renamed-argocd", "targetRevision: 10.9.2")
    _spy_talos_kube(spy)
    kubectl = spy("kubectl")
    kubectl.on("get", "application", "renamed-argocd", stdout=APP_SYNCED_JSON)
    spy("helm").on("show", "chart", stdout=CHART_OLD)
    rows = versions.collect(Context.open(repo_with_configs))
    assert any(r.component == "argocd" for r in rows)
    assert not any(r.component == "renamed-argocd" for r in rows)


def test_chart_row_multi_chart_app_shows_not_single_chart_note(repo_with_configs, spy, version_http_stub):
    apps_dir = repo_with_configs.gitops_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / "twocharts.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: twocharts\n"
        "spec:\n"
        "  sources:\n"
        "    - repoURL: r\n"
        "      chart: c1\n"
        "      targetRevision: 1.0.0\n"
        "    - repoURL: r\n"
        "      chart: c2\n"
        "      targetRevision: 2.0.0\n"
        "  destination:\n"
        "    namespace: ns1\n"
    )
    _spy_talos_kube(spy)
    spy("helm")
    rows = versions.collect(Context.open(repo_with_configs))
    row = next(r for r in rows if r.component == "twocharts")
    assert row.note == "not a single-chart app"
    assert (row.target, row.running, row.latest) == ("?", "?", "?")


# --- render ---


def test_render_marks_outdated_rows_with_a_star_and_up_to_date_without():
    rows = [
        versions.Row("talos", "v1.14.0", "v1.14.0", "v1.15.0"),
        versions.Row("kubernetes", "1.37.0", "1.37.0", "1.37.0"),
    ]
    text = versions.render(rows)
    lines = text.splitlines()
    assert lines[0].startswith(" ")
    talos_line = next(line for line in lines if "talos" in line)
    k8s_line = next(line for line in lines if "kubernetes" in line)
    assert talos_line.startswith("*")
    assert k8s_line.startswith(" ")


def test_render_does_not_mark_question_mark_or_dash_latest():
    rows = [
        versions.Row("talos", "v1.14.0", "?", "?"),
        versions.Row("kubernetes", "1.37.0", "1.37.0", "-"),
    ]
    text = versions.render(rows)
    for line in text.splitlines():
        assert not line.startswith("*")


def test_render_includes_note():
    rows = [
        versions.Row(
            "kubernetes",
            "1.37.0",
            "1.37.0",
            "1.38.0",
            "check the Talos support matrix before upgrading",
        )
    ]
    assert "check the Talos support matrix before upgrading" in versions.render(rows)


# --- versions set ---


def test_set_talos_version_writes_settings_and_returns_hint(empty_repo):
    hint = versions.set_version(empty_repo, "talos", "v1.15.0")
    assert empty_repo.load_settings().talos_version == "v1.15.0"
    assert hint == "run `talkub upgrade talos`"


def test_set_kubernetes_version_strips_leading_v(empty_repo):
    hint = versions.set_version(empty_repo, "kubernetes", "v1.38.0")
    assert empty_repo.load_settings().kubernetes_version == "1.38.0"
    assert hint == "run `talkub upgrade k8s`"


def test_set_kubernetes_version_without_leading_v(empty_repo):
    versions.set_version(empty_repo, "kubernetes", "1.38.0")
    assert empty_repo.load_settings().kubernetes_version == "1.38.0"


@pytest.mark.parametrize(
    "component,value",
    [
        ("talos", "1.15.0"),
        ("talos", "v1.15"),
        ("talos", "not-a-version"),
        ("kubernetes", "vv1.38.0"),
    ],
)
def test_set_version_rejects_invalid_versions(empty_repo, component, value):
    with pytest.raises(ValidationError):
        versions.set_version(empty_repo, component, value)


def test_set_version_rejects_unknown_component(empty_repo):
    with pytest.raises(ValidationError, match="unknown component"):
        versions.set_version(empty_repo, "windows", "1.0.0")


def test_set_version_does_not_write_charts_block(empty_repo):
    versions.set_version(empty_repo, "talos", "v1.15.0")
    assert "charts" not in empty_repo.settings_file.read_text()


# --- CLI ---


def test_cli_versions_command(repo_with_configs, spy, version_http_stub, capfd):
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert "talos" in out and "kubernetes" in out


def test_cli_versions_offline_flag(repo_with_configs, spy, capfd, monkeypatch):
    monkeypatch.setenv("TALKUB_GITHUB_API", "http://127.0.0.1:1")
    monkeypatch.setenv("TALKUB_K8S_RELEASE_URL", "http://127.0.0.1:1/x")
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions", "--offline"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert " -  " in out or "  -" in out


def test_cli_versions_marks_outdated_rows_when_not_a_terminal(repo_with_configs, spy, version_http_stub, capfd):
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert "COMPONENT" in out
    talos_line = next(line for line in out.splitlines() if "talos" in line)
    assert talos_line.startswith("*")


def test_cli_versions_only_shows_the_named_components(repo_with_configs, spy, version_http_stub, capfd):
    _write_argocd_app(repo_with_configs)
    talosctl, _kubectl = _spy_talos_kube(spy)
    spy("kubectl").on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    spy("helm").on("show", "chart", stdout=CHART_OLD)
    try:
        main(["--repo", str(repo_with_configs.root), "versions", "--only", "argocd"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert "argocd" in out
    assert "talos" not in out and "kubernetes" not in out
    assert talosctl.argvs == []


def test_cli_versions_only_unknown_component_fails(repo_with_configs, spy, version_http_stub, capfd):
    _spy_talos_kube(spy)
    spy("helm")
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(repo_with_configs.root), "versions", "--only", "nope"])
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert "error: unknown component(s): nope" in err
    assert "hint: available: talos, kubernetes" in err


def test_cli_versions_outdated_shows_only_rows_with_an_update(repo_with_configs, spy, version_http_stub, capfd):
    version_http_stub.k8s_stable = "v1.37.0\n"
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions", "--outdated"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert "talos" in out
    assert "kubernetes" not in out


def test_cli_versions_outdated_reports_when_everything_is_up_to_date(repo_with_configs, spy, version_http_stub, capfd):
    version_http_stub.talos_tag = "v1.14.0"
    version_http_stub.k8s_stable = "v1.37.0\n"
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions", "--outdated"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert out.strip() == "all components are up to date"


def test_cli_versions_live_table_on_a_terminal(repo_with_configs, spy, version_http_stub, capfd, monkeypatch):
    monkeypatch.setenv("TTY_COMPATIBLE", "1")  # rich: treat the captured stdout as a terminal
    monkeypatch.setenv("COLUMNS", "120")
    _spy_talos_kube(spy)
    spy("helm")
    try:
        main(["--repo", str(repo_with_configs.root), "versions"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capfd.readouterr().out
    assert "fetching talos: running version" in out  # the spinner named the lookups while running
    final = out.rsplit("COMPONENT", 1)[1]  # the table printed after the transient live display
    assert "kubernetes" in final
    talos_line = next(line for line in final.splitlines() if "talos" in line)
    assert "*" in talos_line and "\x1b[" in talos_line  # starred and coloured


def test_cli_versions_set_command(empty_repo, capfd):
    try:
        main(["--repo", str(empty_repo.root), "versions", "set", "talos", "v1.15.0"])
    except SystemExit as exc:
        assert exc.code == 0
    assert empty_repo.load_settings().talos_version == "v1.15.0"
    assert "upgrade talos" in capfd.readouterr().out


def test_cli_versions_set_invalid_version_exits_nonzero(empty_repo, capfd):
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(empty_repo.root), "versions", "set", "talos", "1.15.0"])
    assert exc.value.code == 1
    assert "error" in capfd.readouterr().err


# --- components, progress callbacks and filters ---


def test_components_lists_talos_kubernetes_and_chart_apps(repo_with_configs):
    _write_argocd_app(repo_with_configs)
    assert versions.components(Context.open(repo_with_configs).repo) == ["talos", "kubernetes", "argocd"]


def test_collect_reports_steps_and_rows_in_order(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    _spy_talos_kube(spy)
    spy("kubectl").on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    (
        spy("helm")
        .on("--version", "10.9.2", stdout=CHART_OLD)
        .on("--version", "10.10.0", stdout=CHART_NEW)
        .on("show", "chart", stdout=CHART_NEW)
    )
    steps: list[str] = []
    seen: list[str] = []
    rows = versions.collect(
        Context.open(repo_with_configs), on_step=steps.append, on_row=lambda r: seen.append(r.component)
    )
    assert steps == [
        "talos: running version",
        "talos: latest release",
        "kubernetes: running version",
        "kubernetes: latest release",
        "argocd: running version",
        "argocd: latest chart version",
        "argocd: appVersion change",
    ]
    assert seen == ["talos", "kubernetes", "argocd"]
    assert [row.component for row in rows] == seen


def test_collect_only_restricts_to_named_components_and_skips_the_rest(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    talosctl, _kubectl = _spy_talos_kube(spy)
    spy("kubectl").on("get", "application", "argocd", stdout=APP_SYNCED_JSON)
    spy("helm").on("show", "chart", stdout=CHART_OLD)
    rows = versions.collect(Context.open(repo_with_configs), only=["argocd"])
    assert [row.component for row in rows] == ["argocd"]
    assert talosctl.argvs == []


def test_collect_only_rejects_unknown_component(repo_with_configs, spy, version_http_stub):
    _write_argocd_app(repo_with_configs)
    with pytest.raises(ValidationError) as exc:
        versions.collect(Context.open(repo_with_configs), only=["nope"])
    assert "nope" in str(exc.value)
    assert "talos, kubernetes, argocd" in (exc.value.hint or "")


def test_outdated_keeps_rows_whose_latest_differs_from_target():
    rows = [
        versions.Row("talos", "v1.14.0", "v1.14.0", "v1.15.0"),
        versions.Row("kubernetes", "1.37.0", "1.37.0", "1.37.0"),
        versions.Row("a", "1.0.0", "?", "?"),
        versions.Row("b", "1.0.0", "1.0.0", "-"),
    ]
    assert [row.component for row in versions.outdated(rows)] == ["talos"]
