import pytest

from helpers import need
from talkub import sealing
from talkub.cli.main import main
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops", "kubeseal")


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_seal_from_literal(keyed_repo, sealing_cert, tmp_path):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    out = keyed_repo.root / "gitops" / "manifests" / "app" / "db.sealed.yaml"
    code = _run(["--repo", str(keyed_repo.root), "secret", "seal", "db", "-n", "app",
                 "--from-literal", "password=hunter2", "-o", str(out)])
    assert code == 0
    assert b"kind: SealedSecret" in out.read_bytes()


def test_seal_refuses_manifest_inside_repo(keyed_repo, sealing_cert, capfd):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    manifest = keyed_repo.root / "secret.yaml"
    manifest.write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: db, namespace: app}\ndata: {a: Yg==}\n")
    code = _run(["--repo", str(keyed_repo.root), "secret", "seal", str(manifest), "-o", str(keyed_repo.root / "db.sealed.yaml")])
    assert code == 1
    assert "inside the cluster repo" in capfd.readouterr().err


def test_seal_from_prompt(keyed_repo, sealing_cert, tmp_path, monkeypatch):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2")
    out = tmp_path / "db.sealed.yaml"
    code = _run(["--repo", str(keyed_repo.root), "secret", "seal", "db", "-n", "app",
                 "--prompt", "password", "-o", str(out)])
    assert code == 0
    sealed = out.read_bytes()
    assert b"kind: SealedSecret" in sealed
    assert b"hunter2" not in sealed


def test_seal_refuses_from_file_inside_repo(keyed_repo, sealing_cert, capfd, tmp_path):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    secret_file = keyed_repo.root / "token.txt"
    secret_file.write_text("s3cret")
    code = _run(["--repo", str(keyed_repo.root), "secret", "seal", "db", "-n", "app",
                 "--from-file", f"token={secret_file}", "-o", str(tmp_path / "db.sealed.yaml")])
    assert code == 1
    assert "inside the cluster repo" in capfd.readouterr().err


def test_seal_manifest_from_outside(keyed_repo, sealing_cert, tmp_path):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    manifest = tmp_path / "secret.yaml"
    manifest.write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: db, namespace: app}\ndata: {a: Yg==}\n")
    out = tmp_path / "db.sealed.yaml"
    assert _run(["--repo", str(keyed_repo.root), "secret", "seal", str(manifest), "-o", str(out)]) == 0
    assert out.exists()


@pytest.mark.parametrize(
    "extra, message",
    [
        (["db", "--from-literal", "a=b", "-o", "x.sealed.yaml"], "--namespace"),
        (["db", "-n", "app", "--from-literal", "a=b", "-o", "x.yaml"], ".sealed.yaml"),
    ],
)
def test_seal_argument_errors(keyed_repo, sealing_cert, capfd, extra, message):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    assert _run(["--repo", str(keyed_repo.root), "secret", "seal", *extra]) == 1
    assert message in capfd.readouterr().err
