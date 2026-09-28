import dataclasses

import pytest
from helpers import DEFAULT_SCHEMATIC_ID, need, sample_settings

from talkub.config import NodeSpec
from talkub.context import Context
from talkub.core.errors import CommandFailed
from talkub.core.fsutil import atomic_write
from talkub.talos import tailscale, upgrade
from talkub.talos.nodes import node_patch_path, render_node_patch

pytestmark = need("age", "age-keygen", "sops", "talosctl")

SHOULD_DRAIN = upgrade._should_drain  # the real policy; the fixture below replaces it per test


@pytest.fixture(autouse=True)
def kubectl(spy, monkeypatch):
    """A fake kubectl (talos_repo is bootstrapped); no drains unless a test opts in."""
    monkeypatch.setattr(upgrade, "_should_drain", lambda settings, name: False)
    monkeypatch.setattr(upgrade, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(upgrade, "REBOOT_TIMEOUT", 0.0)  # one probe after the reboot
    return spy("kubectl")


def _drain_all(monkeypatch):
    monkeypatch.setattr(upgrade, "_should_drain", lambda settings, name: True)


def _version(tag):
    return f"Client:\n\tTag: v1.14.0\nServer:\n\tNODE: x\n\tTag: {tag}\n"


def _extensions(schematic):
    return f'{{"spec": {{"metadata": {{"name": "schematic", "version": "{schematic}"}}}}}}'


NODE_STATE = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "files": {{}}, "kubeconfig": False}}) + "\n")
marker = os.path.join({state!r}, args[args.index("-n") + 1])
if "reboot" in args:
    open(marker, "w").close()
rebooted = os.path.exists(marker)
if "version" in args:
    sys.stdout.write({after_version!r} if rebooted else {version!r})
elif "extensions" in args:
    sys.stdout.write({after_extensions!r} if rebooted else {extensions!r})
"""


def _talosctl(spy, tag="v1.13.0", schematic=DEFAULT_SCHEMATIC_ID, *, after="v1.14.0"):
    """A fake talosctl whose nodes report `tag`/`schematic` until `reboot`, then `after` and the default schematic."""
    import sys

    fake = spy("talosctl").on("-n", "10.0.0.99", "version", rc=1, stderr="connection refused")
    state = fake.directory / "rebooted"
    state.mkdir()
    program = fake.directory / "talosctl-node-state"
    program.write_text(
        NODE_STATE.format(
            python=sys.executable,
            log=str(fake.log),
            state=str(state),
            version=_version(tag),
            after_version=_version(after),
            extensions=_extensions(schematic),
            after_extensions=_extensions(DEFAULT_SCHEMATIC_ID),
        )
    )
    program.chmod(0o755)
    for word in ("version", "extensions", "reboot"):
        fake.exec(word, program=str(program))
    return fake


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
    assert "--no-reboot" in call  # talosctl's reboot would drain via the node's kubeconfig
    assert _actions(talosctl)[-3:] == ["reboot", "version", "get"]  # explicit reboot, then verified
    reboot = next(a for a in talosctl.argvs if "reboot" in a)
    assert "--wait=false" in reboot  # talosctl's wait breaks when the node's Tailscale stops


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
    # reboot, verify the running version, then the wait before w-01
    assert actions[first + 1 : first + 5] == ["reboot", "version", "get", "health"]
    assert actions.count("health") == 2


def test_k8s_dry_run_then_upgrade(talos_repo, spy):
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    assert [a[a.index("-e") + 2 :] for a in talosctl.argvs] == [
        ["upgrade-k8s", "--to", "1.37.0", "--dry-run"],
        ["upgrade-k8s", "--to", "1.37.0"],
    ]


def test_k8s_after_lockdown_uses_the_mgmt_endpoint(talos_repo, spy):
    """The node's kubeconfig points at the cluster endpoint, firewalled from the workstation."""
    settings = talos_repo.load_settings()
    settings.nodes["cp-01"] = dataclasses.replace(settings.nodes["cp-01"], mgmt="cp-01.t.ts.net")
    talos_repo.write_settings(settings)
    tailscale.firewall_patch_path(talos_repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(talos_repo).write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    talosctl = spy("talosctl")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    for call in talosctl.argvs:
        assert call[call.index("--endpoint") + 1] == "https://cp-01.t.ts.net:6443"


def test_k8s_dry_run_redacts_secrets(talos_repo, spy, capfd):
    spy("talosctl").on("--dry-run", stdout="+    - TS_AUTHKEY=tskey-auth-x\n")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    out = capfd.readouterr().out
    assert "<redacted>" in out
    assert "tskey-auth-x" not in out


def test_k8s_dry_run_prints_progress_before_the_output(talos_repo, spy, capfd):
    spy("talosctl").on("--dry-run", stdout="Dry run summary:\nNo changes.\n")
    assert upgrade.k8s(Context.open(talos_repo), confirm=lambda q: True) is True
    out = capfd.readouterr().out
    assert "dry-run" in out.lower()
    assert out.index("dry-run") < out.index("Dry run summary")  # progress line precedes the output


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
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == [
        "cp-01",
        "w-01",
        "w-02",
    ]
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


def test_upgrade_skip_precheck_upgrades_an_unhealthy_cluster(talos_repo, spy):
    """A node stuck booting (e.g. an unconfigured extension) never passes `talosctl health`;
    the upgrade that removes the extension is the fix."""
    talosctl = _talosctl(spy).on("health", rc=1)
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True, skip_precheck=True) == ["cp-01"]
    assert "health" not in _actions(talosctl)
    assert "upgrade" in _actions(talosctl)


def test_upgrade_precheck_failure_hint_names_the_upgrade_flag(talos_repo, spy):
    _talosctl(spy).on("health", rc=1)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "`talkub upgrade talos --force-unhealthy`" in exc.value.hint


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
    talosctl.rules.insert(
        0,
        {
            "match": ["get", "extensions"],
            "stdout": "",
            "stderr": "permission denied",
            "rc": 1,
        },
    )
    talosctl._save()
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "cannot read extensions of node 'cp-01'"
    assert "permission denied" in exc.value.hint
    assert "already upgraded: none; not touched: w-ok" in exc.value.hint
    assert "upgrade" not in _actions(talosctl)


def test_drain_and_uncordon_through_the_repo_kubeconfig(talos_repo, spy, kubectl, monkeypatch):
    _drain_all(monkeypatch)
    talosctl = _talosctl(spy)
    assert upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True) == ["cp-01"]
    assert [c["args"][:2] for c in kubectl.calls] == [["drain", "cp-01"], ["uncordon", "cp-01"]]
    assert all(c["kubeconfig"] for c in kubectl.calls)
    assert "--ignore-daemonsets" in kubectl.argvs[0]
    # a drained node is uncordoned only once healthy again, even when it is the last one
    assert _actions(talosctl).count("health") == 2


def test_no_drain_no_kubectl(talos_repo, spy, kubectl):
    _talosctl(spy)
    upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert kubectl.calls == []


def test_drain_failure_stops_before_the_upgrade(talos_repo, spy, kubectl, monkeypatch):
    _drain_all(monkeypatch)
    kubectl.on("drain", rc=1)
    talosctl = _talosctl(spy)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "drain failed for node 'cp-01'"
    assert "uncordon cp-01" in exc.value.hint
    assert "upgrade" not in _actions(talosctl)


def test_failed_upgrade_after_drain_mentions_the_cordon(talos_repo, spy, kubectl, monkeypatch):
    _drain_all(monkeypatch)
    _talosctl(spy).on("upgrade", rc=1)
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert "still cordoned" in exc.value.hint
    assert [a[0] for a in kubectl.argvs] == ["drain"]


def test_reboot_failure_says_the_version_is_installed(talos_repo, spy):
    talosctl = _talosctl(spy)
    talosctl.rules.insert(0, {"match": ["reboot"], "stdout": "", "stderr": "", "rc": 1})
    talosctl._save()
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "reboot failed for node 'cp-01' (the new version is installed)"
    assert "reboot" in exc.value.hint


def test_node_still_on_the_old_version_after_reboot(talos_repo, spy):
    _add_worker(talos_repo, "w-01", "10.0.0.11")
    talosctl = _talosctl(spy, after="v1.13.0")
    with pytest.raises(CommandFailed) as exc:
        upgrade.talos(Context.open(talos_repo), [], confirm=lambda q: True)
    assert str(exc.value) == "node 'cp-01' did not come back on v1.14.0 within 0s"
    assert "last seen: runs talos v1.13.0, schematic 376567988ad3" in exc.value.hint
    assert "not touched: w-01" in exc.value.hint
    assert not any("10.0.0.11" in a for a in talosctl.argvs)


def test_drain_only_when_pods_have_another_node():
    settings = sample_settings()
    assert SHOULD_DRAIN(settings, "cp-01") is False  # single node: evicted pods would stay Pending
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker")
    assert SHOULD_DRAIN(settings, "cp-01") is True
    assert SHOULD_DRAIN(settings, "w-01") is True


def test_wait_rebooted_tolerates_an_unreachable_node(monkeypatch):
    monkeypatch.setattr(upgrade, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(upgrade, "REBOOT_TIMEOUT", 60.0)
    answers = iter(
        [
            ("v1.13.0", "old"),  # the reboot has not started yet
            CommandFailed("cannot reach node 'cp-01' at 10.0.0.10", hint="connection refused"),
            ("v1.14.0", DEFAULT_SCHEMATIC_ID),
        ]
    )

    def running(session, address, name, *, timeout=None):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(upgrade, "_running", running)
    upgrade._wait_rebooted(None, "10.0.0.10", "cp-01", "v1.14.0", DEFAULT_SCHEMATIC_ID)
    assert next(answers, None) is None  # polled until the target showed up


def test_wait_rebooted_reports_the_last_error(monkeypatch):
    monkeypatch.setattr(upgrade, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(upgrade, "REBOOT_TIMEOUT", 0.0)

    def running(session, address, name, *, timeout=None):
        raise CommandFailed("cannot reach node 'cp-01' at 10.0.0.10", hint="connection refused")

    monkeypatch.setattr(upgrade, "_running", running)
    with pytest.raises(CommandFailed) as exc:
        upgrade._wait_rebooted(None, "10.0.0.10", "cp-01", "v1.14.0", DEFAULT_SCHEMATIC_ID)
    assert "last seen: connection refused" in exc.value.hint
