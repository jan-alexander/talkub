"""talkub run: shortcut execution. Secret values must reach only the clipboard tool's stdin."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from helpers import need

from talkub.config import Shortcut
from talkub.context import Context
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.runner import Runner
from talkub.workstation import shortcuts

pytestmark = need("age", "age-keygen", "sops")

VALUE = b"s3cr3t-Pa55w0rd!"
SHORTCUTS = {
    "creds": Shortcut(
        name="creds",
        kind="secret",
        description="db password",
        namespace="prod",
        secret="db-creds",
        key="password",
    ),
    "backup": Shortcut(
        name="backup", kind="job", namespace="prod", cronjob="backup-job"
    ),
    "backup-follow": Shortcut(
        name="backup-follow",
        kind="job",
        namespace="prod",
        cronjob="backup-job",
        follow=True,
    ),
    "pods": Shortcut(name="pods", kind="run", argv=("k", "get", "pods")),
    "members": Shortcut(
        name="members", kind="run", argv=("talosctl", "get", "members")
    ),
    "grafana": Shortcut(
        name="grafana",
        kind="portForward",
        namespace="monitoring",
        service="grafana",
        ports=("3000:3000", "8080"),
    ),
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
    monkeypatch.setattr(
        Runner,
        "spawn_detached",
        lambda self, argv, *, input: calls.append((list(argv), input)),
    )
    return calls


def _secret_json(**data):
    encoded = {key: base64.b64encode(value).decode() for key, value in data.items()}
    return json.dumps({"kind": "Secret", "data": encoded})


def test_secret_goes_to_the_clipboard_on_stdin_only(
    ctx, spy, detached, capfd, monkeypatch
):
    monkeypatch.setenv("TALKUB_CLIPBOARD_CLEAR_SECONDS", "30")
    kubectl = spy("kubectl").on(
        "get", "secret", stdout=_secret_json(password=VALUE, user=b"admin")
    )
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
    assert (
        out.strip()
        == f"copied creds ({len(VALUE)} chars); the clipboard is cleared in 30 s"
    )
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
    spy("kubectl").on(
        "get", "secret", stdout=_secret_json(user=b"admin-value", token=VALUE)
    )
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "creds", [])
    text = f"{exc.value} {exc.value.hint}"
    assert "token" in text and "user" in text
    assert VALUE.decode() not in text and "admin-value" not in text
    assert base64.b64encode(VALUE).decode() not in text


def test_job_name_format_and_length():
    now = datetime(2026, 9, 26, 13, 4, 5, tzinfo=UTC)
    assert shortcuts.job_name("backup-job", now) == "backup-job-manual-20260926130405"
    # the cronjob part is shortened so the timestamp always survives
    long_name = shortcuts.job_name("a" * 50, now)
    assert long_name == "a" * 41 + "-manual-20260926130405"
    assert len(long_name) == 63
    # a shortened cronjob part ending in '-' loses it
    assert (
        shortcuts.job_name("b" * 40 + "-cccc", now)
        == "b" * 40 + "-manual-20260926130405"
    )
    assert shortcuts.job_name("c" * 41, now) == "c" * 41 + "-manual-20260926130405"


def test_job_creates_from_cronjob(ctx, spy, capfd):
    kubectl = spy("kubectl")
    assert shortcuts.run(ctx, "backup", []) == 0
    [create] = kubectl.argvs
    assert create[:4] == ["-n", "prod", "create", "job"]
    assert (
        create[4].startswith("backup-job-manual-")
        and len(create[4]) == len("backup-job-manual-") + 14
    )
    assert create[5:] == ["--from=cronjob/backup-job"]
    assert f"created job prod/{create[4]}" in capfd.readouterr().out


class ScriptedKubectl(Runner):
    """Real runner (sops etc.), but kubectl `get <kind>` answers come from `script[kind]` in order
    (the last answer repeats) and interactive kubectl calls are recorded, not run."""

    def __init__(self, script, *, logs=None):
        self.script = {kind: list(answers) for kind, answers in script.items()}
        self.logs = logs or (lambda: 0)
        self.calls = []

    def run(self, argv, **kwargs):
        if argv[0] != "kubectl":
            return super().run(argv, **kwargs)
        self.calls.append(list(argv[1:]))
        answer = ""
        if argv[3] == "get":
            answers = self.script[argv[4]]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return subprocess.CompletedProcess(list(argv), 0, answer.encode(), b"")

    def call(self, argv, **kwargs):
        if argv[0] != "kubectl":
            return super().call(argv, **kwargs)
        self.calls.append(list(argv[1:]))
        assert kwargs["env"]["KUBECONFIG"]
        return self.logs()

    def verbs(self):
        return [
            " ".join(call[2:4]) if call[2] == "get" else call[2] for call in self.calls
        ]


def _pods(*phases):
    return json.dumps({"items": [{"status": {"phase": phase}} for phase in phases]})


def _job(*, succeeded=None, condition=None):
    status = {}
    if succeeded is not None:
        status["succeeded"] = succeeded
    if condition is not None:
        status["conditions"] = [
            {"type": "Suspended", "status": "False"},
            {"type": condition, "status": "True"},
        ]
    return json.dumps({"status": status})


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(shortcuts, "POLL_INTERVAL", 0)
    monkeypatch.setattr(shortcuts, "POD_START_TIMEOUT", 0.3)
    monkeypatch.setattr(shortcuts, "JOB_SETTLE_TIMEOUT", 0.3)


def _scripted_ctx(repo, script, **kwargs):
    runner = ScriptedKubectl(script, **kwargs)
    return Context.open(repo, runner=runner), runner


def test_job_follow_waits_for_the_pod_streams_logs_and_succeeds(
    ctx, spy, fast_polls, capfd
):
    spy("kubectl")  # only needs to be on PATH
    fctx, runner = _scripted_ctx(
        ctx.repo,
        {
            "pods": [_pods(), _pods("Pending"), _pods("Running")],
            "job": [_job(), _job(condition="Complete")],
        },
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 0
    assert runner.verbs() == [
        "create",
        "get pods",
        "get pods",
        "get pods",
        "logs",
        "get job",
        "get job",
    ]
    job = runner.calls[0][4]
    assert runner.calls[1] == [
        "-n",
        "prod",
        "get",
        "pods",
        "-l",
        f"job-name={job}",
        "-o",
        "json",
    ]
    assert runner.calls[4] == ["-n", "prod", "logs", "-f", f"job/{job}"]
    assert runner.calls[5] == ["-n", "prod", "get", "job", job, "-o", "json"]
    assert "describe job" not in capfd.readouterr().err


def test_job_follow_pod_already_finished_goes_straight_to_logs(ctx, spy, fast_polls):
    spy("kubectl")
    fctx, runner = _scripted_ctx(
        ctx.repo, {"pods": [_pods("Succeeded")], "job": [_job(succeeded=1)]}
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 0
    assert runner.verbs() == ["create", "get pods", "logs", "get job"]


def test_job_follow_failed_job_returns_1_with_hint(ctx, spy, fast_polls, capfd):
    spy("kubectl")
    fctx, runner = _scripted_ctx(
        ctx.repo, {"pods": [_pods("Failed")], "job": [_job(condition="Failed")]}
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 1
    job = runner.calls[0][4]
    err = capfd.readouterr().err
    assert f"job prod/{job} failed" in err
    assert f"inspect with: talkub k -n prod describe job {job}" in err


def test_job_follow_undecided_job_is_still_running(ctx, spy, fast_polls, capfd):
    spy("kubectl")
    fctx, runner = _scripted_ctx(
        ctx.repo, {"pods": [_pods("Running")], "job": [_job()]}
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 1
    job = runner.calls[0][4]
    err = capfd.readouterr().err
    assert f"job prod/{job} is still running" in err
    assert f"describe job {job}" in err
    assert runner.verbs().count("get job") > 1  # polled until the timeout


def test_job_follow_interrupted_logs_check_status_once(
    ctx, spy, fast_polls, monkeypatch, capfd
):
    spy("kubectl")
    monkeypatch.setattr(
        shortcuts, "JOB_SETTLE_TIMEOUT", 60
    )  # must not be waited for after Ctrl-C
    fctx, runner = _scripted_ctx(
        ctx.repo, {"pods": [_pods("Running")], "job": [_job()]}, logs=lambda: 130
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 1
    assert runner.verbs().count("get job") == 1
    assert "is still running" in capfd.readouterr().err


def test_job_follow_pod_never_appears(ctx, spy, fast_polls):
    spy("kubectl")
    fctx, runner = _scripted_ctx(ctx.repo, {"pods": [_pods()], "job": [_job()]})
    with pytest.raises(CommandFailed) as exc:
        shortcuts.run(fctx, "backup", [], follow=True)
    job = runner.calls[0][4]
    assert "job pod did not start within 5 min" in str(exc.value)
    assert exc.value.hint == f"inspect with: talkub k -n prod describe job {job}"
    assert "logs" not in runner.verbs()


def test_job_follow_pod_still_pending_streams_logs_anyway(ctx, spy, fast_polls, capfd):
    spy("kubectl")
    fctx, runner = _scripted_ctx(
        ctx.repo, {"pods": [_pods("Pending")], "job": [_job(succeeded=1)]}
    )
    assert shortcuts.run(fctx, "backup", [], follow=True) == 0
    assert "logs" in runner.verbs()
    assert "still pending" in capfd.readouterr().err


def test_job_follow_end_to_end_with_real_kubeconfig(ctx, spy, fast_polls):
    kubectl = spy("kubectl")
    kubectl.on("get", "pods", stdout=_pods("Running"))
    kubectl.on("get", "job", stdout=_job(condition="Complete"))
    assert shortcuts.run(ctx, "backup-follow", []) == 0
    assert [argv[2] for argv in kubectl.argvs] == ["create", "get", "logs", "get"]
    assert all(call["kubeconfig"] for call in kubectl.calls)


def test_pod_phases_and_job_outcome_parsing():
    assert shortcuts.pod_phases(_pods("Pending", "Running").encode()) == [
        "Pending",
        "Running",
    ]
    assert shortcuts.pod_phases(b"not json") == []
    assert shortcuts.job_outcome(_job(succeeded=1).encode()) == "Complete"
    assert shortcuts.job_outcome(_job(condition="Complete").encode()) == "Complete"
    assert shortcuts.job_outcome(_job(condition="Failed").encode()) == "Failed"
    assert shortcuts.job_outcome(_job().encode()) is None
    assert shortcuts.job_outcome(b"{") is None


@pytest.mark.parametrize("name", ["creds", "backup", "grafana"])
def test_extra_arguments_rejected_for_non_run_kinds(ctx, spy, name):
    kubectl = spy("kubectl")
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, name, ["-f"])
    assert f"shortcut {name} (" in str(exc.value) and "takes no extra arguments" in str(
        exc.value
    )
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
    assert call["args"] == [
        "-n",
        "monitoring",
        "port-forward",
        "svc/grafana",
        "3000:3000",
        "8080",
    ]
    assert call["kubeconfig"]


def test_listing(ctx):
    lines = shortcuts.listing(ctx.repo.load_settings())
    assert len(lines) == len(SHORTCUTS)
    creds = next(line for line in lines if line.startswith("creds"))
    assert creds.split() == ["creds", "secret", "db", "password"]


def test_secret_counts_characters_not_bytes(ctx, spy, detached, capfd):
    value = "pässwörd".encode()
    spy("kubectl").on("get", "secret", stdout=_secret_json(password=value))
    spy("pbcopy").on(read_stdin=True)
    shortcuts.run(ctx, "creds", [])
    assert "copied creds (8 chars)" in capfd.readouterr().out


def test_secret_reports_bytes_when_not_utf8(ctx, spy, detached, capfd):
    spy("kubectl").on("get", "secret", stdout=_secret_json(password=b"\xff\xfe\x00"))
    spy("pbcopy")  # the spy logs stdin as text; this value is not UTF-8
    shortcuts.run(ctx, "creds", [])
    assert "copied creds (3 bytes)" in capfd.readouterr().out


def test_secret_clearer_failure_only_warns(ctx, spy, capfd, monkeypatch):
    def broken(self, argv, *, input):
        raise CommandFailed("cannot start")

    monkeypatch.setattr(Runner, "spawn_detached", broken)
    spy("kubectl").on("get", "secret", stdout=_secret_json(password=VALUE))
    pbcopy = spy("pbcopy").on(read_stdin=True)
    assert shortcuts.run(ctx, "creds", []) == 0
    assert len(pbcopy.calls) == 1
    out, err = capfd.readouterr()
    assert "warning: the clipboard will not be cleared automatically" in err
    assert "copied creds" in out
