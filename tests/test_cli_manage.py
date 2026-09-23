import os

import pytest

from helpers import need
from talkub.cli.main import main
from talkub.identity import identities_dir

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"
OTHER_KEY = "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@need("age-keygen")
def test_identity_new_software(capfd):
    assert _run(["identity", "new", "--software"]) == 0
    out = capfd.readouterr().out
    assert "age1" in out
    assert (identities_dir(os.environ) / "software.txt").is_file()


def test_identity_new_rejects_two_kinds(capfd):
    assert _run(["identity", "new", "--software", "--yubikey"]) == 1
    assert "only one" in capfd.readouterr().err


@need("age", "age-keygen", "sops", "talosctl")
def test_init_then_admin_and_check(tmp_path, software_identity, fake_bin, capfd, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    code = _run([
        "init", "demo",
        "--endpoint", "https://10.0.0.10:6443",
        "--node", "cp-01=10.0.0.10",
        "--git-url", "https://git.example.com/demo.git",
    ])
    assert code == 0
    assert (tmp_path / "demo" / ".talkub.yaml").is_file()
    monkeypatch.chdir(tmp_path / "demo")

    assert _run(["check"]) == 0
    assert "warning: only 1 recipient" in capfd.readouterr().out

    assert _run(["admin", "add", OTHER_KEY, "--name", "backup"]) == 0
    assert _run(["admin", "list"]) == 0
    out = capfd.readouterr().out
    assert "backup\tage1qk2vu7" in out

    assert _run(["admin", "remove", "backup"]) == 0
    assert "keys rotate" in capfd.readouterr().out


@need("age", "age-keygen", "sops")
def test_admin_remove_last_fails(keyed_repo, capfd):
    assert _run(["--repo", str(keyed_repo.root), "admin", "remove", "me"]) == 1
    assert "last recipient" in capfd.readouterr().err


@need("age", "age-keygen", "sops")
def test_check_fails_on_error(repo_with_configs, capfd):
    (repo_with_configs.root / "leak.txt").write_text("AGE-SECRET-KEY-1ABC\n")
    assert _run(["--repo", str(repo_with_configs.root), "check"]) == 1
    assert "error: leak.txt contains an age private key" in capfd.readouterr().out


@need("age", "age-keygen", "sops")
def test_keys_rotate_with_yes(repo_with_configs, capfd):
    old = repo_with_configs.master_pub.read_text()
    assert _run(["--repo", str(repo_with_configs.root), "keys", "rotate", "--yes"]) == 0
    assert repo_with_configs.master_pub.read_text() != old


def test_init_rejects_bad_node_syntax(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(["init", "demo", "--endpoint", "https://x:6443", "--node", "cp-01", "--git-url", "https://g"])
    assert code == 1
    assert "name=ip" in capfd.readouterr().err
