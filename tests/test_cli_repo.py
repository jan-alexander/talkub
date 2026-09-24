from string import Template

import pytest

from helpers import need
from talkub import gitrepo
from talkub.cli.main import main
from talkub.context import Context
from talkub.fsutil import atomic_write
from talkub.scaffold import TEMPLATES

pytestmark = need("age", "age-keygen", "sops", "ssh-keygen")

PLACEHOLDER = "https://example.invalid/CHANGE-ME/test.git"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def repo(repo_with_configs):
    for rel in ("gitops/root.yaml", "gitops/apps/argocd.yaml", "gitops/apps/sealed-secrets.yaml", "gitops/values/argocd.yaml"):
        template = (TEMPLATES / "repo" / (rel + ".tmpl")) if (TEMPLATES / "repo" / (rel + ".tmpl")).is_file() else (TEMPLATES / "repo" / rel)
        text = Template(template.read_text()).safe_substitute({"git_url": PLACEHOLDER, "name": "test"})
        atomic_write(repo_with_configs.root / rel, text.encode())
    return repo_with_configs


def test_set_url_prints_files_and_next_step(repo, capfd):
    assert _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"]) == 0
    out = capfd.readouterr().out
    assert "updated gitops/root.yaml" in out and "talkub repo apply" in out
    assert _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"]) == 0
    assert "already set" in capfd.readouterr().out


def test_credentials_needs_git_url(repo, capfd):
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 1
    assert "talkub repo set-url" in capfd.readouterr().err


def test_ssh_credentials_print_public_key(repo, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    capfd.readouterr()
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 0
    out = capfd.readouterr().out
    assert "ssh-ed25519 " in out and "deploy key" in out and "PRIVATE" not in out


def test_https_credentials_from_token_file(repo, tmp_path, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "https://git.example.com/me/test.git"])
    token = tmp_path / "token"
    token.write_text("tok-abc\n")
    code = _run(["--repo", str(repo.root), "repo", "credentials", "--username", "jam", "--token-file", str(token)])
    assert code == 0
    assert "tok-abc" not in capfd.readouterr().out
    assert gitrepo.credentials_path(repo).is_file()


def test_ssh_url_rejects_https_options(repo, tmp_path, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    assert _run(["--repo", str(repo.root), "repo", "credentials", "--username", "jam"]) == 1


def test_existing_credentials_ask_before_replacing(repo, monkeypatch, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    _run(["--repo", str(repo.root), "repo", "credentials"])
    before = gitrepo.credentials_path(repo).read_bytes()
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    assert _run(["--repo", str(repo.root), "repo", "credentials"]) == 1
    assert gitrepo.credentials_path(repo).read_bytes() == before


def test_remove_credentials(repo, capfd):
    _run(["--repo", str(repo.root), "repo", "set-url", "git@github.com:me/test.git"])
    _run(["--repo", str(repo.root), "repo", "credentials"])
    capfd.readouterr()
    assert _run(["--repo", str(repo.root), "repo", "credentials", "--remove"]) == 0
    assert not gitrepo.credentials_path(repo).exists()
    assert "delete secret repo-test" in capfd.readouterr().out


def test_apply_warns_about_placeholder(repo, spy, capfd):
    kubectl = spy("kubectl")
    assert _run(["--repo", str(repo.root), "repo", "apply"]) == 0
    assert "placeholder" in capfd.readouterr().err
    assert kubectl.argvs == [["apply", "-f", str(repo.gitops_dir / "root.yaml")]]
