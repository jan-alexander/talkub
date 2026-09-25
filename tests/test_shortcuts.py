"""talkub run: shortcut execution. Secret values must reach only the clipboard tool's stdin."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from datetime import UTC, datetime

import pytest

from helpers import need
from talkub import shortcuts
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.repo import Shortcut
from talkub.runner import Runner

pytestmark = need("age", "age-keygen", "sops")

VALUE = b"s3cr3t-Pa55w0rd!"
SHORTCUTS = {
    "creds": Shortcut(name="creds", kind="secret", description="db password", namespace="prod", secret="db-creds", key="password"),
    "backup": Shortcut(name="backup", kind="job", namespace="prod", cronjob="backup-job"),
    "backup-follow": Shortcut(name="backup-follow", kind="job", namespace="prod", cronjob="backup-job", follow=True),
    "pods": Shortcut(name="pods", kind="run", argv=("k", "get", "pods")),
    "members": Shortcut(name="members", kind="run", argv=("talosctl", "get", "members")),
    "grafana": Shortcut(name="grafana", kind="portForward", namespace="monitoring", service="grafana", ports=("3000:3000", "8080")),
}


@pytest.fixture
def ctx(repo_with_configs):
    settings = repo_with_configs.load_settings()
    settings.shortcuts = dict(SHORTCUTS)
    repo_with_configs.write_settings(settings)
    return Context.open(repo_with_configs)


@pytest.fixture
def detached(monkeypatch):
    """Capture Runner.spawn_detached instead of starting the real clearer."""
    calls = []
    monkeypatch.setattr(Runner, "spawn_detached", lambda self, argv, *, input: calls.append((list(argv), input)))
    return calls


def _secret_json(**data):
    encoded = {key: base64.b64encode(value).decode() for key, value in data.items()}
    return json.dumps({"kind": "Secret", "data": encoded})


def test_secret_goes_to_the_clipboard_on_stdin_only(ctx, spy, detached, capfd, monkeypatch):
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "30")
    kubectl = spy("kubectl").on("get", "secret", stdout=_secret_json(password=VALUE, user=b"admin"))
    pbcopy = spy("pbcopy").on(read_stdin=True)

    assert shortcuts.run(ctx, "creds", []) == 0

    [get] = kubectl.calls
    assert get["args"] == ["-n", "prod", "get", "secret", "db-creds", "-o", "json"]
    assert get["kubeconfig"]
    [copy] = pbcopy.calls
    assert copy["stdin"] == VALUE.decode()
    assert VALUE.decode() not in copy["args"]
    [(argv, sent)] = detached
    assert sent == hashlib.sha256(VALUE).hexdigest().encode()
    assert VALUE.decode() not in " ".join(argv)
    out, err = capfd.readouterr()
    assert VALUE.decode() not in out + err
    assert out.strip() == f"copied creds ({len(VALUE)} chars); the clipboard is cleared in 30 s"
    for path in ctx.repo.root.rglob("*"):
        if path.is_file():
            assert VALUE not in path.read_bytes()


def test_secret_default_clear_delay_is_printed(ctx, spy, detached, capfd):
    spy("kubectl").on("get", "secret", stdout=_secret_json(password=VALUE))
    spy("pbcopy").on(read_stdin=True)
    shortcuts.run(ctx, "creds", [])
    assert "cleared in 45 s" in capfd.readouterr().out


def test_secret_print_writes_only_the_value(ctx, spy, detached, monkeypatch):
    spy("kubectl").on("get", "secret", stdout=_secret_json(password=VALUE))
    pbcopy = spy("pbcopy").on(read_stdin=True)
    buffer = io.BytesIO()
    fake_stdout = io.TextIOWrapper(buffer)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    assert shortcuts.run(ctx, "creds", [], print_value=True) == 0

    fake_stdout.flush()
    assert buffer.getvalue() == VALUE  # not a terminal: no trailing newline
    assert pbcopy.calls == []
    assert detached == []


def test_secret_missing_key_lists_keys_without_values(ctx, spy, detached):
    spy("kubectl").on("get", "secret", stdout=_secret_json(user=b"admin-value", token=VALUE))
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "creds", [])
    text = f"{exc.value} {exc.value.hint}"
    assert "token" in text and "user" in text
    assert VALUE.decode() not in text and "admin-value" not in text
    assert base64.b64encode(VALUE).decode() not in text


def test_job_name_format_and_length():
    now = datetime(2026, 9, 26, 13, 4, 5, tzinfo=UTC)
    assert shortcuts.job_name("backup-job", now) == "backup-job-manual-20260926130405"
    long_name = shortcuts.job_name("a" * 50, now)
    assert len(long_name) <= 63
    assert long_name == ("a" * 50 + "-manual-20260926130405")[:63]
    # truncation landing right after a dash drops it
    cut = shortcuts.job_name("b" * 55, now)
    assert cut == "b" * 55 + "-manual"
    assert not cut.endswith("-")
    assert shortcuts.job_name("c" * 62, now) == "c" * 62


def test_job_creates_from_cronjob(ctx, spy, capfd):
    kubectl = spy("kubectl")
    assert shortcuts.run(ctx, "backup", []) == 0
    [create] = kubectl.argvs
    assert create[:4] == ["-n", "prod", "create", "job"]
    assert create[4].startswith("backup-job-manual-") and len(create[4]) == len("backup-job-manual-") + 14
    assert create[5:] == ["--from=cronjob/backup-job"]
    assert f"created job prod/{create[4]}" in capfd.readouterr().out


@pytest.mark.parametrize(("succeeded", "code"), [(1, 0), (0, 1)])
def test_job_follow_waits_streams_logs_and_checks_status(ctx, spy, capfd, succeeded, code):
    kubectl = spy("kubectl")
    kubectl.on("get", "job", stdout=json.dumps({"status": {"succeeded": succeeded} if succeeded else {"failed": 1}}))
    kubectl.on("wait", rc=1, stderr="error: no matching resources found")  # tolerated
    kubectl.on("logs", stdout="log line\n")

    assert shortcuts.run(ctx, "backup", [], follow=True) == code

    create, wait, logs, status = kubectl.argvs
    job = create[4]
    assert wait == ["-n", "prod", "wait", "--for=condition=Ready", "pod", "-l", f"job-name={job}", "--timeout=5m"]
    assert logs == ["-n", "prod", "logs", "-f", f"job/{job}"]
    assert status == ["-n", "prod", "get", "job", job, "-o", "json"]
    assert all(call["kubeconfig"] for call in kubectl.calls)
    captured = capfd.readouterr()
    assert "log line" in captured.out
    hint = f"inspect with: talkub k -n prod describe job {job}"
    assert (hint in captured.out + captured.err) == (code == 1)


def test_job_follow_from_config(ctx, spy):
    kubectl = spy("kubectl").on("get", "job", stdout=json.dumps({"status": {"succeeded": 1}}))
    assert shortcuts.run(ctx, "backup-follow", []) == 0
    assert [argv[2] for argv in kubectl.argvs] == ["create", "wait", "logs", "get"]


@pytest.mark.parametrize("name", ["creds", "backup", "grafana"])
def test_extra_arguments_rejected_for_non_run_kinds(ctx, spy, name):
    kubectl = spy("kubectl")
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, name, ["-f"])
    assert f"shortcut {name} (" in str(exc.value) and "takes no extra arguments" in str(exc.value)
    assert kubectl.calls == []


def test_print_only_for_secret_and_follow_only_for_job(ctx, spy):
    spy("kubectl")
    with pytest.raises(ValidationError):
        shortcuts.run(ctx, "backup", [], print_value=True)
    with pytest.raises(ValidationError):
        shortcuts.run(ctx, "creds", [], follow=True)


def test_unknown_shortcut_lists_known(ctx):
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "nope", [])
    assert "unknown shortcut 'nope'" in str(exc.value)
    assert "creds" in exc.value.hint and "grafana" in exc.value.hint


def test_unknown_shortcut_without_any_defined(repo_with_configs):
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(Context.open(repo_with_configs), "nope", [])
    assert exc.value.hint == "define shortcuts in .talkub.yaml"


def test_run_routes_k_to_kubectl_with_extra_args(ctx, spy):
    kubectl = spy("kubectl").on("pods", rc=4)
    assert shortcuts.run(ctx, "pods", ["-n", "x", "-f", "file.yaml"]) == 4
    [call] = kubectl.calls
    assert call["args"] == ["get", "pods", "-n", "x", "-f", "file.yaml"]
    assert call["kubeconfig"]


def test_run_routes_talosctl(ctx, spy):
    talosctl = spy("talosctl")
    assert shortcuts.run(ctx, "members", ["--nodes", "10.0.0.10"]) == 0
    assert talosctl.argvs == [["get", "members", "--nodes", "10.0.0.10"]]


def test_port_forward_argv(ctx, spy):
    kubectl = spy("kubectl").on("port-forward", rc=130)
    assert shortcuts.run(ctx, "grafana", []) == 130
    [call] = kubectl.calls
    assert call["args"] == ["-n", "monitoring", "port-forward", "svc/grafana", "3000:3000", "8080"]
    assert call["kubeconfig"]


def test_listing(ctx):
    lines = shortcuts.listing(ctx.repo.load_settings())
    assert len(lines) == len(SHORTCUTS)
    creds = next(line for line in lines if line.startswith("creds"))
    assert creds.split() == ["creds", "secret", "db", "password"]
