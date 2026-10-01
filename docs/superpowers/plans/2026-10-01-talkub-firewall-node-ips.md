# Firewall rules from node IPs – Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Talos firewall written by talkub allows cluster ports only from the nodes' own addresses; `nodeCidr` and `--node-cidr` disappear; `talkub tailscale firewall` and `node add` rewrite the patch.

**Architecture:** `talos/tailscale.py` derives source subnets from `settings.nodes` (`node_sources`), renders the patch from them and gains a `firewall()` flow that `lockdown` and `nodes.add_node` share. `config` drops `node_cidr` from `TailscaleSpec` and ignores the legacy key. `cli` loses `--node-cidr` and gains `tailscale firewall`. Docs follow.

**Tech Stack:** Python 3.12, Typer, ruamel.yaml, pytest (fixtures `empty_repo`, `repo`, `talosctl` fake), ruff, import-linter, basedpyright, uv.

**Spec:** `docs/superpowers/specs/2026-10-01-talkub-firewall-node-ips-design.md`

## Global Constraints

- Code, comments and CLI strings in English; commit messages Conventional Commits.
- Layer contract (`cli → … → talos → … → config → core`): `talos/nodes.py` may import `talos/tailscale.py` (same package); nothing in `config` imports `talos`.
- Every behavior change is mirrored in `docs/reference.md` and the guides (CLAUDE.md rule).
- `uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports` must pass before each commit (pre-commit hook); `uv run basedpyright` before the last commit.
- `.talkub.yaml` written by this version must still load in this version with a legacy `nodeCidr` present (ignored).
- No version bump.

## Review Focus

1. A node whose `ip` is a host name on a locked-down cluster: `lockdown`, `firewall` and `node add` refuse with a hint naming the node, nothing is written. Tests in Tasks 2 and 3.
2. IPv6 node address renders as `/128`, IPv4 as `/32`. Test in Task 2.
3. `node add` on an enabled-but-not-locked-down cluster must not create `firewall.yaml`. Test in Task 3.
4. `tailscale firewall` after a hand edit of `firewall.yaml`: the edit is gone, the header says so. Covered by the render header test in Task 2 and docs in Task 5.
5. A repo written by the previous talkub (with `nodeCidr`) loads and, after any write, no longer carries the key. Test in Task 1.

---

### Task 1: Drop `node_cidr` from the settings model

**Files:**
- Modify: `src/talkub/config/specs.py:56-60`
- Modify: `src/talkub/config/settings.py:84-89` (to_dict) and `:172-183` (from_dict)
- Modify: `tests/config/test_repo.py:123-147`
- Modify (mechanical): every test constructing `TailscaleSpec(..., node_cidr="...")`: `tests/talos/test_render.py`, `tests/talos/test_kubeconfig.py`, `tests/talos/test_nodes.py`, `tests/gitops/test_tailscale_operator.py`, `tests/gitops/test_bootstrap.py`, `tests/gitops/test_coredns_takeover.py`

**Interfaces:**
- Produces: `TailscaleSpec(tailnet: str, use_ips: bool = False)`; `Settings.from_dict` ignores `tailscale.nodeCidr`; `to_dict` writes only `tailnet` (+ `useIps` when true).

- [ ] **Step 1: Failing tests**

In `tests/config/test_repo.py` replace the three tailscale tests (`test_settings_roundtrip…` assertion lines 123-130, `test_tailscale_use_ips_roundtrip`, `test_tailscale_use_ips_defaults_false_and_parses_strictly`) so that:

```python
    settings.tailscale = TailscaleSpec(tailnet="tail.ts.net")
    ...
    assert "kernelArgs" in text and "nodeCidr" not in text
```

```python
def test_tailscale_use_ips_roundtrip(tmp_path):
    settings = sample_settings()
    settings.tailscale = TailscaleSpec(tailnet="tail.ts.net", use_ips=True)
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert repo.load_settings() == settings
    assert "useIps: true" in repo.settings_file.read_text()


def test_tailscale_use_ips_defaults_false_and_parses_strictly():
    data = sample_settings().to_dict()
    data["tailscale"] = {"tailnet": "t.ts.net"}
    assert Settings.from_dict(data).tailscale.use_ips is False
    data["tailscale"]["useIps"] = "true"  # not a bool
    with pytest.raises(ValidationError):
        Settings.from_dict(data)


def test_tailscale_legacy_node_cidr_is_ignored_and_dropped(tmp_path):
    data = sample_settings().to_dict()
    data["tailscale"] = {"tailnet": "t.ts.net", "nodeCidr": "10.0.0.0/24"}
    settings = Settings.from_dict(data)
    assert settings.tailscale == TailscaleSpec(tailnet="t.ts.net")
    repo = Repo(tmp_path)
    repo.write_settings(settings)
    assert "nodeCidr" not in repo.settings_file.read_text()


def test_tailscale_needs_tailnet():
    data = sample_settings().to_dict()
    data["tailscale"] = {"useIps": True}
    with pytest.raises(ValidationError, match="tailnet"):
        Settings.from_dict(data)
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/config/test_repo.py -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'node_cidr'` / `got an unexpected keyword` or assertion on `nodeCidr`.

- [ ] **Step 3: Implement**

`src/talkub/config/specs.py`:

```python
@dataclass(frozen=True)
class TailscaleSpec:
    tailnet: str
    use_ips: bool = False
```

`src/talkub/config/settings.py` `to_dict`:

```python
        if self.tailscale is not None:
            data["tailscale"] = {"tailnet": self.tailscale.tailnet}
            if self.tailscale.use_ips:
                data["tailscale"]["useIps"] = True
```

`from_dict`:

```python
        tailscale = None
        raw_tailscale = data.get("tailscale")
        if raw_tailscale is not None:
            if not isinstance(raw_tailscale, Mapping):
                raise ValidationError(f"{MARKER}: 'tailscale' must be a mapping")
            tailnet = optional_text(raw_tailscale.get("tailnet"), "tailscale.tailnet")
            if tailnet is None:
                raise ValidationError(f"{MARKER}: 'tailscale' needs tailnet")
            # A legacy `nodeCidr` (talkub < 0.1.8) is ignored: the firewall rules list the
            # nodes' own addresses; the key disappears on the next write.
            use_ips = raw_tailscale.get("useIps", False)
            if not isinstance(use_ips, bool):
                raise ValidationError(f"{MARKER}: 'tailscale.useIps' must be a boolean")
            tailscale = TailscaleSpec(tailnet=tailnet, use_ips=use_ips)
```

Mechanical sweep in the other test files: remove `, node_cidr="10.0.0.0/24"` (and the `node_cidr=…` kwarg where it is the only extra) from every `TailscaleSpec(...)` call:

```bash
grep -rln 'node_cidr=' tests | xargs sed -i '' -E 's/, node_cidr="[^"]*"//'
grep -rn 'node_cidr' tests   # only test_tailscale.py / test_tailscale_flows.py / test_cli_tailscale.py may remain (Tasks 2, 4)
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/config tests/talos/test_render.py tests/talos/test_kubeconfig.py tests/gitops -q`
Expected: config tests PASS; the others pass too unless they reach `tailscale.enable/lockdown` (those are Task 2). Note any failure that names `node_cidr` in `talos/tailscale.py` — expected until Task 2.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src tests && uv run ruff format src tests
git add src/talkub/config tests
git commit -m "refactor(config): drop tailscale.nodeCidr, ignore the legacy key"
```

---

### Task 2: Firewall from node addresses, `firewall()` flow

**Files:**
- Modify: `src/talkub/talos/tailscale.py` (remove `validate_cidr`, `default_node_cidr`, `outside_node_network`; add `node_sources`, `write_firewall_patch`, `firewall`; change `render_firewall_patch`, `enable`, `lockdown`)
- Modify: `tests/talos/test_tailscale.py:34-56, 75-97, 128`
- Modify: `tests/talos/test_tailscale_flows.py:45-60, 170-191` (+ new tests)

**Interfaces:**
- Consumes: `TailscaleSpec(tailnet, use_ips)` from Task 1.
- Produces: `node_sources(settings) -> list[str]`; `render_firewall_patch(sources: Sequence[str]) -> str`; `write_firewall_patch(repo, settings) -> list[str]` (returns the sources); `firewall(repo) -> list[str]` (notes); `enable(ctx, *, tailnet, authkey)`; `lockdown(ctx, *, use_ips=False)`. `FIREWALL_HEADER` text contains "rewritten by".

- [ ] **Step 1: Failing unit tests** (`tests/talos/test_tailscale.py`)

Replace `test_validators` (drop the cidr lines), delete `test_default_node_cidr`, and replace `test_firewall_patch`:

```python
def test_node_sources_one_subnet_per_node():
    settings = sample_settings()
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10")
    settings.nodes["w-01"] = NodeSpec(ip="2001:db8::10", role="worker", mgmt="w-01.t.ts.net")
    assert tailscale.node_sources(settings) == ["10.0.0.10/32", "2001:db8::10/128"]


def test_node_sources_rejects_host_names():
    settings = sample_settings()
    settings.nodes["w-02"] = NodeSpec(ip="w-02.lan", role="worker")
    with pytest.raises(ValidationError) as exc:
        tailscale.node_sources(settings)
    assert "w-02" in str(exc.value)
    assert "nodes.w-02.ip" in exc.value.hint


def test_firewall_patch():
    text = tailscale.render_firewall_patch(["10.0.0.10/32", "10.0.0.11/32"])
    assert "rewritten by `talkub tailscale firewall`" in text
    docs = [d for d in yamlio.load_all(text) if d]
    kinds = [(d["kind"], d.get("name")) for d in docs]
    assert kinds == [
        ("NetworkDefaultActionConfig", None),
        ("NetworkRuleConfig", "admin-via-tailnet"),
        ("NetworkRuleConfig", "cluster-internal"),
        ("NetworkRuleConfig", "flannel-vxlan"),
    ]
    assert docs[0]["ingress"] == "block"
    assert docs[1]["portSelector"] == {"ports": [50000, 6443], "protocol": "tcp"}
    assert docs[1]["ingress"] == [{"subnet": "100.64.0.0/10"}, {"subnet": "fd7a:115c:a1e0::/48"}]
    assert docs[2]["portSelector"]["ports"] == [50000, 50001, 6443, "2379-2380", 10250]
    assert docs[2]["ingress"] == [
        {"subnet": "10.0.0.10/32"},
        {"subnet": "10.0.0.11/32"},
        {"subnet": "10.244.0.0/16"},
        {"subnet": "10.96.0.0/12"},
    ]
    assert docs[3]["portSelector"] == {"ports": [4789], "protocol": "udp"}
    assert docs[3]["ingress"] == [{"subnet": "10.0.0.10/32"}, {"subnet": "10.0.0.11/32"}]
```

In `test_patches_validate_with_talosctl`: `tailscale.render_firewall_patch(["10.0.0.10/32"])`.

- [ ] **Step 2: Failing flow tests** (`tests/talos/test_tailscale_flows.py`)

`test_enable_keeps_addresses_and_encrypts_key`: replace the `node_cidr` assertion with `assert "nodeCidr" not in repo.settings_file.read_text()`. In `test_enable_twice_and_bad_input` drop the `node_cidr=` kwarg and its assertion. Replace `test_lockdown_rejects_nodes_outside_the_node_network` and `test_lockdown_skips_non_ip_addresses_in_the_cidr_check` with:

```python
def test_lockdown_firewall_lists_the_nodes_addresses(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    text = tailscale.firewall_patch_path(repo).read_text()
    assert "subnet: 10.0.0.10/32" in text and "subnet: 10.0.0.20/32" in text
    assert "/24" not in text


def test_lockdown_refuses_a_node_without_an_ip_address(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    settings = repo.load_settings()
    settings.nodes["w-02"] = NodeSpec(ip="w-02.lan", role="worker")
    repo.write_settings(settings)
    before = repo.settings_file.read_bytes()
    with pytest.raises(ValidationError, match="w-02"):
        tailscale.lockdown(Context.open(repo))
    assert not tailscale.is_locked_down(repo)
    assert repo.settings_file.read_bytes() == before


def test_firewall_requires_lockdown(repo):
    with pytest.raises(ValidationError, match="not enabled"):
        tailscale.firewall(repo)
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    with pytest.raises(ValidationError) as exc:
        tailscale.firewall(repo)
    assert "lockdown" in exc.value.hint


def test_firewall_rewrites_the_patch_from_the_nodes(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    path = tailscale.firewall_patch_path(repo)
    path.write_text("# hand edit\n")
    settings = repo.load_settings()
    settings.nodes["w-02"] = NodeSpec(ip="10.0.0.30", role="worker", mgmt="w-02.t.ts.net")
    repo.write_settings(settings)
    notes = tailscale.firewall(repo)
    text = path.read_text()
    assert "hand edit" not in text
    assert "subnet: 10.0.0.30/32" in text and "subnet: 10.0.0.10/32" in text
    assert any("10.0.0.30/32" in n for n in notes)
    assert any("talkub apply" in n for n in notes)
```

Check what the `repo` fixture's nodes are (`10.0.0.10` cp-01, `10.0.0.20` w-01 per the old outside test) and adjust the literals if they differ.

- [ ] **Step 3: Run, expect failure**

Run: `uv run pytest tests/talos/test_tailscale.py tests/talos/test_tailscale_flows.py -q 2>&1 | tail -5`
Expected: FAIL (`AttributeError: ... has no attribute 'node_sources'` / `firewall`, TypeError on `render_firewall_patch`).

- [ ] **Step 4: Implement** (`src/talkub/talos/tailscale.py`)

Remove `validate_cidr`, `default_node_cidr`, `outside_node_network`. Add after `controlplanes`:

```python
def node_sources(settings: Settings) -> list[str]:
    """One host subnet per node (`/32`, `/128`) for the cluster-internal firewall rules."""
    sources = []
    for name, node in settings.nodes.items():
        try:
            address = ipaddress.ip_address(node.ip)
        except ValueError:
            raise ValidationError(
                f"node '{name}' has no IP address (ip: {node.ip!r}); the firewall rules need one",
                hint=f"set nodes.{name}.ip in .talkub.yaml to the node's IP address (mgmt may stay a name)",
            ) from None
        sources.append(f"{address}/{address.max_prefixlen}")
    return sources
```

Replace `render_firewall_patch`:

```python
FIREWALL_HEADER = (
    "# Talos firewall written by `talkub tailscale lockdown` and rewritten by `talkub tailscale firewall`\n"
    "# and `talkub node add`: from outside, the Talos and Kubernetes APIs are reachable only via the\n"
    "# tailnet; cluster ports are open only to the nodes' own addresses. Edits here are overwritten;\n"
    "# put extra rules into talos/nodes/<node>.yaml or another patch.\n"
)


def render_firewall_patch(sources: Sequence[str]) -> str:
    node_rules = [{"subnet": s} for s in sources]
    docs = [
        {"apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block"},
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "admin-via-tailnet",
            "portSelector": {"ports": [50000, 6443], "protocol": "tcp"},
            "ingress": [{"subnet": TAILNET_V4}, {"subnet": TAILNET_V6}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "cluster-internal",
            "portSelector": {"ports": [50000, 50001, 6443, "2379-2380", 10250], "protocol": "tcp"},
            "ingress": [*node_rules, {"subnet": POD_SUBNET}, {"subnet": SERVICE_SUBNET}],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "flannel-vxlan",
            "portSelector": {"ports": [4789], "protocol": "udp"},
            "ingress": node_rules,
        },
    ]
    return FIREWALL_HEADER + "---\n".join(yamlio.dump(doc) for doc in docs)


def write_firewall_patch(repo: Repo, settings: Settings) -> list[str]:
    """Write talos/patches/firewall.yaml for the nodes in `settings`; returns their subnets."""
    sources = node_sources(settings)
    atomic_write(firewall_patch_path(repo), render_firewall_patch(sources).encode())
    return sources


def firewall(repo: Repo) -> list[str]:
    """Rewrite the firewall patch from the current nodes (after changing a node's ip, or once
    after upgrading from a talkub that allowed a whole node network)."""
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if not is_locked_down(repo):
        raise ValidationError(
            f"{repo.rel(firewall_patch_path(repo))} does not exist",
            hint="run `talkub tailscale lockdown` first; it writes the firewall",
        )
    sources = write_firewall_patch(repo, settings)
    return [
        f"rewrote {repo.rel(firewall_patch_path(repo))}: cluster ports open to {', '.join(sources)}",
        "run `talkub apply` to activate it on the nodes",
    ]
```

`enable`: signature `enable(ctx, *, tailnet, authkey)`; drop the `cidr` lines; `settings.tailscale = TailscaleSpec(tailnet=tailnet)`; drop the "node network for the firewall rules" note (keep the others).

`lockdown`: signature `lockdown(ctx, *, use_ips=False)`; remove the `cidr`/`outside` block; `settings.tailscale = TailscaleSpec(tailnet=settings.tailscale.tailnet, use_ips=use_ips)`; right after that line add `sources = node_sources(settings)` (fails before anything is written); at the end replace the `atomic_write(firewall_patch_path…)` call with `atomic_write(firewall_patch_path(repo), render_firewall_patch(sources).encode())` and the note with:

```python
        (
            "run `talkub apply` to activate the firewall; afterwards 50000/6443 are open only to the tailnet "
            f"and cluster ports only to the nodes' own addresses ({', '.join(sources)})"
        ),
```

Remove the now-unused `ipaddress` import only if nothing else uses it (`is_tailnet_address` does; keep it).

- [ ] **Step 5: Run**

Run: `uv run pytest tests/talos -q 2>&1 | tail -5`
Expected: all PASS except `tests/talos/test_nodes.py` tests touching `node_cidr`/outside (Task 3).

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run lint-imports
git add src/talkub/talos/tailscale.py tests/talos/test_tailscale.py tests/talos/test_tailscale_flows.py
git commit -m "feat(tailscale): firewall rules from the nodes' addresses, \`tailscale firewall\` flow"
```

---

### Task 3: `node add` rewrites the firewall on a locked-down cluster

**Files:**
- Modify: `src/talkub/talos/nodes.py:199-205` (remove `_outside`), `:216-235` (add_node)
- Modify: `tests/talos/test_nodes.py:262-328`

**Interfaces:**
- Consumes: `tailscale.write_firewall_patch(repo, settings)`, `tailscale.node_sources` (Task 2).
- Produces: `add_node` warning text containing the new address and "talkub apply" when locked down.

- [ ] **Step 1: Failing tests**

Replace `test_add_node_after_lockdown_warns_outside_the_node_network` with:

```python
def test_add_node_after_lockdown_rewrites_the_firewall(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    empty_repo.write_settings(settings)
    # enabled, not locked down: no firewall is created
    assert add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions()) == []
    assert not tailscale.firewall_patch_path(empty_repo).exists()
    tailscale.write_firewall_patch(empty_repo, empty_repo.load_settings())
    warnings = add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.12", role="worker"), NodeOptions())
    text = tailscale.firewall_patch_path(empty_repo).read_text()
    assert "subnet: 10.0.0.12/32" in text and "subnet: 10.0.0.11/32" in text
    assert len(warnings) == 1
    assert "10.0.0.12" in warnings[0] and "talkub apply" in warnings[0]


def test_add_node_after_lockdown_needs_an_ip_address(empty_repo):
    from talkub.config import TailscaleSpec
    from talkub.talos import tailscale

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net")
    empty_repo.write_settings(settings)
    tailscale.write_firewall_patch(empty_repo, settings)
    before = tailscale.firewall_patch_path(empty_repo).read_text()
    with pytest.raises(ValidationError, match="w-04"):
        add_node(empty_repo, "w-04", NodeSpec(ip="w-04.lan", role="worker"), NodeOptions())
    assert "w-04" not in empty_repo.load_settings().nodes
    assert not node_patch_path(empty_repo, "w-04").exists()
    assert tailscale.firewall_patch_path(empty_repo).read_text() == before
```

The two older lockdown tests write `"x"` into `firewall.yaml`; they keep working (any content marks lockdown), but `test_add_node_after_lockdown_uses_tailnet_mgmt` now also rewrites the file: add `assert "subnet: 10.0.0.12/32" in tailscale.firewall_patch_path(empty_repo).read_text()` there. Check the `empty_repo` fixture's first node ip (`cp-01`) so the sweep in Task 1 did not break the constructors; `node_patch_path` and `pytest`/`ValidationError` must be imported in the test module (check the file head).

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/talos/test_nodes.py -q 2>&1 | tail -5`
Expected: FAIL on the firewall content / missing ValidationError.

- [ ] **Step 3: Implement** (`src/talkub/talos/nodes.py`)

Add `from talkub.talos import tailscale` (check the existing imports; `nodes.py` must not be imported by `tailscale.py`). Remove `_outside`. In `add_node`, replace the `if locked_down and _outside(...)` block with a pre-check right after `locked_down` is computed:

```python
    if locked_down:
        try:
            ipaddress.ip_address(spec.ip)
        except ValueError:
            raise ValidationError(
                f"node '{name}': --ip must be an IP address on a locked-down cluster (got {spec.ip!r})",
                hint="the firewall rules list the nodes' addresses; pass the node's IP, a name may go into --mgmt",
            ) from None
```

and after `repo.write_settings(settings)` (the node entry is in `settings` by then):

```python
    if locked_down:
        tailscale.write_firewall_patch(repo, settings)
        warnings.append(
            f"{repo.rel(firewall)} now allows {spec.ip}; until `talkub apply` has run on the existing nodes, "
            "their firewall blocks cluster traffic from the new node"
        )
```

Keep `ipaddress` imported (used by `_outside` before; now by the check).

- [ ] **Step 4: Run**

Run: `uv run pytest tests/talos -q 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run lint-imports
git add src/talkub/talos/nodes.py tests/talos/test_nodes.py
git commit -m "feat(nodes): node add rewrites the firewall patch on a locked-down cluster"
```

---

### Task 4: CLI: `tailscale firewall`, no `--node-cidr`

**Files:**
- Modify: `src/talkub/cli/tailscale.py` (remove `NODE_CIDR`, `node_cidr` params; add `firewall` command)
- Modify: `src/talkub/cli/setup.py:86-98, 136-139`
- Modify: `tests/cli/test_cli_tailscale.py:62-66, 265-295` (+ new test)

**Interfaces:**
- Consumes: `tailscale.firewall(repo)`, `tailscale.enable(ctx, *, tailnet, authkey)`, `tailscale.lockdown(ctx, *, use_ips)`.

- [ ] **Step 1: Failing tests**

`test_lockdown_use_ips_flag`: `assert calls == [{"use_ips": True}]`. Rename `test_init_tailscale_enable_failure_hint_keeps_node_cidr` to `test_init_tailscale_enable_failure_hint`, drop the `--node-cidr`, `10.0.0.0/16` args, assert `"talkub tailscale enable --tailnet t.ts.net && talkub tailscale lockdown" in ...err`. Add:

```python
def test_firewall_command_rewrites_the_patch(repo, capfd, monkeypatch):
    calls = []
    monkeypatch.setattr(tailscale, "firewall", lambda r: calls.append(r.root) or ["rewrote it"])
    assert _run(["--repo", str(repo.root), "tailscale", "firewall"]) == 0
    assert calls == [repo.root]
    assert "note: rewrote it" in capfd.readouterr().out


def test_node_cidr_option_is_gone(repo):
    assert _run(["--repo", str(repo.root), "tailscale", "lockdown", "--node-cidr", "10.0.0.0/24"]) != 0
```

(Check how `_run` reports Typer usage errors; Typer exits 2 on an unknown option.)

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/cli/test_cli_tailscale.py -q 2>&1 | tail -5`
Expected: FAIL (`firewall` command missing, kwargs mismatch).

- [ ] **Step 3: Implement**

`src/talkub/cli/tailscale.py`: delete `NODE_CIDR`; `enable(...)` and `lockdown(...)` lose `node_cidr`; calls become `tailscale.enable(context, tailnet=tailnet, authkey=authkey)` and `tailscale.lockdown(ctx.obj.context(), use_ips=use_ips)`. Add after `lockdown`:

```python
@app.command("firewall")
def firewall(ctx: typer.Context) -> None:
    """Rewrite talos/patches/firewall.yaml from the nodes in .talkub.yaml.

    After changing a node's ip, or once after upgrading from a talkub whose rules allowed a whole
    node network (nodeCidr). `node add` does this itself. Then `talkub apply`.
    """
    print_notes(tailscale.firewall(ctx.obj.repo()))
```

Update the module docstring (`"""tailscale enable, lockdown, firewall, …"""`). `src/talkub/cli/setup.py`: remove the `node_cidr` option, its validation, the `elif` mention (`"--tailnet/--tailscale-authkey-file need --tailscale"`), pass no `node_cidr` to `enable`, and build the hint without `cidr`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/cli -q 2>&1 | tail -3`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run lint-imports
git add src/talkub/cli tests/cli
git commit -m "feat(cli): tailscale firewall; drop --node-cidr"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/tailscale.md` (1a, 1c, 1e, 1f, troubleshooting), `docs/nodes.md` (section 1 bullet, section 5 table), `docs/reference.md` (Tailscale block, version-skew note)

- [ ] **Step 1: docs/tailscale.md**

1a: delete the sentence "`--node-cidr` sets the node network if the `/24` of the first node does not cover every node." 1b command list: unchanged. 1c table: replace the row

```
| 50000, 50001, 6443, 2379-2380, 10250/tcp | the nodes' own addresses (`ip` in `.talkub.yaml`, one `/32` each), pod and service networks | cluster traffic: etcd, kubelet |
| 4789/udp                                 | the nodes' own addresses                               | Flannel VXLAN                  |
```

and replace the paragraph after the table ("The nodes keep their LAN/public addresses … `node add` warns.") with:

```
The nodes keep their LAN/public addresses for the cluster itself and do not use Tailscale DNS
(`TS_ACCEPT_DNS=false`); only admin clients go through the tailnet. Everything else, NodePorts
included, is blocked from outside. The rules name each node's `ip`, so a node needs a fixed address
(DHCP reservation or `--static`) and `ip` must be an IP, not a name (`mgmt` may be a name).
`talkub node add` rewrites `talos/patches/firewall.yaml` with the new node; after changing a node's
`ip` run `talkub tailscale firewall`. Both need `talkub apply` afterwards. The file is generated:
put extra rules into `talos/nodes/<node>.yaml` (like `--public-ingress`) or another patch.

Repos written by talkub before this change allowed a whole network (`tailscale.nodeCidr`, usually
the `/24` around the first node, which on a hosted server is shared with strangers). The key is
ignored now; run `talkub tailscale firewall` and `talkub apply` once to narrow the rules.
```

1e: add a bullet "**A node's `ip` changed**: update it in `.talkub.yaml`, `talkub tailscale firewall`, `talkub apply`." 1f step 4: replace "node network (`--node-cidr`)" with "the nodes' addresses". Troubleshooting row "New node `NotReady`, flannel errors": cause "The existing nodes still run the old firewall: `talkub apply`. A node whose `ip` changed: `talkub tailscale firewall`, `talkub apply`."

- [ ] **Step 2: docs/nodes.md**

Section 1 bullet: replace "An `ip` outside `--node-cidr` is warned about: the firewall blocks cluster traffic from it until the rules in `talos/patches/firewall.yaml` are extended." with "`node add` rewrites `talos/patches/firewall.yaml` to allow the new node's `ip` (which must be an IP address, not a name); `talkub apply` activates it on the existing nodes before the new node can join." Section 5 table, row "The node's IP": append ", then `talkub tailscale firewall` and `talkub apply` so the firewall of the other nodes allows the new address (with Tailscale lockdown)".

- [ ] **Step 3: docs/reference.md**

Tailscale block: after the `lockdown` line add
`    talkub tailscale firewall                           # rewrite the firewall from the nodes' ips (then apply)`.
Replace the sentence "After `lockdown` and `apply`, … nodes keep their LAN/public addresses for cluster traffic." with "After `lockdown` and `apply`, the Talos and Kubernetes APIs are reachable only via the tailnet; cluster ports are open only to the nodes' own addresses (`ip` in `.talkub.yaml`), the pod and the service network. `node add` and `tailscale firewall` rewrite the patch." Version skew paragraph: append "`tailscale.nodeCidr` from older versions is ignored and dropped on the next write; run `talkub tailscale firewall` once to narrow the rules."

- [ ] **Step 4: Check**

Run: `grep -rn "node-cidr\|nodeCidr\|node network" docs/*.md README.md src`
Expected: only the migration note (tailscale.md, reference.md) and the legacy comment in `settings.py` remain.

- [ ] **Step 5: Commit**

```bash
git add docs/tailscale.md docs/nodes.md docs/reference.md
git commit -m "docs: firewall rules from node addresses, tailscale firewall"
```

---

### Task 6: Whole-suite verification

- [ ] **Step 1:** `uv run pytest -q 2>&1 | tail -3` → all pass (compare with the baseline run; `need(...)` skips are fine).
- [ ] **Step 2:** `uv run ruff check src tests && uv run ruff format --check src tests && uv run lint-imports && uv run basedpyright 2>&1 | tail -3` → clean.
- [ ] **Step 3:** `uv run talkub tailscale --help | grep firewall` and `uv run talkub tailscale lockdown --help | grep -c node-cidr` → `firewall` listed, `0`.
- [ ] **Step 4:** Commit the spec and plan if not yet committed: `git add docs/superpowers && git commit -m "docs: spec and plan for firewall rules from node ips"`.
