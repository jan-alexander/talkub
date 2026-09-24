import base64

import pytest

from helpers import need
from talkub import sealing, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write

KEY_LIST = """apiVersion: v1
kind: List
items:
- apiVersion: v1
  kind: Secret
  type: kubernetes.io/tls
  metadata:
    name: sealed-secrets-keyabcde
    namespace: kube-system
    labels:
      sealedsecrets.bitnami.com/sealed-secrets-key: active
    resourceVersion: "123"
    uid: 0000-1111
    creationTimestamp: "2026-09-24T00:00:00Z"
    annotations:
      kubectl.kubernetes.io/last-applied-configuration: "{}"
  data:
    tls.crt: Y2VydA==
    tls.key: a2V5
"""


def test_secret_manifest_from_literals_and_files(tmp_path):
    token = tmp_path / "token.txt"
    token.write_bytes(b"s3cret\n")
    doc = yamlio.load(sealing.secret_manifest("db", "app", ["user=admin"], [f"api={token}", str(token)]).decode())
    assert doc["kind"] == "Secret"
    assert doc["metadata"] == {"name": "db", "namespace": "app"}
    assert base64.b64decode(doc["data"]["user"]) == b"admin"
    assert base64.b64decode(doc["data"]["api"]) == b"s3cret\n"
    assert base64.b64decode(doc["data"]["token.txt"]) == b"s3cret\n"


@pytest.mark.parametrize(
    "literals, files, message",
    [
        ([], [], "at least one"),
        (["novalue"], [], "key=value"),
        (["bad key=x"], [], "invalid secret key"),
        (["a=1", "a=2"], [], "duplicate"),
        ([], ["/does/not/exist"], "cannot read"),
    ],
)
def test_secret_manifest_errors(literals, files, message):
    with pytest.raises(ValidationError) as exc:
        sealing.secret_manifest("db", "app", literals, files)
    assert message in str(exc.value) + (exc.value.hint or "")


@pytest.mark.parametrize(
    "text, message",
    [
        ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: a}\n", "kind: Secret"),
        ("apiVersion: v1\nkind: Secret\nmetadata: {name: x}\n", "namespace"),
        ("kind: Secret\nmetadata: {name: x, namespace: a}\n---\nkind: Secret\nmetadata: {name: y, namespace: a}\n", "exactly one"),
        ("{not yaml", "YAML"),
    ],
)
def test_check_secret_manifest(text, message):
    with pytest.raises(ValidationError) as exc:
        sealing.check_secret_manifest(text.encode())
    assert message in str(exc.value) + (exc.value.hint or "")


pytestmark_keys = need("age", "age-keygen", "sops", "kubeseal")


@pytestmark_keys
def test_seal_falls_back_to_stored_cert(keyed_repo, sealing_cert, capfd):
    atomic_write(sealing.cert_path(keyed_repo), sealing_cert)
    sealed = sealing.seal(Context.open(keyed_repo), sealing.secret_manifest("db", "app", ["password=hunter2"], []))
    doc = [d for d in yamlio.load_all(sealed.decode()) if d][0]
    assert doc["kind"] == "SealedSecret"
    assert doc["metadata"]["namespace"] == "app"
    assert b"hunter2" not in sealed
    assert "sealed-secrets.pem" in capfd.readouterr().err


@pytestmark_keys
def test_seal_prefers_live_cert(repo_with_configs, sealing_cert, spy):
    spy("kubectl")
    kubeseal = spy("kubeseal").on("--fetch-cert", stdout=sealing_cert.decode()).real("--cert")
    sealed = sealing.seal(Context.open(repo_with_configs), sealing.secret_manifest("db", "app", ["a=b"], []))
    assert b"SealedSecret" in sealed
    fetch = kubeseal.calls[0]
    assert "--fetch-cert" in fetch["args"] and fetch["kubeconfig"] is True
    assert not sealing.cert_path(repo_with_configs).exists()


@pytestmark_keys
def test_seal_without_any_cert(keyed_repo):
    with pytest.raises(ValidationError) as exc:
        sealing.seal(Context.open(keyed_repo), sealing.secret_manifest("db", "app", ["a=b"], []))
    assert "backup-sealing-key" in exc.value.hint


def test_write_sealed_requires_suffix(tmp_path):
    with pytest.raises(ValidationError):
        sealing.write_sealed(b"x", tmp_path / "db.yaml")
    sealing.write_sealed(b"x", tmp_path / "apps" / "db.sealed.yaml")
    assert (tmp_path / "apps" / "db.sealed.yaml").read_bytes() == b"x"


@need("age", "age-keygen", "sops")
def test_backup_sealing_key(repo_with_configs, sealing_cert, spy):
    spy("kubectl").on("get", "secret", stdout=KEY_LIST)
    spy("kubeseal").on("--fetch-cert", stdout=sealing_cert.decode())
    ctx = Context.open(repo_with_configs)
    written = sealing.backup_sealing_key(ctx)
    backup = sealing.key_backup_path(repo_with_configs)
    assert written == [backup, sealing.cert_path(repo_with_configs)]
    assert b"a2V5" not in backup.read_bytes()  # encrypted
    doc = yamlio.load(ctx.sops.decrypt(backup).decode())
    item = doc["items"][0]
    assert doc["kind"] == "List"
    assert item["data"]["tls.key"] == "a2V5"
    for field in ("resourceVersion", "uid", "creationTimestamp"):
        assert field not in item["metadata"]
    assert "kubectl.kubernetes.io/last-applied-configuration" not in item["metadata"].get("annotations", {})
    assert sealing.cert_path(repo_with_configs).read_bytes() == sealing_cert


@need("age", "age-keygen", "sops")
def test_backup_without_key_fails(repo_with_configs, spy):
    spy("kubectl").on("get", "secret", stdout="apiVersion: v1\nkind: List\nitems: []\n")
    spy("kubeseal")
    with pytest.raises(ValidationError) as exc:
        sealing.backup_sealing_key(Context.open(repo_with_configs))
    assert "controller" in exc.value.hint
