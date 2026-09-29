import pytest

from talkub.cli.main import main


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def _complete(monkeypatch, capfd, line):
    """Ask for bash completions of `line` the way the installed script does; return the candidates."""
    words = line.split(" ")
    monkeypatch.setenv("_TALKUB_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", line)
    monkeypatch.setenv("COMP_CWORD", str(len(words) - 1))
    assert _run([]) == 0
    return capfd.readouterr().out.split()


def _with_shortcuts(repo):
    from talkub.config import Shortcut, user

    settings = repo.load_settings()
    settings.shortcuts = {
        "pods": Shortcut(name="pods", kind="run", description="list pods", argv=("k", "get", "pods")),
        "backup": Shortcut(name="backup", kind="job", namespace="prod", cronjob="backup-job"),
    }
    repo.write_settings(settings)
    user.save_global_shortcut(Shortcut(name="nodes", kind="run", argv=("k", "get", "nodes")))


@pytest.mark.parametrize("shell", ["zsh", "bash", "fish"])
def test_completion_prints_script(shell, capfd):
    assert _run(["completion", shell]) == 0
    out = capfd.readouterr().out
    assert "_TALKUB_COMPLETE" in out
    assert f"complete_{shell}" in out


def test_completion_rejects_unknown_shell(capfd):
    assert _run(["completion", "tcsh"]) == 2
    assert "tcsh" in capfd.readouterr().err


def test_completes_subcommands(monkeypatch, capfd, tmp_path):
    monkeypatch.chdir(tmp_path)
    candidates = _complete(monkeypatch, capfd, "talkub co")
    assert {"completion", "coredns"} <= set(candidates)


def test_completes_shortcut_names_from_project_and_global_pool(empty_repo, monkeypatch, capfd):
    _with_shortcuts(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert sorted(_complete(monkeypatch, capfd, "talkub run ")) == ["backup", "nodes", "pods"]


def test_shortcut_names_match_the_prefix(empty_repo, monkeypatch, capfd):
    _with_shortcuts(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, "talkub run po") == ["pods"]


def test_shortcut_names_honour_repo_option(empty_repo, monkeypatch, capfd, tmp_path):
    _with_shortcuts(empty_repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    line = f"talkub --repo {empty_repo.root} run p"
    assert _complete(monkeypatch, capfd, line) == ["pods"]


def test_shortcut_names_outside_a_repo_is_silent(monkeypatch, capfd, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _complete(monkeypatch, capfd, "talkub run ") == []


def test_completes_run_flags(monkeypatch, capfd, tmp_path):
    monkeypatch.chdir(tmp_path)
    candidates = _complete(monkeypatch, capfd, "talkub run --")
    assert {"--print", "--follow", "--save-global", "--force"} <= set(candidates)


def test_shortcut_names_with_broken_global_pool_is_silent(empty_repo, monkeypatch, capfd):
    from talkub.config import user

    path = user.global_shortcuts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("shortcuts: [\n")
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, "talkub run ") == []


def _with_node_shortcut(repo):
    from talkub.config import NodeSpec, Shortcut

    settings = repo.load_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker")
    settings.nodes["cp-02"] = NodeSpec(ip="10.0.0.11")
    settings.shortcuts = {
        "ext": Shortcut(name="ext", kind="run", argv=("t", "-n", "{node}", "get", "extensions")),
        "pods": Shortcut(name="pods", kind="run", argv=("k", "get", "pods")),
    }
    repo.write_settings(settings)


def test_completes_node_names_after_a_node_shortcut(empty_repo, monkeypatch, capfd):
    _with_node_shortcut(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, "talkub run ext ") == ["cp-01", "cp-02", "w-01"]
    assert _complete(monkeypatch, capfd, "talkub run ext cp") == ["cp-01", "cp-02"]


@pytest.mark.parametrize("line", ["talkub run pods ", "talkub run nope "])
def test_no_node_names_for_other_shortcuts(empty_repo, monkeypatch, capfd, line):
    _with_node_shortcut(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, line) == []


def test_no_node_names_outside_a_repo(monkeypatch, capfd, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _complete(monkeypatch, capfd, "talkub run ext ") == []


def test_run_usage_mentions_the_node():
    from talkub.cli.main import RUN_USAGE

    assert "<name> [<node>] [args...]" in RUN_USAGE
