# talkub rotate-ca Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `talkub rotate-ca` rotates the Talos API CA with `talosctl rotate-ca` and brings the repo (talosconfig, secrets bundle `certs.os`) in line, so `talkub plan` shows "No changes" afterwards; `talkub rotate-ca --finish` resumes an interrupted run.

**Architecture:** Parsing lives in `talos/inspect.py` (`machine_ca`). `talos/client.py` gains `TalosSession.run_global` (no `-n`) and `talos_session_from(path)`. `lifecycle.py` gains a quiet `node_drift` (reused by pre-check and final check). The flow is in a new `src/talkub/rotation.py`; `src/talkub/cli/rotate.py` is a thin Typer command registered in `cli/main.py`.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; age, sops, talosctl 1.14.

**Spec:** `docs/superpowers/specs/2026-09-25-talkub-rotate-ca-design.md`

## Global Constraints

- English code, comments, CLI output. `runner.py` is the only subprocess starter; argv lists only.
- Every talosctl output of `rotate-ca` and `get machineconfig` is either captured and never printed, or printed only through `talos.client._print_redacted` / `TalosSession.call_redacted` (`inspect.redact_diff`). CA keys and client keys never reach stdout/stderr.
- Plaintext talosconfig/CA material only in `private_tempdir()`/`decrypted()`/memory. **Single exception (spec §3):** when `rotate-ca` (the real run) or the step that stores the new talosconfig fails, the private temp dir holding the new talosconfig is kept (not deleted) and its path printed.
- Encrypted writes: `ctx.sops.encrypt(data, filename=path.name, public_key=ctx.keyring.public_key())` + `atomic_write`.
- rotate-ca flags, exactly: `rotate-ca --talos=true --kubernetes=false --dry-run=<true|false> --with-docs=false --with-examples=false --control-plane-nodes <comma-joined cp mgmt> [--worker-nodes <comma-joined worker mgmt>]` plus `-o <path>` on the real run.
- Backup file: `talos/talosconfig.previous.sops.yaml` (byte copy of the encrypted talosconfig). Its presence = rotation in progress.
- Secrets bundle keys: `certs.os.crt`, `certs.os.key` (base64 PEM strings, same encoding as `machine.ca.crt/key`).
- Commits end with `Co-Authored-By: <model> <noreply@anthropic.com>`. Run tests with `uv run pytest`.

## Review Focus

1. **Interrupted run after the real rotation:** the new talosconfig must survive (encrypted in the repo, or the kept temp dir) — test in Task 3.
2. **Secret leakage:** spy talosctl prints `key: …`/client `key:` lines during dry-run and real run; none reaches captured stdout/stderr — test in Task 3.
3. **Repo not in sync before rotating:** pre-check sees "changes" → abort, nothing written — test in Task 3.
4. **Bundle round-trip:** only `certs.os` changes; other secrets untouched — test in Task 1.
5. **`machineconfig` output variants:** `spec` as YAML string (observed on Talos 1.14) and as mapping; trailing empty document — test in Task 1.

---

### Task 1: `machine_ca` parser and bundle update

**Files:**
- Modify: `src/talkub/talos/inspect.py`
- Create: `src/talkub/rotation.py` (only `replace_os_ca` in this task)
- Test: `tests/test_inspect.py`, `tests/test_rotation_ca.py` (new)

**Interfaces:**
- Produces: `inspect.machine_ca(text: str) -> tuple[str, str]` (crt, key as the base64 strings found in the config; raises `ValidationError("no machine CA in the machine config")`); `rotation.replace_os_ca(bundle: bytes, crt: str, key: str) -> bytes`.

- [ ] **Step 1: Failing tests**

`tests/test_inspect.py` (append):

```python
import base64

CRT = base64.b64encode(
    b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
).decode()
KEY = base64.b64encode(
    b"-----BEGIN ED25519 PRIVATE KEY-----\nMC4C\n-----END ED25519 PRIVATE KEY-----\n"
).decode()
MACHINE = f"version: v1alpha1\nmachine:\n  type: controlplane\n  ca:\n    crt: {CRT}\n    key: {KEY}\ncluster:\n  id: x\n---\napiVersion: v1alpha1\nkind: HostnameConfig\nhostname: cp-01\n"


def _resource(spec) -> str:
    from talkub import yamlio

    return (
        yamlio.dump(
            {
                "node": "10.0.0.10",
                "metadata": {
                    "namespace": "config",
                    "type": "MachineConfigs.config.talos.dev",
                    "id": "v1alpha1",
                },
                "spec": spec,
            }
        )
        + "---\n"
    )


def test_machine_ca_from_spec_string():
    from talkub.talos.inspect import machine_ca

    assert machine_ca(_resource(MACHINE)) == (CRT, KEY)


def test_machine_ca_from_spec_mapping():
    from talkub import yamlio
    from talkub.talos.inspect import machine_ca

    assert machine_ca(_resource(yamlio.load_all(MACHINE)[0])) == (CRT, KEY)


def test_machine_ca_missing():
    import pytest

    from talkub.errors import ValidationError
    from talkub.talos.inspect import machine_ca

    with pytest.raises(ValidationError):
        machine_ca(_resource("version: v1alpha1\nmachine:\n  type: controlplane\n"))
```

`tests/test_rotation_ca.py` (new):

```python
from talkub import rotation, yamlio

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
    assert (
        doc["certs"]["etcd"]["key"] == "ETCDKEY"
        and doc["secrets"]["bootstraptoken"] == "abc.def"
    )
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_inspect.py tests/test_rotation_ca.py -q`

- [ ] **Step 3: Implement**

`inspect.py`:

```python
def machine_ca(text: str) -> tuple[str, str]:
    """(crt, key) of machine.ca from `talosctl get machineconfig -o yaml`; never printed by callers."""
    for resource in yamlio.load_all(text):
        if not isinstance(resource, dict):
            continue
        spec = resource.get("spec")
        documents = yamlio.load_all(spec) if isinstance(spec, str) else [spec]
        for doc in documents:
            ca = (
                (doc or {}).get("machine", {}).get("ca")
                if isinstance(doc, dict)
                else None
            )
            if (
                isinstance(ca, dict)
                and isinstance(ca.get("crt"), str)
                and isinstance(ca.get("key"), str)
            ):
                return ca["crt"], ca["key"]
    raise ValidationError("no machine CA in the machine config")
```

(Use the module's existing imports; add `yamlio`/`ValidationError` imports if missing. If `yamlio.load` preserves a byte-identical dump for the bundle, use it; the test requires byte equality apart from the two values — the bundle is written by `talosctl gen secrets` with 4-space indentation, so dump with the bundle's own indentation: detect it or use `ruamel` `YAML()` with `indent(mapping=4, sequence=4, offset=2)` inside `replace_os_ca`. Adjust until the test passes; do not weaken it.)

`rotation.py`:

```python
"""talkub rotate-ca: rotate the Talos API CA and bring the repo in line."""

from __future__ import annotations

from io import StringIO

from ruamel.yaml import YAML

from talkub.errors import ValidationError


def _bundle_yaml() -> YAML:
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def replace_os_ca(bundle: bytes, crt: str, key: str) -> bytes:
    """The secrets bundle with certs.os replaced; everything else round-trips unchanged."""
    yaml = _bundle_yaml()
    doc = yaml.load(bundle.decode())
    os_certs = doc.get("certs", {}).get("os") if isinstance(doc, dict) else None
    if not isinstance(os_certs, dict):
        raise ValidationError("the Talos secrets bundle has no certs.os")
    os_certs["crt"] = crt
    os_certs["key"] = key
    buffer = StringIO()
    yaml.dump(doc, buffer)
    return buffer.getvalue().encode()
```

- [ ] **Step 4: Run, expect PASS**; also run the test against a real bundle: add a test marked `need("talosctl")` that runs `talosctl gen secrets` into `tmp_path`, calls `replace_os_ca` and asserts every line except the two `certs.os` lines is unchanged.

- [ ] **Step 5: Commit** `feat: machine CA parser and secrets bundle CA update`

---

### Task 2: quiet drift check and session helpers

**Files:**
- Modify: `src/talkub/talos/client.py`, `src/talkub/lifecycle.py`
- Test: `tests/test_lifecycle.py`, `tests/test_client.py` (new if none exists)

**Interfaces:**
- Produces: `TalosSession.run_global(args, endpoints: Sequence[str], *, check=True) -> CompletedProcess` — argv `["talosctl", "--talosconfig", path, "-e", ",".join(endpoints), *args]` (no `-n`); `TalosSession.call_global_redacted(args, endpoints) -> int` — like `call_redacted` but via `run_global`; `talos_session_from(runner: Runner, path: Path) -> TalosSession` (plain constructor with `require_tool("talosctl")` and an `is_file` check → `ValidationError`); `lifecycle.node_drift(talos, settings, name, config: bytes) -> str` — quiet dry-run (`talos.run(..., check=False)` inside `private_file`), returns `inspect.config_drift(stdout+stderr)`; raises `CommandFailed` on non-zero exit like `_dry_run` (message without output).

- [ ] **Step 1: Failing tests**
  - `run_global` argv has `-e a,b` and no `-n` (FakeRunner from `tests/helpers.py`).
  - `call_global_redacted` prints redacted output: FakeRunner output `key: SECRET\n` → captured stdout contains `<redacted>` and not `SECRET` (extend FakeRunner use as in existing client tests, or use a `spy("talosctl")`).
  - `node_drift` returns `"in sync"` for spy output `No changes.` and `"changes"` for `Config diff:\n+ foo`, prints nothing (capfd empty), raises `CommandFailed` on rc 1.
  - `talos_session_from` on a missing path raises `ValidationError`.
- [ ] **Step 2: FAIL** — focused run.
- [ ] **Step 3: Implement** as specified above (reuse `_print_redacted`; `node_drift` shares the `private_file` + argv building with `_dry_run` — extract a small helper `_dry_run_args(address, path, mode)` rather than duplicating).
- [ ] **Step 4: PASS** — focused run, then `uv run pytest -q`.
- [ ] **Step 5: Commit** `feat: quiet drift check and endpoint-only talosctl calls`

---

### Task 3: rotation flow

**Files:**
- Modify: `src/talkub/rotation.py`
- Test: `tests/test_rotation_ca.py`

**Interfaces:**
- Consumes: Task 1–2; `lifecycle.prepare(ctx, [], refresh=False)`, `lifecycle.wait_healthy`, `tailscale.controlplanes`, `tailscale.rewrite_talosconfig(data, endpoints, node)`, `talos_session`, `private_tempdir`, `ctx.sops`.
- Produces: `previous_path(repo) -> Path` (`repo.talos_dir / "talosconfig.previous.sops.yaml"`); `rotate(ctx, confirm: Callable[[str], bool]) -> list[str]` (notes); `finish(ctx, talosconfig: Path | None = None) -> list[str]`; exception type use: `ValidationError`/`CommandFailed` from `talkub.errors`.

Behaviour (spec §3), implement exactly:

```python
def rotate(ctx, confirm):
    repo = ctx.repo
    if previous_path(repo).exists():
        raise ValidationError(
            "a CA rotation is in progress", hint="talkub rotate-ca --finish"
        )
    settings, order, configs = prepare(ctx, [], refresh=False)
    cps = [n for n in order if settings.nodes[n].role == "controlplane"]
    workers = [n for n in order if n not in cps]
    cp_addrs = [settings.mgmt_address(n) for n in cps]
    rotate_args = [
        "rotate-ca",
        "--talos=true",
        "--kubernetes=false",
        "--with-docs=false",
        "--with-examples=false",
        "--control-plane-nodes",
        ",".join(cp_addrs),
    ]
    if workers:
        rotate_args += [
            "--worker-nodes",
            ",".join(settings.mgmt_address(n) for n in workers),
        ]
    with talos_session(ctx) as talos:
        wait_healthy(talos, cp_addrs[0])
        drifting = [
            n for n in order if node_drift(talos, settings, n, configs[n]) != "in sync"
        ]
        if drifting:
            raise ValidationError(
                f"the repo is not in sync with: {', '.join(drifting)}",
                hint="run `talkub plan`/`talkub apply` first",
            )
        print("== rotate-ca dry-run", flush=True)
        if talos.call_global_redacted([*rotate_args, "--dry-run=true"], cp_addrs) != 0:
            raise CommandFailed("rotate-ca dry-run failed")
        if not confirm("Rotate the Talos CA now?"):
            raise ValidationError("aborted; nothing was changed")
        atomic_write(previous_path(repo), repo.talosconfig.read_bytes())
        work = Path(
            tempfile.mkdtemp(prefix="talkub-rotate-")
        )  # 0700; deleted only on success
        new = work / "talosconfig"
        try:
            code = talos.call_global_redacted(
                [*rotate_args, "--dry-run=false", "-o", str(new)], cp_addrs
            )
            if code != 0 or not new.is_file():
                raise CommandFailed(f"rotate-ca failed (exit {code})")
            _store_talosconfig(ctx, settings, new.read_bytes())
        except BaseException:
            print(
                f"warning: the new talosconfig (if any) is kept in {work}; if the rotation completed, "
                f"only it works: `talkub rotate-ca --finish --talosconfig {new}`; delete {work} afterwards",
                file=sys.stderr,
                flush=True,
            )
            raise
        shutil.rmtree(work, ignore_errors=True)
    return finish(ctx)
```

- `_store_talosconfig(ctx, settings, data)`: `rewrite_talosconfig(data, cp_addrs_of(settings), cp_addrs[0])`, encrypt to `repo.talosconfig` (atomic). Endpoints = mgmt of all control planes, node = first.
- `finish(ctx, talosconfig=None)`:
  1. `previous_path` missing → `ValidationError("no CA rotation in progress")`.
  2. `talosconfig` given → `_store_talosconfig(ctx, settings, talosconfig.read_bytes())`, note "delete <path> now".
  3. `with talos_session(ctx) as talos:` `out = talos.run(cp_addrs[0], ["get", "machineconfig", "-o", "yaml"]).stdout.decode()`; `crt, key = inspect.machine_ca(out)`; plausibility via base64 decode prefixes (`-----BEGIN CERTIFICATE-----`, `-----BEGIN`) else `ValidationError` (never include values in messages).
  4. Bundle: `plain = ctx.sops.decrypt(repo.talos_secrets)`; `atomic_write(repo.talos_secrets, ctx.sops.encrypt(replace_os_ca(plain, crt, key), filename=..., public_key=...))`.
  5. Final check in a fresh `talos_session`: `talos.run(addr, ["version"])` for every node (check=True), then `prepare(ctx, [], refresh=False)` + `node_drift` for every node. All "in sync" → delete `previous_path`, notes `["the Talos CA was rotated", "commit talos/"]`; otherwise keep it and raise `ValidationError(f"still not in sync: …", hint="inspect with talkub plan; the old talosconfig is in talos/talosconfig.previous.sops.yaml")`.
- Imports at module top (`shutil`, `sys`, `tempfile`, `Path`, `atomic_write`, `inspect`, lifecycle and tailscale helpers). If importing `tailscale` from `rotation` creates a cycle, move `rewrite_talosconfig`/`controlplanes` usage behind a local import and note it.

- [ ] **Step 1: Failing tests** in `tests/test_rotation_ca.py`, using `talos_repo` (real encrypted secrets bundle + talosconfig) and a `spy("talosctl")` with `.real("gen")`/`.real("validate")` like `tests/test_bootstrap.py`'s `cluster` fixture, plus rules:
  - `apply-config --dry-run` → stdout `No changes.` (in sync)
  - `health` → rc 0; `version` → rc 0
  - `rotate-ca --dry-run=true` → stdout containing `  key: SUPERSECRETCAKEY\n` and `        key: CLIENTKEYSECRET\n`
  - `rotate-ca --dry-run=false` → must create the `-o` file: use a rule with `exec` pointing to a tiny script written in the test (writes a valid talosconfig YAML with context `test`, endpoints, ca/crt/key values, to the path after `-o`, prints `key: SUPERSECRETCAKEY`) — see `Spy.real`/`exec` in `tests/helpers.py`.
  - `get machineconfig` → stdout = `_resource(MACHINE)` with new CRT/KEY (reuse the constants).
  Tests:
  1. happy path with `confirm=lambda q: True`: order of calls (health → apply-config dry-runs → rotate-ca dry-run → rotate-ca real → get machineconfig → version → apply-config dry-runs); decrypted repo talosconfig has endpoints = cp mgmt; decrypted bundle `certs.os` = new CRT/KEY; `previous_path` gone; captured stdout+stderr contain neither `SUPERSECRETCAKEY` nor `CLIENTKEYSECRET`.
  2. `confirm=lambda q: False`: `ValidationError`; no `rotate-ca --dry-run=false` call; talosconfig and bundle bytes unchanged; no `previous` file.
  3. pre-check drift (`apply-config --dry-run` → `Config diff:\n+ x`): `ValidationError` naming the node; no rotate-ca call.
  4. `previous` exists → `ValidationError` with hint `--finish`, no talosctl node calls.
  5. real rotate-ca rc 1: `CommandFailed`; stderr names a kept directory that still exists; `previous` exists; bundle unchanged. Clean the kept dir in the test.
  6. `finish` without rotation → `ValidationError("no CA rotation in progress")`.
  7. `finish(ctx, talosconfig=path)`: repo talosconfig replaced (decrypt and compare context/crt), bundle updated, previous removed.
  8. final check drift → `previous` kept, `ValidationError`.
- [ ] **Step 2: FAIL**. **Step 3: Implement.** **Step 4: PASS** focused, then full suite.
- [ ] **Step 5: Commit** `feat: talkub rotate-ca flow with resumable finish`

---

### Task 4: CLI `talkub rotate-ca`

**Files:**
- Create: `src/talkub/cli/rotate.py`; Modify: `src/talkub/cli/main.py`, `README.md` (one short section)
- Test: `tests/test_cli_rotate.py` (new)

```python
"""rotate-ca: rotate the Talos API CA."""

from __future__ import annotations

from pathlib import Path

import typer

from talkub import rotation
from talkub.errors import ValidationError


def rotate_ca(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Do not ask before rotating."),
    finish: bool = typer.Option(
        False, "--finish", help="Finish an interrupted rotation."
    ),
    talosconfig: Path = typer.Option(
        None,
        "--talosconfig",
        help="With --finish: plaintext talosconfig left by an interrupted rotation.",
    ),
) -> None:
    """Rotate the Talos API CA and update the talosconfig and secrets bundle in the repo."""
    context = ctx.obj.context()
    if talosconfig is not None and not finish:
        raise ValidationError("--talosconfig needs --finish")
    if finish:
        notes = rotation.finish(
            context, talosconfig.expanduser() if talosconfig else None
        )
    else:
        notes = rotation.rotate(
            context,
            confirm=lambda question: yes or typer.confirm(question, default=False),
        )
    for note in notes:
        print(f"note: {note}")
```

Register: `from talkub.cli import rotate as rotate_cli` and `app.command("rotate-ca")(rotate_cli.rotate_ca)`.

README: a "Rotating the Talos CA" section: when (key exposure), what it does, `--finish`, commit `talos/` afterwards.

Tests (spy talosctl as in Task 3 or monkeypatch `rotation.rotate/finish` to record calls — prefer monkeypatch here since Task 3 covers the flow): `--talosconfig` without `--finish` → exit 1; `--finish` calls `finish` with the path; plain call passes a confirm that returns True with `--yes` without prompting; notes printed.

- [ ] Steps: failing tests → implement → PASS → full suite → commit `feat: talkub rotate-ca command`
