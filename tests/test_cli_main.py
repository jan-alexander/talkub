import pytest

from helpers import need
from talkub.cli.main import main, split_globals

pytestmark = need("age", "age-keygen", "sops")

ECHO_ARGS = "printf 'args:%s\\n' \"$*\"\nexit 3\n"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_split_globals():
    state, rest = split_globals(["--debug", "--repo", "/x", "k", "--repo", "y"])
    assert state.debug and str(state.repo_path) == "/x"
    assert rest == ["k", "--repo", "y"]


@pytest.mark.parametrize("alias", ["t", "talosctl"])
def test_talosctl_alias_passes_everything_verbatim(repo_with_configs, fake_bin, capfd, alias):
    fake_bin("talosctl", ECHO_ARGS)
    code = _run(["--repo", str(repo_with_configs.root), alias, "--help", "get", "--", "x"])
    assert code == 3
    assert "args:--help get -- x" in capfd.readouterr().out


def test_kubectl_exec_double_dash(repo_with_configs, fake_bin, capfd):
    fake_bin("kubectl", ECHO_ARGS)
    _run(["--repo", str(repo_with_configs.root), "k", "exec", "-it", "pod", "--", "sh", "-c", "id"])
    assert "args:exec -it pod -- sh -c id" in capfd.readouterr().out


def test_repo_found_from_cwd(repo_with_configs, fake_bin, capfd, monkeypatch):
    fake_bin("kubectl", ECHO_ARGS)
    monkeypatch.chdir(repo_with_configs.root / "talos")
    assert _run(["k", "version"]) == 3


def test_errors_are_reported_without_traceback(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _run(["k", "get", "pods"]) == 1
    err = capfd.readouterr().err
    assert err.startswith("error: no .talkub.yaml found")
    assert "hint:" in err
    assert "Traceback" not in err


def test_help_lists_commands(capfd):
    assert _run(["--help"]) == 0
    out = capfd.readouterr().out
    for command in ("talosctl", "kubectl", "shell", "sops"):
        assert command in out


def test_sops_decrypt_to_stdout(repo_with_configs, capfd):
    code = _run(["--repo", str(repo_with_configs.root), "sops", "decrypt", str(repo_with_configs.talosconfig)])
    assert code == 0
    assert "talos: marker" in capfd.readouterr().out


def test_shell_command(repo_with_configs, fake_bin, capfd):
    program = fake_bin("fakeshell", "printf 'session:%s\\n' \"$TALKUB_SESSION\"\n")
    assert _run(["--repo", str(repo_with_configs.root), "shell", "--program", str(program)]) == 0
    assert "session:test" in capfd.readouterr().out
