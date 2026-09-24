import pytest

from helpers import need
from talkub import tailscale
from talkub.cli.main import main
from talkub.context import Context
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops")

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"
TALOSCONFIG = b"context: test\ncontexts:\n    test:\n        endpoints: [10.0.0.10]\n        nodes: [10.0.0.10]\n"
KEY = "tskey-auth-fromfile"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def repo(repo_with_configs):
    ctx = Context.open(repo_with_configs)
    atomic_write(
        repo_with_configs.talosconfig,
        ctx.sops.encrypt(TALOSCONFIG, filename="talosconfig.sops.yaml", public_key=ctx.keyring.public_key()),
    )
    repo_with_configs.kubeconfig.unlink()
    return repo_with_configs


def test_enable_with_key_file_then_lockdown(repo, tmp_path, capfd):
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n")
    assert _run(["--repo", str(repo.root), "tailscale", "enable", "--tailnet", "t.ts.net", "--authkey-file", str(key_file)]) == 0
    assert "note:" in capfd.readouterr().out
    assert _run(["--repo", str(repo.root), "tailscale", "lockdown"]) == 0
    assert tailscale.is_locked_down(repo)
    assert repo.load_settings().nodes["cp-01"].mgmt == "cp-01.t.ts.net"


def test_enable_prompts_for_the_key(repo, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": KEY)
    assert _run(["--repo", str(repo.root), "tailscale", "enable", "--tailnet", "t.ts.net"]) == 0
    assert KEY.encode() in Context.open(repo).sops.decrypt(tailscale.extension_patch_path(repo))


def test_enable_rejects_bad_key_file(repo, tmp_path, capfd):
    assert _run(["--repo", str(repo.root), "tailscale", "enable", "--tailnet", "t.ts.net",
                 "--authkey-file", str(tmp_path / "missing")]) == 1
    assert "cannot read" in capfd.readouterr().err


def test_init_tailscale_needs_tailnet(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(["init", "demo", "--endpoint", "https://10.0.0.10:6443", "--git-url", "https://g",
                 "--node", "cp-01=10.0.0.10", "--tailscale"])
    assert code == 1
    assert "--tailnet" in capfd.readouterr().err
    assert not (tmp_path / "demo").exists()


@need("talosctl")
def test_init_with_tailscale(tmp_path, software_identity, fake_bin, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    code = _run(["init", "demo", "--endpoint", "https://10.0.0.10:6443", "--git-url", "https://g",
                 "--node", "cp-01=10.0.0.10", "--tailscale", "--tailnet", "t.ts.net",
                 "--tailscale-authkey-file", str(key_file)])
    assert code == 0
    from talkub.repo import Repo

    repo = Repo((tmp_path / "demo").resolve())
    settings = repo.load_settings()
    assert settings.endpoint == "https://10.0.0.10:6443"
    assert settings.nodes["cp-01"].mgmt == "cp-01.t.ts.net"
    assert tailscale.is_locked_down(repo)
    assert tailscale.EXTENSION in settings.schematics["metal"].extensions
