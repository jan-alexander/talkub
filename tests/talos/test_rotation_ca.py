import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import need
from test_inspect import _resource

from talkub.config import NodeSpec
from talkub.context import Context
from talkub.core import yamlio
from talkub.core.errors import CommandFailed, ValidationError
from talkub.core.fsutil import atomic_write
from talkub.talos import rotation
from talkub.talos.nodes import node_patch_path, render_node_patch

BUNDLE = (
    "cluster:\n    id: cid\n    secret: csecret\n"
    "secrets:\n    bootstraptoken: abc.def\n"
    "certs:\n    etcd:\n        crt: ETCDCRT\n        key: ETCDKEY\n"
    "    os:\n        crt: OLDCRT\n        key: OLDKEY\n"
)


def test_replace_os_ca_changes_only_certs_os():
    out = rotation.replace_os_ca(BUNDLE.encode(), "NEWCRT", "NEWKEY").decode()
    assert out == BUNDLE.replace("OLDCRT", "NEWCRT").replace("OLDKEY", "NEWKEY")
    doc = yamlio.load(out)
    assert doc["certs"]["etcd"]["key"] == "ETCDKEY" and doc["secrets"]["bootstraptoken"] == "abc.def"


@need("talosctl")
def test_replace_os_ca_against_real_bundle(tmp_path):
    bundle_path = tmp_path / "secrets.yaml"
    subprocess.run(
        ["talosctl", "gen", "secrets", "-o", str(bundle_path)],
        check=True,
        capture_output=True,
    )
    original = bundle_path.read_bytes()

    out = rotation.replace_os_ca(original, "NEWCRT", "NEWKEY")

    original_lines = original.decode().splitlines()
    out_lines = out.decode().splitlines()
    assert len(original_lines) == len(out_lines)
    changed = [i for i, (a, b) in enumerate(zip(original_lines, out_lines, strict=False)) if a != b]
    assert len(changed) == 2
    assert out_lines[changed[0]].strip() == "crt: NEWCRT"
    assert out_lines[changed[1]].strip() == "key: NEWKEY"


# --- rotate / finish flow -------------------------------------------------------------

OLD_TALOSCONFIG = (
    "context: test\ncontexts:\n    test:\n        endpoints:\n            - 10.0.0.10\n"
    "        ca: OLDCA\n        crt: OLDCLIENTCRT\n        key: OLDCLIENTKEY\n"
)
NEW_TALOSCONFIG = (
    "context: test\ncontexts:\n    test:\n        endpoints:\n            - 10.0.0.10\n"
    "        ca: NEWCA\n        crt: NEWCLIENTCRT\n        key: CLIENTKEYSECRET\n"
)
SECRET_OUTPUT = "  key: SUPERSECRETCAKEY\ncontexts:\n    test:\n        key: CLIENTKEYSECRET\n"
ROTATOR = """#!{python}
import json, os, sys
args = sys.argv[1:]
entry = {{"args": args, "files": {{}}, "kubeconfig": False, "session_leader": os.getsid(0) == os.getpid()}}
with open({log!r}, "a") as fh:
    fh.write(json.dumps(entry) + "\\n")
with open(args[args.index("-o") + 1], "w") as fh:
    fh.write({talosconfig!r})
sys.stdout.write({output!r} + "ROTATED\\n")
"""


def _verbs(spy) -> list[str]:
    return [a[a.index("-e") + 2] for a in spy.argvs]


def _decrypt(repo, path) -> str:
    return Context.open(repo).sops.decrypt(path).decode()


def _os_ca(repo) -> tuple[str, str]:
    os_certs = yamlio.load(_decrypt(repo, repo.talos_secrets))["certs"]["os"]
    return os_certs["crt"], os_certs["key"]


@pytest.fixture
def rotating(talos_repo, spy, tmp_path):
    """talos_repo with a realistic talosconfig and a talosctl spy for a clean, in-sync cluster."""
    ctx = Context.open(talos_repo)
    atomic_write(
        talos_repo.talosconfig,
        ctx.sops.encrypt(
            OLD_TALOSCONFIG.encode(),
            filename=talos_repo.talosconfig.name,
            public_key=ctx.keyring.public_key(),
        ),
    )
    talosctl = spy("talosctl").real("gen").real("validate")
    rotator = tmp_path / "rotator.py"
    rotator.write_text(
        ROTATOR.format(
            python=sys.executable,
            log=str(talosctl.log),
            talosconfig=NEW_TALOSCONFIG,
            output=SECRET_OUTPUT,
        )
    )
    rotator.chmod(0o755)
    talosctl.rotator = str(rotator)
    bundle = tmp_path / "new-secrets.yaml"  # a real CA: rendering validates certs.os
    subprocess.run(
        ["talosctl", "gen", "secrets", "-o", str(bundle)],
        check=True,
        capture_output=True,
    )
    os_certs = yamlio.load(bundle.read_text())["certs"]["os"]
    talosctl.new_ca = (os_certs["crt"], os_certs["key"])
    return talos_repo, talosctl


def _machineconfig(crt: str, key: str) -> str:
    return _resource(
        f"version: v1alpha1\nmachine:\n  type: controlplane\n  ca:\n    crt: {crt}\n    key: {key}\ncluster:\n  id: x\n"
    )


def _in_sync(talosctl, *, real_rotation: bool = True):
    talosctl.on(
        "apply-config",
        "--dry-run",
        stdout="Applied configuration without a reboot\nNo changes.\n",
    )
    talosctl.on("rotate-ca", "--dry-run=true", stdout=SECRET_OUTPUT)
    if real_rotation:
        talosctl.exec("rotate-ca", "--dry-run=false", program=talosctl.rotator)
    talosctl.on("get", "machineconfig", stdout=_machineconfig(*talosctl.new_ca))
    return talosctl


def test_rotate_happy_path(rotating, capfd, monkeypatch):
    repo, talosctl = rotating
    _in_sync(talosctl)
    written = []
    real_write = rotation.atomic_write
    monkeypatch.setattr(
        rotation,
        "atomic_write",
        lambda path, data: (written.append(path), real_write(path, data)),
    )

    notes = rotation.rotate(Context.open(repo), confirm=lambda q: True)

    assert _verbs(talosctl) == [
        "health",
        "apply-config",
        "rotate-ca",
        "rotate-ca",
        "get",
        "version",
        "apply-config",
    ]
    argvs = talosctl.argvs
    assert "--dry-run=true" in argvs[2] and "--dry-run=false" in argvs[3] and "-o" in argvs[3]
    for rotate_args in argvs[2:4]:
        assert rotate_args[rotate_args.index("--control-plane-nodes") + 1] == "10.0.0.10"
        assert "--talos=true" in rotate_args and "--kubernetes=false" in rotate_args
        assert "--worker-nodes" not in rotate_args
    talosconfig = yamlio.load(_decrypt(repo, repo.talosconfig))
    assert talosconfig["contexts"]["test"]["endpoints"] == ["10.0.0.10"]
    assert talosconfig["contexts"]["test"]["nodes"] == ["10.0.0.10"]
    assert talosconfig["contexts"]["test"]["key"] == "CLIENTKEYSECRET"
    assert _os_ca(repo) == talosctl.new_ca
    assert not rotation.previous_path(repo).exists()
    assert "commit talos/" in notes
    out, err = capfd.readouterr()
    assert "SUPERSECRETCAKEY" not in out + err and "CLIENTKEYSECRET" not in out + err
    assert "<redacted>" in out
    interrupted = re.search(
        r"if this is interrupted, the new talosconfig will be in (\S+); finish with "
        r"`talkub rotate-ca --finish --talosconfig (\S+)`",
        out,
    )
    assert interrupted and interrupted.group(1) == interrupted.group(2)
    assert out.index(interrupted.group(0)) < out.index("ROTATED")
    assert "do not interrupt it" in out
    assert talosctl.calls[3]["session_leader"]  # a terminal Ctrl-C does not reach talosctl
    assert not Path(interrupted.group(1)).parent.exists()  # deleted on success
    assert written.index(repo.talosconfig) < written.index(repo.talos_secrets)


def test_rotate_declined_changes_nothing(rotating):
    repo, talosctl = rotating
    _in_sync(talosctl)
    before = (repo.talosconfig.read_bytes(), repo.talos_secrets.read_bytes())

    with pytest.raises(ValidationError, match="aborted"):
        rotation.rotate(Context.open(repo), confirm=lambda q: False)

    assert not any("--dry-run=false" in a for a in talosctl.argvs)
    assert (repo.talosconfig.read_bytes(), repo.talos_secrets.read_bytes()) == before
    assert not rotation.previous_path(repo).exists()


def test_rotate_refuses_when_drifting(rotating):
    repo, talosctl = rotating
    talosctl.on("apply-config", "--dry-run", stdout="Config diff:\n+ x\n")

    with pytest.raises(ValidationError, match="cp-01"):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    assert "rotate-ca" not in _verbs(talosctl)
    assert not rotation.previous_path(repo).exists()


def test_rotate_refuses_while_a_rotation_is_in_progress(rotating):
    repo, talosctl = rotating
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())

    with pytest.raises(ValidationError) as info:
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    assert "--finish" in info.value.hint
    assert "talkub-rotate-*/talosconfig" in info.value.hint and "--finish --talosconfig" in info.value.hint
    assert talosctl.argvs == []


def test_rotate_failure_keeps_the_new_talosconfig_dir(rotating, capfd):
    repo, talosctl = rotating
    talosctl.on("rotate-ca", "--dry-run=false", rc=1)
    _in_sync(talosctl, real_rotation=False)
    bundle = repo.talos_secrets.read_bytes()

    with pytest.raises(CommandFailed):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    err = capfd.readouterr().err
    match = re.search(r"kept in (\S+);", err)
    assert match, err
    kept = match.group(1)
    try:
        assert "--finish --talosconfig" in err
        assert Path(kept).is_dir()
        assert rotation.previous_path(repo).exists()
        assert repo.talos_secrets.read_bytes() == bundle
        assert "get" not in _verbs(talosctl)
    finally:
        shutil.rmtree(kept, ignore_errors=True)


def test_finish_without_rotation(rotating):
    repo, _ = rotating
    with pytest.raises(ValidationError, match="no CA rotation in progress"):
        rotation.finish(Context.open(repo))


def test_finish_with_talosconfig(rotating, tmp_path):
    repo, talosctl = rotating
    _in_sync(talosctl, real_rotation=False)
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())
    new = tmp_path / "new-talosconfig"
    new.write_text(NEW_TALOSCONFIG)

    notes = rotation.finish(Context.open(repo), talosconfig=new)

    talosconfig = yamlio.load(_decrypt(repo, repo.talosconfig))
    assert talosconfig["context"] == "test"
    assert talosconfig["contexts"]["test"]["crt"] == "NEWCLIENTCRT"
    assert _os_ca(repo) == talosctl.new_ca
    assert not rotation.previous_path(repo).exists()
    assert any(str(new) in note for note in notes)
    assert _verbs(talosctl) == [
        "version",
        "get",
        "version",
        "apply-config",
    ]  # candidate checked first


def test_finish_keeps_previous_when_still_drifting(rotating):
    repo, talosctl = rotating
    talosctl.on("apply-config", "--dry-run", stdout="Config diff:\n+ x\n")
    talosctl.on("get", "machineconfig", stdout=_machineconfig(*talosctl.new_ca))
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())

    with pytest.raises(ValidationError, match="still not in sync: cp-01") as info:
        rotation.finish(Context.open(repo))

    assert "do not run `talkub apply` until `talkub plan` shows only expected changes" in info.value.hint
    assert "`talkub t version` works" in info.value.hint

    assert rotation.previous_path(repo).exists()
    assert _os_ca(repo) == talosctl.new_ca


def test_rotate_points_to_finish_when_the_final_check_fails(rotating, capfd):
    repo, talosctl = rotating
    talosctl.on("apply-config", "--dry-run", stdout="No changes.\n")
    talosctl.on("rotate-ca", "--dry-run=true", stdout=SECRET_OUTPUT)
    talosctl.exec("rotate-ca", "--dry-run=false", program=talosctl.rotator)
    talosctl.on("get", "machineconfig", rc=1)

    with pytest.raises(CommandFailed):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    assert "talkub rotate-ca --finish`" in capfd.readouterr().err
    assert yamlio.load(_decrypt(repo, repo.talosconfig))["contexts"]["test"]["key"] == "CLIENTKEYSECRET"
    assert rotation.previous_path(repo).exists()


def test_rotate_store_failure_keeps_the_new_talosconfig(rotating, capfd, monkeypatch):
    repo, talosctl = rotating
    _in_sync(talosctl)
    before = repo.talosconfig.read_bytes()

    def broken(ctx, settings, data):
        raise ValidationError("cannot store")

    monkeypatch.setattr(rotation, "_store_talosconfig", broken)
    with pytest.raises(ValidationError, match="cannot store"):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    match = re.search(r"--finish --talosconfig (\S+)`", capfd.readouterr().err)
    assert match
    new = Path(match.group(1))
    try:
        assert new.is_file() and "CLIENTKEYSECRET" in new.read_text()
        assert repo.talosconfig.read_bytes() == before
        assert rotation.previous_path(repo).exists()
    finally:
        shutil.rmtree(new.parent, ignore_errors=True)


def test_finish_refuses_a_talosconfig_that_does_not_work(rotating, tmp_path):
    repo, talosctl = rotating
    _in_sync(talosctl, real_rotation=False)
    talosctl.on("version", rc=1, stderr="SECRETSTDERR")
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())
    before = (repo.talosconfig.read_bytes(), repo.talos_secrets.read_bytes())
    new = tmp_path / "stale-talosconfig"
    new.write_text(NEW_TALOSCONFIG)

    with pytest.raises(
        ValidationError, match=re.escape("does not work against 10.0.0.10; nothing was changed")
    ) as info:
        rotation.finish(Context.open(repo), talosconfig=new)

    assert "SECRETSTDERR" not in str(info.value) + str(info.value.hint)
    assert (repo.talosconfig.read_bytes(), repo.talos_secrets.read_bytes()) == before
    assert _verbs(talosctl) == ["version"]
    assert talosctl.calls[0]["files"]["--talosconfig"]
    assert rotation.previous_path(repo).exists()


def test_finish_machineconfig_failure_points_to_kept_talosconfig(rotating):
    repo, talosctl = rotating
    talosctl.on("get", "machineconfig", rc=1, stderr="key: LEAKED")
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())
    bundle = repo.talos_secrets.read_bytes()

    with pytest.raises(CommandFailed) as info:
        rotation.finish(Context.open(repo))

    assert "LEAKED" not in str(info.value) + str(info.value.hint)
    assert "talkub-rotate-*/talosconfig" in info.value.hint and "--finish --talosconfig" in info.value.hint
    assert repo.talos_secrets.read_bytes() == bundle


def test_finish_version_failure_hides_stderr(rotating):
    repo, talosctl = rotating
    _in_sync(talosctl, real_rotation=False)
    talosctl.on("version", rc=1, stderr="key: LEAKED")
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())

    with pytest.raises(CommandFailed) as info:
        rotation.finish(Context.open(repo))

    assert "LEAKED" not in str(info.value) + str(info.value.hint)
    assert rotation.previous_path(repo).exists()


def test_rotate_failure_keeps_the_raw_output(rotating, capfd):
    repo, talosctl = rotating
    talosctl.on(
        "rotate-ca",
        "--dry-run=false",
        rc=1,
        stdout=SECRET_OUTPUT,
        stderr="  key: STDERRSECRET\n",
    )
    _in_sync(talosctl, real_rotation=False)

    with pytest.raises(CommandFailed):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    out, err = capfd.readouterr()
    match = re.search(r"kept in (\S+);", err)
    assert match, err
    kept = Path(match.group(1))
    try:
        raw = kept / "rotate-ca.out"
        assert str(raw) in err and "new CA and client key" in err
        assert raw.stat().st_mode & 0o777 == 0o600
        text = raw.read_text()
        assert "SUPERSECRETCAKEY" in text and "CLIENTKEYSECRET" in text and "STDERRSECRET" in text
        for secret in ("SUPERSECRETCAKEY", "CLIENTKEYSECRET", "STDERRSECRET"):
            assert secret not in out + err
    finally:
        shutil.rmtree(kept, ignore_errors=True)


def test_rotate_without_new_talosconfig_fails_and_keeps_the_dir(rotating, capfd):
    repo, talosctl = rotating
    talosctl.on("rotate-ca", "--dry-run=false", rc=0)
    _in_sync(talosctl, real_rotation=False)
    before = repo.talosconfig.read_bytes()

    with pytest.raises(CommandFailed, match="exit 0"):
        rotation.rotate(Context.open(repo), confirm=lambda q: True)

    kept = Path(re.search(r"kept in (\S+);", capfd.readouterr().err).group(1))
    try:
        assert kept.is_dir()
        assert repo.talosconfig.read_bytes() == before
        assert rotation.previous_path(repo).exists()
    finally:
        shutil.rmtree(kept, ignore_errors=True)


def test_rotate_passes_worker_nodes(rotating):
    repo, talosctl = rotating
    settings = repo.load_settings()
    settings.nodes["wk-01"] = NodeSpec(ip="10.0.0.11", role="worker")
    repo.write_settings(settings)
    atomic_write(node_patch_path(repo, "wk-01"), render_node_patch("wk-01").encode())
    _in_sync(talosctl)

    rotation.rotate(Context.open(repo), confirm=lambda q: True)

    rotate_calls = [a for a in talosctl.argvs if "rotate-ca" in a]
    assert len(rotate_calls) == 2
    for argv in rotate_calls:
        assert argv[argv.index("--control-plane-nodes") + 1] == "10.0.0.10"
        assert argv[argv.index("--worker-nodes") + 1] == "10.0.0.11"


def test_finish_after_a_rotation_that_changed_nothing(rotating):
    repo, talosctl = rotating
    old_crt, old_key = _os_ca(repo)
    talosctl.on("apply-config", "--dry-run", stdout="No changes.\n")
    talosctl.on("get", "machineconfig", stdout=_machineconfig(old_crt, old_key))
    rotation.previous_path(repo).write_bytes(repo.talosconfig.read_bytes())
    bundle = repo.talos_secrets.read_bytes()

    rotation.finish(Context.open(repo))

    assert repo.talos_secrets.read_bytes() == bundle  # no SOPS churn
    assert not rotation.previous_path(repo).exists()
