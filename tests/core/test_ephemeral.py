import os
import signal
from pathlib import Path

import pytest

from talkub.core.ephemeral import decrypted, private_tempdir


class FakeSops:
    def decrypt(self, path: Path) -> bytes:
        return f"plain:{path.name}".encode()


def test_decrypted_files_are_private_and_removed(tmp_path):
    with decrypted(FakeSops(), {"talosconfig": tmp_path / "t.sops.yaml"}) as paths:
        target = paths["talosconfig"]
        assert target.read_bytes() == b"plain:t.sops.yaml"
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.parent.stat().st_mode & 0o777 == 0o700
    assert not target.exists()
    assert not target.parent.exists()


def test_removed_when_body_raises(tmp_path):
    with (
        pytest.raises(RuntimeError),
        decrypted(FakeSops(), {"kubeconfig": tmp_path / "k.sops.yaml"}) as paths,
    ):
        target = paths["kubeconfig"]
        raise RuntimeError("boom")
    assert not target.exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_removed_on_termination_signal(tmp_path, signum):
    previous = signal.getsignal(signum)
    with (
        pytest.raises(SystemExit) as exc,
        decrypted(FakeSops(), {"talosconfig": tmp_path / "t.sops.yaml"}) as paths,
    ):
        target = paths["talosconfig"]
        os.kill(os.getpid(), signum)
    assert exc.value.code == 128 + signum
    assert not target.exists()
    assert signal.getsignal(signum) == previous


def test_private_tempdir_mode():
    with private_tempdir() as path:
        assert path.stat().st_mode & 0o777 == 0o700
    assert not path.exists()
