# Token Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new shortcut kind `token` that creates a ServiceAccount token with `kubectl create token` and delivers it exactly like a `secret` shortcut (clipboard with auto-clear, or stdout with `--print`).

**Architecture:** `config/shortcuts.py` parses/validates/serializes the new kind (`namespace`, `serviceAccount`, optional `duration`) into two new `Shortcut` fields. `workstation/shortcuts.py` extracts the delivery half of `_secret` into `_deliver()` and adds `_token()`, which captures the token from kubectl's stdout and hands it to `_deliver()`. CLI help and README are updated.

**Tech Stack:** Python 3.12, pytest (with the `spy` fake-binary fixture), uv.

**Spec:** `docs/superpowers/specs/2026-09-29-talkub-shortcut-token-design.md`

## Global Constraints

- YAML form: `token: {namespace: <label>, serviceAccount: <label>, duration: <optional Go duration>}`.
- `duration`: one or more `<digits><unit>` blocks, units `h`, `m`, `s` in that order (`8h`, `30m`, `1h30m`), total not zero; missing → no `--duration` flag.
- Load error for a bad duration: `.talkub.yaml: shortcuts.<name>.token.duration must be a duration like 8h or 1h30m`.
- `pipe:` next to `token` is an error; `token` takes no extra arguments.
- The token never reaches argv, env, the terminal or a file; trailing whitespace is stripped; empty output is an error.
- `--print` allowed for `secret` and `token`; message otherwise: `--print only applies to secret and token shortcuts, <name> is <kind>`.
- Clipboard clearing exactly as for `secret` (`TALKUB_CLIPBOARD_CLEAR_SECONDS`, default 45).
- Code, comments, CLI strings in English; line length 120; Conventional Commits ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; README in sync.

## Review Focus

1. kubectl's output ends with `\n` → the clipboard must get the token without it (a pasted newline can submit a login form early). Test in Task 2.
2. The token must not appear in talkub's own stdout/stderr or anywhere in the repo after copying. Test in Task 2.
3. `duration: 30` in YAML is an int, not a string → load error, not a crash. Test in Task 1.
4. `--save-global` of a token shortcut → it round-trips through `shortcut_to_dict` unchanged, with and without `duration`. Test in Task 1.
5. kubectl fails (unknown ServiceAccount) → `CommandFailed` with kubectl's stderr as the hint, nothing copied. Test in Task 2.

---

### Task 1: Parse and serialize `token` shortcuts

**Files:**
- Modify: `src/talkub/config/shortcuts.py` (kinds tuple, duration regex, `Shortcut` fields, `shortcut_from_dict`, `shortcut_to_dict`)
- Test: `tests/config/test_config_shortcuts.py` (append)

**Interfaces:**
- Produces: `Shortcut.service_account: str | None` and `Shortcut.duration: str | None` (both default `None`); `Shortcut.kind == "token"`; `namespace` is reused.

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_config_shortcuts.py`:

```python
def test_token_is_parsed_and_round_trips():
    spec = {"description": "login", "token": {"namespace": "headlamp", "serviceAccount": "headlamp", "duration": "8h"}}
    shortcut = shortcut_from_dict("hl", spec)
    assert shortcut.kind == "token"
    assert (shortcut.namespace, shortcut.service_account, shortcut.duration) == ("headlamp", "headlamp", "8h")
    assert shortcut_to_dict(shortcut) == spec
    assert shortcut_from_dict("hl", shortcut_to_dict(shortcut)) == shortcut


def test_token_without_duration():
    spec = {"token": {"namespace": "headlamp", "serviceAccount": "headlamp"}}
    shortcut = shortcut_from_dict("hl", spec)
    assert shortcut.duration is None
    assert shortcut_to_dict(shortcut) == spec


@pytest.mark.parametrize("duration", ["8h", "30m", "45s", "1h30m", "1h0m", "2h30m15s", "90m"])
def test_valid_token_durations(duration):
    spec = {"token": {"namespace": "a", "serviceAccount": "b", "duration": duration}}
    assert shortcut_from_dict("x", spec).duration == duration


@pytest.mark.parametrize("duration", ["8hours", "0h", "0h0m", "h", "8", "-1h", "", "1.5h", "30m1h", "8H", 30, True])
def test_invalid_token_durations(duration):
    spec = {"token": {"namespace": "a", "serviceAccount": "b", "duration": duration}}
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", spec)
    assert str(exc.value) == ".talkub.yaml: shortcuts.x.token.duration must be a duration like 8h or 1h30m"


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("headlamp", "shortcuts.x.token must be a mapping"),
        ({"serviceAccount": "b"}, "shortcuts.x.token.namespace must be a valid DNS label"),
        ({"namespace": "a"}, "shortcuts.x.token.serviceAccount must be a valid DNS label"),
        ({"namespace": "a", "serviceAccount": "B_x"}, "shortcuts.x.token.serviceAccount must be a valid DNS label"),
    ],
)
def test_invalid_token(token, message):
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", {"token": token})
    assert str(exc.value) == f".talkub.yaml: {message}"


def test_token_is_one_of_the_kinds():
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", {"token": {"namespace": "a", "serviceAccount": "b"}, "run": ["k", "get", "pods"]})
    assert "token" in str(exc.value)
```

Also add `{"token": {"namespace": "a", "serviceAccount": "b"}}` to the parametrize list of the existing `test_pipe_only_with_run_or_kubernetes`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_config_shortcuts.py -v`
Expected: the new tests FAIL (`must have exactly one of: …` or `AttributeError: 'Shortcut' object has no attribute 'service_account'`).

- [ ] **Step 3: Implement**

In `src/talkub/config/shortcuts.py`:

```python
_SHORTCUT_KINDS = ("secret", "token", "job", "run", "portForward", "kubernetes")
```

Below `_valid_port_spec`:

```python
# `kubectl create token --duration`: Go duration restricted to h/m/s blocks in that order, e.g. 8h or 1h30m.
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _valid_duration(value: object) -> bool:
    match = _DURATION.match(value) if isinstance(value, str) else None
    return match is not None and any(group is not None and int(group) > 0 for group in match.groups())
```

In `Shortcut` (after `key`), and update the `kind` comment and the `namespace` comment:

```python
    kind: str  # "secret" | "token" | "job" | "run" | "portForward" | "kubernetes"
    description: str | None = None
    namespace: str | None = None  # secret, token, job, portForward
    secret: str | None = None  # secret: Secret name
    key: str | None = None  # secret
    service_account: str | None = None  # token
    duration: str | None = None  # token: passed as --duration if set
```

In `shortcut_from_dict`, after the `secret` branch:

```python
    if kind == "token":
        raw = spec["token"]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{MARKER}: {where}.token must be a mapping")
        namespace = label(raw.get("namespace"), f"{where}.token.namespace")
        service_account = label(raw.get("serviceAccount"), f"{where}.token.serviceAccount")
        duration = raw.get("duration")
        if duration is not None and not _valid_duration(duration):
            raise ValidationError(f"{MARKER}: {where}.token.duration must be a duration like 8h or 1h30m")
        return Shortcut(
            name=name,
            kind=kind,
            description=description,
            namespace=namespace,
            service_account=service_account,
            duration=duration,
        )
```

In `shortcut_to_dict`, after the `secret` branch:

```python
    elif shortcut.kind == "token":
        token: dict = {"namespace": shortcut.namespace, "serviceAccount": shortcut.service_account}
        if shortcut.duration is not None:
            token["duration"] = shortcut.duration
        data["token"] = token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/config -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests && uv run ruff format src tests
git add src/talkub/config/shortcuts.py tests/config/test_config_shortcuts.py
git commit -m "feat(shortcuts): parse token shortcuts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Run `token` shortcuts, docs

**Files:**
- Modify: `src/talkub/workstation/shortcuts.py` (`run`, `_secret`, new `_deliver`, new `_token`)
- Modify: `src/talkub/cli/main.py:88` and `:106` (`--print` help)
- Modify: `README.md` (Shortcuts section)
- Test: `tests/workstation/test_shortcuts.py` (extend `SHORTCUTS`, append tests)

**Interfaces:**
- Consumes: `Shortcut.kind == "token"`, `Shortcut.namespace`, `Shortcut.service_account`, `Shortcut.duration` (Task 1).
- Produces (module-private): `_deliver(ctx: Context, name: str, value: bytes, print_value: bool) -> int`, `_token(ctx: Context, shortcut: Shortcut, print_value: bool) -> int`.

- [ ] **Step 1: Write the failing tests**

In `tests/workstation/test_shortcuts.py`, add to `SHORTCUTS` (after `"creds"`):

```python
    "hl-token": Shortcut(
        name="hl-token",
        kind="token",
        description="headlamp login",
        namespace="headlamp",
        service_account="headlamp",
        duration="8h",
    ),
    "short-token": Shortcut(name="short-token", kind="token", namespace="apps", service_account="ci"),
```

Add `"hl-token"` to the parametrize list of `test_extra_arguments_rejected_for_non_run_kinds`.

Append:

```python
TOKEN = "eyJhbGciOiJSUzI1NiJ9.payload.signature"


def test_token_goes_to_the_clipboard_without_the_newline(ctx, spy, detached, capfd):
    kubectl = spy("kubectl").on("create", "token", stdout=TOKEN + "\n")
    pbcopy = spy("pbcopy").on(read_stdin=True)

    assert shortcuts.run(ctx, "hl-token", []) == 0

    [create] = kubectl.calls
    assert create["args"] == ["-n", "headlamp", "create", "token", "headlamp", "--duration=8h"]
    assert create["kubeconfig"]
    [copy] = pbcopy.calls
    assert copy["stdin"] == TOKEN
    [(argv, sent)] = detached
    assert sent == hashlib.sha256(TOKEN.encode()).hexdigest().encode()
    assert TOKEN not in " ".join(argv)
    out, err = capfd.readouterr()
    assert TOKEN not in out + err
    assert out.strip() == f"copied hl-token ({len(TOKEN)} chars); the clipboard is cleared in 45 s"
    for path in ctx.repo.root.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


def test_token_without_duration_passes_no_flag(ctx, spy, detached):
    kubectl = spy("kubectl").on("create", "token", stdout=TOKEN + "\n")
    spy("pbcopy").on(read_stdin=True)
    shortcuts.run(ctx, "short-token", [])
    [create] = kubectl.calls
    assert create["args"] == ["-n", "apps", "create", "token", "ci"]


def test_token_print_writes_only_the_token(ctx, spy, detached, monkeypatch):
    spy("kubectl").on("create", "token", stdout=TOKEN + "\n")
    pbcopy = spy("pbcopy").on(read_stdin=True)
    buffer = io.BytesIO()
    fake_stdout = io.TextIOWrapper(buffer)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    assert shortcuts.run(ctx, "hl-token", [], print_value=True) == 0

    fake_stdout.flush()
    assert buffer.getvalue() == TOKEN.encode()  # not a terminal: no trailing newline
    assert pbcopy.calls == []
    assert detached == []


def test_token_empty_output_is_an_error(ctx, spy, detached):
    spy("kubectl").on("create", "token", stdout="\n")
    pbcopy = spy("pbcopy").on(read_stdin=True)
    with pytest.raises(CommandFailed) as exc:
        shortcuts.run(ctx, "hl-token", [])
    assert str(exc.value) == "kubectl printed no token for headlamp/headlamp"
    assert pbcopy.calls == []


def test_token_kubectl_failure_copies_nothing(ctx, spy, detached):
    spy("kubectl").on("create", "token", stderr='serviceaccounts "headlamp" not found', rc=1)
    pbcopy = spy("pbcopy").on(read_stdin=True)
    with pytest.raises(CommandFailed) as exc:
        shortcuts.run(ctx, "hl-token", [])
    assert "not found" in (exc.value.hint or "")
    assert pbcopy.calls == []
    assert detached == []


def test_print_message_names_secret_and_token(ctx, spy):
    spy("kubectl")
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "backup", [], print_value=True)
    assert str(exc.value) == "--print only applies to secret and token shortcuts, backup is job"
```

In `test_listing`, add:

```python
    token = next(line for line in lines if line.startswith("hl-token"))
    assert token.split() == ["hl-token", "token", "headlamp", "login", "project"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/workstation/test_shortcuts.py -v -k "token or print or extra or listing"`
Expected: token tests FAIL (the kind falls through to `_port_forward`, or `--print only applies to secret shortcuts`).

- [ ] **Step 3: Implement**

In `src/talkub/workstation/shortcuts.py`, update the module docstring's first paragraph to:

```python
"""Run the shortcuts defined in .talkub.yaml and in the global pool (`talkub run <name>`).

A secret's value or a fresh token is held in memory only: it goes to the clipboard tool on stdin
(or to stdout with --print) and never into argv, env, the terminal or a file.
"""
```

Add a constant next to `_TAKES_EXTRA`:

```python
_DELIVERED = ("secret", "token")  # value goes to the clipboard or, with --print, to stdout
```

In `run()`, replace the `--print` check:

```python
    if print_value and shortcut.kind not in _DELIVERED:
        raise ValidationError(f"--print only applies to secret and token shortcuts, {name} is {shortcut.kind}")
```

and dispatch after the `secret` branch:

```python
    if shortcut.kind == "token":
        return _token(ctx, shortcut, print_value)
```

Replace the delivery part of `_secret` (everything from `if print_value:` to its `return 0`) with `return _deliver(ctx, shortcut.name, value, print_value)`, and add below `_secret`:

```python
def _token(ctx: Context, shortcut: Shortcut, print_value: bool) -> int:
    ns, account = shortcut.namespace, shortcut.service_account
    args = ["-n", ns, "create", "token", account]
    if shortcut.duration is not None:
        args.append(f"--duration={shortcut.duration}")
    with kube_session(ctx) as kube:
        value = kube.kubectl(args).stdout.rstrip()
    if not value:
        raise CommandFailed(f"kubectl printed no token for {ns}/{account}")
    return _deliver(ctx, shortcut.name, value, print_value)


def _deliver(ctx: Context, name: str, value: bytes, print_value: bool) -> int:
    """Write `value` to stdout (--print) or copy it to the clipboard and schedule the clearer."""
    if print_value:
        sys.stdout.flush()
        sys.stdout.buffer.write(value + (b"\n" if sys.stdout.isatty() else b""))
        sys.stdout.buffer.flush()
        return 0
    clipboard.copy(ctx.runner, value)
    try:
        clipboard.schedule_clear(ctx.runner, value)
    except TalkubError:
        print("warning: the clipboard will not be cleared automatically", file=sys.stderr)
    try:
        size = f"{len(value.decode())} chars"
    except UnicodeDecodeError:
        size = f"{len(value)} bytes"
    print(f"copied {name} ({size}); the clipboard is cleared in {clipboard.clear_seconds():g} s")
    return 0
```

In `src/talkub/cli/main.py`:
- line 88: `  --print        secret, token: write the value to stdout instead of the clipboard`
- line 106: `help="secret, token: write the value to stdout."`

In `README.md`, Shortcuts section:
- In the YAML example after `db-password`, add:
  ```yaml
    headlamp-token:
      description: fresh Headlamp login token
      token: {namespace: headlamp, serviceAccount: headlamp, duration: 8h}
  ```
- In the command examples after the `--print db-password` line, add:
  `    talkub run headlamp-token               # copy a fresh ServiceAccount token to the clipboard`
- In the kinds list after the `secret` item, add:
  ```markdown
  - `token`: creates a token for the ServiceAccount with `kubectl create token` (`--duration` only if
    `duration` is set, e.g. `8h` or `1h30m`; otherwise the API server's default applies) and delivers it
    like `secret`: to the clipboard, cleared after 45 s, or with `--print` to stdout.
  ```

- [ ] **Step 4: Run the full suite and checks**

Run: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports && uv run basedpyright`
Expected: all PASS / no new basedpyright errors compared with `main`.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/workstation/shortcuts.py src/talkub/cli/main.py README.md tests/workstation/test_shortcuts.py
git commit -m "feat(shortcuts): token shortcuts copy a fresh ServiceAccount token

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
