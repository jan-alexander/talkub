import pytest

from helpers import need
from talkub.context import Context
from talkub.errors import ToolMissing, ValidationError
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops")

# First run writes broken YAML, every later run writes valid YAML; counts runs in a file.
EDITOR = """n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
if [ "$n" = 1 ]; then printf 'a: [unclosed\\n' > "$1"; else printf 'a: fixed\\n' > "$1"; fi
"""


@pytest.fixture
def secret(keyed_repo):
    ctx = Context.open(keyed_repo)
    path = keyed_repo.root / "x.sops.yaml"
    atomic_write(path, ctx.sops.encrypt(b"a: 1\n", filename=path.name, public_key=ctx.keyring.public_key()))
    return ctx, path


def _editor(fake_bin, tmp_path, monkeypatch):
    fake_bin("fake-editor", EDITOR.format(counter=tmp_path / "count"))
    monkeypatch.setenv("EDITOR", "fake-editor")
    return tmp_path / "count"


def test_invalid_yaml_reopens_editor(secret, fake_bin, tmp_path, monkeypatch):
    ctx, path = secret
    counter = _editor(fake_bin, tmp_path, monkeypatch)
    questions = []
    code = ctx.sops.edit(path, ctx.keyring.public_key(), retry=lambda q: questions.append(q) or True)
    assert code == 0
    assert counter.read_text().strip() == "2"
    assert "not valid YAML" in questions[0]
    assert ctx.sops.decrypt(path) == b"a: fixed\n"


def test_declined_reopen_writes_nothing(secret, fake_bin, tmp_path, monkeypatch, capfd):
    import tempfile

    ctx, path = secret
    _editor(fake_bin, tmp_path, monkeypatch)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    before = path.read_bytes()
    assert ctx.sops.edit(path, ctx.keyring.public_key(), retry=lambda q: False) == 1
    assert path.read_bytes() == before
    assert "discarded" in capfd.readouterr().err
    assert list(temp.iterdir()) == []  # the decrypted plaintext's talkub-* directory is gone


@pytest.mark.parametrize("variable", ["SOPS_EDITOR", "EDITOR"])
def test_blank_editor_is_reported(secret, monkeypatch, variable):
    ctx, path = secret
    monkeypatch.delenv("SOPS_EDITOR", raising=False)
    monkeypatch.setenv(variable, "   ")
    with pytest.raises(ValidationError) as exc:
        ctx.sops.edit(path, ctx.keyring.public_key())
    assert "$SOPS_EDITOR/$EDITOR is empty" in str(exc.value)


def test_missing_editor_is_reported_before_decrypting(keyed_repo, monkeypatch):
    ctx = Context.open(keyed_repo)
    path = keyed_repo.root / "broken.sops.yaml"
    path.write_text("not: encrypted\n")  # decrypting this would fail with DecryptFailed
    monkeypatch.setenv("EDITOR", "definitely-not-an-editor-xyz")
    with pytest.raises(ToolMissing):
        ctx.sops.edit(path, ctx.keyring.public_key())


def test_cli_declined_reopen_exits_nonzero(secret, fake_bin, tmp_path, monkeypatch):
    import io
    import sys

    from talkub.cli.main import main

    ctx, path = secret
    _editor(fake_bin, tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ctx.repo.root), "sops", "edit", str(path)])
    assert exc.value.code == 1
