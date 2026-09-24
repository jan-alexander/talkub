import base64
import subprocess

import pytest

from helpers import need
from talkub import gitrepo, yamlio
from talkub.context import Context
from talkub.errors import CommandFailed, ValidationError
from talkub.gitrepo import classify_url

pytestmark = need("age", "age-keygen", "sops", "ssh-keygen")

VALUES = "# Helm values for Argo CD\ndex:\n  enabled: false\n"


def _host_line(tmp_path, host="[git.example.com]:2222"):
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    kind, blob = key.with_suffix(".pub").read_text().split()[:2]
    return f"{host} {kind} {blob}"


def _secret(ctx):
    return yamlio.load(ctx.sops.decrypt(gitrepo.credentials_path(ctx.repo)).decode())


def _b64(value):
    return base64.b64decode(value).decode()


@pytest.fixture
def ctx(repo_with_configs):
    (repo_with_configs.gitops_dir / "values").mkdir(parents=True, exist_ok=True)
    (repo_with_configs.gitops_dir / "values" / "argocd.yaml").write_text(VALUES)
    return Context.open(repo_with_configs)


def test_build_secret_shape():
    doc = yamlio.load(gitrepo.build_secret("homelab", "https://h/x.git", username="u", password="p").decode())
    assert doc["kind"] == "Secret" and doc["metadata"]["name"] == "repo-homelab"
    assert doc["metadata"]["namespace"] == "argocd"
    assert doc["metadata"]["labels"] == {"argocd.argoproj.io/secret-type": "repository"}
    assert {k: _b64(v) for k, v in doc["data"].items()} == {
        "type": "git", "url": "https://h/x.git", "username": "u", "password": "p",
    }
    assert gitrepo.credentials_kind(gitrepo.build_secret("c", "u", username="u", password="p")) == "https"
    assert gitrepo.credentials_kind(gitrepo.build_secret("c", "u", ssh_private_key="k")) == "ssh"


@pytest.mark.parametrize(("username", "password"), [("", "p"), ("u", ""), ("u\nx", "p"), ("u", "p\n")])
def test_build_secret_rejects_empty_or_multiline_https_values(username, password):
    with pytest.raises(ValidationError):
        gitrepo.build_secret("c", "https://h/x.git", username=username, password=password)


def test_replace_secret_url():
    manifest = gitrepo.build_secret("c", "git@a:x.git", ssh_private_key="KEY\n")
    doc = yamlio.load(gitrepo.replace_secret_url(manifest, "git@b:y.git").decode())
    assert _b64(doc["data"]["url"]) == "git@b:y.git" and _b64(doc["data"]["sshPrivateKey"]) == "KEY\n"


def test_needs_known_hosts():
    assert not gitrepo.needs_known_hosts(classify_url("git@github.com:me/x.git"))
    assert gitrepo.needs_known_hosts(classify_url("ssh://git@github.com:2222/me/x.git"))
    assert gitrepo.needs_known_hosts(classify_url("git@git.example.com:me/x.git"))
    assert not gitrepo.needs_known_hosts(classify_url("https://git.example.com/me/x.git"))


def test_fingerprint_matches_ssh_keygen(tmp_path):
    line = _host_line(tmp_path, host="git.example.com")
    (tmp_path / "kh").write_text(line + "\n")
    expected = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "kh")], capture_output=True, text=True, check=True)
    assert gitrepo.fingerprint(line).split()[1] in expected.stdout


def test_ssh_credentials_for_github_skip_keyscan(ctx, spy):
    keyscan = spy("ssh-keyscan")
    result = gitrepo.create_ssh_credentials(ctx, "test", classify_url("git@github.com:me/x.git"), confirm=lambda q: False)
    assert keyscan.calls == []
    assert result.public_key.startswith("ssh-ed25519 ") and result.public_key.endswith(" talkub-test")
    assert not result.known_hosts_written
    secret = _secret(ctx)
    assert _b64(secret["data"]["url"]) == "git@github.com:me/x.git"
    assert "OPENSSH PRIVATE KEY" in _b64(secret["data"]["sshPrivateKey"])
    for path in ctx.repo.root.rglob("*"):
        if path.is_file():
            assert b"PRIVATE KEY" not in path.read_bytes(), path  # only ciphertext on disk


def test_ssh_credentials_custom_host_confirmed_adds_extra_hosts_once(ctx, spy, tmp_path):
    line = _host_line(tmp_path)
    spy("ssh-keyscan").on("-p", "2222", "git.example.com", stdout=f"# git.example.com:2222 SSH-2.0\n{line}\n")
    url = classify_url("ssh://git@git.example.com:2222/me/x.git")
    questions = []
    result = gitrepo.create_ssh_credentials(ctx, "test", url, confirm=lambda q: questions.append(q) or True)
    assert result.known_hosts_written
    assert gitrepo.fingerprint(line) in questions[0]
    values_path = ctx.repo.gitops_dir / "values" / "argocd.yaml"
    values = yamlio.load(values_path.read_text())
    assert values["configs"]["ssh"]["extraHosts"].splitlines() == [line]
    assert values_path.read_text().startswith("# Helm values for Argo CD\n")  # comment kept
    again = gitrepo.create_ssh_credentials(ctx, "test", url, confirm=lambda q: True)
    assert not again.known_hosts_written
    assert yamlio.load(values_path.read_text())["configs"]["ssh"]["extraHosts"].splitlines() == [line]


def test_ssh_credentials_declined_host_keys_write_nothing(ctx, spy, tmp_path):
    spy("ssh-keyscan").on("git.example.com", stdout=_host_line(tmp_path, host="git.example.com") + "\n")
    with pytest.raises(ValidationError):
        gitrepo.create_ssh_credentials(ctx, "test", classify_url("git@git.example.com:me/x.git"), confirm=lambda q: False)
    assert not gitrepo.credentials_path(ctx.repo).exists()
    assert (ctx.repo.gitops_dir / "values" / "argocd.yaml").read_text() == VALUES


def test_ssh_credentials_unreachable_host_fails_before_keygen(ctx, spy):
    spy("ssh-keyscan")  # prints nothing, exit 0 (like a real unreachable host)
    keygen = spy("ssh-keygen")
    with pytest.raises(CommandFailed) as exc:
        gitrepo.create_ssh_credentials(ctx, "test", classify_url("git@git.example.com:me/x.git"), confirm=lambda q: True)
    assert "git.example.com" in str(exc.value)
    assert keygen.calls == []
    assert not gitrepo.credentials_path(ctx.repo).exists()


def test_https_credentials(ctx):
    path = gitrepo.create_https_credentials(ctx, "test", classify_url("https://h.example/x.git"), "jam", "tok-123")
    assert path == gitrepo.credentials_path(ctx.repo)
    assert b"tok-123" not in path.read_bytes()
    data = _secret(ctx)["data"]
    assert (_b64(data["username"]), _b64(data["password"])) == ("jam", "tok-123")
