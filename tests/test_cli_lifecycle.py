import io
import sys

import pytest

from helpers import need
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
