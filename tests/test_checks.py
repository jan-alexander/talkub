from helpers import need

from talkub.context import Context
from talkub.core.runner import Runner
from talkub.quality.checks import run_checks
from talkub.secrets import age
from talkub.secrets.keyring import Keyring
from talkub.secrets.recipients import Recipient
from talkub.secrets.sops import render_sops_config

pytestmark = need("age", "age-keygen", "sops")


def _messages(repo, level):
    return [f.message for f in run_checks(repo) if f.level == level]


def test_clean_repo_has_only_single_recipient_warning(repo_with_configs):
    assert _messages(repo_with_configs, "error") == []
    assert any("recipient" in m for m in _messages(repo_with_configs, "warning"))


def test_two_recipients_no_warning(repo_with_configs):
    Keyring(repo_with_configs, Runner()).add(
        Recipient(
            "backup", "age1qk2vu7xxezxcfhyhasnx8c7z5j479xwevu5tqq8tc2fjqjzdegjqvepymq"
        )
    )
    assert _messages(repo_with_configs, "warning") == []


def test_unencrypted_sops_file(repo_with_configs):
    (repo_with_configs.root / "talos" / "extra.sops.yaml").write_text(
        "password: hunter2\n"
    )
    assert any("extra.sops.yaml" in m for m in _messages(repo_with_configs, "error"))


def test_plain_secret_in_gitops(repo_with_configs):
    path = repo_with_configs.gitops_dir / "manifests" / "app" / "secret.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "apiVersion: v1\nkind: ConfigMap\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n"
    )
    assert any(
        "secret.yaml" in m and "Secret" in m
        for m in _messages(repo_with_configs, "error")
    )


def test_age_secret_key_anywhere(repo_with_configs):
    (repo_with_configs.root / "notes.txt").write_text("AGE-SECRET-KEY-1ABC\n")
    assert any("notes.txt" in m for m in _messages(repo_with_configs, "error"))


def test_known_plaintext_names(repo_with_configs):
    (repo_with_configs.root / "kubernetes" / "kubeconfig").write_text("x")
    assert any(
        "kubernetes/kubeconfig" in m for m in _messages(repo_with_configs, "error")
    )


def test_sops_file_not_encrypted_to_master_pub(repo_with_configs):
    other = age.generate(Runner()).public
    path = repo_with_configs.root / "talos" / "extra.sops.yaml"
    path.write_bytes(
        Context.open(repo_with_configs).sops.encrypt(
            b"a: 1\n", filename=path.name, public_key=other
        )
    )
    assert "talos/extra.sops.yaml is not encrypted to keys/master.pub" in _messages(
        repo_with_configs, "error"
    )


def test_sops_config_without_master_pub(repo_with_configs):
    other = age.generate(Runner()).public
    repo_with_configs.sops_config.write_text(render_sops_config(other))
    assert any(
        ".sops.yaml" in m and "keys/master.pub" in m
        for m in _messages(repo_with_configs, "error")
    )


def test_missing_sops_config(repo_with_configs):
    repo_with_configs.sops_config.unlink()
    assert any(".sops.yaml" in m for m in _messages(repo_with_configs, "error"))


def test_interrupted_rotation_warned(repo_with_configs):
    repo_with_configs.master_next_age.write_text("x")
    assert any("master.next.age" in m for m in _messages(repo_with_configs, "warning"))


def test_plaintext_repo_secret_in_bootstrap_is_an_error(repo_with_configs):
    from talkub.gitops import gitrepo

    target = repo_with_configs.root / "bootstrap" / "repo.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(
        gitrepo.build_secret("demo", "git@github.com:me/x.git", ssh_private_key="K\n")
    )
    messages = [f.message for f in run_checks(repo_with_configs)]
    assert any("bootstrap/repo.yaml" in m and "plaintext" in m for m in messages)
