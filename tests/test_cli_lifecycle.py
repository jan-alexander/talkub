import io
import sys

import pytest

from helpers import need
from talkub import lifecycle
from talkub.cli.main import main

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def _calls(log):
    return [l for l in log.read_text().splitlines() if l.startswith("call ")] if log.exists() else []


def test_plan_command(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "plan"]) == 0
    assert len(_calls(talosctl_spy)) == 1


def test_apply_yes(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "apply", "cp-01", "--yes", "--mode", "staged"]) == 0
    assert _calls(talosctl_spy)[-1].endswith("--mode staged")


def test_apply_declined_on_prompt(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    _run(["--repo", str(talos_repo.root), "apply"])
    calls = _calls(talosctl_spy)
    assert len(calls) == 1 and calls[0].endswith("--dry-run --mode auto")


def test_insecure_needs_exactly_one_node(talos_repo, talosctl_spy, capfd):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "--yes"]) == 1
    assert "exactly one node" in capfd.readouterr().err


def test_insecure_declined_exits_nonzero(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01"]) == 1
    assert _calls(talosctl_spy) == []


def test_insecure_yes(talos_repo, talosctl_spy):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01", "--yes"]) == 0
    assert "--insecure" in _calls(talosctl_spy)[0]


def test_insecure_rejects_mode(talos_repo, talosctl_spy, capfd):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01", "--yes", "--mode", "staged"]) == 1
    assert "--mode" in capfd.readouterr().err
    assert _calls(talosctl_spy) == []


def test_insecure_rejects_force_unhealthy(talos_repo, talosctl_spy, capfd):
    assert _run(["--repo", str(talos_repo.root), "apply", "--insecure", "cp-01", "--yes", "--force-unhealthy"]) == 1
    assert "--force-unhealthy" in capfd.readouterr().err
    assert _calls(talosctl_spy) == []


def test_apply_force_unhealthy_flag_reaches_lifecycle(talos_repo, talosctl_spy, monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "apply", lambda ctx, nodes, **kw: calls.append(kw) or [])
    assert _run(["--repo", str(talos_repo.root), "apply", "--yes", "--force-unhealthy"]) == 0
    assert calls[0]["skip_precheck"] is True


def test_apply_without_force_unhealthy_defaults_to_precheck_enabled(talos_repo, talosctl_spy, monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "apply", lambda ctx, nodes, **kw: calls.append(kw) or [])
    assert _run(["--repo", str(talos_repo.root), "apply", "--yes"]) == 0
    assert calls[0]["skip_precheck"] is False


def test_apply_eof_at_confirm_sends_only_dry_run(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert _run(["--repo", str(talos_repo.root), "apply"]) != 0
    calls = _calls(talosctl_spy)
    assert len(calls) == 1 and "--dry-run" in calls[0]
