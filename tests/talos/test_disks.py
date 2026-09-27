import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import FakeRunner

from talkub.core.errors import CommandFailed, ValidationError
from talkub.talos import disks
from talkub.talos.client import TalosSession

DISK = json.dumps({"metadata": {"id": "sda"}, "spec": {"dev_path": "/dev/sda", "size": 1000}})
SYSTEM = json.dumps({"metadata": {"id": "system-disk"}, "spec": {"diskID": "sda"}})
VOLUMES = json.dumps(
    {"metadata": {"id": "sda"}, "spec": {"dev_path": "/dev/sda", "type": "disk", "size": 1000, "name": "gpt"}}
)


@dataclass
class ScriptedRunner(FakeRunner):
    """Answers by resource name (`disks`, `systemdisk`, …); `fail` makes those exit 1."""

    answers: dict[str, str] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)

    def run(self, argv, *, input=None, env=None, cwd=None, check=True, passthrough_stderr=False, timeout=None):
        super().run(argv, check=check, timeout=timeout)
        key = next((word for word in argv if word in self.answers or word in self.fail), None)
        code = 1 if key in self.fail else 0
        if code and check:
            raise CommandFailed(f"{argv[0]} failed")
        return subprocess.CompletedProcess(list(argv), code, self.answers.get(key, "").encode(), b"")


@pytest.fixture(autouse=True)
def _talosctl(monkeypatch):
    monkeypatch.setattr(disks, "require_tool", lambda name: None)


def test_read_insecure_uses_maintenance_api_and_tolerates_missing_volumes():
    runner = ScriptedRunner(answers={"disks": DISK}, fail={"discoveredvolumes"})
    inventory = disks.read_insecure(runner, "10.0.0.5")
    assert [d.id for d in inventory.disks] == ["sda"]
    assert inventory.system_disk is None and not inventory.installed and inventory.discovered == ()
    assert runner.calls[0].argv == ["talosctl", "get", "disks", "--insecure", "-n", "10.0.0.5", "-o", "json"]


def test_read_installed_reads_system_disk_and_volumes():
    runner = ScriptedRunner(answers={"disks": DISK, "systemdisk": SYSTEM, "discoveredvolumes": VOLUMES})
    inventory = disks.read_installed(TalosSession(runner, Path("tc")), "cp-01.ts.net")
    assert inventory.installed and inventory.system_disk == "sda" and inventory.discovered[0].name == "gpt"
    assert all("-n" in c.argv and "cp-01.ts.net" in c.argv for c in runner.calls)


def test_no_disks_is_an_error():
    with pytest.raises(ValidationError, match="no disks"):
        disks.read_insecure(ScriptedRunner(answers={"disks": ""}), "10.0.0.5")


def test_auto_falls_back_to_insecure_without_talosconfig(empty_repo, monkeypatch):
    from talkub.context import Context

    runner = ScriptedRunner(answers={"disks": DISK})
    ctx = Context.open(empty_repo, runner)
    inventory = disks.read(ctx, "cp-01", "auto")
    assert not inventory.installed
    assert "--insecure" in runner.calls[0].argv and "10.0.0.10" in runner.calls[0].argv


def test_auto_uses_session_when_node_answers(empty_repo, monkeypatch):
    from contextlib import contextmanager

    from talkub.context import Context

    runner = ScriptedRunner(answers={"version": "ok", "disks": DISK, "systemdisk": SYSTEM})
    empty_repo.talosconfig.parent.mkdir(parents=True, exist_ok=True)
    empty_repo.talosconfig.write_text("x")

    @contextmanager
    def fake_session(ctx):
        yield TalosSession(runner, Path("tc"))

    monkeypatch.setattr(disks, "talos_session", fake_session)
    inventory = disks.read(Context.open(empty_repo, runner), "cp-01", "auto")
    assert inventory.installed and inventory.system_disk == "sda"


def test_auto_falls_back_when_session_probe_fails(empty_repo, monkeypatch):
    from contextlib import contextmanager

    from talkub.context import Context

    runner = ScriptedRunner(answers={"disks": DISK}, fail={"version"})
    empty_repo.talosconfig.parent.mkdir(parents=True, exist_ok=True)
    empty_repo.talosconfig.write_text("x")

    @contextmanager
    def fake_session(ctx):
        yield TalosSession(runner, Path("tc"))

    monkeypatch.setattr(disks, "talos_session", fake_session)
    assert not disks.read(Context.open(empty_repo, runner), "cp-01", "auto").installed
