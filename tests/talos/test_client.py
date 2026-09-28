from __future__ import annotations

import pytest
from helpers import FakeRunner

from talkub.core.errors import ValidationError
from talkub.talos.client import talos_session_from


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
