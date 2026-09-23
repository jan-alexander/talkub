import pytest

from helpers import need
from talkub import yamlio
from talkub.context import Context
from talkub.crypto.sops import Sops, render_sops_config
from talkub.errors import DecryptFailed
from talkub.runner import Runner

pytestmark = need("age", "age-keygen", "sops")


def test_encrypt_decrypt_roundtrip(keyed_repo, tmp_path):
    ctx = Context.open(keyed_repo)
    ciphertext = ctx.sops.encrypt(b"a: 1\nb: secret\n", filename="x.sops.yaml", public_key=ctx.keyring.public_key())
    assert "sops" in yamlio.load(ciphertext.decode())
    assert b"secret" not in ciphertext
    path = tmp_path / "x.sops.yaml"
    path.write_bytes(ciphertext)
    assert yamlio.load(ctx.sops.decrypt(path).decode()) == {"a": 1, "b": "secret"}


def test_encrypt_needs_no_private_key(keyed_repo):
    def no_key():
        raise AssertionError("encrypt must not resolve the master key")

    public = Context.open(keyed_repo).keyring.public_key()
    assert Sops(Runner(), no_key).encrypt(b"a: 1\n", filename="x.sops.yaml", public_key=public)


def test_decrypt_with_wrong_key(keyed_repo, tmp_path):
    ctx = Context.open(keyed_repo)
    path = tmp_path / "x.sops.yaml"
    path.write_bytes(ctx.sops.encrypt(b"a: 1\n", filename=path.name, public_key=ctx.keyring.public_key()))
    wrong = "AGE-SECRET-KEY-133FP6CADJCE6M9H6AUXY3NLXNX73NSSLQS0CHGVLPSVZKK87V80SPY0R56"
    with pytest.raises(DecryptFailed):
        Sops(Runner(), lambda: wrong).decrypt(path)


def _use_editor(fake_bin, monkeypatch, body):
    monkeypatch.delenv("SOPS_EDITOR", raising=False)
    monkeypatch.setenv("EDITOR", str(fake_bin("fake-editor", body)))


def test_edit_creates_missing_file(keyed_repo, fake_bin, monkeypatch):
    _use_editor(fake_bin, monkeypatch, 'printf "token: abc\\n" > "$1"\n')
    ctx = Context.open(keyed_repo)
    path = keyed_repo.root / "kubernetes" / "kubeconfig.sops.yaml"
    assert ctx.sops.edit(path, ctx.keyring.public_key()) == 0
    assert "sops" in yamlio.load(path.read_text())
    assert yamlio.load(ctx.sops.decrypt(path).decode()) == {"token": "abc"}


def test_edit_failed_editor_writes_nothing(keyed_repo, fake_bin, monkeypatch, tmp_path):
    _use_editor(fake_bin, monkeypatch, 'printf "x: 1\\n" > "$1"\nexit 3\n')
    ctx = Context.open(keyed_repo)
    path = tmp_path / "new.sops.yaml"
    assert ctx.sops.edit(path, ctx.keyring.public_key()) == 3
    assert not path.exists()


def test_edit_prefers_sops_editor_with_arguments(keyed_repo, fake_bin, monkeypatch, tmp_path):
    fake_bin("fake-editor", 'printf "flag: %s\\n" "$1" > "$2"\n')
    monkeypatch.setenv("EDITOR", "vi-must-not-run")
    monkeypatch.setenv("SOPS_EDITOR", "fake-editor --wait")
    ctx = Context.open(keyed_repo)
    path = tmp_path / "x.sops.yaml"
    assert ctx.sops.edit(path, ctx.keyring.public_key()) == 0
    assert yamlio.load(ctx.sops.decrypt(path).decode()) == {"flag": "--wait"}


def test_render_sops_config():
    text = render_sops_config("age1abc")
    assert yamlio.load(text) == {"creation_rules": [{"path_regex": r"\.sops\.yaml$", "age": "age1abc"}]}
