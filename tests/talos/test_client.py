from __future__ import annotations

import sys

import pytest
from helpers import FakeRunner

from talkub.core.errors import ValidationError
from talkub.talos.client import TalosSession, talos_session_from


@pytest.fixture(autouse=True)
def _talosctl_on_path(fake_bin):
    """talos_session_from() checks that talosctl exists; the calls themselves go to FakeRunner."""
    fake_bin("talosctl", "exit 0\n")


def test_run_global_argv_has_endpoints_and_no_node_flag(tmp_path):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    runner = FakeRunner()
    talos = talos_session_from(runner, talosconfig)
    talos.run_global(["get", "machineconfig"], ["10.0.0.10", "10.0.0.11"])
    assert len(runner.calls) == 1
    argv = runner.calls[0].argv
    assert argv == [
        "talosctl",
        "--talosconfig",
        str(talosconfig),
        "-e",
        "10.0.0.10,10.0.0.11",
        "get",
        "machineconfig",
    ]
    assert "-n" not in argv


def test_call_global_redacted_redacts_secrets(tmp_path, capfd):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    runner = FakeRunner(outputs={"talosctl": b"key: SECRET\n"})
    talos = talos_session_from(runner, talosconfig)
    code = talos.call_global_redacted(["get", "machineconfig"], ["10.0.0.10"])
    assert code == 0
    out = capfd.readouterr().out
    assert "<redacted>" in out
    assert "SECRET" not in out


def test_talos_session_from_missing_path_raises(tmp_path):
    with pytest.raises(ValidationError):
        talos_session_from(FakeRunner(), tmp_path / "nope")


class _Tty:
    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def test_call_redacted_colors_the_redacted_output(tmp_path, fake_bin, capfd):
    fake_bin("delta", "exit 0\n")  # found on PATH; the call itself goes to FakeRunner
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    runner = FakeRunner(outputs={"talosctl": b"+key: SECRET\n", "delta": b"COLORED\n"})
    talos = TalosSession(runner, talosconfig, color=True)
    assert talos.call_redacted("10.0.0.10", ["apply-config", "--dry-run"]) == 0
    assert capfd.readouterr().out == "COLORED\n"
    delta = [call for call in runner.calls if call.argv[0] == "delta"]
    assert len(delta) == 1
    assert delta[0].input == b"+key: <redacted>\n"


def test_call_redacted_is_plain_without_color(tmp_path, fake_bin, capfd):
    fake_bin("delta", "exit 0\n")
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    runner = FakeRunner(outputs={"talosctl": b"+key: SECRET\n", "delta": b"COLORED\n"})
    talos = TalosSession(runner, talosconfig)
    talos.call_redacted("10.0.0.10", ["apply-config", "--dry-run"])
    assert capfd.readouterr().out == "+key: <redacted>\n"
    assert all(call.argv[0] != "delta" for call in runner.calls)


def test_talos_session_from_colors_on_a_terminal(tmp_path, monkeypatch):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    monkeypatch.setattr(sys, "stdout", _Tty())
    assert talos_session_from(FakeRunner(), talosconfig).color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert talos_session_from(FakeRunner(), talosconfig).color is False


def test_talos_session_from_is_plain_when_piped(tmp_path, capfd):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    assert talos_session_from(FakeRunner(), talosconfig).color is False
