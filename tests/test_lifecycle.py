import pytest

from helpers import need, sample_settings
from talkub import lifecycle
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec
from talkub.runner import Runner
from talkub.talos.client import TalosSession

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


def test_plan_redacts_secrets_in_the_dry_run_diff(talos_repo, spy, capfd):
    spy("talosctl").real("gen").real("validate").on(
        "apply-config", "--dry-run",
        stdout="         key: LS0tLS1CRUdJTiBFRDI1NTE5IFBSSVZBVEUgS0VZLS0tLS0K\n+    - TS_AUTHKEY=tskey-auth-x\n",
    )
    lifecycle.plan(Context.open(talos_repo), [])
    out = capfd.readouterr().out
    assert "<redacted>" in out
    assert "TS_AUTHKEY=tskey-auth-x" not in out
    assert "LS0tLS1CRUdJTiBFRDI1NTE5IFBSSVZBVEUgS0VZLS0tLS0K" not in out
    assert "== cp-01 (10.0.0.10)" in out  # unrelated output is unaffected


def test_dry_run_failure_redacts_secrets_in_stderr(talos_repo, spy, capfd):
    spy("talosctl").real("gen").real("validate").on(
        "apply-config", "--dry-run", stderr="+    - TS_AUTHKEY=tskey-auth-x\n", rc=1
    )
    with pytest.raises(CommandFailed) as exc:
        lifecycle.plan(Context.open(talos_repo), [])
    assert "dry-run failed for node 'cp-01'" in str(exc.value)  # exit-code handling unchanged
    err = capfd.readouterr().err
    assert "<redacted>" in err
    assert "TS_AUTHKEY=tskey-auth-x" not in err


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
    assert len(calls) == 1 and calls[0].endswith("--dry-run --mode no-reboot")


def test_plan_dry_run_has_no_mode(talos_repo, talosctl_spy):
    lifecycle.plan(Context.open(talos_repo), [])
    assert "--mode" not in _calls(talosctl_spy)[0]


def test_dry_run_failure_lists_applied_nodes(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-bad", "10.0.0.99")
    _add_node(talos_repo, "w-ok", "10.0.0.12")
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "dry-run failed for node 'w-bad'" in str(exc.value)
    assert "already applied: cp-01; not touched: w-ok" in exc.value.hint


def test_real_apply_failure_names_node_and_lists_applied(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-98", "10.0.0.98")
    _add_node(talos_repo, "w-ok", "10.0.0.12")
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "apply failed for node 'w-98'"
    assert "already applied: cp-01; not touched: w-ok" in exc.value.hint
    calls = _calls(talosctl_spy)
    dry, real = [c for c in calls if "10.0.0.98" in c]  # dry-run passed, the real apply failed
    assert dry.endswith("--dry-run --mode auto") and real.endswith("--mode auto") and "--dry-run" not in real
    assert not any("10.0.0.12" in call for call in calls)


def test_failure_on_first_node_says_none_applied(talos_repo, talosctl_spy):
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.98")
    talos_repo.write_settings(settings)
    talos_repo.kubeconfig.unlink()  # not bootstrapped: no health pre-check against the failing node
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "apply failed for node 'cp-01'"
    assert "already applied: none; not touched: none" in exc.value.hint


def test_apply_insecure_refuses_unknown_image_or_disk(talos_repo, talosctl_spy, monkeypatch):
    asked = []
    for summary in (("unknown", "/dev/sda"), ("factory.talos.dev/metal-installer/x:v1.14.0", "unknown")):
        monkeypatch.setattr(lifecycle, "install_summary", lambda config, s=summary: s)
        with pytest.raises(ValidationError) as exc:
            lifecycle.apply_insecure(Context.open(talos_repo), "cp-01", confirm=lambda q: asked.append(q) or True)
        assert str(exc.value) == "cannot determine installer image/install disk from the rendered config for 'cp-01'"
        assert "refusing to install" in exc.value.hint
    assert asked == []
    assert _calls(talosctl_spy) == []


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


def test_apply_waits_for_health_between_nodes(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    calls = _calls(talosctl_spy)
    health = [i for i, c in enumerate(calls) if " health --wait-timeout 10m" in c]
    assert len(health) == 2  # the pre-check before cp-01, the wait after it
    assert all("-n 10.0.0.10" in calls[i] for i in health)
    assert "10.0.0.11" in calls[health[1] + 1]  # the worker comes after the wait


def test_no_health_wait_without_reboot_mode_or_bootstrap(talos_repo, talosctl_spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    lifecycle.apply(Context.open(talos_repo), [], mode="no-reboot", confirm=lambda q: True)
    assert not any(" health " in c for c in _calls(talosctl_spy))
    talos_repo.kubeconfig.unlink()
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert not any(" health " in c for c in _calls(talosctl_spy))


def test_health_failure_stops_and_reports_progress(talos_repo, spy):
    _add_node(talos_repo, "cp-02", "10.0.0.13", role="controlplane")
    _add_node(talos_repo, "w-01", "10.0.0.11")
    # the pre-check (via cp-01) passes; the wait after cp-01 goes through cp-02 and fails
    talosctl = spy("talosctl").real("gen").real("validate").on("-n", "10.0.0.13", "health", rc=1)
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "10.0.0.13" in str(exc.value)
    assert "already applied: cp-01" in exc.value.hint
    assert "not touched: cp-02, w-01" in exc.value.hint
    assert not any("10.0.0.11" in argv for argv in talosctl.argvs)


def _node_of(argv):
    return argv[argv.index("-n") + 1]


def _action(argv):
    return argv[argv.index("-e") + 2]


def _health(argvs):
    return [(i, _node_of(a)) for i, a in enumerate(argvs) if "health" in a]


def _real_applies(argvs):
    return [i for i, a in enumerate(argvs) if "apply-config" in a and "--dry-run" not in a]


def test_health_target_prefers_another_controlplane(talos_repo):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    settings = talos_repo.load_settings()
    assert lifecycle.health_target(settings, "cp-01") == "10.0.0.10"  # single CP: itself
    assert lifecycle.health_target(settings, "w-01") == "10.0.0.10"
    _add_node(talos_repo, "cp-02", "10.0.0.13", role="controlplane", mgmt="cp-02.ts.net")
    settings = talos_repo.load_settings()
    assert lifecycle.health_target(settings, "cp-01") == "cp-02.ts.net"
    assert lifecycle.health_target(settings, "cp-02") == "10.0.0.10"
    assert lifecycle.health_target(settings, "w-01") == "10.0.0.10"


def test_apply_health_always_targets_a_controlplane(talos_repo, spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    _add_node(talos_repo, "w-02", "10.0.0.12")
    talosctl = spy("talosctl").real("gen").real("validate")
    assert lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01", "w-01", "w-02"]
    health = _health(talosctl.argvs)
    assert [node for _, node in health] == ["10.0.0.10"] * 3  # pre-check, after cp-01, after w-01


def test_apply_health_uses_the_other_controlplane(talos_repo, spy):
    _add_node(talos_repo, "cp-02", "10.0.0.13", role="controlplane")
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    argvs = talosctl.argvs
    applied = [_node_of(argvs[i]) for i in _real_applies(argvs)]
    assert applied == ["10.0.0.10", "10.0.0.13", "10.0.0.11"]
    after = [node for _, node in _health(argvs)][1:]  # [0] is the pre-check
    assert after == ["10.0.0.13", "10.0.0.10"]  # after cp-01 → cp-02, after cp-02 → cp-01


def test_apply_settles_before_health(talos_repo, spy, monkeypatch):
    monkeypatch.delenv("TALKUB_APPLY_SETTLE_SECONDS", raising=False)
    slept = []
    monkeypatch.setattr(lifecycle.time, "sleep", slept.append)
    _add_node(talos_repo, "w-01", "10.0.0.11")
    spy("talosctl").real("gen").real("validate")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert slept == [30.0]  # only the post-apply wait settles, not the pre-check


def test_apply_prechecks_health_once_before_first_change(talos_repo, spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    argvs = talosctl.argvs
    first_apply = _real_applies(argvs)[0]
    health = [i for i, _ in _health(argvs)]
    assert health[0] == first_apply - 1  # after cp-01's dry-run and confirmation
    assert "--dry-run" in argvs[health[0] - 1]
    assert len([i for i in health if i < first_apply]) == 1


def test_apply_precheck_failure_changes_nothing(talos_repo, spy):
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate").on("health", rc=1)
    with pytest.raises(CommandFailed) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "the cluster is not healthy; nothing was changed"
    assert "already applied: none" in exc.value.hint
    assert "--force-unhealthy" in exc.value.hint
    assert _real_applies(talosctl.argvs) == []


def test_apply_force_unhealthy_skips_only_the_precheck(talos_repo, spy):
    """A single-controlplane apply: the only health call is the pre-check (no other node to
    wait for afterwards), so --force-unhealthy lets it proceed despite `health` always failing."""
    talosctl = spy("talosctl").real("gen").real("validate").on("health", rc=1)
    with pytest.raises(CommandFailed):
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    before = len(talosctl.argvs)
    applied = lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True, skip_precheck=True)
    assert applied == ["cp-01"]
    assert not any("health" in argv for argv in talosctl.argvs[before:])


def test_apply_force_unhealthy_skips_precheck_but_keeps_the_rolling_health_wait(talos_repo, spy):
    """Two nodes: --force-unhealthy must skip only the pre-check, not the health wait between
    applying cp-01 and w-01."""
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate")
    applied = lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True, skip_precheck=True)
    assert applied == ["cp-01", "w-01"]
    assert len(_health(talosctl.argvs)) == 1  # only the wait between the two applies


def test_declined_apply_does_not_precheck(talos_repo, spy):
    talosctl = spy("talosctl").real("gen").real("validate")
    lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: False)
    assert _health(talosctl.argvs) == []


def test_invalid_settle_value_fails_before_any_change(talos_repo, spy, monkeypatch):
    monkeypatch.setenv("TALKUB_APPLY_SETTLE_SECONDS", "soon")
    _add_node(talos_repo, "w-01", "10.0.0.11")
    talosctl = spy("talosctl").real("gen").real("validate")
    with pytest.raises(ValidationError) as exc:
        lifecycle.apply(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "TALKUB_APPLY_SETTLE_SECONDS" in str(exc.value)
    assert _real_applies(talosctl.argvs) == []


def test_node_drift_in_sync(tmp_path, spy, capfd):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    spy("talosctl").on("apply-config", "--dry-run", stdout="No changes.\n")
    talos = TalosSession(Runner(), talosconfig)
    result = lifecycle.node_drift(talos, sample_settings(), "cp-01", b"config bytes")
    assert result == "in sync"
    assert capfd.readouterr() == ("", "")


def test_node_drift_changes(tmp_path, spy, capfd):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    spy("talosctl").on("apply-config", "--dry-run", stdout="Config diff:\n+ foo\n")
    talos = TalosSession(Runner(), talosconfig)
    result = lifecycle.node_drift(talos, sample_settings(), "cp-01", b"config bytes")
    assert result == "changes"
    assert capfd.readouterr() == ("", "")


def test_node_drift_failure_raises(tmp_path, spy):
    talosconfig = tmp_path / "talosconfig"
    talosconfig.write_text("marker\n")
    spy("talosctl").on("apply-config", "--dry-run", rc=1)
    talos = TalosSession(Runner(), talosconfig)
    with pytest.raises(CommandFailed):
        lifecycle.node_drift(talos, sample_settings(), "cp-01", b"config bytes")
