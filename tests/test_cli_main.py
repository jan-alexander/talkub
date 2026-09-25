import pytest

from helpers import need
from talkub import yamlio
from talkub.cli.main import PASSTHROUGH, main, split_globals
from talkub.context import Context

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


def test_ctrl_c_exits_130_without_traceback(repo_with_configs, capfd, monkeypatch):
    def interrupted(ctx, args):
        raise KeyboardInterrupt

    monkeypatch.setitem(PASSTHROUGH, "k", interrupted)
    assert _run(["--repo", str(repo_with_configs.root), "k", "get", "pods"]) == 130
    assert "Traceback" not in capfd.readouterr().err


def _editor(fake_bin, monkeypatch, tmp_path, body):
    """A fake $EDITOR that records whether it saw the master key, then runs `body`."""
    seen = tmp_path / "editor-saw-key.txt"
    editor = fake_bin("fake-editor", f'printf "%s" "${{SOPS_AGE_KEY:-absent}}" > {seen}\n{body}')
    monkeypatch.delenv("SOPS_EDITOR", raising=False)
    monkeypatch.setenv("EDITOR", str(editor))
    return seen


def test_sops_edit_hides_key_from_editor_and_reencrypts(repo_with_configs, fake_bin, monkeypatch, tmp_path):
    ctx = Context.open(repo_with_configs)
    # Even a master key given via the environment must not reach the editor.
    monkeypatch.setenv("SOPS_AGE_KEY", ctx.keyring.master_key())
    seen = _editor(fake_bin, monkeypatch, tmp_path, 'printf "added: yes\\n" >> "$1"\n')
    path = repo_with_configs.talosconfig
    assert _run(["--repo", str(repo_with_configs.root), "sops", "edit", str(path)]) == 0
    assert seen.read_text() == "absent"
    assert "sops" in yamlio.load(path.read_text())
    assert yamlio.load(Context.open(repo_with_configs).sops.decrypt(path).decode()) == {
        "talos": "marker",
        "added": "yes",
    }


def test_sops_edit_unchanged_exits_zero_and_keeps_file(repo_with_configs, fake_bin, monkeypatch, tmp_path):
    _editor(fake_bin, monkeypatch, tmp_path, "exit 0\n")
    path = repo_with_configs.talosconfig
    before = path.read_bytes()
    assert _run(["--repo", str(repo_with_configs.root), "sops", "edit", str(path)]) == 0
    assert path.read_bytes() == before
def _with_shortcuts(repo):
    from talkub.repo import Shortcut

    settings = repo.load_settings()
    settings.shortcuts = {
        "pods": Shortcut(name="pods", kind="run", description="list pods", argv=("k", "get", "pods")),
        "backup": Shortcut(name="backup", kind="job", namespace="prod", cronjob="backup-job"),
    }
    repo.write_settings(settings)


def test_run_without_name_lists_shortcuts(repo_with_configs, capfd):
    _with_shortcuts(repo_with_configs)
    assert _run(["--repo", str(repo_with_configs.root), "run"]) == 0
    lines = capfd.readouterr().out.splitlines()
    assert any(line.split() == ["pods", "run", "list", "pods"] for line in lines)
    assert any(line.split() == ["backup", "job"] for line in lines)


def test_run_without_shortcuts_says_so(repo_with_configs, capfd):
    assert _run(["--repo", str(repo_with_configs.root), "run"]) == 0
    assert "no shortcuts defined in .talkub.yaml" in capfd.readouterr().out


def test_run_unknown_name_is_an_error(repo_with_configs, capfd):
    _with_shortcuts(repo_with_configs)
    assert _run(["--repo", str(repo_with_configs.root), "run", "nope"]) == 1
    err = capfd.readouterr().err
    assert "unknown shortcut 'nope'" in err and "backup" in err


def test_run_passes_dash_f_after_the_name_through(repo_with_configs, fake_bin, capfd):
    _with_shortcuts(repo_with_configs)
    fake_bin("kubectl", ECHO_ARGS)
    assert _run(["--repo", str(repo_with_configs.root), "run", "pods", "-f", "--follow", "x"]) == 3
    assert "args:get pods -f --follow x" in capfd.readouterr().out


def test_run_flags_before_the_name(repo_with_configs, monkeypatch):
    from talkub import shortcuts

    seen = []
    monkeypatch.setattr(
        shortcuts, "run", lambda ctx, name, extra, *, print_value=False, follow=False: seen.append((name, extra, print_value, follow)) or 0
    )
    assert _run(["--repo", str(repo_with_configs.root), "run", "--follow", "--print", "creds", "--print"]) == 0
    assert seen == [("creds", ["--print"], True, True)]


def test_run_help(capfd):
    assert _run(["run", "--help"]) == 0
    out = capfd.readouterr().out
    assert "talkub run [--print] [--follow] <name>" in out


def test_top_level_help_mentions_run(capfd):
    assert _run(["--help"]) == 0
    assert "Run a shortcut" in capfd.readouterr().out
