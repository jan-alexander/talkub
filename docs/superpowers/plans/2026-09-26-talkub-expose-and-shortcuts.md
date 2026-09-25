# Tailscale expose and shortcuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `talkub tailscale expose/unexpose` writes Tailscale Ingress manifests; `.talkub.yaml` `shortcuts` define secret/job/run/portForward quick commands executed with `talkub run`.

**Architecture:** Settings model + validation in `repo.py`; ingress renderer and flows in `tailscale_operator.py`; clipboard helpers + detached clearer (`clipboard.py`, `clipboard_clear.py`, `Runner.spawn_detached`); shortcut execution in `shortcuts.py`; `run` parsed raw in `cli/main.py` like `t`/`k`.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; kubectl, talosctl, pbcopy/pbpaste | wl-copy/wl-paste | xclip.

**Spec:** `docs/superpowers/specs/2026-09-26-talkub-expose-and-shortcuts-design.md`

## Global Constraints

- English code, comments, CLI output. `runner.py` is the only subprocess starter; argv lists only.
- Secret values never on argv, in env, printed (except with `--print`), or written to any file; the clipboard tool gets the value on stdin; the clearer gets only the SHA-256 hex digest on stdin.
- Clipboard tools in order: macOS `pbcopy`/`pbpaste`; Wayland `wl-copy`/`wl-paste`; X11 `xclip -selection clipboard` / `xclip -selection clipboard -o`. Detection via `shutil.which` (through `runner.require_tool`-style helpers, not by running them).
- Clear delay: 45 s; overridable by env `TALKUB_CLIPBOARD_CLEAR_SECONDS` (tests set 0 via conftest's isolated_env or monkeypatch).
- DNS-1123 label regex (reuse one helper for shortcut names, expose names, namespaces, services, port names): `[a-z0-9]([-a-z0-9]*[a-z0-9])?`, ≤ 63.
- Ingress file: `gitops/manifests/tailscale/ingress/<name>.yaml`, object name `tailscale-<name>`, content per spec §2 with header `# Managed by talkub (talkub tailscale expose).`
- Commits end with `Co-Authored-By: <model> <noreply@anthropic.com>`. `uv run pytest`.

## Review Focus

1. **Secret leakage** through argv/env/stdout/files or the clearer's argv — test in Task 4.
2. **Clearer deletes a newer clipboard** (user copied something else) — test in Task 3.
3. **`-f` or other flags after the shortcut name** must reach kubectl, not talkub — test in Task 4.
4. **Invalid `.talkub.yaml` shortcut** gives a path-precise error, not a traceback — test in Task 1.
5. **Job follow** returns the job's success/failure as exit code, also when the pod finished before `wait` — test in Task 4.

---

### Task 1: Shortcuts in settings

**Files:** Modify `src/talkub/repo.py`; Test `tests/test_repo.py` (or new `tests/test_shortcuts_settings.py`).

**Interfaces (produced):**
```python
@dataclass(frozen=True)
class Shortcut:
    name: str
    kind: str                     # "secret" | "job" | "run" | "portForward"
    description: str | None = None
    namespace: str | None = None  # secret, job, portForward
    secret: str | None = None     # secret: Secret name
    key: str | None = None        # secret
    cronjob: str | None = None    # job
    follow: bool = False          # job
    argv: tuple[str, ...] = ()    # run
    service: str | None = None    # portForward
    ports: tuple[str, ...] = ()   # portForward
```
`Settings.shortcuts: dict[str, Shortcut] = field(default_factory=dict)` (after `git`); `to_dict` writes `shortcuts` only when non-empty, in the YAML shape of spec §3 (keys `secret: {namespace, name, key}`, `job: {namespace, cronjob, follow}` — `follow` only when true —, `run: [...]`, `portForward: {namespace, service, ports}`, plus `description` when set); `repo.DNS_LABEL` regex + `valid_label(value) -> bool` helper (≤ 63).

Validation rules (all `ValidationError(f"{MARKER}: shortcuts.<name>…")`): mapping; name valid label; exactly one kind key; `description` optional non-empty string; secret: namespace/name/key non-empty strings, namespace and name valid labels (key may contain `.`/`_`: use the Kubernetes secret-key rule `[-._a-zA-Z0-9]+`); job: namespace/cronjob valid labels, follow bool if present; run: non-empty list of strings, first in {k, kubectl, t, talosctl}; portForward: namespace/service valid labels, ports non-empty list, each `N` or `N:M` with 1 ≤ N,M ≤ 65535.

- [ ] Tests first: parse a full example (all four kinds) and round-trip; absent block not written; one test per rule with the exact path in the message (e.g. `shortcuts.backup.job.cronjob`); non-mapping `shortcuts`.
- [ ] Implement, PASS, full suite, commit `feat: shortcuts in .talkub.yaml`.

---

### Task 2: `talkub tailscale expose` / `unexpose`

**Files:** Modify `src/talkub/tailscale_operator.py`, `src/talkub/cli/tailscale.py`, `README.md`; Test `tests/test_tailscale_operator.py`.

**Interfaces:** `ingress_path(repo, name) -> Path`; `ingress(name, namespace, service, port: int | str, path: str | None = None) -> str`; `expose(ctx, name, *, service: str, port: str, path: str | None = None) -> Path`; `unexpose(ctx, name) -> Path`.

- `expose`: operator enabled (as egress_add); `name` valid label; `service` exactly `ns/svc`, both valid labels; `port` all digits → int 1–65535, else valid label (port name); `path` must start with `/`; file exists → `ValidationError(..., hint="talkub tailscale unexpose <name>")`. Writes the renderer output; prints `wrote …`, `URL: https://<name>.<tailnet>`, and notes: backend must speak HTTP (TLS ends at the Tailscale proxy); HTTPS certificates must be enabled in the tailnet; commit and push gitops/.
- Renderer: `defaultBackend` without `--path`; with `--path`: `rules: [{http: {paths: [{path: <p>, pathType: Prefix, backend: {service: {name, port}}}]}}]`. Port: `{number: n}` or `{name: s}`. `tls: [{hosts: [<name>]}]`.
- CLI: `talkub tailscale expose NAME --service NS/SVC --port PORT [--path P]`, `talkub tailscale unexpose NAME`.
- README "Ingress": replace "Not automated by talkub" with `expose`/`unexpose` usage and the Argo CD chart example (`configs.params.server.insecure: true`, `server.ingress: {enabled: true, ingressClassName: tailscale, hostname: argocd, tls: true}`) with the reason for `server.insecure`.
- [ ] Tests: renderer (number/name port, path variant) parses as expected; validation cases; operator not enabled; existing file; unexpose missing/success; `talkub check` clean; CLI round trip.
- [ ] Implement, PASS, full suite, commit `feat: talkub tailscale expose/unexpose`.

---

### Task 3: Clipboard and detached clearer

**Files:** Modify `src/talkub/runner.py`; Create `src/talkub/clipboard.py`, `src/talkub/clipboard_clear.py`; Test `tests/test_clipboard.py`, `tests/test_runner.py`.

**Interfaces:**
- `Runner.spawn_detached(argv, *, input: bytes) -> None`: `require_tool(argv[0])`; `subprocess.Popen(argv, stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL, env=child_env(None), start_new_session=True, close_fds=True)`; write `input`, close stdin; do not wait.
- `clipboard.tool() -> ClipboardTool | None` with `copy: list[str]`, `paste: list[str]` (first available per Global Constraints).
- `clipboard.copy(runner, value: bytes) -> ClipboardTool` (raises `ValidationError("no clipboard tool found", hint="install pbcopy/wl-copy/xclip or use --print")`); runs `copy` argv with `input=value`.
- `clipboard.schedule_clear(runner, value: bytes) -> None`: `runner.spawn_detached([sys.executable, "-m", "talkub.clipboard_clear"], input=hashlib.sha256(value).hexdigest().encode())`.
- `clipboard_clear.main()`: read the hex digest from stdin; sleep `float(os.environ.get("TALKUB_CLIPBOARD_CLEAR_SECONDS", "45"))`; find the tool; read the clipboard with `paste` (via `Runner().run(..., check=False)`); if `sha256(content)` equals the digest → run `copy` with empty input; never print anything. `if __name__ == "__main__": main()`.
- [ ] Tests: `spawn_detached` with a real child (python -c that writes its stdin to a file given via argv) → file content equals input; child in its own session. `copy` with spy `pbcopy` (read_stdin) gets the value on stdin, not in argv; no tool → error. `clipboard_clear.main` with spy pbpaste returning the same value → pbcopy called with empty stdin; different value → pbcopy not called; digest never in any argv. (Put fake tools first on PATH via the `spy`/`fake_bin` fixtures; hide real ones by setting PATH to the fake bin + python's dir where needed.)
- [ ] Implement, PASS, full suite, commit `feat: clipboard copy with delayed clearing`.

---

### Task 4: Shortcut execution and `talkub run`

**Files:** Create `src/talkub/shortcuts.py`; Modify `src/talkub/cli/main.py`, `README.md`; Test `tests/test_shortcuts.py`, `tests/test_cli_main.py` (or new `tests/test_cli_run.py`).

**Interfaces:** `shortcuts.run(ctx, name: str, extra: list[str], *, print_value: bool = False, follow: bool = False) -> int`; `shortcuts.listing(settings) -> list[str]`; `shortcuts.job_name(cronjob: str, now: datetime) -> str`.

- Unknown name → `ValidationError(f"unknown shortcut {name!r}", hint="known: …" or "define shortcuts in .talkub.yaml")`. `extra` non-empty for kinds other than run → `ValidationError("shortcut <name> (<kind>) takes no extra arguments")`. `--print` only for secret, `--follow` only for job (else error).
- secret: in `kube_session`: `kube.kubectl(["-n", ns, "get", "secret", name, "-o", "json"])`; parse JSON; `data[key]` missing → error listing the available keys (names only); decode base64 in memory. print_value → `sys.stdout.buffer.write(value + (b"\n" if sys.stdout.isatty() else b""))`, return 0. Else `clipboard.copy`, `clipboard.schedule_clear`, print `copied <shortcut> (<n> chars); the clipboard is cleared in <s> s`, return 0.
- job: `job_name(cronjob, now)` = f"{cronjob}-manual-{now:%Y%m%d%H%M%S}"[:63].rstrip("-"); `kube.kubectl(["-n", ns, "create", "job", jn, f"--from=cronjob/{cronjob}"])`; print `created job <ns>/<jn>`. follow (config or flag): `kubectl -n ns wait --for=condition=Ready pod -l job-name=<jn> --timeout=5m` (check=False), then logs via `ctx.runner.call(["kubectl", "-n", ns, "logs", "-f", f"job/{jn}"], env=kube.env)` (interactive), then `get job <jn> -o json` → `status.succeeded >= 1` → 0 else print hint `inspect with: talkub k -n <ns> describe job <jn>` and return 1.
- run: first element k/kubectl → `access.kubectl(ctx, argv[1:] + extra)`; t/talosctl → `access.talosctl(ctx, …)`; return its code.
- portForward: in `kube_session`: `ctx.runner.call(["kubectl", "-n", ns, "port-forward", f"svc/{service}", *ports], env=kube.env)`; return code.
- CLI `cli/main.py`: before Typer, if `rest[0] == "run"`: parse `rest[1:]`: leading `--print`/`--follow` flags, then name, then the rest verbatim as extra; no name → print the listing (`name  kind  description`) or "no shortcuts defined in .talkub.yaml" and exit 0. Help: `talkub run --help` prints a short usage. Register `run` in the Typer help too (a hidden no-op command or the app's help text) so `talkub --help` mentions it.
- README "Shortcuts": example block from the spec, behaviour per kind, clipboard clearing, `--print`/`--follow`.
- [ ] Tests (spies for kubectl/pbcopy; monkeypatch `clipboard.schedule_clear` or spy `spawn_detached` to capture input): secret to clipboard — value only on pbcopy stdin, not in argv/capfd/files, clearer input is the digest; `--print`; missing key lists keys without values; job create argv + name format/length; follow success (succeeded 1) → 0, failure → 1, wait failure tolerated; extra args rejected; run k/t routing with extra args incl. `-f`; portForward argv; CLI: listing, unknown name, flags before name, `-f` after name passed through.
- [ ] Implement, PASS, full suite, commit `feat: talkub run shortcuts`.
