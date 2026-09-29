# Node Placeholders in Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `run` and `kubernetes` shortcuts may contain `{node}`, `{node.addr|name|ip|mgmt|role}`; the node is chosen at call time as the first argument after the shortcut name.

**Architecture:** `config/shortcuts.py` owns the placeholder *syntax* (regex, allowed fields, load-time check, `needs_node`). `workstation/shortcuts.py` resolves placeholders against `Settings` (`pick_node`, `fill_node`) before anything is decrypted, then hands the filled argv to the unchanged `access.*` helpers. `cli/` gets a completion callback for node names and an updated usage text.

**Tech Stack:** Python 3.12, Typer/Click shell completion, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-09-29-talkub-shortcut-node-placeholders-design.md`

## Global Constraints

- Only the pattern `\{node(?:\.([a-z]+))?\}` is a placeholder; any other `{…}` (jsonpath `{.items[0]}`, Go templates `{{.x}}`) stays literal.
- Allowed fields, in this order: `addr`, `name`, `ip`, `mgmt`, `role`. `{node}` means `{node.addr}` = `mgmt or ip` (`Settings.mgmt_address`).
- Placeholders are replaced only in the configured argv, never in the extra arguments typed at call time.
- Only `run` and `kubernetes` shortcuts need a node; other kinds are unchanged.
- Node resolution happens before any decryption.
- Code, comments, CLI strings in English. Line length 120. Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Layer contract: `config` must not import `workstation`; `cli` may import both. Check with `uv run lint-imports`.
- `README.md` documents user-facing behavior and must be updated in the same change.

## Review Focus

1. User types an option but no node in a one-node cluster (`talkub run ext -o yaml`) → runs against the only node with `-o yaml` appended (not "unknown node '-o'"). Test in Task 2.
2. A shortcut combining `{node}` with jsonpath braces in the same argument (`-o=jsonpath={.items[0]}` next to `{node.name}`) → only `{node…}` replaced. Test in Task 1 (syntax) and Task 2 (fill).
3. `{node.mgmt}` on a node without `mgmt` → error before decrypting (no identity needed). Test in Task 2.
4. A global-pool shortcut with an invalid placeholder → error message names the global file, not `.talkub.yaml`. Test in Task 1.
5. Completion of the node argument for a shortcut *without* placeholders, or for an unknown shortcut → no candidates, no traceback. Test in Task 3.

---

### Task 1: Placeholder syntax in `config/shortcuts.py`

**Files:**
- Modify: `src/talkub/config/shortcuts.py`
- Create: `tests/config/test_shortcuts.py`
- Modify: `tests/config/test_user.py` (one test appended)

**Interfaces:**
- Produces (in `talkub.config.shortcuts`):
  - `NODE_FIELDS: tuple[str, ...] = ("addr", "name", "ip", "mgmt", "role")`
  - `NODE_PLACEHOLDER: re.Pattern[str]` — group 1 is the field or `None` for bare `{node}`
  - `needs_node(shortcut: Shortcut) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/config/test_shortcuts.py`:

```python
"""Node placeholders in shortcut definitions: syntax only."""

from __future__ import annotations

import pytest

from talkub.config.shortcuts import Shortcut, needs_node, shortcut_from_dict, shortcut_to_dict
from talkub.core.errors import ValidationError


@pytest.mark.parametrize("kind", ["run", "kubernetes"])
@pytest.mark.parametrize(
    "arg",
    ["{node}", "{node.addr}", "{node.name}", "{node.ip}", "{node.mgmt}", "{node.role}", "--nodes={node},x"],
)
def test_valid_node_placeholders(kind, arg):
    program = "t" if kind == "run" else "stern"
    shortcut = shortcut_from_dict("ext", {kind: [program, "-n", arg]})
    assert shortcut.argv == (program, "-n", arg)
    assert needs_node(shortcut)
    assert shortcut_from_dict("ext", shortcut_to_dict(shortcut)) == shortcut


def test_unknown_field_is_rejected_with_path_and_hint():
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("ext", {"run": ["t", "-n", "{node.adr}", "get", "extensions"]})
    assert str(exc.value) == ".talkub.yaml: shortcuts.ext.run: unknown placeholder {node.adr}"
    assert exc.value.hint == "one of {node}, {node.addr}, {node.name}, {node.ip}, {node.mgmt}, {node.role}"


def test_unknown_field_in_kubernetes_is_rejected():
    with pytest.raises(ValidationError) as exc:
        shortcut_from_dict("logs", {"kubernetes": ["stern", "--node={node.host}"]})
    assert str(exc.value) == ".talkub.yaml: shortcuts.logs.kubernetes: unknown placeholder {node.host}"


@pytest.mark.parametrize(
    "arg",
    ["-o=jsonpath={.items[0].metadata.name}", "{{.status}}", "{nodes}", "{Node}", "{node.ADDR}", "{kubeconfig}"],
)
def test_other_braces_are_literal(arg):
    shortcut = shortcut_from_dict("x", {"run": ["k", "get", "pods", arg]})
    assert shortcut.argv[-1] == arg
    assert not needs_node(shortcut)


def test_jsonpath_and_node_in_one_argument():
    shortcut = shortcut_from_dict("x", {"run": ["k", "get", "node", "-o=jsonpath={.metadata.name}/{node.name}"]})
    assert needs_node(shortcut)


def test_needs_node_only_for_argv_kinds():
    assert not needs_node(Shortcut(name="p", kind="run", argv=("k", "get", "pods")))
    assert not needs_node(Shortcut(name="s", kind="secret", namespace="{node}", secret="a", key="b"))
```

Append to `tests/config/test_user.py`:

```python
def test_invalid_placeholder_names_the_global_file(global_file):
    global_file.write_text("shortcuts:\n  ext:\n    run: [t, -n, '{node.adr}', get, extensions]\n")
    with pytest.raises(ValidationError) as exc:
        user.load_global_shortcuts()
    assert str(exc.value) == f"{user.display_path(global_file)}: shortcuts.ext.run: unknown placeholder {{node.adr}}"
    assert exc.value.hint.startswith("one of {node}")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_shortcuts.py tests/config/test_user.py -q`
Expected: collection error `ImportError: cannot import name 'needs_node'`.

- [ ] **Step 3: Implement**

In `src/talkub/config/shortcuts.py`, after `_PORT_SPEC`/`_valid_port_spec`, add:

```python
# `{node}` or `{node.<field>}` in a run/kubernetes argument; filled in at call time from the chosen node.
# Anything else in braces (kubectl jsonpath, Go templates) is left alone.
NODE_FIELDS = ("addr", "name", "ip", "mgmt", "role")
NODE_PLACEHOLDER = re.compile(r"\{node(?:\.([a-z]+))?\}")


def _check_placeholders(argv: list[str], where: str) -> None:
    for arg in argv:
        for match in NODE_PLACEHOLDER.finditer(arg):
            field = match.group(1)
            if field is not None and field not in NODE_FIELDS:
                known = ", ".join(["{node}", *(f"{{node.{name}}}" for name in NODE_FIELDS)])
                raise ValidationError(
                    f"{MARKER}: {where}: unknown placeholder {match.group(0)}",
                    hint=f"one of {known}",
                )
```

After the `Shortcut` dataclass add:

```python
def needs_node(shortcut: Shortcut) -> bool:
    """Whether running the shortcut needs a node: a run/kubernetes argument has a {node…} placeholder."""
    return shortcut.kind in ("run", "kubernetes") and any(NODE_PLACEHOLDER.search(arg) for arg in shortcut.argv)
```

In `shortcut_from_dict`, call the check right before each `return Shortcut(...)` of the `run` and `kubernetes` branches:

```python
        _check_placeholders(raw, f"{where}.run")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw))
```

```python
        _check_placeholders(raw, f"{where}.kubernetes")
        return Shortcut(name=name, kind=kind, description=description, argv=tuple(raw))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/config -q && uv run ruff check src tests && uv run lint-imports`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/config/shortcuts.py tests/config/test_shortcuts.py tests/config/test_user.py
git commit -m "feat(shortcuts): accept {node} placeholders in run and kubernetes shortcuts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Resolve the node at call time in `workstation/shortcuts.py`

**Files:**
- Modify: `src/talkub/workstation/shortcuts.py` (imports; new `pick_node`, `fill_node`; `run()` at ~line 145; `listing()` at ~line 65)
- Create: `tests/workstation/test_shortcut_nodes.py` (pure, no external tools)
- Modify: `tests/workstation/test_shortcuts.py` (integration, needs age/sops)

**Interfaces:**
- Consumes: `NODE_PLACEHOLDER`, `needs_node` from `talkub.config.shortcuts` (Task 1); `Settings.nodes`, `Settings.node(name)`, `Settings.mgmt_address(name)`; `NodeSpec(ip, role="controlplane", mgmt=None, ...)`.
- Produces (in `talkub.workstation.shortcuts`):
  - `pick_node(settings: Settings, shortcut_name: str, extra: Sequence[str]) -> tuple[str, list[str]]`
  - `fill_node(argv: Sequence[str], settings: Settings, node: str) -> list[str]`
  - `NO_NODES_HINT = "add one with talkub node add"`

- [ ] **Step 1: Write the failing pure tests**

Create `tests/workstation/test_shortcut_nodes.py`:

```python
"""Choosing the node for a shortcut and filling its {node…} placeholders (no external tools)."""

from __future__ import annotations

import dataclasses

import pytest
from helpers import sample_settings

from talkub.config import NodeSpec, Shortcut
from talkub.core.errors import ValidationError
from talkub.workstation import shortcuts


def _settings(**nodes):
    return dataclasses.replace(sample_settings(), nodes=nodes)


THREE = _settings(
    **{
        "cp-01": NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net"),
        "cp-02": NodeSpec(ip="10.0.0.11"),
        "w-01": NodeSpec(ip="10.0.0.20", role="worker"),
    }
)
ONE = _settings(**{"cp-01": NodeSpec(ip="10.0.0.10")})


def test_first_argument_is_the_node():
    assert shortcuts.pick_node(THREE, "ext", ["w-01", "-o", "yaml"]) == ("w-01", ["-o", "yaml"])


def test_unknown_node_lists_known():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(THREE, "ext", ["cp-9"])
    assert str(exc.value) == "unknown node 'cp-9'"
    assert exc.value.hint == "one of cp-01, cp-02, w-01"


def test_single_node_is_the_default():
    assert shortcuts.pick_node(ONE, "ext", []) == ("cp-01", [])


def test_leading_option_means_no_node_given():
    assert shortcuts.pick_node(ONE, "ext", ["-o", "yaml"]) == ("cp-01", ["-o", "yaml"])


def test_several_nodes_need_an_argument():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(THREE, "ext", ["-o", "yaml"])
    assert str(exc.value) == "shortcut ext needs a node"
    assert exc.value.hint == "one of cp-01, cp-02, w-01"


def test_no_nodes_at_all():
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(_settings(), "ext", [])
    assert exc.value.hint == shortcuts.NO_NODES_HINT
    with pytest.raises(ValidationError) as exc:
        shortcuts.pick_node(_settings(), "ext", ["cp-01"])
    assert exc.value.hint == shortcuts.NO_NODES_HINT


@pytest.mark.parametrize(
    ("node", "arg", "expected"),
    [
        ("cp-01", "{node}", "cp-01.tail.ts.net"),
        ("cp-01", "{node.addr}", "cp-01.tail.ts.net"),
        ("cp-02", "{node}", "10.0.0.11"),
        ("cp-01", "{node.ip}", "10.0.0.10"),
        ("cp-01", "{node.mgmt}", "cp-01.tail.ts.net"),
        ("w-01", "{node.name}", "w-01"),
        ("w-01", "{node.role}", "worker"),
        ("cp-02", "--nodes={node},{node.name}", "--nodes=10.0.0.11,cp-02"),
        ("cp-02", "-o=jsonpath={.items[0]}/{node.name}", "-o=jsonpath={.items[0]}/cp-02"),
        ("cp-02", "{{.x}}", "{{.x}}"),
        ("cp-02", "{kubeconfig}", "{kubeconfig}"),
    ],
)
def test_fill_node(node, arg, expected):
    assert shortcuts.fill_node(["t", "-n", arg], THREE, node) == ["t", "-n", expected]


def test_missing_mgmt_is_an_error():
    with pytest.raises(ValidationError) as exc:
        shortcuts.fill_node(["t", "-n", "{node.mgmt}"], THREE, "cp-02")
    assert str(exc.value) == "node cp-02 has no mgmt address"
    assert exc.value.hint == "use {node.addr} or set mgmt on the node"


def test_listing_marks_shortcuts_that_take_a_node():
    ext = Shortcut(name="ext", kind="run", description="extensions", argv=("t", "-n", "{node}", "get", "extensions"))
    pods = Shortcut(name="pods", kind="run", argv=("k", "get", "pods"))
    lines = shortcuts.listing({"ext": (ext, "global"), "pods": (pods, "project")})
    assert [line.split() for line in lines] == [
        ["ext", "<node>", "run", "extensions", "global"],
        ["pods", "run", "project"],
    ]
    assert lines[0].index("run") == lines[1].index("run")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/workstation/test_shortcut_nodes.py -q`
Expected: FAIL with `AttributeError: module 'talkub.workstation.shortcuts' has no attribute 'pick_node'` (and the listing test fails on the missing `<node>`).

- [ ] **Step 3: Implement `pick_node`, `fill_node`, listing**

In `src/talkub/workstation/shortcuts.py` change the imports:

```python
import re
...
from collections.abc import Callable, Mapping, Sequence
...
from talkub.config import MARKER, Repo, Settings, Shortcut
from talkub.config.shortcuts import NODE_PLACEHOLDER, needs_node
```

Add after `save_global`:

```python
NO_NODES_HINT = "add one with talkub node add"


def _node_hint(settings: Settings) -> str:
    return f"one of {', '.join(settings.nodes)}" if settings.nodes else NO_NODES_HINT


def pick_node(settings: Settings, shortcut_name: str, extra: Sequence[str]) -> tuple[str, list[str]]:
    """The node for a shortcut with {node…} placeholders and the remaining extra arguments: the first
    argument unless it is an option (node names never start with '-'), else the only node."""
    if extra and not extra[0].startswith("-"):
        name, *rest = extra
        if name not in settings.nodes:
            raise ValidationError(f"unknown node {name!r}", hint=_node_hint(settings))
        return name, rest
    if len(settings.nodes) == 1:
        return next(iter(settings.nodes)), list(extra)
    raise ValidationError(f"shortcut {shortcut_name} needs a node", hint=_node_hint(settings))


def fill_node(argv: Sequence[str], settings: Settings, node: str) -> list[str]:
    """`argv` with every {node…} placeholder replaced by the value of `node`."""
    spec = settings.node(node)
    values = {
        None: settings.mgmt_address(node),
        "addr": settings.mgmt_address(node),
        "name": node,
        "ip": spec.ip,
        "mgmt": spec.mgmt,
        "role": spec.role,
    }

    def value(match: re.Match[str]) -> str:
        filled = values[match.group(1)]
        if filled is None:
            raise ValidationError(f"node {node} has no mgmt address", hint="use {node.addr} or set mgmt on the node")
        return filled

    return [NODE_PLACEHOLDER.sub(value, arg) for arg in argv]
```

Replace `listing()`:

```python
def listing(entries: Mapping[str, tuple[Shortcut, Origin]]) -> list[str]:
    """One aligned line per shortcut: name (with `<node>` if it takes one), kind, description, origin."""
    if not entries:
        return []
    shown = {name: f"{name} <node>" if needs_node(s) else name for name, (s, _) in entries.items()}
    width = max(len(text) for text in shown.values())
    kind_width = max(len(s.kind) for s, _ in entries.values())
    text_width = max(len(s.description or "") for s, _ in entries.values())
    return [
        f"{shown[name]:<{width}}  {s.kind:<{kind_width}}  {s.description or '':<{text_width}}  {_ORIGIN_LABELS[origin]}"
        for name, (s, origin) in sorted(entries.items())
    ]
```

- [ ] **Step 4: Run the pure tests**

Run: `uv run pytest tests/workstation/test_shortcut_nodes.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing integration tests**

In `tests/workstation/test_shortcuts.py`, add to the `SHORTCUTS` dict:

```python
    "ext": Shortcut(name="ext", kind="run", argv=("t", "-n", "{node}", "get", "extensions")),
    "node-ui": Shortcut(
        name="node-ui", kind="kubernetes", argv=("fake-ui", "--node={node.name}", "--config={kubeconfig}")
    ),
    "mgmt": Shortcut(name="mgmt", kind="run", argv=("t", "-n", "{node.mgmt}", "version")),
```

and append these tests (the `repo_with_configs` repo has the single node `cp-01`, ip `10.0.0.10`, no mgmt):

```python
def _add_nodes(ctx):
    from talkub.config import NodeSpec

    settings = ctx.repo.load_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.tail.ts.net")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker")
    ctx.repo.write_settings(settings)


def test_node_shortcut_with_explicit_node_and_extras(ctx, spy):
    _add_nodes(ctx)
    talosctl = spy("talosctl")
    assert shortcuts.run(ctx, "ext", ["w-01", "-o", "yaml"]) == 0
    assert talosctl.argvs == [["-n", "10.0.0.20", "get", "extensions", "-o", "yaml"]]


def test_node_shortcut_uses_mgmt_address(ctx, spy):
    _add_nodes(ctx)
    talosctl = spy("talosctl")
    shortcuts.run(ctx, "ext", ["cp-01"])
    assert talosctl.argvs == [["-n", "cp-01.tail.ts.net", "get", "extensions"]]


def test_node_shortcut_single_node_default_with_options(ctx, spy):
    talosctl = spy("talosctl")
    shortcuts.run(ctx, "ext", ["-o", "yaml"])
    assert talosctl.argvs == [["-n", "10.0.0.10", "get", "extensions", "-o", "yaml"]]


def test_node_shortcut_needs_a_node_among_several(ctx, spy):
    _add_nodes(ctx)
    talosctl = spy("talosctl")
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "ext", [])
    assert str(exc.value) == "shortcut ext needs a node"
    assert talosctl.calls == []


def test_node_errors_come_before_decrypting(ctx, monkeypatch, tmp_path):
    # No identity available: decrypting first would raise NoIdentity instead.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-identities"))
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "mgmt", [])
    assert str(exc.value) == "node cp-01 has no mgmt address"
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "ext", ["cp-9"])
    assert str(exc.value) == "unknown node 'cp-9'"


def test_kubernetes_shortcut_with_node_and_kubeconfig(ctx, fake_bin, capfd):
    fake_bin("fake-ui", UI_REPORT)
    assert shortcuts.run(ctx, "node-ui", ["cp-01", "--x={node}"]) == 3
    out = capfd.readouterr().out
    kubeconfig = out.split("env:")[1].splitlines()[0]
    # Extras stay verbatim, including a literal {node}.
    assert f"arg:--node=cp-01\narg:--config={kubeconfig}\narg:--x={{node}}\n" in out


def test_global_node_shortcut_runs_against_the_project_nodes(ctx, spy):
    user.save_global_shortcut(Shortcut(name="version", kind="run", argv=("t", "-n", "{node}", "version")))
    talosctl = spy("talosctl")
    shortcuts.run(ctx, "version", [])
    assert talosctl.argvs == [["-n", "10.0.0.10", "version"]]
```

`test_listing` asserts `len(lines) == len(SHORTCUTS)` and only inspects `creds`, so it stays valid.

- [ ] **Step 6: Run the integration tests to verify they fail**

Run: `uv run pytest tests/workstation/test_shortcuts.py -q -k "node"`
Expected: FAIL — `talosctl` receives the literal `{node}` / `w-01` as arguments.

- [ ] **Step 7: Wire it into `run()`**

In `run()`, after the `--follow` check and before `if shortcut.kind == "secret":`, add; then use `argv` in the two argv branches:

```python
    argv = shortcut.argv
    if needs_node(shortcut):
        # Resolved from .talkub.yaml alone, so a wrong node is reported before anything is decrypted.
        settings = ctx.repo.load_settings()
        node, extra = pick_node(settings, name, extra)
        argv = tuple(fill_node(argv, settings, node))
```

```python
    if shortcut.kind == "run":
        tool, *args = argv
        passthrough = access.kubectl if tool in _KUBECTL else access.talosctl
        return passthrough(ctx, [*args, *extra])
    if shortcut.kind == "kubernetes":
        return access.with_kubeconfig(ctx, argv, extra)
```

- [ ] **Step 8: Run the full suite and checks**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports && uv run basedpyright`
Expected: all pass (basedpyright: no new errors in the touched files).

- [ ] **Step 9: Commit**

```bash
git add src/talkub/workstation/shortcuts.py tests/workstation/test_shortcut_nodes.py tests/workstation/test_shortcuts.py
git commit -m "feat(shortcuts): choose the node for {node} placeholders at call time

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Completion, usage text, README

**Files:**
- Modify: `src/talkub/cli/completion.py` (new `node_names`)
- Modify: `src/talkub/cli/main.py:83-107` (`RUN_USAGE`, `_run_help`)
- Modify: `README.md` (completion note ~line 41; Shortcuts section ~lines 88-139)
- Test: `tests/cli/test_cli_completion.py`

**Interfaces:**
- Consumes: `needs_node` from `talkub.config.shortcuts`; `shortcuts.available(repo)`.
- Produces: `completion.node_names(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_cli_completion.py`:

```python
def _with_node_shortcut(repo):
    from talkub.config import NodeSpec, Shortcut

    settings = repo.load_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker")
    settings.nodes["cp-02"] = NodeSpec(ip="10.0.0.11")
    settings.shortcuts = {
        "ext": Shortcut(name="ext", kind="run", argv=("t", "-n", "{node}", "get", "extensions")),
        "pods": Shortcut(name="pods", kind="run", argv=("k", "get", "pods")),
    }
    repo.write_settings(settings)


def test_completes_node_names_after_a_node_shortcut(empty_repo, monkeypatch, capfd):
    _with_node_shortcut(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, "talkub run ext ") == ["cp-01", "cp-02", "w-01"]
    assert _complete(monkeypatch, capfd, "talkub run ext cp") == ["cp-01", "cp-02"]


@pytest.mark.parametrize("line", ["talkub run pods ", "talkub run nope "])
def test_no_node_names_for_other_shortcuts(empty_repo, monkeypatch, capfd, line):
    _with_node_shortcut(empty_repo)
    monkeypatch.chdir(empty_repo.root)
    assert _complete(monkeypatch, capfd, line) == []


def test_no_node_names_outside_a_repo(monkeypatch, capfd, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _complete(monkeypatch, capfd, "talkub run ext ") == []


def test_run_usage_mentions_the_node(capfd):
    from talkub.cli.main import RUN_USAGE

    assert "<name> [<node>] [args...]" in RUN_USAGE
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/cli/test_cli_completion.py -q`
Expected: the node-name test fails (no candidates / file names), the usage test fails.

- [ ] **Step 3: Implement completion**

In `src/talkub/cli/completion.py` add the import and function:

```python
from talkub.config.shortcuts import needs_node
```

```python
def node_names(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    """Candidates for `talkub run <name> <TAB>`: node names, if <name> is a shortcut with {node} placeholders."""
    name = ctx.params.get("name")
    try:
        repo = find_repo(Path.cwd(), _repo_option(ctx))
        entry = shortcuts.available(repo).get(name) if name else None
        if entry is None or not needs_node(entry[0]):
            return []
        nodes = repo.load_settings().nodes
    except TalkubError:
        return []
    return [
        (node, f"{spec.role} {spec.mgmt or spec.ip}") for node, spec in sorted(nodes.items()) if node.startswith(incomplete)
    ]
```

In `src/talkub/cli/main.py`, `_run_help` gets a second argument after `name`:

```python
    node: str = typer.Argument(
        None, autocompletion=completion.node_names, help="Node, for shortcuts with {node} placeholders."
    ),
```

and `RUN_USAGE` becomes:

```python
RUN_USAGE = """\
usage: talkub run                                                list the shortcuts (project and global)
       talkub run [--print] [--follow] <name> [<node>] [args...]  run one
       talkub run --save-global [--force] <name>                  move a project shortcut to the global pool

  --print        secret: write the value to stdout instead of the clipboard
  --follow       job: wait for the pod, stream its logs, exit with the job's result
  --save-global  move <name> from .talkub.yaml to ~/.config/talkub/shortcuts.yaml
  --force        with --save-global: replace a global shortcut of the same name

Shortcuts come from .talkub.yaml and the global pool ($XDG_CONFIG_HOME/talkub/shortcuts.yaml);
a project shortcut hides a global one with the same name. Options go before <name>; arguments
after it are passed on verbatim (run and kubernetes shortcuts only). A shortcut with {node}
placeholders takes the node name as its first argument (optional with a single node)."""
```

- [ ] **Step 4: Update README.md**

Replace line 41:

```markdown
Arguments after `talkub t`/`talkub k` are not completed; after a shortcut name only the node of a
shortcut with `{node}` placeholders is.
```

In the Shortcuts YAML example, add after `argocd-apps`:

```yaml
  extensions:
    run: [t, -n, "{node}", get, extensions]
```

In the command list, after the `argocd-apps -o wide` line:

```
    talkub run extensions cp-01             # a {node} shortcut takes the node first
```

After the kinds list (before the "`--print` and `--follow` go before the name" paragraph), add:

```markdown
`run` and `kubernetes` shortcuts can refer to a node without naming it, so they also work as
global shortcuts. In the configured arguments, `{node}` (or `{node.addr}`) becomes the node's
`mgmt` address, or its `ip` if `mgmt` is not set; `{node.name}`, `{node.ip}`, `{node.mgmt}` and
`{node.role}` give the single field (`{node.mgmt}` is an error for a node without one). The node
is the first argument after the shortcut name (`talkub run extensions cp-01 -o yaml`); it may be
left out in a cluster with a single node. Other braces, like kubectl's `-o jsonpath={.items[0]}`,
are left alone; an unknown `{node.<field>}` is reported when the shortcuts are loaded. The listing
shows such shortcuts as `extensions <node>`.
```

- [ ] **Step 5: Run the full suite and checks**

Run: `uv run pytest -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports && uv run basedpyright`
Expected: all pass.

- [ ] **Step 6: Manual smoke check**

Run: `uv run talkub run --help` → shows the new usage line. Then, in a scratch dir:

```bash
uv run talkub --repo <a cluster repo> run            # a {node} shortcut listed as "<name> <node>"
```

(Real-cluster run is optional; the integration tests cover argv.)

- [ ] **Step 7: Commit**

```bash
git add src/talkub/cli/completion.py src/talkub/cli/main.py tests/cli/test_cli_completion.py README.md
git commit -m "feat(shortcuts): complete node names and document {node} placeholders

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
