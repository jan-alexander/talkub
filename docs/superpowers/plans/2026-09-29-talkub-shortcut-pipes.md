# Pipes in Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `run` and `kubernetes` shortcuts get an optional `pipe:` (list of argv stages) whose stages receive the first stage's output, without credentials.

**Architecture:** `core/runner.py` gains `pipe_status()` (pure pipefail rule with SIGPIPE exception) and `Runner.pipeline()` (Popen chain). `config/shortcuts.py` parses/validates/serializes `Shortcut.pipe`. `workstation/access.py` checks all stage tools before decrypting and runs a pipeline whose later stages see `KUBECONFIG`/`TALOSCONFIG` pointing at a missing file. `workstation/shortcuts.py` fills `{node…}` in pipe stages, passes `pipe` on and marks piped kinds in the listing.

**Tech Stack:** Python 3.12 `subprocess`, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-09-29-talkub-shortcut-pipes-design.md`

## Global Constraints

- `pipe:` only with `run` or `kubernetes`; non-empty list of non-empty string lists; stage program must be non-blank.
- `{kubeconfig}` in a pipe stage is a load error; `{node…}` in pipe stages is validated like the first stage and filled with the same node.
- Extras from the call are appended to the first stage only.
- All stage programs are checked with `require_tool` before anything is decrypted.
- Only the first stage gets credentials; later stages get `child_env` plus `KUBECONFIG`/`TALOSCONFIG` pointing at a non-existent file in the private temp dir.
- Exit code: rightmost non-zero stage code (signal N → 128+N); a non-last stage that died of SIGPIPE (141) counts as 0.
- `Runner.call()` stays unchanged. `core/runner.Runner` stays the only place that starts processes.
- Code, comments, CLI strings in English; line length 120; Conventional Commits ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; README in sync.
- Layer contract: `config` must not import `workstation` (so `{kubeconfig}` is spelled as a literal in config). Check with `uv run lint-imports`.

## Review Focus

1. Ctrl-C while a pipeline runs → talkub waits for all stages and returns their pipefail code (no orphaned stages, no traceback). Test in Task 1.
2. `… | head -n 1` on an endless producer → finishes promptly with 0 (parent closed its pipe ends; SIGPIPE exception). Test in Task 1.
3. A `kubectl`/`talosctl` in a later stage while the user has their own `KUBECONFIG` exported → must not reach that cluster. Test in Task 3.
4. A typo'd program in a later stage → `ToolMissing` before decrypting (no hardware-key touch). Test in Task 3.
5. `pipe:` survives `--save-global` into the global pool unchanged. Test in Task 2.

---

### Task 1: `Runner.pipeline` and `pipe_status`

**Files:**
- Modify: `src/talkub/core/runner.py` (new constant, function after `require_tool`, method after `call`)
- Test: `tests/core/test_runner.py` (append)

**Interfaces:**
- Produces (in `talkub.core.runner`):
  - `SIGPIPE_EXIT: int` (= 128 + SIGPIPE = 141)
  - `pipe_status(codes: Sequence[int]) -> int`
  - `Runner.pipeline(self, stages: Sequence[Sequence[str]], *, env: Mapping[str, str] | None = None, rest_env: Mapping[str, str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_runner.py` (add `pipe_status` to the existing `from talkub.core.runner import ...` line):

```python
@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([0], 0),
        ([3], 3),
        ([0, 0], 0),
        ([1, 0], 1),
        ([0, 1], 1),
        ([2, 3], 3),
        ([2, 0, 0], 2),
        ([141, 0], 0),
        ([141, 1], 1),
        ([0, 141], 141),
        ([141, 141, 0], 0),
    ],
)
def test_pipe_status(codes, expected):
    assert pipe_status(codes) == expected


def test_pipe_status_needs_a_stage():
    with pytest.raises(ValueError):
        pipe_status([])


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


UPPER = "import sys; sys.stdout.write(sys.stdin.read().upper())"


def test_pipeline_passes_output_through_the_stages(capfd):
    stages = [_py("print('abc')"), _py(UPPER), _py("import sys; sys.stdout.write(sys.stdin.read() * 2)")]
    assert Runner().pipeline(stages) == 0
    assert capfd.readouterr().out == "ABC\nABC\n"


def test_pipeline_single_stage_is_like_call(capfd):
    assert Runner().pipeline([_py("print('x'); raise SystemExit(4)")]) == 4
    assert capfd.readouterr().out == "x\n"


def test_pipeline_first_stage_failure_is_not_hidden(capfd):
    assert Runner().pipeline([_py("raise SystemExit(5)"), _py(UPPER)]) == 5


def test_pipeline_rightmost_failure_wins(capfd):
    assert Runner().pipeline([_py("raise SystemExit(2)"), _py("import sys; sys.stdin.read(); sys.exit(3)")]) == 3


def test_pipeline_signal_death_maps_to_shell_code(capfd):
    kill = "import os, signal, sys; sys.stdin.read(); os.kill(os.getpid(), signal.SIGTERM)"
    assert Runner().pipeline([_py("print('x')"), _py(kill)]) == 143


def test_pipeline_env_only_for_the_first_stage(monkeypatch, capfd):
    monkeypatch.setenv("TALKUB_TEST_PARENT", "inherited")
    first = "import os; print(os.environ.get('A'), os.environ.get('B'), os.environ.get('TALKUB_TEST_PARENT'))"
    rest = "import os, sys; print(sys.stdin.read().strip(), os.environ.get('A'), os.environ.get('B'))"
    assert Runner().pipeline([_py(first), _py(rest)], env={"A": "1"}, rest_env={"B": "2"}) == 0
    assert capfd.readouterr().out == "1 None inherited None 2\n"


def test_pipeline_early_close_by_the_reader_is_success():
    # `yes` writes forever and dies of SIGPIPE once `head` exits: without closing the parent's pipe ends
    # it never gets SIGPIPE and this hangs; its 141 must not count as failure. (A Python writer would not
    # do: the interpreter ignores SIGPIPE and exits 1 with BrokenPipeError.) Run in a child process so a
    # hang fails the test via the timeout instead of blocking the suite.
    script = (
        "from talkub.core.runner import Runner; raise SystemExit(Runner().pipeline([['yes'], ['head', '-n', '1']]))"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20)
    assert proc.returncode == 0
    assert proc.stdout == b"y\n"


def test_pipeline_missing_tool_starts_nothing(tmp_path):
    marker = tmp_path / "started"
    with pytest.raises(ToolMissing):
        Runner().pipeline([_py(f"open({str(marker)!r}, 'w')"), ["talkub-no-such-tool"]])
    assert not marker.exists()


def test_pipeline_waits_for_all_stages_on_ctrl_c(capfd):
    slow = "import time; time.sleep(1); print('marker')"
    start = time.monotonic()
    timer = _send_later(signal.SIGINT)
    try:
        assert Runner().pipeline([_py(slow), _py(UPPER)]) == 0
    finally:
        timer.cancel()
    assert time.monotonic() - start >= 1.0
    assert capfd.readouterr().out == "MARKER\n"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_runner.py -q`
Expected: collection error `ImportError: cannot import name 'pipe_status'`.

- [ ] **Step 3: Implement**

In `src/talkub/core/runner.py` add after `require_tool` (`Sequence` and `signal` are already imported; check and add if not):

```python
SIGPIPE_EXIT = 128 + signal.SIGPIPE


def pipe_status(codes: Sequence[int]) -> int:
    """Exit code of a pipeline, like `set -o pipefail`: the rightmost non-zero stage code. A stage before
    the last that died of SIGPIPE only saw its reader stop early (`… | head`), which is not a failure."""
    if not codes:
        raise ValueError("a pipeline needs at least one stage")
    *front, last = codes
    relevant = [0 if code == SIGPIPE_EXIT else code for code in front] + [last]
    return next((code for code in reversed(relevant) if code != 0), 0)
```

Add after `Runner.call`:

```python
    def pipeline(
        self,
        stages: Sequence[Sequence[str]],
        *,
        env: Mapping[str, str] | None = None,
        rest_env: Mapping[str, str] | None = None,
    ) -> int:
        """Run `stages` connected stdout → stdin like a shell pipe, with inherited stdin (first stage),
        stdout (last stage) and stderr (all). Only the first stage gets `env`; the others get `rest_env`.
        Returns pipe_status() of the stage codes; a stage killed by signal N counts as 128 + N."""
        for stage in stages:
            require_tool(stage[0])
        procs: list[subprocess.Popen[bytes]] = []
        previous = None
        try:
            for index, stage in enumerate(stages):
                proc = subprocess.Popen(
                    list(stage),
                    stdin=previous,
                    stdout=None if index == len(stages) - 1 else subprocess.PIPE,
                    env=child_env(env if index == 0 else rest_env),
                )
                if previous is not None:
                    previous.close()  # the child holds its own copy; ours would keep the writer from SIGPIPE
                previous = proc.stdout
                procs.append(proc)
        except BaseException:
            if previous is not None:
                previous.close()
            _stop(procs)
            raise
        codes = []
        for proc in procs:
            while True:
                try:
                    code = proc.wait()
                    break
                except KeyboardInterrupt:
                    # The stages got SIGINT too (same process group); let them decide when to exit.
                    continue
                except BaseException:
                    _stop(procs)
                    raise
            codes.append(128 - code if code < 0 else code)
        return pipe_status(codes)
```

And a module-level helper after `_HELD_SIGNALS`/`_redeliver` (end of file):

```python
def _stop(procs: Sequence[subprocess.Popen[bytes]]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        proc.wait()
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/core/test_runner.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/core/runner.py tests/core/test_runner.py
git commit -m "feat(runner): run a pipeline of processes with pipefail exit codes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `pipe:` in shortcut definitions

**Files:**
- Modify: `src/talkub/config/shortcuts.py`
- Test: `tests/config/test_config_shortcuts.py` (append), `tests/config/test_user.py` (append)

**Interfaces:**
- Consumes: `_check_placeholders(argv: list[str], where: str)`, `NODE_PLACEHOLDER` (existing).
- Produces: `Shortcut.pipe: tuple[tuple[str, ...], ...] = ()`; `needs_node` also scans `pipe`; `shortcut_to_dict` writes `pipe` after `run`/`kubernetes`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_config_shortcuts.py`:

```python
def test_pipe_is_parsed_and_round_trips():
    spec = {"run": ["t", "get", "extensions", "-o", "json"], "pipe": [["jq", "-r", ".spec.name"], ["sort"]]}
    shortcut = shortcut_from_dict("ext", spec)
    assert shortcut.pipe == (("jq", "-r", ".spec.name"), ("sort",))
    assert shortcut_to_dict(shortcut) == spec
    assert shortcut_from_dict("ext", shortcut_to_dict(shortcut)) == shortcut


def test_pipe_with_kubernetes():
    shortcut = shortcut_from_dict("x", {"kubernetes": ["stern", "app"], "pipe": [["grep", "ERROR"]]})
    assert shortcut.pipe == (("grep", "ERROR"),)


def test_without_pipe_nothing_is_written():
    assert "pipe" not in shortcut_to_dict(shortcut_from_dict("p", {"run": ["k", "get", "pods"]}))


@pytest.mark.parametrize(
    ("pipe", "message"),
    [
        ([], "shortcuts.x.pipe must be a non-empty list of stages"),
        ("sort", "shortcuts.x.pipe must be a non-empty list of stages"),
        ([[]], "shortcuts.x.pipe[0] must be a non-empty list of strings"),
        ([["sort"], "jq"], "shortcuts.x.pipe[1] must be a non-empty list of strings"),
        ([["sort"], ["jq", 1]], "shortcuts.x.pipe[1] must be a non-empty list of strings"),
        ([["  "]], "shortcuts.x.pipe[0] must be a non-empty list of strings"),
        ([["sh", "-c", "cat {kubeconfig}"]], "shortcuts.x.pipe[0]: {kubeconfig} is only available in the first stage"),
        ([["sort"], ["grep", "{node.adr}"]], "shortcuts.x.pipe[1]: unknown placeholder {node.adr}"),
    ],
)
def test_invalid_pipe(pipe, message):
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", {"run": ["k", "get", "pods"], "pipe": pipe})
    assert str(exc.value) == f".talkub.yaml: {message}"


@pytest.mark.parametrize(
    "spec",
    [
        {"secret": {"namespace": "a", "name": "b", "key": "c"}},
        {"job": {"namespace": "a", "cronjob": "b"}},
        {"portForward": {"namespace": "a", "service": "b", "ports": ["80"]}},
    ],
)
def test_pipe_only_with_run_or_kubernetes(spec):
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("x", {**spec, "pipe": [["sort"]]})
    assert str(exc.value) == ".talkub.yaml: shortcuts.x.pipe is only allowed with run or kubernetes"


def test_node_only_in_pipe_needs_a_node():
    shortcut = shortcut_from_dict("x", {"run": ["k", "get", "pods", "-A", "-o", "wide"], "pipe": [["grep", "{node.name}"]]})
    assert needs_node(shortcut)
```

Append to `tests/config/test_user.py` (uses the existing `global_file` fixture; add `Shortcut` import from `talkub.config` if not imported there):

```python
def test_pipe_survives_the_global_pool(global_file):
    from talkub.config import Shortcut

    shortcut = Shortcut(name="ext", kind="run", argv=("t", "get", "extensions"), pipe=(("sort",), ("uniq", "-c")))
    user.save_global_shortcut(shortcut)
    assert user.load_global_shortcuts() == {"ext": shortcut}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/config -q`
Expected: FAIL — `TypeError: Shortcut.__init__() got an unexpected keyword argument 'pipe'` and pipe keys ignored/rejected assertions.

- [ ] **Step 3: Implement**

In `src/talkub/config/shortcuts.py`:

Add to the `Shortcut` dataclass after `ports`:

```python
    pipe: tuple[tuple[str, ...], ...] = ()  # run, kubernetes: further stages fed by the first one's stdout
```

Change `needs_node` to scan the pipe too:

```python
def needs_node(shortcut: Shortcut) -> bool:
    """Whether running the shortcut needs a node: a run/kubernetes argument has a {node…} placeholder."""
    args = [*shortcut.argv, *(arg for stage in shortcut.pipe for arg in stage)]
    return shortcut.kind in ("run", "kubernetes") and any(NODE_PLACEHOLDER.search(arg) for arg in args)
```

Add after `_check_placeholders`:

```python
# workstation.access replaces it in the first stage only; spelled out here because config sits below workstation.
_KUBECONFIG_PLACEHOLDER = "{kubeconfig}"


def _pipe(spec: Mapping, where: str) -> tuple[tuple[str, ...], ...]:
    raw = spec.get("pipe")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"{MARKER}: {where}.pipe must be a non-empty list of stages")
    stages: list[tuple[str, ...]] = []
    for index, stage in enumerate(raw):
        at = f"{where}.pipe[{index}]"
        if (
            not isinstance(stage, list)
            or not stage
            or not all(isinstance(item, str) for item in stage)
            or not stage[0].strip()
        ):
            raise ValidationError(f"{MARKER}: {at} must be a non-empty list of strings")
        if any(_KUBECONFIG_PLACEHOLDER in arg for arg in stage):
            raise ValidationError(f"{MARKER}: {at}: {_KUBECONFIG_PLACEHOLDER} is only available in the first stage")
        _check_placeholders(stage, at)
        stages.append(tuple(stage))
    return tuple(stages)
```

In `shortcut_from_dict`, right after `description = optional_text(...)`:

```python
    if "pipe" in spec and kind not in ("run", "kubernetes"):
        raise ValidationError(f"{MARKER}: {where}.pipe is only allowed with run or kubernetes")
```

In the `run` and `kubernetes` branches, pass the pipe:

```python
        _check_placeholders(raw, f"{where}.run")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw), pipe=_pipe(spec, where))
```

```python
        _check_placeholders(raw, f"{where}.kubernetes")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw), pipe=_pipe(spec, where))
```

In `shortcut_to_dict`, leave the whole `if/elif/else` chain as it is and insert these two lines between its end
(the `portForward` `else:` block) and the final `return data`:

```python
    if shortcut.pipe:
        data["pipe"] = [list(stage) for stage in shortcut.pipe]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/config -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/config/shortcuts.py tests/config/test_config_shortcuts.py tests/config/test_user.py
git commit -m "feat(shortcuts): accept pipe stages in run and kubernetes shortcuts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Run piped shortcuts, listing, README

**Files:**
- Modify: `src/talkub/workstation/access.py` (`talosctl`, `kubectl`, `with_kubeconfig`, new helpers)
- Modify: `src/talkub/workstation/shortcuts.py` (`run()`, `listing()`)
- Modify: `README.md` (Shortcuts section)
- Create: `tests/workstation/test_shortcut_pipes.py` (pure listing test)
- Modify: `tests/workstation/test_shortcuts.py` (integration)

**Interfaces:**
- Consumes: `Runner.pipeline(stages, *, env, rest_env)` (Task 1); `Shortcut.pipe` (Task 2); `fill_node(argv, settings, node) -> list[str]` (existing).
- Produces: `access.talosctl(ctx, args, pipe=())`, `access.kubectl(ctx, args, pipe=())`, `access.with_kubeconfig(ctx, argv, extra=(), pipe=())`, `access.PIPE_NO_CREDENTIALS = "no-credentials-in-pipe"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/workstation/test_shortcut_pipes.py`:

```python
"""Listing of shortcuts with pipe stages (no external tools)."""

from __future__ import annotations

from talkub.config import Shortcut
from talkub.workstation import shortcuts


def test_listing_marks_piped_kinds():
    ext = Shortcut(name="ext", kind="run", argv=("t", "get", "extensions"), pipe=(("sort",),))
    pods = Shortcut(name="pods", kind="run", description="pods", argv=("k", "get", "pods"))
    lines = shortcuts.listing({"ext": (ext, "global"), "pods": (pods, "project")})
    assert [line.split() for line in lines] == [
        ["ext", "run", "|", "global"],
        ["pods", "run", "pods", "project"],
    ]
    assert lines[0].index("global") == lines[1].index("project")
```

In `tests/workstation/test_shortcuts.py` add to `SHORTCUTS`:

```python
    "ext-up": Shortcut(
        name="ext-up", kind="run", argv=("t", "get", "extensions"), pipe=(("tr", "a-z", "A-Z"),)
    ),
    "pods-grep": Shortcut(
        name="pods-grep", kind="run", argv=("k", "get", "pods"), pipe=(("grep", "{node.name}"),)
    ),
    "ui-up": Shortcut(
        name="ui-up", kind="kubernetes", argv=("fake-ui", "--config={kubeconfig}"), pipe=(("tr", "a-z", "A-Z"),)
    ),
```

and append:

```python
LATER_STAGE_CONFIGS = (
    'cat >/dev/null; for f in "$KUBECONFIG" "$TALOSCONFIG"; do '
    '[ -n "$f" ] && [ ! -e "$f" ] && echo missing; done'
)


def test_run_pipes_the_output(ctx, spy, capfd):
    talosctl = spy("talosctl").on("extensions", stdout="gvisor\n")
    assert shortcuts.run(ctx, "ext-up", ["-o", "yaml"]) == 0
    assert talosctl.argvs == [["get", "extensions", "-o", "yaml"]]  # extras go to the first stage
    assert capfd.readouterr().out == "GVISOR\n"


def test_first_stage_failure_is_the_exit_code(ctx, spy, capfd):
    spy("talosctl").on("extensions", rc=5)
    assert shortcuts.run(ctx, "ext-up", []) == 5


def test_node_placeholder_in_pipe_is_filled(ctx, spy, capfd):
    spy("kubectl").on("pods", stdout="a on cp-01\nb on w-01\n")
    assert shortcuts.run(ctx, "pods-grep", ["cp-01"]) == 0
    assert capfd.readouterr().out == "a on cp-01\n"


def test_missing_pipe_tool_reported_before_decrypting(ctx, spy, monkeypatch, tmp_path):
    spy("kubectl")  # the first stage's tool exists; only the pipe stage is missing
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-identities"))
    settings = ctx.repo.load_settings()
    settings.shortcuts["broken"] = Shortcut(
        name="broken", kind="run", argv=("k", "get", "pods"), pipe=(("talkub-no-such-tool",),)
    )
    ctx.repo.write_settings(settings)
    with pytest.raises(ToolMissing) as exc:
        shortcuts.run(ctx, "broken", [])
    assert "talkub-no-such-tool" in str(exc.value)


def test_later_stages_get_no_usable_cluster_config(ctx, spy, capfd, monkeypatch, tmp_path):
    own = tmp_path / "own-kubeconfig"
    own.write_text("the user's own cluster\n")
    monkeypatch.setenv("KUBECONFIG", str(own))
    monkeypatch.setenv("TALOSCONFIG", str(own))
    spy("kubectl").on("pods", stdout="x\n")
    settings = ctx.repo.load_settings()
    settings.shortcuts["probe"] = Shortcut(
        name="probe", kind="run", argv=("k", "get", "pods"), pipe=(("sh", "-c", LATER_STAGE_CONFIGS),)
    )
    ctx.repo.write_settings(settings)
    assert shortcuts.run(ctx, "probe", []) == 0
    assert capfd.readouterr().out == "missing\nmissing\n"


def test_kubernetes_shortcut_with_pipe(ctx, fake_bin, capfd):
    fake_bin("fake-ui", UI_REPORT)
    assert shortcuts.run(ctx, "ui-up", []) == 3  # fake-ui exits 3, tr 0: pipefail keeps the 3
    out = capfd.readouterr().out
    assert "ARG:--CONFIG=/" in out and "KUBE: MARKER" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/workstation/test_shortcut_pipes.py tests/workstation/test_shortcuts.py -q -k "pipe or listing or later or kubernetes_shortcut_with"`
Expected: FAIL — output not uppercased (pipe ignored), listing lacks `|`.

- [ ] **Step 3: Implement `access.py`**

In `src/talkub/workstation/access.py`:

```python
PIPE_NO_CREDENTIALS = "no-credentials-in-pipe"


def _require_stages(pipe: Sequence[Sequence[str]]) -> None:
    for stage in pipe:
        require_tool(stage[0])


def _launch(ctx: Context, argv: Sequence[str], env: dict[str, str], pipe: Sequence[Sequence[str]], workdir: Path) -> int:
    """Run `argv` with `env`; with pipe stages, feed its output through them. The stages get no credentials,
    and KUBECONFIG/TALOSCONFIG point at a missing file so a kubectl or talosctl there fails instead of
    silently using the caller's own default cluster."""
    if not pipe:
        return ctx.runner.call(argv, env=env)
    missing = str(workdir / PIPE_NO_CREDENTIALS)
    return ctx.runner.pipeline([argv, *pipe], env=env, rest_env={"KUBECONFIG": missing, "TALOSCONFIG": missing})


def talosctl(ctx: Context, args: Sequence[str], pipe: Sequence[Sequence[str]] = ()) -> int:
    require_tool("talosctl")
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.talosconfig, TALOSCONFIG_HINT)
    with decrypted(ctx.sops, {"talosconfig": source}) as paths:
        talosconfig = paths["talosconfig"]
        return _launch(ctx, ["talosctl", *args], {"TALOSCONFIG": str(talosconfig)}, pipe, talosconfig.parent)


def kubectl(ctx: Context, args: Sequence[str], pipe: Sequence[Sequence[str]] = ()) -> int:
    require_tool("kubectl")
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        kubeconfig = paths["kubeconfig"]
        return _launch(ctx, ["kubectl", *args], {"KUBECONFIG": str(kubeconfig)}, pipe, kubeconfig.parent)
```

`with_kubeconfig` becomes:

```python
def with_kubeconfig(
    ctx: Context, argv: Sequence[str], extra: Sequence[str] = (), pipe: Sequence[Sequence[str]] = ()
) -> int:
    """Run any program with the decrypted kubeconfig in KUBECONFIG. In `argv` (not in `extra` or `pipe`),
    every occurrence of {kubeconfig} is replaced by the file's path."""
    program, *args = argv
    require_tool(program)
    _require_stages(pipe)
    source = _existing(ctx, ctx.repo.kubeconfig, KUBECONFIG_HINT)
    with decrypted(ctx.sops, {"kubeconfig": source}) as paths:
        kubeconfig = paths["kubeconfig"]
        filled = [arg.replace(KUBECONFIG_PLACEHOLDER, str(kubeconfig)) for arg in args]
        return _launch(ctx, [program, *filled, *extra], {"KUBECONFIG": str(kubeconfig)}, pipe, kubeconfig.parent)
```

(The t/k CLI callers pass two positional args and keep working unchanged.)

- [ ] **Step 4: Implement `workstation/shortcuts.py`**

In `run()`:

```python
    argv, pipe = shortcut.argv, shortcut.pipe
    if needs_node(shortcut):
        # Resolved from .talkub.yaml alone, so a wrong node is reported before anything is decrypted.
        settings = ctx.repo.load_settings()
        node, extra = pick_node(settings, name, extra)
        argv = tuple(fill_node(argv, settings, node))
        pipe = tuple(tuple(fill_node(stage, settings, node)) for stage in pipe)
```

```python
    if shortcut.kind == "run":
        tool, *args = argv
        passthrough = access.kubectl if tool in _KUBECTL else access.talosctl
        return passthrough(ctx, [*args, *extra], pipe)
    if shortcut.kind == "kubernetes":
        return access.with_kubeconfig(ctx, argv, extra, pipe)
```

In `listing()`:

```python
    shown = {name: f"{name} <node>" if needs_node(s) else name for name, (s, _) in entries.items()}
    kinds = {name: f"{s.kind} |" if s.pipe else s.kind for name, (s, _) in entries.items()}
    width = max(len(text) for text in shown.values())
    kind_width = max(len(text) for text in kinds.values())
    text_width = max(len(s.description or "") for s, _ in entries.values())
    return [
        f"{shown[name]:<{width}}  {kinds[name]:<{kind_width}}  {s.description or '':<{text_width}}  {_ORIGIN_LABELS[origin]}"
        for name, (s, origin) in sorted(entries.items())
    ]
```

Update the docstring: `"""One aligned line per shortcut: name (with `<node>` if it takes one), kind (with `|` if piped), description, origin."""`

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/workstation -q`
Expected: PASS.

- [ ] **Step 6: Update README.md**

In the Shortcuts YAML example, after the `extensions` shortcut add:

```yaml
  extension-names:
    run: [t, -n, "{node}", get, extensions, -o, json]
    pipe:
      - [jq, -r, .spec.metadata.name]
      - [sort]
```

After the `{node}` paragraph (before "`--print` and `--follow` go before the name"), add:

```markdown
`run` and `kubernetes` shortcuts can feed their output through further programs with `pipe:`, a list
of stages in the same list form (`{node}` placeholders work there too). Arguments after the name go
to the first stage. Only the first stage gets the cluster credentials: in the others `KUBECONFIG`
and `TALOSCONFIG` point at a missing file, so a `kubectl` or `talosctl` there fails instead of
reaching whatever cluster your own configuration selects; `{kubeconfig}` is not allowed in `pipe:`.
If a later stage really needs the cluster, use a `kubernetes` shortcut with `[sh, -c, "…"]`. The
exit code is that of the rightmost failing stage (like `set -o pipefail`); an earlier stage that
stops only because a later one closed its input (`… | head`) does not count as failing. The listing
shows such shortcuts with the kind `run |` or `kubernetes |`. For one-off pipes, pipe
`talkub run <name>` in your own shell.
```

- [ ] **Step 7: Full suite and checks**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports && uv run basedpyright src/talkub/core/runner.py src/talkub/workstation/access.py`
Expected: all green; basedpyright: no errors on new lines.

- [ ] **Step 8: Commit**

```bash
git add src/talkub/workstation/access.py src/talkub/workstation/shortcuts.py README.md tests/workstation/test_shortcut_pipes.py tests/workstation/test_shortcuts.py
git commit -m "feat(shortcuts): run pipe stages without credentials and mark them in the listing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
