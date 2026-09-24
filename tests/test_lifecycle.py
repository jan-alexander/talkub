import pytest

from helpers import need
from talkub import lifecycle
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _calls(log):
    return [line[5:] for line in log.read_text().splitlines() if line.startswith("call ")] if log.exists() else []


def _add_node(repo, name, ip, role="worker", mgmt=None):
    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip, role=role, mgmt=mgmt)
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, name), render_node_patch(name).encode())


def test_plan_runs_dry_run_with_ephemeral_talosconfig(talos_repo, talosctl_spy, capfd):
    lifecycle.plan(Context.open(talos_repo), [])
    calls = _calls(talosctl_spy)
    assert len(calls) == 1
    assert " -n 10.0.0.10 -e 10.0.0.10 apply-config --file " in calls[0]
    assert calls[0].endswith("--dry-run")
    assert "file 1" in talosctl_spy.read_text()  # rendered config existed and had a hostname
    assert "== cp-01 (10.0.0.10)" in capfd.readouterr().out


def test_plan_uses_mgmt_and_orders_controlplanes_first(talos_repo, talosctl_spy):
    _add_node(talos_repo, "a-worker", "10.0.0.11")
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net")
    talos_repo.write_settings(settings)
    lifecycle.plan(Context.open(talos_repo), [])
    calls = _calls(talosctl_spy)
    assert "-n cp-01.tail.ts.net" in calls[0]
    assert "-n 10.0.0.11" in calls[1]


def test_apply_confirmed_and_declined(talos_repo, talosctl_spy):
    assert lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01"]
    assert _calls(talosctl_spy)[-1].endswith("--mode auto")
    talosctl_spy.unlink()
    assert lifecycle.apply(Context.open(talos_repo), [], mode="no-reboot", confirm=lambda q: False) == []
    calls = _calls(talosctl_spy)
    assert len(calls) == 1 and calls[0].endswith("--dry-run")


def test_apply_stops_at_failing_node(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-bad", "10.0.0.99")
    _add_node(talos_repo, "w-ok", "10.0.0.12")
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "w-bad" in str(exc.value)
    assert not any("10.0.0.12" in call for call in _calls(talosctl_spy))


def test_apply_rejects_unknown_mode_and_node(talos_repo, talosctl_spy):
    with pytest.raises(ValidationError):
        lifecycle.apply(Context.open(talos_repo), [], mode="reboot", confirm=lambda q: True)
    with pytest.raises(ValidationError):
        lifecycle.plan(Context.open(talos_repo), ["nope"])
    assert _calls(talosctl_spy) == []


def test_apply_insecure(talos_repo, talosctl_spy, capfd):
    assert lifecycle.apply_insecure(Context.open(talos_repo), "cp-01", confirm=lambda q: True) is True
    calls = _calls(talosctl_spy)
    assert calls == [calls[0]]
    assert calls[0].startswith("apply-config --insecure -n 10.0.0.10 --file ")
    assert "--talosconfig" not in calls[0]
    out = capfd.readouterr().out
    assert "metal-installer/" in out
    assert 'disk.dev_path == "/dev/sda"' in out


def test_apply_insecure_declined_sends_nothing(talos_repo, talosctl_spy):
    assert lifecycle.apply_insecure(Context.open(talos_repo), "cp-01", confirm=lambda q: False) is False
    assert _calls(talosctl_spy) == []


def test_outdated_schematic_is_refreshed_before_render(talos_repo, talosctl_spy, factory_stub, capfd):
    settings = talos_repo.load_settings()
    settings.schematics["metal"].extensions.append("siderolabs/tailscale")
    talos_repo.write_settings(settings)
    lifecycle.plan(Context.open(talos_repo), [])
    assert [r[0] for r in factory_stub.requests] == ["POST"]
    assert talos_repo.load_settings().schematics["metal"].is_current
    assert "updated schematic IDs" in capfd.readouterr().err


def test_current_schematic_needs_no_factory(talos_repo, talosctl_spy, monkeypatch):
    monkeypatch.setenv("TALKUB_FACTORY_URL", "http://127.0.0.1:9")
    lifecycle.plan(Context.open(talos_repo), [])
    assert len(_calls(talosctl_spy)) == 1


def test_used_platform_without_schematic_fails_before_network(talos_repo, talosctl_spy, factory_stub):
    settings = talos_repo.load_settings()
    settings.nodes["h-01"] = NodeSpec(ip="10.0.0.20", role="worker", platform="hcloud")
    talos_repo.write_settings(settings)
    atomic_write(node_patch_path(talos_repo, "h-01"), render_node_patch("h-01").encode())
    with pytest.raises(ValidationError) as exc:
        lifecycle.plan(Context.open(talos_repo), [])
    assert str(exc.value) == "no schematic for platform 'hcloud' (used by node 'h-01')"
    assert "talkub image add --platform hcloud" in exc.value.hint
    assert factory_stub.requests == []
    assert _calls(talosctl_spy) == []
    assert "hcloud" not in talos_repo.load_settings().schematics
