# talkub Global Shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A global shortcut pool in `$XDG_CONFIG_HOME/talkub/shortcuts.yaml` that `talkub run` merges with the project's `.talkub.yaml` shortcuts, plus `talkub run --save-global <name>` to move a project shortcut into the pool.

**Architecture:** New `config/user.py` owns the global file (path, load/validate, comment-preserving insert). `workstation/shortcuts.py` merges both sources (`effective_shortcuts`, project wins), lists them with their origin, runs from the merged set and implements the move. `cli/main.py` parses the new raw flags.

**Tech Stack:** Python 3, ruamel.yaml (round-trip via `talkub.core.yamlio`), typer (only for help), pytest with the repo's spy/fake_bin fixtures, `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-09-27-talkub-global-shortcuts-design.md`

## Global Constraints

- English code, comments and CLI output; match the existing module style (module docstring, `from __future__ import annotations`).
- Global file: `$XDG_CONFIG_HOME/talkub/shortcuts.yaml`; `XDG_CONFIG_HOME` unset or empty → `~/.config/talkub/shortcuts.yaml`.
- Global file format: top-level mapping with a `shortcuts:` mapping, same entries and validation as `.talkub.yaml` (`shortcut_from_dict`).
- Precedence: a project shortcut hides a global one with the same name.
- Only `talkub run` reads the global file; no other command calls into `talkub.config.user`.
- `--save-global`: write the global file first, then remove the entry from `.talkub.yaml`.
- Import layers (`lint-imports`): `config` may import only `config.*` and `core`; `workstation` may import `config`, `context`, `kube`, `core`.
- Tests never touch the real `~/.config`: `tests/conftest.py::isolated_env` already sets `XDG_CONFIG_HOME=<tmp_path>/xdg` for every test.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Pre-commit runs lint-imports, ruff check, ruff format.

## Review Focus

- A hand-edited global file with comments: `--save-global` must keep the comments and the other entries (`save_global_shortcut` inserts into the round-trip document instead of re-serializing). Test in Task 1.
- A broken global file must never be overwritten by `--save-global`, since that would lose the user's pool. The whole file is validated before any write. Test in Task 1.
- A validation error from the global file must name that file, not `.talkub.yaml`. Test in Task 1.
- A conflict without `--force` must leave both files byte-for-byte unchanged. Test in Task 2.
- Options after the name (`talkub run --save-global pods --force`) must be an error, not silently ignored. Test in Task 3.

---

### Task 1: The global shortcut file (`config/user.py`)

**Files:**
- Create: `src/talkub/config/user.py`
- Test: `tests/config/test_user.py`

**Interfaces:**
- Consumes: `shortcut_from_dict(name, spec) -> Shortcut`, `shortcut_to_dict(Shortcut) -> dict` (`talkub.config.shortcuts`); `MARKER` (`talkub.config.fields`); `yamlio.load/dump`; `atomic_write(path, data)`.
- Produces:
  - `user_config_dir() -> Path`
  - `global_shortcuts_file() -> Path`
  - `display_path(path: Path) -> str`: `~/…` when under the home directory, else `str(path)`
  - `load_global_shortcuts() -> dict[str, Shortcut]`
  - `save_global_shortcut(shortcut: Shortcut, *, force: bool = False) -> Path`: raises `ValidationError` if the name exists and `force` is false; returns the file path

- [ ] **Step 1: Write the failing tests**

`tests/config/test_user.py`:

```python
"""The global shortcut pool in $XDG_CONFIG_HOME/talkub/shortcuts.yaml."""

from __future__ import annotations

import pytest

from talkub.config import Shortcut, user
from talkub.core.errors import ValidationError

PODS = Shortcut(name="pods", kind="run", description="all pods", argv=("k", "get", "pods", "-A"))


@pytest.fixture
def global_file(tmp_path):
    path = tmp_path / "xdg" / "talkub" / "shortcuts.yaml"
    path.parent.mkdir(parents=True)
    return path


def test_config_dir_follows_xdg(tmp_path):
    assert user.user_config_dir() == tmp_path / "xdg" / "talkub"
    assert user.global_shortcuts_file() == tmp_path / "xdg" / "talkub" / "shortcuts.yaml"


@pytest.mark.parametrize("xdg", [None, ""])
def test_config_dir_defaults_to_dot_config(monkeypatch, tmp_path, xdg):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    assert user.user_config_dir() == tmp_path / "home" / ".config" / "talkub"


def test_display_path_abbreviates_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert user.display_path(tmp_path / "home" / ".config" / "x.yaml") == "~/.config/x.yaml"
    assert user.display_path(tmp_path / "elsewhere.yaml") == str(tmp_path / "elsewhere.yaml")


def test_missing_empty_or_blockless_file_is_an_empty_pool(global_file):
    assert user.load_global_shortcuts() == {}
    global_file.write_text("")
    assert user.load_global_shortcuts() == {}
    global_file.write_text("shortcuts:\n")
    assert user.load_global_shortcuts() == {}


def test_load(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    description: all pods\n    run: [k, get, pods, -A]\n")
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_invalid_entry_names_the_global_file(global_file):
    global_file.write_text("shortcuts:\n  charts:\n    run: [helm, ls]\n")
    with pytest.raises(ValidationError) as exc:
        user.load_global_shortcuts()
    message = str(exc.value)
    assert message.startswith(f"{user.display_path(global_file)}: shortcuts.charts.run must start with")
    assert ".talkub.yaml" not in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shortcuts: [\n", "is not valid YAML"),
        ("- pods\n", "must be a mapping"),
        ("shortcuts: [pods]\n", "'shortcuts' must be a mapping"),
    ],
)
def test_invalid_file_structure(global_file, text, expected):
    global_file.write_text(text)
    with pytest.raises(ValidationError) as exc:
        user.load_global_shortcuts()
    assert str(exc.value).startswith(user.display_path(global_file))
    assert expected in str(exc.value)


def test_save_creates_directory_and_file(tmp_path):
    path = user.save_global_shortcut(PODS)
    assert path == tmp_path / "xdg" / "talkub" / "shortcuts.yaml"
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_keeps_comments_and_other_entries(global_file):
    global_file.write_text("# my pool\nshortcuts:\n  nodes:  # talos view\n    run: [t, get, members]\n")
    user.save_global_shortcut(PODS)
    text = global_file.read_text()
    assert "# my pool" in text and "# talos view" in text
    assert set(user.load_global_shortcuts()) == {"nodes", "pods"}


def test_save_into_file_without_block(global_file):
    global_file.write_text("# nothing yet\n")
    user.save_global_shortcut(PODS)
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_conflict_without_force_changes_nothing(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    run: [k, get, pods]\n")
    before = global_file.read_bytes()
    with pytest.raises(ValidationError) as exc:
        user.save_global_shortcut(PODS)
    assert "global shortcut 'pods' already exists" in str(exc.value)
    assert exc.value.hint == "--force replaces it"
    assert global_file.read_bytes() == before


def test_save_with_force_replaces(global_file):
    global_file.write_text("shortcuts:\n  pods:\n    run: [k, get, pods]\n")
    user.save_global_shortcut(PODS, force=True)
    assert user.load_global_shortcuts() == {"pods": PODS}


def test_save_refuses_to_touch_a_broken_file(global_file):
    global_file.write_text("shortcuts:\n  charts:\n    run: [helm, ls]\n")
    before = global_file.read_bytes()
    with pytest.raises(ValidationError):
        user.save_global_shortcut(PODS, force=True)
    assert global_file.read_bytes() == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_user.py -v`
Expected: collection error `ImportError: cannot import name 'user' from 'talkub.config'`

- [ ] **Step 3: Implement `src/talkub/config/user.py`**

```python
"""The admin's own talkub settings outside any cluster repo: the global shortcut pool in
$XDG_CONFIG_HOME/talkub/shortcuts.yaml (default ~/.config/talkub/shortcuts.yaml).

Only `talkub run` reads this file, so a broken pool never blocks other commands.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub.config.fields import MARKER
from talkub.config.shortcuts import Shortcut, shortcut_from_dict, shortcut_to_dict
from talkub.core import yamlio
from talkub.core.errors import ValidationError
from talkub.core.fsutil import atomic_write


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "talkub"


def global_shortcuts_file() -> Path:
    return user_config_dir() / "shortcuts.yaml"


def display_path(path: Path) -> str:
    """`path` for messages, with the home directory shown as ~."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _read(path: Path) -> object:
    """The file's YAML document (round-trip, keeps comments); None if the file does not exist."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return yamlio.load(text)
    except YAMLError as err:
        raise ValidationError(f"{display_path(path)} is not valid YAML: {err}") from err


def _parse(data: object, where: str) -> dict[str, Shortcut]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValidationError(f"{where} must be a mapping")
    raw = data.get("shortcuts")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{where}: 'shortcuts' must be a mapping")
    shortcuts: dict[str, Shortcut] = {}
    for name, spec in raw.items():
        try:
            shortcuts[str(name)] = shortcut_from_dict(str(name), spec)
        except ValidationError as err:
            # The field helpers always name .talkub.yaml; point at the global file instead.
            message = str(err).removeprefix(f"{MARKER}: ")
            raise ValidationError(f"{where}: {message}", hint=err.hint) from None
    return shortcuts


def load_global_shortcuts() -> dict[str, Shortcut]:
    path = global_shortcuts_file()
    return _parse(_read(path), display_path(path))


def save_global_shortcut(shortcut: Shortcut, *, force: bool = False) -> Path:
    """Add `shortcut` to the pool, keeping the file's comments and other entries. The whole file
    is validated first, so a broken pool is never overwritten."""
    path = global_shortcuts_file()
    where = display_path(path)
    data = _read(path)
    if shortcut.name in _parse(data, where) and not force:
        raise ValidationError(
            f"global shortcut {shortcut.name!r} already exists in {where}",
            hint="--force replaces it",
        )
    root: MutableMapping = data if isinstance(data, MutableMapping) else {}
    if root.get("shortcuts") is None:
        root["shortcuts"] = {}
    root["shortcuts"][shortcut.name] = shortcut_to_dict(shortcut)
    atomic_write(path, yamlio.dump(root).encode())
    return path
```

`from talkub.config import Shortcut, user` in the test works without touching `config/__init__.py`: importing a submodule binds it as an attribute of its package. Leave `__init__.py` as it is, so importing `talkub.config` does not load `user` for every command.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/config/test_user.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/talkub/config/user.py tests/config/test_user.py
git commit -m "feat: global shortcut file in \$XDG_CONFIG_HOME/talkub

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Merge, list, run and move (`workstation/shortcuts.py`)

**Files:**
- Modify: `src/talkub/workstation/shortcuts.py` (imports, `listing` at ~line 32, `run` at ~line 97, new functions)
- Test: `tests/workstation/test_shortcuts.py` (update `test_listing` ~line 387, `test_unknown_shortcut_without_any_defined` ~line 352; add new tests)

**Interfaces:**
- Consumes (Task 1): `load_global_shortcuts()`, `save_global_shortcut(shortcut, *, force)`, `global_shortcuts_file()`, `display_path(path)` from `talkub.config.user`.
- Produces:
  - `Origin = Literal["project", "global", "project-overrides-global"]`
  - `effective_shortcuts(project: Mapping[str, Shortcut], global_: Mapping[str, Shortcut]) -> dict[str, tuple[Shortcut, Origin]]`
  - `available(repo: Repo) -> dict[str, tuple[Shortcut, Origin]]`
  - `listing(entries: Mapping[str, tuple[Shortcut, Origin]]) -> list[str]` (**signature change**: was `listing(settings)`)
  - `save_global(repo: Repo, name: str, *, force: bool = False) -> Path`
  - `run(ctx, name, extra, *, print_value=False, follow=False) -> int` (unchanged signature, now resolves from both sources)

- [ ] **Step 1: Write the failing tests**

In `tests/workstation/test_shortcuts.py`, extend the imports:

```python
import dataclasses

from talkub.config import Shortcut, user
```

Replace `test_unknown_shortcut_without_any_defined`:

```python
def test_unknown_shortcut_without_any_defined(repo_with_configs):
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(Context.open(repo_with_configs), "nope", [])
    where = user.display_path(user.global_shortcuts_file())
    assert exc.value.hint == f"define shortcuts in .talkub.yaml or {where}"
```

Replace `test_listing`:

```python
def test_listing(ctx):
    lines = shortcuts.listing(shortcuts.available(ctx.repo))
    assert len(lines) == len(SHORTCUTS)
    creds = next(line for line in lines if line.startswith("creds"))
    assert creds.split() == ["creds", "secret", "db", "password", "project"]
```

Add at the end of the file:

```python
NODES = Shortcut(name="nodes", kind="run", description="all nodes", argv=("k", "get", "nodes"))
ALL_PODS = Shortcut(name="pods", kind="run", argv=("k", "get", "pods", "-A"))


def test_effective_shortcuts_project_hides_global():
    project = {"pods": SHORTCUTS["pods"], "creds": SHORTCUTS["creds"]}
    merged = shortcuts.effective_shortcuts(project, {"pods": ALL_PODS, "nodes": NODES})
    assert merged == {
        "pods": (SHORTCUTS["pods"], "project-overrides-global"),
        "creds": (SHORTCUTS["creds"], "project"),
        "nodes": (NODES, "global"),
    }


def test_listing_shows_origin():
    merged = shortcuts.effective_shortcuts({"pods": SHORTCUTS["pods"]}, {"pods": ALL_PODS, "nodes": NODES})
    assert [line.split() for line in shortcuts.listing(merged)] == [
        ["nodes", "run", "all", "nodes", "global"],
        ["pods", "run", "project", "(overrides", "global)"],
    ]
    assert shortcuts.listing({}) == []


def test_global_shortcut_runs_against_the_project_cluster(ctx, spy):
    user.save_global_shortcut(NODES)
    kubectl = spy("kubectl")
    assert shortcuts.run(ctx, "nodes", ["-o", "wide"]) == 0
    [call] = kubectl.calls
    assert call["args"] == ["get", "nodes", "-o", "wide"]
    assert call["kubeconfig"]


def test_project_shortcut_wins_over_global(ctx, spy):
    user.save_global_shortcut(ALL_PODS)
    kubectl = spy("kubectl")
    shortcuts.run(ctx, "pods", [])
    assert kubectl.argvs == [["get", "pods"]]


def test_unknown_shortcut_lists_global_names_too(ctx):
    user.save_global_shortcut(NODES)
    with pytest.raises(ValidationError) as exc:
        shortcuts.run(ctx, "nope", [])
    assert "nodes" in exc.value.hint and "creds" in exc.value.hint


def test_save_global_moves_the_shortcut(ctx):
    before = ctx.repo.load_settings()
    path = shortcuts.save_global(ctx.repo, "pods")
    assert path == user.global_shortcuts_file()
    assert user.load_global_shortcuts() == {"pods": SHORTCUTS["pods"]}
    remaining = {name: s for name, s in before.shortcuts.items() if name != "pods"}
    assert ctx.repo.load_settings() == dataclasses.replace(before, shortcuts=remaining)


def test_save_global_unknown_name(ctx):
    with pytest.raises(ValidationError) as exc:
        shortcuts.save_global(ctx.repo, "nope")
    assert str(exc.value) == "no shortcut 'nope' in .talkub.yaml"
    assert "pods" in exc.value.hint


def test_save_global_name_that_is_only_global(ctx):
    user.save_global_shortcut(NODES)
    with pytest.raises(ValidationError) as exc:
        shortcuts.save_global(ctx.repo, "nodes")
    assert str(exc.value) == "nodes is already a global shortcut"


def test_save_global_conflict_without_force_changes_nothing(ctx):
    user.save_global_shortcut(ALL_PODS)
    project_before = ctx.repo.settings_file.read_bytes()
    global_before = user.global_shortcuts_file().read_bytes()
    with pytest.raises(ValidationError) as exc:
        shortcuts.save_global(ctx.repo, "pods")
    assert exc.value.hint == "--force replaces it"
    assert ctx.repo.settings_file.read_bytes() == project_before
    assert user.global_shortcuts_file().read_bytes() == global_before


def test_save_global_force_replaces(ctx):
    user.save_global_shortcut(ALL_PODS)
    shortcuts.save_global(ctx.repo, "pods", force=True)
    assert user.load_global_shortcuts() == {"pods": SHORTCUTS["pods"]}
    assert "pods" not in ctx.repo.load_settings().shortcuts
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/workstation/test_shortcuts.py -v`
Expected: the new and updated tests FAIL (`AttributeError: module 'talkub.workstation.shortcuts' has no attribute 'available'` / `'effective_shortcuts'` / `'save_global'`, hint mismatch); the old tests still PASS.

- [ ] **Step 3: Implement in `src/talkub/workstation/shortcuts.py`**

Update the module docstring's first line and the imports:

```python
"""Run the shortcuts defined in .talkub.yaml and in the global pool (`talkub run <name>`).
```

```python
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from talkub.config import MARKER, Repo, Shortcut
from talkub.config.user import display_path, global_shortcuts_file, load_global_shortcuts, save_global_shortcut
```

(`Settings` is no longer used in this module; drop it from the import.)

Replace `listing` with:

```python
Origin = Literal["project", "global", "project-overrides-global"]

_ORIGIN_LABELS: dict[str, str] = {
    "project": "project",
    "global": "global",
    "project-overrides-global": "project (overrides global)",
}


def effective_shortcuts(
    project: Mapping[str, Shortcut],
    global_: Mapping[str, Shortcut],
) -> dict[str, tuple[Shortcut, Origin]]:
    """All shortcuts by name with where they come from; a project shortcut hides a global one."""
    merged: dict[str, tuple[Shortcut, Origin]] = {name: (s, "global") for name, s in global_.items()}
    for name, s in project.items():
        merged[name] = (s, "project-overrides-global" if name in global_ else "project")
    return merged


def available(repo: Repo) -> dict[str, tuple[Shortcut, Origin]]:
    return effective_shortcuts(repo.load_settings().shortcuts, load_global_shortcuts())


def _sources() -> str:
    return f"{MARKER} or {display_path(global_shortcuts_file())}"


def listing(entries: Mapping[str, tuple[Shortcut, Origin]]) -> list[str]:
    """One aligned line per shortcut: name, kind, description, origin."""
    if not entries:
        return []
    width = max(len(name) for name in entries)
    kind_width = max(len(s.kind) for s, _ in entries.values())
    text_width = max(len(s.description or "") for s, _ in entries.values())
    return [
        f"{name:<{width}}  {s.kind:<{kind_width}}  {s.description or '':<{text_width}}  {_ORIGIN_LABELS[origin]}"
        for name, (s, origin) in sorted(entries.items())
    ]


def save_global(repo: Repo, name: str, *, force: bool = False) -> Path:
    """Move shortcut `name` from .talkub.yaml into the global pool. The pool is written first, so
    an interruption leaves the shortcut in both places rather than in neither."""
    settings = repo.load_settings()
    shortcut = settings.shortcuts.get(name)
    if shortcut is None:
        if name in load_global_shortcuts():
            raise ValidationError(f"{name} is already a global shortcut")
        known = ", ".join(sorted(settings.shortcuts)) or "none"
        raise ValidationError(f"no shortcut {name!r} in {MARKER}", hint=f"project shortcuts: {known}")
    path = save_global_shortcut(shortcut, force=force)
    settings.shortcuts = {key: s for key, s in settings.shortcuts.items() if key != name}
    repo.write_settings(settings)
    return path
```

In `run`, replace the first five lines of the body (from `settings = ctx.repo.load_settings()` through `raise ValidationError(f"unknown shortcut ...`) with:

```python
    entries = available(ctx.repo)
    if name not in entries:
        known = ", ".join(sorted(entries))
        hint = f"known: {known}" if known else f"define shortcuts in {_sources()}"
        raise ValidationError(f"unknown shortcut {name!r}", hint=hint)
    shortcut, _ = entries[name]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/workstation/test_shortcuts.py tests/config/test_user.py -v`
Expected: all PASS. `tests/cli/test_cli_main.py` fails at this point because `cli/main.py` still calls `listing(settings)`. Task 3 fixes that, so don't run the full suite yet.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/workstation/shortcuts.py tests/workstation/test_shortcuts.py
git commit -m "feat: merge global and project shortcuts, move with save_global

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: CLI flags, help and README

**Files:**
- Modify: `src/talkub/cli/main.py:80-113` (`RUN_USAGE`, `_run_help` docstring, `run_shortcut`)
- Modify: `README.md` (section "Shortcuts", ~lines 50-90)
- Test: `tests/cli/test_cli_main.py` (update `test_run_without_name_lists_shortcuts`, `test_run_without_shortcuts_says_so`, `test_run_help`; add new tests)

**Interfaces:**
- Consumes (Task 2): `shortcuts.available(repo)`, `shortcuts.listing(entries)`, `shortcuts.save_global(repo, name, *, force)`, `shortcuts.run(...)`; (Task 1): `display_path`, `global_shortcuts_file`.
- Produces: `talkub run [--save-global [--force]] …` behaviour per spec §4.

- [ ] **Step 1: Write the failing tests**

In `tests/cli/test_cli_main.py`, update the two listing tests and the help test:

```python
def test_run_without_name_lists_shortcuts(repo_with_configs, capfd):
    from talkub.config import Shortcut, user

    _with_shortcuts(repo_with_configs)
    user.save_global_shortcut(Shortcut(name="nodes", kind="run", argv=("k", "get", "nodes")))
    assert _run(["--repo", str(repo_with_configs.root), "run"]) == 0
    lines = capfd.readouterr().out.splitlines()
    assert any(line.split() == ["pods", "run", "list", "pods", "project"] for line in lines)
    assert any(line.split() == ["backup", "job", "project"] for line in lines)
    assert any(line.split() == ["nodes", "run", "global"] for line in lines)


def test_run_without_shortcuts_says_so(repo_with_configs, capfd):
    from talkub.config import user

    assert _run(["--repo", str(repo_with_configs.root), "run"]) == 0
    where = user.display_path(user.global_shortcuts_file())
    assert f"no shortcuts defined in .talkub.yaml or {where}" in capfd.readouterr().out


def test_run_help(capfd):
    assert _run(["run", "--help"]) == 0
    out = capfd.readouterr().out
    assert "talkub run [--print] [--follow] <name>" in out
    assert "talkub run --save-global [--force] <name>" in out
```

Add after `test_run_flags_before_the_name`:

```python
def test_run_save_global_moves_and_reports(repo_with_configs, capfd):
    from talkub.config import user

    _with_shortcuts(repo_with_configs)
    assert _run(["--repo", str(repo_with_configs.root), "run", "--save-global", "pods"]) == 0
    where = user.display_path(user.global_shortcuts_file())
    assert f"moved pods to {where}" in capfd.readouterr().out
    assert set(user.load_global_shortcuts()) == {"pods"}
    assert set(repo_with_configs.load_settings().shortcuts) == {"backup"}


def test_run_save_global_force_before_the_name(repo_with_configs, monkeypatch):
    from talkub.workstation import shortcuts

    seen = []
    monkeypatch.setattr(
        shortcuts,
        "save_global",
        lambda repo, name, *, force=False: seen.append((name, force)) or repo.root,
    )
    assert _run(["--repo", str(repo_with_configs.root), "run", "--force", "--save-global", "pods"]) == 0
    assert seen == [("pods", True)]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--save-global", "pods", "--force"], "--save-global takes only the shortcut name"),
        (["--save-global", "--print", "pods"], "--save-global cannot be combined with --print or --follow"),
        (["--save-global", "--follow", "pods"], "--save-global cannot be combined with --print or --follow"),
        (["--force", "pods"], "--force only applies to --save-global"),
        (["--save-global"], "--save-global needs a shortcut name"),
    ],
)
def test_run_save_global_usage_errors(repo_with_configs, capfd, args, expected):
    _with_shortcuts(repo_with_configs)
    before = repo_with_configs.settings_file.read_bytes()
    assert _run(["--repo", str(repo_with_configs.root), "run", *args]) == 1
    assert expected in capfd.readouterr().err
    assert repo_with_configs.settings_file.read_bytes() == before


def test_broken_global_file_only_affects_run(repo_with_configs, capfd):
    from talkub.config import user

    root = str(repo_with_configs.root)
    check_code = _run(["--repo", root, "check"])
    capfd.readouterr()
    path = user.global_shortcuts_file()
    path.parent.mkdir(parents=True)
    path.write_text("shortcuts: [\n")
    assert _run(["--repo", root, "check"]) == check_code
    capfd.readouterr()
    assert _run(["--repo", root, "run"]) == 1
    assert "is not valid YAML" in capfd.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_cli_main.py -v -k run`
Expected: the listing tests FAIL (`TypeError`/`AttributeError` from `listing(settings)`), the help test FAILs on the missing `--save-global` line, and the `save_global` tests FAIL because `--save-global` is treated as a shortcut name. `test_broken_global_file_only_affects_run` may already pass; that's fine.

- [ ] **Step 3: Implement in `src/talkub/cli/main.py`**

Add imports next to the existing ones:

```python
from talkub.config import MARKER
from talkub.config.user import display_path, global_shortcuts_file
from talkub.core.errors import TalkubError, ValidationError
```

(replace the existing `from talkub.core.errors import TalkubError` line with the one above).

Replace `RUN_USAGE`, the `_run_help` docstring and `run_shortcut`:

```python
RUN_USAGE = """\
usage: talkub run                                       list the shortcuts (project and global)
       talkub run [--print] [--follow] <name> [args...]  run one
       talkub run --save-global [--force] <name>         move a project shortcut to the global pool

  --print        secret: write the value to stdout instead of the clipboard
  --follow       job: wait for the pod, stream its logs, exit with the job's result
  --save-global  move <name> from .talkub.yaml to ~/.config/talkub/shortcuts.yaml
  --force        with --save-global: replace a global shortcut of the same name

Shortcuts come from .talkub.yaml and the global pool ($XDG_CONFIG_HOME/talkub/shortcuts.yaml);
a project shortcut hides a global one with the same name. Options go before <name>; arguments
after it are passed on verbatim (run shortcuts only)."""


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def _run_help() -> None:
    """Run a shortcut from .talkub.yaml or the global pool; without a name, list them."""
    # Never reached via main(), which handles `run` raw (like t/k) so options after the name pass through.
    print(RUN_USAGE)


_RUN_FLAGS = ("--print", "--follow", "--save-global", "--force")


def run_shortcut(state: State, args: list[str]) -> int:
    """`talkub run [flags] [<name> [args...]]`, parsed raw: flags only before the name."""
    flags: set[str] = set()
    index = 0
    while index < len(args) and args[index] in (*_RUN_FLAGS, "--help", "-h"):
        if args[index] in ("--help", "-h"):
            print(RUN_USAGE)
            return 0
        flags.add(args[index])
        index += 1
    save = "--save-global" in flags
    if "--force" in flags and not save:
        raise ValidationError("--force only applies to --save-global")
    if save and flags & {"--print", "--follow"}:
        raise ValidationError("--save-global cannot be combined with --print or --follow")

    if index == len(args):
        if save:
            raise ValidationError("--save-global needs a shortcut name")
        lines = shortcuts.listing(shortcuts.available(state.repo()))
        where = display_path(global_shortcuts_file())
        print("\n".join(lines) if lines else f"no shortcuts defined in {MARKER} or {where}")
        return 0

    name, extra = args[index], args[index + 1 :]
    if save:
        if extra:
            raise ValidationError("--save-global takes only the shortcut name", hint="options go before the name")
        path = shortcuts.save_global(state.repo(), name, force="--force" in flags)
        print(f"moved {name} to {display_path(path)}")
        return 0
    return shortcuts.run(
        state.context(),
        name,
        extra,
        print_value="--print" in flags,
        follow="--follow" in flags,
    )
```

Note: `cli/main.py` imports `talkub.config.user` at module level, but only `run_shortcut` calls into it, so the file is read only by `talkub run`. Keep the Typer docstring in sync: `test_top_level_help_mentions_run` checks for "Run a shortcut".

- [ ] **Step 4: Update `README.md`, section "Shortcuts"**

Change the intro line and the command block, and add a paragraph before "## Nodes and machine configs":

````markdown
Frequent commands can be named in `.talkub.yaml` (or in the global pool, see below) and run with
`talkub run`:
````

Replace the first line of the command block `talkub run                              # list: name, kind, description` with:

```
    talkub run                              # list: name, kind, description, origin
```

and append to that block:

```
    talkub run --save-global argocd-apps    # move it from .talkub.yaml to the global pool
```

Add after the paragraph that starts with "`--print` and `--follow` go before the name":

````markdown
### Global shortcuts

Shortcuts you use in every cluster live in `~/.config/talkub/shortcuts.yaml`
(`$XDG_CONFIG_HOME/talkub/shortcuts.yaml` if set), in the same format:

```yaml
shortcuts:
  pods:
    description: all pods
    run: [k, get, pods, -A]
```

`talkub run` lists and runs them in any cluster repo, always against that repo's cluster. A
project shortcut hides a global one with the same name; the listing shows
`project (overrides global)`. `talkub run --save-global <name>` moves a project shortcut into the
pool (the file's comments are kept); `--force` replaces a global shortcut of the same name. Only
`talkub run` reads the global file, so a broken one never blocks other commands.
````

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS (tests skipped for missing tools stay skipped).

Run: `uv run lint-imports && uv run ruff check && uv run ruff format --check`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/talkub/cli/main.py tests/cli/test_cli_main.py README.md
git commit -m "feat: talkub run --save-global and the global pool in the listing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
