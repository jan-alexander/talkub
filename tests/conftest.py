"""Shared fixtures. talkub imports are lazy so early tasks run before later modules exist."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from helpers import sample_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Never touch the developer's real keys or kube/talos configs."""
    for var in (
        "SOPS_AGE_KEY",
        "SOPS_AGE_KEY_FILE",
        "TALKUB_IDENTITY",
        "TALOSCONFIG",
        "KUBECONFIG",
        "TALKUB_SESSION",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TALKUB_APPLY_SETTLE_SECONDS", "0")  # rolling apply must not really sleep


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Install a fake executable (POSIX sh script) in front of PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def install(name: str, script: str) -> Path:
        path = bindir / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    return install


@pytest.fixture
def spy(fake_bin, tmp_path):
    """spy("kubectl") puts a logging fake `kubectl` in front of PATH and returns its Spy."""
    import sys

    from helpers import SPY_SCRIPT, Spy

    directory = tmp_path / "spies"
    directory.mkdir()
    script = directory / "spy.py"
    script.write_text(SPY_SCRIPT)

    def make(name: str) -> Spy:
        instance = Spy(name=name, directory=directory, real_path=shutil.which(name))
        fake_bin(
            name,
            f'exec "{sys.executable}" "{script}" "{instance.log}" "{instance.rules_file}" "$@"\n',
        )
        return instance

    return make


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


@pytest.fixture
def software_identity():
    """A software age identity in the isolated default identities dir: (path, public key)."""
    _require("age-keygen")
    from talkub.core.runner import Runner
    from talkub.secrets.identity import create_identity, identities_dir

    return create_identity(Runner(), "software", identities_dir(os.environ))


@pytest.fixture
def empty_repo(tmp_path):
    """A directory with a valid .talkub.yaml and nothing else."""
    from talkub.config import Repo

    root = tmp_path / "cluster"
    root.mkdir()
    repo = Repo(root)
    repo.write_settings(sample_settings())
    return repo


@pytest.fixture
def keyed_repo(empty_repo, software_identity):
    """empty_repo plus a master key encrypted to software_identity (recipient 'me')."""
    _require("age")
    from talkub.core.runner import Runner
    from talkub.secrets.keyring import Keyring
    from talkub.secrets.recipients import Recipient

    Keyring(empty_repo, Runner()).create([Recipient("me", software_identity[1])])
    return empty_repo


@pytest.fixture
def repo_with_configs(keyed_repo):
    """keyed_repo plus .sops.yaml and SOPS-encrypted talosconfig and kubeconfig with marker content."""
    _require("sops")
    from talkub.context import Context
    from talkub.core.fsutil import atomic_write
    from talkub.secrets.sops import render_sops_config

    ctx = Context.open(keyed_repo)
    public = ctx.keyring.public_key()
    atomic_write(keyed_repo.sops_config, render_sops_config(public).encode())
    for path, text in (
        (keyed_repo.talosconfig, b"talos: marker\n"),
        (keyed_repo.kubeconfig, b"kube: marker\n"),
    ):
        atomic_write(path, ctx.sops.encrypt(text, filename=path.name, public_key=public))
    return keyed_repo


@pytest.fixture
def talos_repo(repo_with_configs):
    """repo_with_configs plus real SOPS-encrypted Talos secrets and the cp-01 node patch."""
    _require("talosctl")
    from talkub.context import Context
    from talkub.core.ephemeral import private_tempdir
    from talkub.core.fsutil import atomic_write
    from talkub.core.runner import Runner
    from talkub.talos import gen
    from talkub.talos.nodes import node_patch_path, render_node_patch

    ctx = Context.open(repo_with_configs)
    public = ctx.keyring.public_key()
    with private_tempdir() as work:
        secrets = gen.gen_secrets(Runner(), work)
        atomic_write(
            repo_with_configs.talos_secrets,
            ctx.sops.encrypt(secrets.read_bytes(), filename="secrets.sops.yaml", public_key=public),
        )
    atomic_write(node_patch_path(repo_with_configs, "cp-01"), render_node_patch("cp-01").encode())
    return repo_with_configs


@pytest.fixture
def talosctl_spy(fake_bin, tmp_path):
    """Fake talosctl: real binary for gen/validate, logged no-op for node calls.

    Node 10.0.0.99 fails every call; node 10.0.0.98 passes the dry-run but fails the real apply.
    """
    _require("talosctl")
    real = shutil.which("talosctl")
    log = tmp_path / "talosctl.log"
    fake_bin(
        "talosctl",
        f"""case "$1" in gen|validate) exec "{real}" "$@";; esac
printf 'call %s\\n' "$*" >> "{log}"
if [ "$1" = "--talosconfig" ] && [ ! -f "$2" ]; then exit 97; fi
dry=""
for a in "$@"; do [ "$a" = "--dry-run" ] && dry=1; done
prev=""
for a in "$@"; do
  if [ "$prev" = "--file" ] && [ -f "$a" ]; then printf 'file %s\\n' "$(grep -c 'hostname:' "$a")" >> "{log}"; fi
  [ "$a" = "10.0.0.99" ] && exit 1
  [ "$a" = "10.0.0.98" ] && [ -z "$dry" ] && exit 1
  prev="$a"
done
exit 0
""",
    )
    return log


@pytest.fixture(scope="session")
def sealing_cert(tmp_path_factory):
    """A self-signed certificate kubeseal can encrypt to (stands in for the controller's cert)."""
    import subprocess

    _require("openssl")
    directory = tmp_path_factory.mktemp("sealing")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=sealed-secret",
        ],
        check=True,
        capture_output=True,
    )
    return (directory / "cert.pem").read_bytes()


@pytest.fixture
def factory_stub(monkeypatch):
    """Local stand-in for the Talos Image Factory; sets TALKUB_FACTORY_URL.

    POST /schematics answers {"id": sha256(body)}; GET .../extensions/official lists
    `state.extensions`. `state.fail = True` makes every request return HTTP 500.
    """
    import hashlib
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    state = SimpleNamespace(
        requests=[],
        extensions=[
            "siderolabs/intel-ucode",
            "siderolabs/iscsi-tools",
            "siderolabs/tailscale",
        ],
        fail=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append(("POST", self.path, body))
            if state.fail:
                return self._reply(500, {"error": "boom"})
            self._reply(201, {"id": hashlib.sha256(body).hexdigest()})

        def do_GET(self):
            state.requests.append(("GET", self.path, b""))
            if state.fail:
                return self._reply(500, {"error": "boom"})
            self._reply(
                200,
                [{"name": name, "ref": f"ghcr.io/{name}:1"} for name in state.extensions],
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TALKUB_FACTORY_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield state
    server.shutdown()
    server.server_close()
