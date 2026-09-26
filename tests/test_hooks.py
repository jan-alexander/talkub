"""Tests for src/talkub/hooks.py: the pre-commit hook install/uninstall."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest
from helpers import need

from talkub import hooks
from talkub.config import Repo
from talkub.core.errors import ValidationError
from talkub.core.runner import Runner
from talkub.lint import DEFAULT_YAMLLINT

pytestmark = need("git")


def _git_repo(tmp_path) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return Repo(root)


def _fake_talkub(directory, exit_code: int):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "fake-talkub"
    path.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


# -- talkub_path --------------------------------------------------------------


def test_talkub_path_resolves_via_which(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "faketalkub"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", ["faketalkub"])
    assert hooks.talkub_path() == str(script.resolve())


def test_talkub_path_falls_back_to_resolved_executable(tmp_path, monkeypatch):
    """Not found via PATH, but exists (relative to cwd) and is executable."""
    script = tmp_path / "talkub-run"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr(sys, "argv", ["talkub-run"])
    assert hooks.talkub_path() == str(script.resolve())


def test_talkub_path_falls_back_to_bare_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "does-not-exist")])
    assert hooks.talkub_path() == "talkub"


# -- render_hook ---------------------------------------------------------------


def test_render_hook_content():
    assert hooks.render_hook("/usr/local/bin/talkub") == (
        "#!/bin/sh\n"
        "# talkub pre-commit hook (managed by talkub; reinstall with `talkub hooks install`)\n"
        "TALKUB=/usr/local/bin/talkub\n"
        '[ -x "$TALKUB" ] || TALKUB=talkub\n'
        'command -v "$TALKUB" >/dev/null 2>&1 || { echo "talkub pre-commit hook: talkub not found; '
        "reinstall with 'talkub hooks install' or commit with --no-verify\" >&2; exit 1; }\n"
        '"$TALKUB" check && "$TALKUB" lint --offline\n'
    )


def test_render_hook_quotes_unsafe_path():
    text = hooks.render_hook("/path with spaces/talkub")
    assert f"TALKUB={shlex.quote('/path with spaces/talkub')}\n" in text
    assert "TALKUB='/path with spaces/talkub'\n" in text


# -- hook_path ------------------------------------------------------------------


def test_hook_path_in_real_repo(tmp_path):
    repo = _git_repo(tmp_path)
    assert (
        hooks.hook_path(repo, Runner()) == repo.root / ".git" / "hooks" / "pre-commit"
    )


def test_hook_path_not_a_git_repo(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()
    with pytest.raises(ValidationError) as exc:
        hooks.hook_path(Repo(root), Runner())
    assert exc.value.hint == "git init"


def test_hook_path_in_linked_worktree_uses_common_hooks_dir(tmp_path):
    """A linked worktree has no .git/hooks of its own; hooks live in the main repo's git dir."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "x",
        ],
        cwd=main,
        check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree)], cwd=main, check=True
    )

    assert (
        hooks.hook_path(Repo(worktree), Runner())
        == main / ".git" / "hooks" / "pre-commit"
    )


def test_hook_path_honors_relative_core_hooks_path(tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(
        ["git", "config", "core.hooksPath", "custom-hooks"], cwd=repo.root, check=True
    )
    assert hooks.hook_path(repo, Runner()) == repo.root / "custom-hooks" / "pre-commit"


# -- install ----------------------------------------------------------------


def test_install_writes_executable_hook_and_yamllint(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    fake = _fake_talkub(tmp_path / "fake", 0)
    monkeypatch.setattr(sys, "argv", [str(fake)])

    notes = hooks.install(repo, Runner())

    path = hooks.hook_path(repo, Runner())
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o755
    content = path.read_text()
    assert hooks.MARKER in content
    assert shlex.quote(str(fake.resolve())) in content
    assert (repo.root / ".yamllint").read_text() == DEFAULT_YAMLLINT
    assert notes == ["wrote .yamllint — commit it"]


def test_install_leaves_existing_yamllint_and_no_note(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo.root / ".yamllint").write_text("custom: true\n")
    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake", 0))])

    notes = hooks.install(repo, Runner())

    assert notes == []
    assert (repo.root / ".yamllint").read_text() == "custom: true\n"


def test_install_overwrites_own_marked_hook(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake1", 0))])
    hooks.install(repo, Runner())
    path = hooks.hook_path(repo, Runner())
    first = path.read_text()

    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake2", 0))])
    hooks.install(repo, Runner())
    second = path.read_text()

    assert first != second
    assert hooks.MARKER in second


def test_install_refuses_foreign_hook_without_force(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    path = hooks.hook_path(repo, Runner())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho someone else's hook\n")
    path.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake", 0))])

    with pytest.raises(ValidationError) as exc:
        hooks.install(repo, Runner())
    assert "already exists" in str(exc.value)
    assert exc.value.hint == "talkub hooks install --force"
    assert path.read_text() == "#!/bin/sh\necho someone else's hook\n"


def test_install_force_overwrites_foreign_hook(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    path = hooks.hook_path(repo, Runner())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho someone else's hook\n")
    path.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake", 0))])

    hooks.install(repo, Runner(), force=True)
    assert hooks.MARKER in path.read_text()


# -- uninstall ----------------------------------------------------------------


def test_uninstall_removes_own_hook(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(_fake_talkub(tmp_path / "fake", 0))])
    hooks.install(repo, Runner())
    path = hooks.hook_path(repo, Runner())

    removed = hooks.uninstall(repo, Runner())
    assert removed == path
    assert not path.exists()


def test_uninstall_refuses_foreign_hook(tmp_path):
    repo = _git_repo(tmp_path)
    path = hooks.hook_path(repo, Runner())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho foreign\n")
    path.chmod(0o755)

    with pytest.raises(ValidationError):
        hooks.uninstall(repo, Runner())
    assert path.exists()


def test_uninstall_without_hook_fails(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValidationError):
        hooks.uninstall(repo, Runner())


# -- real commit --------------------------------------------------------------


def test_real_commit_is_blocked_then_allowed_by_installed_hook(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    fake = _fake_talkub(tmp_path / "fake", 1)
    monkeypatch.setattr(sys, "argv", [str(fake)])
    hooks.install(repo, Runner())

    (repo.root / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo.root, check=True)
    blocked = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-m",
            "x",
        ],
        cwd=repo.root,
        capture_output=True,
    )
    assert blocked.returncode != 0

    fake.write_text("#!/bin/sh\nexit 0\n")
    allowed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-m",
            "x",
        ],
        cwd=repo.root,
        capture_output=True,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_real_commit_reports_missing_talkub_when_stored_path_and_path_both_lack_it(
    tmp_path, monkeypatch
):
    repo = _git_repo(tmp_path)
    fake = _fake_talkub(tmp_path / "fake", 0)
    monkeypatch.setattr(sys, "argv", [str(fake)])
    hooks.install(repo, Runner())
    fake.unlink()  # the stored path is gone, and this PATH has no bare `talkub` either

    (repo.root / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo.root, check=True)
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-m",
            "x",
        ],
        cwd=repo.root,
        capture_output=True,
        env=env,
    )
    assert result.returncode != 0
    assert (
        "talkub pre-commit hook: talkub not found; reinstall with 'talkub hooks install' or commit with --no-verify"
        in result.stderr.decode()
    )
