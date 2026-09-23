import os

import pytest

from helpers import need
from talkub import access
from talkub.context import Context
from talkub.errors import ToolMissing, ValidationError

pytestmark = need("age", "age-keygen", "sops")

REPORT = """printf 'args:%s\\n' "$*"
printf 'key:%s\\n' "${SOPS_AGE_KEY:-absent}"
printf 'ident:%s\\n' "${TALKUB_IDENTITY:-absent}"
printf 'session:%s\\n' "${TALKUB_SESSION:-none}"
[ -n "$TALOSCONFIG" ] && [ -f "$TALOSCONFIG" ] && cat "$TALOSCONFIG"
[ -n "$KUBECONFIG" ] && [ -f "$KUBECONFIG" ] && cat "$KUBECONFIG"
printf 'tc:%s\\n' "$TALOSCONFIG"
exit 5
"""


def test_talosctl_gets_decrypted_config_and_verbatim_args(
    repo_with_configs, software_identity, fake_bin, capfd, monkeypatch
):
    fake_bin("talosctl", REPORT)
    # Used by talkub to decrypt, but must not be visible to talosctl.
    monkeypatch.setenv("TALKUB_IDENTITY", str(software_identity[0]))
    code = access.talosctl(Context.open(repo_with_configs), ["get", "members", "--", "-x"])
    out = capfd.readouterr().out
    assert code == 5
    assert "args:get members -- -x" in out
    assert "talos: marker" in out
    assert "key:absent" in out
    assert "ident:absent" in out
    talosconfig = out.split("tc:")[1].strip()
    assert talosconfig and not os.path.exists(talosconfig)


def test_kubectl_uses_kubeconfig(repo_with_configs, fake_bin, capfd):
    fake_bin("kubectl", REPORT)
    assert access.kubectl(Context.open(repo_with_configs), ["get", "pods"]) == 5
    assert "kube: marker" in capfd.readouterr().out


def test_kubectl_without_kubeconfig_explains(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        access.kubectl(Context.open(keyed_repo), ["get", "pods"])
    assert "bootstrap" in exc.value.hint
    assert "talkub sops edit kubernetes/kubeconfig.sops.yaml" in exc.value.hint


@pytest.mark.parametrize("command", ["talosctl", "kubectl"])
def test_missing_tool_reported_before_decrypting(repo_with_configs, monkeypatch, tmp_path, command):
    # No identity available: decrypting first would raise NoIdentity instead.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-identities"))
    ctx = Context.open(repo_with_configs)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    with pytest.raises(ToolMissing) as exc:
        getattr(access, command)(ctx, ["version"])
    assert command in str(exc.value)


def test_shell_missing_program_reported_before_decrypting(repo_with_configs, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-identities"))
    with pytest.raises(ToolMissing):
        access.shell(Context.open(repo_with_configs), str(tmp_path / "no-such-shell"))


def test_shell_sets_session_and_both_configs(repo_with_configs, fake_bin, capfd):
    program = fake_bin("fakeshell", REPORT)
    assert access.shell(Context.open(repo_with_configs), str(program)) == 5
    captured = capfd.readouterr()
    assert "session:test" in captured.out
    assert "talos: marker" in captured.out and "kube: marker" in captured.out
    assert "credentials removed" in captured.err


def test_shell_without_kubeconfig_does_not_leak_parent_kubeconfig(repo_with_configs, fake_bin, capfd, monkeypatch, tmp_path):
    repo_with_configs.kubeconfig.unlink()
    other = tmp_path / "other-cluster.yaml"
    other.write_text("other: cluster\n")
    monkeypatch.setenv("KUBECONFIG", str(other))
    program = fake_bin("fakeshell", REPORT)
    access.shell(Context.open(repo_with_configs), str(program))
    assert "other: cluster" not in capfd.readouterr().out


def test_nested_shell_refused(repo_with_configs, monkeypatch):
    monkeypatch.setenv("TALKUB_SESSION", "test")
    with pytest.raises(ValidationError):
        access.shell(Context.open(repo_with_configs), "/bin/sh")
