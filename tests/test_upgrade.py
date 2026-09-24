import pytest

from helpers import DEFAULT_SCHEMATIC_ID, need
from talkub import upgrade
from talkub.context import Context
from talkub.errors import CommandFailed
from talkub.fsutil import atomic_write
from talkub.nodes import node_patch_path, render_node_patch
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops", "talosctl")


def _version(tag):
    return f"Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: {tag}\n"


def _extensions(schematic):
    return '{"spec": {"metadata": {"name": "schematic", "version": "%s"}}}' % schematic


def _talosctl(spy, tag="v1.13.0", schematic=DEFAULT_SCHEMATIC_ID):
    return (
        spy("talosctl")
        .on("-n", "10.0.0.99", "version", rc=1, stderr="connection refused")
        .on("version", stdout=_version(tag))
        .on("get", "extensions", stdout=_extensions(schematic))
    )


def _add_worker(repo, name, ip):
    settings = repo.load_settings()
    settings.nodes[name] = NodeSpec(ip=ip, role="worker")
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, name), render_node_patch(name).encode())


def _actions(fake):
    return [a[a.index("-e") + 2] for a in fake.argvs]


def test_upgrade_outdated_node(talos_repo, spy):
    talosctl = _talosctl(spy, tag="v1.13.0")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01"]
    call = next(a for a in talosctl.argvs if "upgrade" in a)
    assert call[call.index("--image") + 1] == f"factory.talos.dev/metal-installer/{DEFAULT_SCHEMATIC_ID}:v1.14.0"
    assert "--wait" in call


def test_up_to_date_node_is_skipped(talos_repo, spy, capfd):
    talosctl = _talosctl(spy, tag="v1.14.0")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == []
    assert "upgrade" not in _actions(talosctl)
    assert "up to date" in capfd.readouterr().out


def test_schematic_change_triggers_upgrade(talos_repo, spy):
    _talosctl(spy, tag="v1.14.0", schematic="0000")
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01"]


def test_declined_upgrade(talos_repo, spy):
    talosctl = _talosctl(spy)
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: False) == []
    assert "upgrade" not in _actions(talosctl)


def test_unreachable_node_stops_the_run(talos_repo, spy):
    _add_worker(talos_repo, "w-99", "10.0.0.99")
    _add_worker(talos_repo, "w-ok", "10.0.0.12")
    talosctl = _talosctl(spy)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "w-99" in str(exc.value)
    assert "already upgraded: cp-01" in exc.value.hint
    assert not any("10.0.0.12" in a for a in talosctl.argvs)


def test_health_wait_between_upgrades(talos_repo, spy):
    _add_worker(talos_repo, "w-01", "10.0.0.11")
    talosctl = _talosctl(spy)
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    actions = _actions(talosctl)
    first = actions.index("upgrade")
    assert actions[first - 1] == "health"  # the pre-check
    assert actions[first + 1] == "health"  # the wait before w-01
    assert actions.count("health") == 2


def test_k8s_dry_run_then_upgrade(talos_repo, spy):
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    assert [a[a.index("-e") + 2:] for a in talosctl.argvs] == [
        ["upgrade-k8s", "--to", "1.37.0", "--dry-run"],
        ["upgrade-k8s", "--to", "1.37.0"],
    ]


def test_k8s_dry_run_redacts_secrets(talos_repo, spy, capfd):
    spy("talosctl").on("--dry-run", stdout="+    - TS_AUTHKEY=tskey-auth-x\n")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    out = capfd.readouterr().out
    assert "<redacted>" in out
    assert "tskey-auth-x" not in out


def test_k8s_declined_or_failing_dry_run(talos_repo, spy):
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: False) is False
    assert len(talosctl.argvs) == 1
    talosctl.on("--dry-run", rc=1)
    with pytest.raises(CommandFailed):
        upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True)


def _health_nodes(fake):
    return [a[a.index("-n") + 1] for a in fake.argvs if "health" in a]


def test_upgrade_health_always_targets_a_controlplane(talos_repo, spy):
    _add_worker(talos_repo, "w-01", "10.0.0.11")
    _add_worker(talos_repo, "w-02", "10.0.0.12")
    talosctl = _talosctl(spy)
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01", "w-01", "w-02"]
    assert _health_nodes(talosctl) == ["10.0.0.10"] * 3  # pre-check, after cp-01, after w-01


def test_upgrade_health_uses_the_other_controlplane(talos_repo, spy):
    settings = talos_repo.load_settings()
    settings.nodes["cp-02"] = NodeSpec(ip="10.0.0.13", role="controlplane")
    talos_repo.write_settings(settings)
    atomic_write(node_patch_path(talos_repo, "cp-02"), render_node_patch("cp-02").encode())
    _add_worker(talos_repo, "w-01", "10.0.0.11")
    talosctl = _talosctl(spy)
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert _health_nodes(talosctl) == ["10.0.0.10", "10.0.0.13", "10.0.0.10"]


def test_upgrade_precheck_failure_changes_nothing(talos_repo, spy):
    talosctl = _talosctl(spy).on("health", rc=1)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "the cluster is not healthy; nothing was changed"
    assert "upgrade" not in _actions(talosctl)


def test_up_to_date_or_declined_runs_do_not_precheck(talos_repo, spy):
    talosctl = _talosctl(spy, tag="v1.14.0")
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    talosctl.rules.insert(0, {"match": ["version"], "stdout": _version("v1.13.0"), "stderr": "", "rc": 0})
    talosctl._save()
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: False)
    assert "health" not in _actions(talosctl)


def test_extensions_failure_stops_the_run(talos_repo, spy):
    _add_worker(talos_repo, "w-ok", "10.0.0.12")
    talosctl = _talosctl(spy)
    talosctl.rules.insert(0, {"match": ["get", "extensions"], "stdout": "", "stderr": "permission denied", "rc": 1})
    talosctl._save()
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "cannot read extensions of node 'cp-01'"
    assert "permission denied" in exc.value.hint
    assert "already upgraded: none; not touched: w-ok" in exc.value.hint
    assert "upgrade" not in _actions(talosctl)
