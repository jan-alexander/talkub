# talkub Phase 2c (Tailscale Admin Access, sops edit follow-ups) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tailscale the only way to reach the Talos and Kubernetes APIs from outside (`init --tailscale`, `tailscale enable`, `tailscale lockdown`), and finish the `sops edit` follow-ups from phase 1.

**Architecture:** `tailscale.py` holds pure renderers (extension patch, firewall patch), validators and client-config rewriters (talosconfig endpoints, kubeconfig server — edited as YAML in memory), plus two flows: `enable` (auth-key patch + `siderolabs/tailscale` in every used schematic; addresses unchanged, so a running cluster can still be upgraded via LAN) and `lockdown` (firewall patch, `mgmt = <node>.<tailnet>`, client configs pointed at the tailnet). The cluster endpoint stays node-reachable (spec §5 correction). `Sops.edit` re-opens the editor on invalid YAML and checks the editor before decrypting.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; age, sops, talosctl 1.14 (validation of the rendered patches).

**Spec:** `docs/superpowers/specs/2026-09-23-talkub-phase2-design.md` §5 (incl. the 2026-09-24 endpoint correction), §9. Phase-1 spec still binds.

## Global Constraints

- All code, identifiers, comments, docstrings and CLI output in **English**.
- `runner.py` is the only module that starts subprocesses; argv lists only.
- The Tailscale auth key is never on a command line and never on disk in plaintext: read via `getpass` or `--authkey-file`, written only SOPS-encrypted to `talos/patches/tailscale.sops.yaml`.
- Decrypted talosconfig/kubeconfig are only held in memory while rewriting; the files are written back SOPS-encrypted with `atomic_write`.
- Extension patch: `ExtensionServiceConfig` `tailscale`, environment `TS_AUTHKEY=<key>`, `TS_ACCEPT_DNS=false`.
- Firewall patch `talos/patches/firewall.yaml` (plaintext): `NetworkDefaultActionConfig` `ingress: block`; `NetworkRuleConfig` `admin-via-tailnet` tcp 50000, 6443 from `100.64.0.0/10`, `fd7a:115c:a1e0::/48`; `cluster-internal` tcp 50000, 50001, 6443, `2379-2380`, 10250 from `nodeCidr`, `10.244.0.0/16`, `10.96.0.0/12`; `flannel-vxlan` udp 4789 from `nodeCidr`.
- The cluster endpoint (`.talkub.yaml` `endpoint`) is never changed by Tailscale commands. Client side only: talosconfig endpoints = `mgmt` of the control planes, talosconfig nodes = first CP `mgmt`, kubeconfig `server` = `https://<mgmt of first CP>:6443`.
- `lockdown` state ⇔ `talos/patches/firewall.yaml` exists.
- Commits end with a Co-Authored-By trailer (the model that wrote the commit).
- Run tests with `uv run pytest`; tests needing a binary skip when it is missing.

## Review Focus

1. **Running cluster:** `tailscale enable` must not change any address talkub uses, so `upgrade talos`/`apply` still reach the nodes via LAN (test in Task 2).
2. **Auth key leakage:** the key never appears in argv, `.talkub.yaml`, or any plaintext file (tests in Tasks 2 and 3).
3. **Nodes added after lockdown** get `mgmt = <node>.<tailnet>`; nodes added before lockdown keep LAN addresses (test in Task 2).
4. **Bootstrap after lockdown** stores a kubeconfig whose `server` is the tailnet name, not the node IP (test in Task 2).
5. **Invalid YAML in `sops edit`:** declining the re-open writes nothing and says so; the plaintext temp file is still removed (test in Task 4).

---

### Task 1: Tailscale renderers, validators and config rewriters

**Files:**
- Create: `src/talkub/tailscale.py` (pure part), `tests/test_tailscale.py`

**Interfaces:**
- Consumes: `yamlio`, `ValidationError`, `lifecycle.ordered_nodes`, `repo.Settings/Repo`.
- Produces: `EXTENSION`, `TAILNET_V4`, `TAILNET_V6`, `POD_SUBNET`, `SERVICE_SUBNET`, `extension_patch_path(repo)`, `firewall_patch_path(repo)`, `is_locked_down(repo) -> bool`, `validate_tailnet(tailnet) -> str`, `validate_authkey(key) -> str`, `validate_cidr(cidr) -> str`, `default_node_cidr(settings) -> str`, `controlplanes(settings) -> list[str]`, `admin_server(settings) -> str`, `render_extension_patch(authkey) -> bytes`, `render_firewall_patch(node_cidr) -> str`, `rewrite_talosconfig(data: bytes, endpoints, node) -> bytes`, `rewrite_kubeconfig_server(data: bytes, server) -> bytes`.

- [ ] **Step 1: Write the failing tests**

`tests/test_tailscale.py`:

```python
import shutil
import subprocess

import pytest

from helpers import need, sample_settings
from talkub import tailscale, yamlio
from talkub.errors import ValidationError
from talkub.repo import NodeSpec

TALOSCONFIG = b"""context: test
contexts:
    test:
        endpoints:
            - 10.0.0.10
        nodes:
            - 10.0.0.10
        ca: Y2E=
        crt: Y3J0
        key: a2V5
"""

KUBECONFIG = b"""apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: https://10.0.0.10:6443
    certificate-authority-data: Y2E=
users: []
"""


def test_validators():
    assert tailscale.validate_tailnet("tail1234.ts.net") == "tail1234.ts.net"
    assert tailscale.validate_authkey("  tskey-auth-abc  \n") == "tskey-auth-abc"
    assert tailscale.validate_cidr("10.0.0.0/24") == "10.0.0.0/24"
    assert tailscale.validate_cidr("10.0.0.7/24") == "10.0.0.0/24"
    for bad in ("tail", "-x.ts.net", "Tail.TS.net", "a b.ts.net"):
        with pytest.raises(ValidationError):
            tailscale.validate_tailnet(bad)
    with pytest.raises(ValidationError):
        tailscale.validate_authkey("abc")
    with pytest.raises(ValidationError):
        tailscale.validate_cidr("10.0.0.0/33")


def test_default_node_cidr():
    settings = sample_settings()
    assert tailscale.default_node_cidr(settings) == "10.0.0.0/24"
    settings.nodes = {"cp-01": NodeSpec(ip="cp.example.com")}
    with pytest.raises(ValidationError) as exc:
        tailscale.default_node_cidr(settings)
    assert "--node-cidr" in exc.value.hint


def test_admin_server_uses_first_controlplane_mgmt():
    settings = sample_settings()
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.11", role="worker", mgmt="w-01.t.ts.net")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    assert tailscale.controlplanes(settings) == ["cp-01"]
    assert tailscale.admin_server(settings) == "https://cp-01.t.ts.net:6443"


def test_extension_patch():
    doc = yamlio.load(tailscale.render_extension_patch("tskey-auth-abc").decode())
    assert doc == {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": ["TS_AUTHKEY=tskey-auth-abc", "TS_ACCEPT_DNS=false"],
    }


def test_firewall_patch():
    docs = [d for d in yamlio.load_all(tailscale.render_firewall_patch("10.0.0.0/24")) if d]
    kinds = [(d["kind"], d.get("name")) for d in docs]
    assert kinds == [
        ("NetworkDefaultActionConfig", None),
        ("NetworkRuleConfig", "admin-via-tailnet"),
        ("NetworkRuleConfig", "cluster-internal"),
        ("NetworkRuleConfig", "flannel-vxlan"),
    ]
    assert docs[0]["ingress"] == "block"
    assert docs[1]["portSelector"] == {"ports": [50000, 6443], "protocol": "tcp"}
    assert docs[1]["ingress"] == [
        {"subnet": "100.64.0.0/10"},
        {"subnet": "fd7a:115c:a1e0::/48"},
    ]
    assert docs[2]["portSelector"]["ports"] == [50000, 50001, 6443, "2379-2380", 10250]
    assert docs[2]["ingress"] == [
        {"subnet": "10.0.0.0/24"},
        {"subnet": "10.244.0.0/16"},
        {"subnet": "10.96.0.0/12"},
    ]
    assert docs[3]["portSelector"] == {"ports": [4789], "protocol": "udp"}


def test_rewrite_talosconfig():
    doc = yamlio.load(
        tailscale.rewrite_talosconfig(TALOSCONFIG, ["cp-01.t.ts.net", "cp-02.t.ts.net"], "cp-01.t.ts.net").decode()
    )
    context = doc["contexts"]["test"]
    assert list(context["endpoints"]) == ["cp-01.t.ts.net", "cp-02.t.ts.net"]
    assert list(context["nodes"]) == ["cp-01.t.ts.net"]
    assert context["key"] == "a2V5"
    with pytest.raises(ValidationError):
        tailscale.rewrite_talosconfig(b"context: nope\ncontexts: {}\n", ["x"], "x")


def test_rewrite_kubeconfig_server():
    doc = yamlio.load(tailscale.rewrite_kubeconfig_server(KUBECONFIG, "https://cp-01.t.ts.net:6443").decode())
    assert doc["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
    assert doc["clusters"][0]["cluster"]["certificate-authority-data"] == "Y2E="
    with pytest.raises(ValidationError):
        tailscale.rewrite_kubeconfig_server(b"kind: Config\nclusters: []\n", "https://x:6443")


@need("talosctl")
def test_patches_validate_with_talosctl(tmp_path):
    secrets = tmp_path / "secrets.yaml"
    subprocess.run(
        ["talosctl", "gen", "secrets", "-o", str(secrets)],
        check=True,
        capture_output=True,
    )
    (tmp_path / "ts.yaml").write_bytes(tailscale.render_extension_patch("tskey-auth-abc"))
    (tmp_path / "fw.yaml").write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    out = tmp_path / "cp.yaml"
    subprocess.run(
        [
            "talosctl",
            "gen",
            "config",
            "demo",
            "https://10.0.0.10:6443",
            "--with-secrets",
            str(secrets),
            "-t",
            "controlplane",
            "-o",
            str(out),
            "--with-docs=false",
            "--with-examples=false",
            "--config-patch",
            f"@{tmp_path / 'ts.yaml'}",
            "--config-patch",
            f"@{tmp_path / 'fw.yaml'}",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["talosctl", "validate", "--config", str(out), "--mode", "metal"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_is_locked_down(empty_repo):
    assert tailscale.is_locked_down(empty_repo) is False
    tailscale.firewall_patch_path(empty_repo).parent.mkdir(parents=True)
    tailscale.firewall_patch_path(empty_repo).write_text("x")
    assert tailscale.is_locked_down(empty_repo) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tailscale.py -q`
Expected: `ImportError: cannot import name 'tailscale'`.

- [ ] **Step 3: Implement the pure part of `src/talkub/tailscale.py`**

```python
"""Tailscale as the only admin path: extension config, Talos firewall, client endpoints.

The cluster endpoint (used by the nodes themselves) is never changed: nodes run with
TS_ACCEPT_DNS=false and cannot resolve tailnet names. Only admin clients (talosctl,
kubectl) are pointed at the tailnet.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from ruamel.yaml.error import YAMLError

from talkub import yamlio
from talkub.errors import ValidationError
from talkub.lifecycle import ordered_nodes
from talkub.repo import Repo, Settings

EXTENSION = "siderolabs/tailscale"
TAILNET_V4 = "100.64.0.0/10"
TAILNET_V6 = "fd7a:115c:a1e0::/48"
POD_SUBNET = "10.244.0.0/16"
SERVICE_SUBNET = "10.96.0.0/12"

_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_TAILNET = re.compile(rf"{_LABEL}(\.{_LABEL})+")


def extension_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "tailscale.sops.yaml"


def firewall_patch_path(repo: Repo) -> Path:
    return repo.talos_dir / "patches" / "firewall.yaml"


def is_locked_down(repo: Repo) -> bool:
    return firewall_patch_path(repo).is_file()


def validate_tailnet(tailnet: str) -> str:
    if not _TAILNET.fullmatch(tailnet):
        raise ValidationError(
            f"invalid tailnet {tailnet!r}",
            hint="e.g. tail1234.ts.net (Tailscale admin console → DNS)",
        )
    return tailnet


def validate_authkey(key: str) -> str:
    key = key.strip()
    if not key.startswith("tskey-"):
        raise ValidationError(
            "the Tailscale auth key must start with 'tskey-'",
            hint="create a tagged, reusable, pre-approved auth key in the Tailscale admin console",
        )
    return key


def validate_cidr(cidr: str) -> str:
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except ValueError:
        raise ValidationError(f"invalid --node-cidr {cidr!r}", hint="e.g. 10.0.0.0/24") from None


def controlplanes(settings: Settings) -> list[str]:
    return [name for name in ordered_nodes(settings, []) if settings.nodes[name].role == "controlplane"]


def default_node_cidr(settings: Settings) -> str:
    first = ordered_nodes(settings, [])[0]
    ip = settings.nodes[first].ip
    try:
        return str(ipaddress.ip_interface(f"{ip}/24").network)
    except ValueError:
        raise ValidationError(
            f"cannot derive the node network from {ip!r}",
            hint="pass --node-cidr, e.g. 10.0.0.0/24",
        ) from None


def admin_server(settings: Settings) -> str:
    """kubeconfig server for admins: the first control plane's management address."""
    cps = controlplanes(settings)
    if not cps:
        raise ValidationError("no controlplane node defined")
    return f"https://{settings.mgmt_address(cps[0])}:6443"


def render_extension_patch(authkey: str) -> bytes:
    doc = {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": [f"TS_AUTHKEY={authkey}", "TS_ACCEPT_DNS=false"],
    }
    return yamlio.dump(doc).encode()


def render_firewall_patch(node_cidr: str) -> str:
    docs = [
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkDefaultActionConfig",
            "ingress": "block",
        },
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
            "portSelector": {
                "ports": [50000, 50001, 6443, "2379-2380", 10250],
                "protocol": "tcp",
            },
            "ingress": [
                {"subnet": node_cidr},
                {"subnet": POD_SUBNET},
                {"subnet": SERVICE_SUBNET},
            ],
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "NetworkRuleConfig",
            "name": "flannel-vxlan",
            "portSelector": {"ports": [4789], "protocol": "udp"},
            "ingress": [{"subnet": node_cidr}],
        },
    ]
    header = (
        "# Talos firewall written by `talkub tailscale lockdown`: from outside, the Talos and\n"
        "# Kubernetes APIs are reachable only via the tailnet; the node network keeps cluster ports.\n"
    )
    return header + "---\n".join(yamlio.dump(doc) for doc in docs)


def _load_mapping(data: bytes, what: str) -> dict:
    try:
        doc = yamlio.load(data.decode())
    except (YAMLError, UnicodeDecodeError):
        raise ValidationError(f"{what} is not valid YAML") from None
    if not isinstance(doc, Mapping):
        raise ValidationError(f"{what} is not a YAML mapping")
    return doc


def rewrite_talosconfig(data: bytes, endpoints: Sequence[str], node: str) -> bytes:
    doc = _load_mapping(data, "talosconfig")
    contexts = doc.get("contexts")
    name = doc.get("context")
    if not isinstance(contexts, Mapping) or not isinstance(contexts.get(name), Mapping):
        raise ValidationError("talosconfig has no current context")
    contexts[name]["endpoints"] = list(endpoints)
    contexts[name]["nodes"] = [node]
    return yamlio.dump(doc).encode()


def rewrite_kubeconfig_server(data: bytes, server: str) -> bytes:
    doc = _load_mapping(data, "kubeconfig")
    clusters = doc.get("clusters")
    entries = [c.get("cluster") for c in clusters if isinstance(c, Mapping)] if isinstance(clusters, list) else []
    targets = [cluster for cluster in entries if isinstance(cluster, Mapping)]
    if not targets:
        raise ValidationError("kubeconfig has no clusters to update")
    for cluster in targets:
        cluster["server"] = server
    return yamlio.dump(doc).encode()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tailscale.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/talkub/tailscale.py tests/test_tailscale.py
git commit -m "feat: tailscale patch renderers and client config rewriters"
```

---

### Task 2: `enable` / `lockdown` flows, `node add` default mgmt, bootstrap kubeconfig server

**Files:**
- Modify: `src/talkub/tailscale.py` (add flows), `src/talkub/nodes.py` (`add_node`), `src/talkub/bootstrap.py` (`_store_kubeconfig` / its caller)
- Test: `tests/test_tailscale_flows.py`, `tests/test_nodes.py` (extend), `tests/test_bootstrap.py` (extend)

**Interfaces:**
- Consumes: Task 1 functions; `Context` (repo, sops.encrypt/decrypt, keyring.public_key); `images.used_platforms`; `lifecycle.is_bootstrapped`; `atomic_write`; `repo.TailscaleSpec`; `dataclasses.replace`.
- Produces: `tailscale.enable(ctx, *, tailnet, authkey, node_cidr=None) -> list[str]` (notes), `tailscale.lockdown(ctx, *, node_cidr=None) -> list[str]` (notes); `add_node` sets `mgmt = <name>.<tailnet>` when locked down and no `--mgmt` was given; bootstrap rewrites the fetched kubeconfig's server to `admin_server(settings)` when locked down.

- [ ] **Step 1: Write the failing tests**

`tests/test_tailscale_flows.py`:

```python
import pytest

from helpers import need
from talkub import tailscale, yamlio
from talkub.context import Context
from talkub.errors import ValidationError
from talkub.fsutil import atomic_write
from talkub.repo import NodeSpec

pytestmark = need("age", "age-keygen", "sops")

TALOSCONFIG = b"context: test\ncontexts:\n    test:\n        endpoints: [10.0.0.10]\n        nodes: [10.0.0.10]\n        key: a2V5\n"
KUBECONFIG = b"apiVersion: v1\nkind: Config\nclusters:\n- name: test\n  cluster:\n    server: https://10.0.0.10:6443\n"
KEY = "tskey-auth-secretvalue"


@pytest.fixture
def repo(repo_with_configs):
    """Two control planes and a worker, real-looking talosconfig/kubeconfig (encrypted)."""
    ctx = Context.open(repo_with_configs)
    public = ctx.keyring.public_key()
    for path, data in (
        (repo_with_configs.talosconfig, TALOSCONFIG),
        (repo_with_configs.kubeconfig, KUBECONFIG),
    ):
        atomic_write(path, ctx.sops.encrypt(data, filename=path.name, public_key=public))
    settings = repo_with_configs.load_settings()
    settings.nodes["cp-02"] = NodeSpec(ip="10.0.0.12")
    settings.nodes["w-01"] = NodeSpec(ip="10.0.0.20", role="worker", mgmt="w-01.lan")
    repo_with_configs.write_settings(settings)
    return repo_with_configs


def _plain_files(root):
    return [p for p in root.rglob("*") if p.is_file() and ".sops." not in p.name and p.suffix != ".age"]


def test_enable_keeps_addresses_and_encrypts_key(repo):
    before_talosconfig = repo.talosconfig.read_bytes()
    notes = tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    settings = repo.load_settings()
    assert settings.tailscale.tailnet == "t.ts.net"
    assert settings.tailscale.node_cidr == "10.0.0.0/24"
    assert settings.nodes["cp-01"].mgmt is None  # addresses untouched: LAN still works
    assert repo.talosconfig.read_bytes() == before_talosconfig
    assert tailscale.EXTENSION in settings.schematics["metal"].extensions
    assert not settings.schematics["metal"].is_current  # resolved by the next image show / plan / upgrade
    assert not tailscale.is_locked_down(repo)
    patch = Context.open(repo).sops.decrypt(tailscale.extension_patch_path(repo))
    assert f"TS_AUTHKEY={KEY}".encode() in patch
    assert not any(KEY.encode() in p.read_bytes() for p in _plain_files(repo.root))
    assert any("upgrade talos" in n for n in notes)  # the repo has a kubeconfig → running cluster


def test_enable_twice_and_bad_input(repo):
    with pytest.raises(ValidationError):
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey="nope")
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY, node_cidr="10.0.0.0/16")
    assert repo.load_settings().tailscale.node_cidr == "10.0.0.0/16"
    with pytest.raises(ValidationError) as exc:
        tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    assert "already enabled" in str(exc.value)


def test_lockdown_switches_clients_to_the_tailnet(repo):
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    notes = tailscale.lockdown(Context.open(repo))
    settings = repo.load_settings()
    assert settings.nodes["cp-01"].mgmt == "cp-01.t.ts.net"
    assert settings.nodes["cp-02"].mgmt == "cp-02.t.ts.net"
    assert settings.nodes["w-01"].mgmt == "w-01.lan"  # explicit mgmt is kept
    assert settings.endpoint == "https://10.0.0.10:6443"  # cluster endpoint unchanged
    ctx = Context.open(repo)
    talosconfig = yamlio.load(ctx.sops.decrypt(repo.talosconfig).decode())["contexts"]["test"]
    assert list(talosconfig["endpoints"]) == ["cp-01.t.ts.net", "cp-02.t.ts.net"]
    assert list(talosconfig["nodes"]) == ["cp-01.t.ts.net"]
    kubeconfig = yamlio.load(ctx.sops.decrypt(repo.kubeconfig).decode())
    assert kubeconfig["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
    assert tailscale.is_locked_down(repo)
    assert "10.0.0.0/24" in tailscale.firewall_patch_path(repo).read_text()
    assert any("talkub apply" in n for n in notes)


def test_lockdown_requires_enable_and_runs_once(repo):
    with pytest.raises(ValidationError) as exc:
        tailscale.lockdown(Context.open(repo))
    assert "tailscale enable" in exc.value.hint
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    with pytest.raises(ValidationError):
        tailscale.lockdown(Context.open(repo))


def test_lockdown_without_kubeconfig(repo):
    repo.kubeconfig.unlink()
    tailscale.enable(Context.open(repo), tailnet="t.ts.net", authkey=KEY)
    tailscale.lockdown(Context.open(repo))
    assert not repo.kubeconfig.exists()
```

Append to `tests/test_nodes.py`:

```python
def test_add_node_after_lockdown_uses_tailnet_mgmt(empty_repo):
    from talkub import tailscale
    from talkub.repo import TailscaleSpec

    settings = empty_repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    empty_repo.write_settings(settings)
    add_node(empty_repo, "w-01", NodeSpec(ip="10.0.0.11", role="worker"), NodeOptions())
    assert empty_repo.load_settings().nodes["w-01"].mgmt is None  # enabled, not locked down
    tailscale.firewall_patch_path(empty_repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(empty_repo).write_text("x")
    add_node(empty_repo, "w-02", NodeSpec(ip="10.0.0.12", role="worker"), NodeOptions())
    add_node(
        empty_repo,
        "w-03",
        NodeSpec(ip="10.0.0.13", role="worker", mgmt="w3.lan"),
        NodeOptions(),
    )
    nodes = empty_repo.load_settings().nodes
    assert nodes["w-02"].mgmt == "w-02.t.ts.net"
    assert nodes["w-03"].mgmt == "w3.lan"
```

Append to `tests/test_bootstrap.py` (uses its `cluster` fixture and `KUBECONFIG`):

```python
def test_bootstrap_after_lockdown_points_kubeconfig_at_tailnet(cluster):
    from talkub import tailscale, yamlio
    from talkub.repo import NodeSpec, TailscaleSpec

    repo, spies = cluster
    settings = repo.load_settings()
    settings.tailscale = TailscaleSpec(tailnet="t.ts.net", node_cidr="10.0.0.0/24")
    settings.nodes["cp-01"] = NodeSpec(ip="10.0.0.10", mgmt="cp-01.t.ts.net")
    repo.write_settings(settings)
    tailscale.firewall_patch_path(repo).parent.mkdir(parents=True, exist_ok=True)
    tailscale.firewall_patch_path(repo).write_text(tailscale.render_firewall_patch("10.0.0.0/24"))
    spies["talosctl"].rules.insert(
        0,
        {
            "match": ["kubeconfig"],
            "rc": 0,
            "stderr": "",
            "stdout": "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        },
    )
    spies["talosctl"]._save()
    bootstrap.run(Context.open(repo))
    stored = yamlio.load(Context.open(repo).sops.decrypt(repo.kubeconfig).decode())
    assert stored["clusters"][0]["cluster"]["server"] == "https://cp-01.t.ts.net:6443"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tailscale_flows.py tests/test_nodes.py tests/test_bootstrap.py -q`
Expected: `AttributeError: module 'talkub.tailscale' has no attribute 'enable'`; the node and bootstrap tests fail on their assertions.

- [ ] **Step 3: Add the flows to `src/talkub/tailscale.py`**

Add imports at the top: `import dataclasses`, `from talkub import images`, `from talkub.context import Context`, `from talkub.fsutil import atomic_write`, `from talkub.lifecycle import is_bootstrapped` (extend the existing lifecycle import), `from talkub.repo import TailscaleSpec` (extend the repo import). Append:

```python
def enable(ctx: Context, *, tailnet: str, authkey: str, node_cidr: str | None = None) -> list[str]:
    """Step 1: auth-key patch + extension in every used schematic. Addresses stay unchanged,
    so a running cluster can still be upgraded and configured via LAN."""
    repo = ctx.repo
    settings = repo.load_settings()
    if settings.tailscale is not None or extension_patch_path(repo).exists():
        raise ValidationError(
            "tailscale is already enabled",
            hint="change the auth key with `talkub sops edit talos/patches/tailscale.sops.yaml`",
        )
    validate_tailnet(tailnet)
    authkey = validate_authkey(authkey)
    cidr = validate_cidr(node_cidr) if node_cidr else default_node_cidr(settings)
    settings.tailscale = TailscaleSpec(tailnet=tailnet, node_cidr=cidr)
    for platform in images.used_platforms(settings):
        spec = settings.ensure_schematic(platform)
        if EXTENSION not in spec.extensions:
            spec.extensions = sorted([*spec.extensions, EXTENSION])
    patch = extension_patch_path(repo)
    encrypted = ctx.sops.encrypt(
        render_extension_patch(authkey),
        filename=patch.name,
        public_key=ctx.keyring.public_key(),
    )
    atomic_write(patch, encrypted)
    repo.write_settings(settings)
    notes = [f"node network for the firewall rules: {cidr} (change with `talkub tailscale lockdown --node-cidr`)"]
    if is_bootstrapped(ctx):
        notes.append(
            "running cluster: `talkub upgrade talos` installs the extension, `talkub apply` configures it; "
            "once `tailscale status` lists every node, run `talkub tailscale lockdown`"
        )
    notes.append("use a tagged, reusable, pre-approved auth key and disable key expiry for these nodes")
    return notes


def lockdown(ctx: Context, *, node_cidr: str | None = None) -> list[str]:
    """Step 2: firewall patch, mgmt = <node>.<tailnet>, client configs pointed at the tailnet."""
    repo = ctx.repo
    settings = repo.load_settings()
    if settings.tailscale is None:
        raise ValidationError("tailscale is not enabled", hint="run `talkub tailscale enable` first")
    if is_locked_down(repo):
        raise ValidationError(
            f"{repo.rel(firewall_patch_path(repo))} already exists",
            hint="already locked down",
        )
    if node_cidr:
        settings.tailscale = TailscaleSpec(tailnet=settings.tailscale.tailnet, node_cidr=validate_cidr(node_cidr))
    tailnet = settings.tailscale.tailnet
    settings.nodes = {
        name: node if node.mgmt else dataclasses.replace(node, mgmt=f"{name}.{tailnet}")
        for name, node in settings.nodes.items()
    }
    endpoints = [settings.mgmt_address(name) for name in controlplanes(settings)]
    if not endpoints:
        raise ValidationError("no controlplane node defined")
    public = ctx.keyring.public_key()
    # Everything is computed in memory first; files are written only when all of it worked.
    talosconfig = ctx.sops.encrypt(
        rewrite_talosconfig(ctx.sops.decrypt(repo.talosconfig), endpoints, endpoints[0]),
        filename=repo.talosconfig.name,
        public_key=public,
    )
    kubeconfig = None
    if repo.kubeconfig.is_file():
        kubeconfig = ctx.sops.encrypt(
            rewrite_kubeconfig_server(ctx.sops.decrypt(repo.kubeconfig), admin_server(settings)),
            filename=repo.kubeconfig.name,
            public_key=public,
        )
    atomic_write(
        firewall_patch_path(repo),
        render_firewall_patch(settings.tailscale.node_cidr).encode(),
    )
    atomic_write(repo.talosconfig, talosconfig)
    if kubeconfig is not None:
        atomic_write(repo.kubeconfig, kubeconfig)
    repo.write_settings(settings)
    return [
        f"talkub now reaches the nodes via: {', '.join(settings.mgmt_address(n) for n in settings.nodes)}",
        "run `talkub apply` to activate the firewall; afterwards 50000/6443 are open only to the tailnet "
        f"and the node network {settings.tailscale.node_cidr}",
    ]
```

- [ ] **Step 4: `add_node` default mgmt after lockdown (`src/talkub/nodes.py`)**

In `add_node`, right after `settings = repo.load_settings()`, add (use `import dataclasses` at the top; do not import `talkub.tailscale` here — it would create an import cycle via lifecycle → render → nodes):

```python
    firewall = repo.talos_dir / "patches" / "firewall.yaml"
    if spec.mgmt is None and settings.tailscale is not None and firewall.is_file():
        # Locked down: talkub reaches nodes only via the tailnet (see tailscale.lockdown).
        spec = dataclasses.replace(spec, mgmt=f"{name}.{settings.tailscale.tailnet}")
```

(Keep the existing `--public-ingress` warning, which checks the same file.)

- [ ] **Step 5: Bootstrap kubeconfig server after lockdown (`src/talkub/bootstrap.py`)**

Import `from talkub import tailscale` (bootstrap is not imported by tailscale, so no cycle). In `run`, replace `_store_kubeconfig(ctx, kubeconfig)` with:

```python
if tailscale.is_locked_down(ctx.repo):
    kubeconfig = tailscale.rewrite_kubeconfig_server(kubeconfig, tailscale.admin_server(settings))
_store_kubeconfig(ctx, kubeconfig)
```

- [ ] **Step 6: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_tailscale_flows.py tests/test_nodes.py tests/test_bootstrap.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/talkub/tailscale.py src/talkub/nodes.py src/talkub/bootstrap.py tests/test_tailscale_flows.py tests/test_nodes.py tests/test_bootstrap.py
git commit -m "feat: tailscale enable and lockdown flows"
```

---

### Task 3: CLI (`tailscale enable|lockdown`, `init --tailscale`) and README

**Files:**
- Create: `src/talkub/cli/tailscale.py`, `tests/test_cli_tailscale.py`
- Modify: `src/talkub/cli/setup.py` (init flags), `src/talkub/cli/main.py`, `README.md`

**Interfaces:**
- Consumes: `tailscale.enable/lockdown/validate_tailnet/validate_cidr/validate_authkey`, `Context.open`, `scaffold.init_cluster`.
- Produces: `cli.tailscale.read_authkey(path: Path | None) -> str`; `talkub tailscale enable --tailnet T [--authkey-file F] [--node-cidr C]`; `talkub tailscale lockdown [--node-cidr C]`; `talkub init … --tailscale --tailnet T [--tailscale-authkey-file F] [--node-cidr C]` (enable + lockdown right after creating the repo).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_tailscale.py`:

```python
import pytest

from helpers import need
from talkub import tailscale
from talkub.cli.main import main
from talkub.context import Context
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops")

HELM = "printf 'apiVersion: v2\\nname: x\\nversion: 1.2.3\\n'\n"
TALOSCONFIG = b"context: test\ncontexts:\n    test:\n        endpoints: [10.0.0.10]\n        nodes: [10.0.0.10]\n"
KEY = "tskey-auth-fromfile"


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


@pytest.fixture
def repo(repo_with_configs):
    ctx = Context.open(repo_with_configs)
    atomic_write(
        repo_with_configs.talosconfig,
        ctx.sops.encrypt(
            TALOSCONFIG,
            filename="talosconfig.sops.yaml",
            public_key=ctx.keyring.public_key(),
        ),
    )
    repo_with_configs.kubeconfig.unlink()
    return repo_with_configs


def test_enable_with_key_file_then_lockdown(repo, tmp_path, capfd):
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n")
    assert (
        _run(
            [
                "--repo",
                str(repo.root),
                "tailscale",
                "enable",
                "--tailnet",
                "t.ts.net",
                "--authkey-file",
                str(key_file),
            ]
        )
        == 0
    )
    assert "note:" in capfd.readouterr().out
    assert _run(["--repo", str(repo.root), "tailscale", "lockdown"]) == 0
    assert tailscale.is_locked_down(repo)
    assert repo.load_settings().nodes["cp-01"].mgmt == "cp-01.t.ts.net"


def test_enable_prompts_for_the_key(repo, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": KEY)
    assert _run(["--repo", str(repo.root), "tailscale", "enable", "--tailnet", "t.ts.net"]) == 0
    assert KEY.encode() in Context.open(repo).sops.decrypt(tailscale.extension_patch_path(repo))


def test_enable_rejects_bad_key_file(repo, tmp_path, capfd):
    assert (
        _run(
            [
                "--repo",
                str(repo.root),
                "tailscale",
                "enable",
                "--tailnet",
                "t.ts.net",
                "--authkey-file",
                str(tmp_path / "missing"),
            ]
        )
        == 1
    )
    assert "cannot read" in capfd.readouterr().err


def test_init_tailscale_needs_tailnet(tmp_path, capfd, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            "cp-01=10.0.0.10",
            "--tailscale",
        ]
    )
    assert code == 1
    assert "--tailnet" in capfd.readouterr().err
    assert not (tmp_path / "demo").exists()


@need("talosctl")
def test_init_with_tailscale(tmp_path, software_identity, fake_bin, monkeypatch):
    fake_bin("helm", HELM)
    monkeypatch.chdir(tmp_path)
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    code = _run(
        [
            "init",
            "demo",
            "--endpoint",
            "https://10.0.0.10:6443",
            "--git-url",
            "https://g",
            "--node",
            "cp-01=10.0.0.10",
            "--tailscale",
            "--tailnet",
            "t.ts.net",
            "--tailscale-authkey-file",
            str(key_file),
        ]
    )
    assert code == 0
    from talkub.repo import Repo

    repo = Repo((tmp_path / "demo").resolve())
    settings = repo.load_settings()
    assert settings.endpoint == "https://10.0.0.10:6443"
    assert settings.nodes["cp-01"].mgmt == "cp-01.t.ts.net"
    assert tailscale.is_locked_down(repo)
    assert tailscale.EXTENSION in settings.schematics["metal"].extensions
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_tailscale.py -q`
Expected: failures (`No such command 'tailscale'`, `No such option: --tailscale`).

- [ ] **Step 3: Implement `src/talkub/cli/tailscale.py`**

```python
"""tailscale enable, tailscale lockdown."""

from __future__ import annotations

import getpass
from pathlib import Path

import typer

from talkub import tailscale
from talkub.errors import ValidationError

app = typer.Typer(
    help="Tailscale as the only admin path to the Talos and Kubernetes APIs.",
    no_args_is_help=True,
)
NODE_CIDR = typer.Option(
    None,
    "--node-cidr",
    help="Node network for cluster-internal rules (default: /24 of the first node).",
)


def read_authkey(path: Path | None) -> str:
    """From a file, or prompted without echo — never from the command line."""
    if path is not None:
        try:
            return path.expanduser().read_text().strip()
        except OSError as err:
            raise ValidationError(f"cannot read {path}: {err.strerror}") from None
    return getpass.getpass("Tailscale auth key: ")


def print_notes(notes: list[str]) -> None:
    for note in notes:
        print(f"note: {note}")


@app.command("enable")
def enable(
    ctx: typer.Context,
    tailnet: str = typer.Option(..., "--tailnet", help="Tailnet DNS name, e.g. tail1234.ts.net."),
    authkey_file: Path = typer.Option(None, "--authkey-file", help="File with the auth key (default: prompt)."),
    node_cidr: str = NODE_CIDR,
) -> None:
    """Step 1: add the Tailscale extension and auth key (addresses stay on the LAN)."""
    context = ctx.obj.context()
    tailscale.validate_tailnet(tailnet)
    print_notes(
        tailscale.enable(
            context,
            tailnet=tailnet,
            authkey=read_authkey(authkey_file),
            node_cidr=node_cidr,
        )
    )


@app.command("lockdown")
def lockdown(ctx: typer.Context, node_cidr: str = NODE_CIDR) -> None:
    """Step 2: Talos firewall + tailnet addresses for talosctl/kubectl."""
    print_notes(tailscale.lockdown(ctx.obj.context(), node_cidr=node_cidr))
```

Register in `src/talkub/cli/main.py`: add `from talkub.cli import tailscale as tailscale_cli` and `app.add_typer(tailscale_cli.app, name="tailscale")`.

- [ ] **Step 4: `init --tailscale` in `src/talkub/cli/setup.py`**

Add imports `from talkub import tailscale`, `from talkub.cli.tailscale import print_notes, read_authkey`, `from talkub.context import Context`. Add parameters to `init` (after `public_ingress`):

```python
use_tailscale: bool = (typer.Option(False, "--tailscale", help="Tailscale as the only admin path."),)
tailnet: str = (typer.Option(None, "--tailnet", help="Tailnet DNS name (with --tailscale)."),)
tailscale_authkey_file: Path = (
    typer.Option(None, "--tailscale-authkey-file", help="Auth key file (default: prompt)."),
)
node_cidr: str = (typer.Option(None, "--node-cidr", help="Node network for the firewall (with --tailscale)."),)
```

At the start of the function body (before anything is created), validate and read the key:

```python
authkey = None
if use_tailscale:
    if not tailnet:
        raise ValidationError("--tailscale needs --tailnet", hint="e.g. --tailnet tail1234.ts.net")
    tailscale.validate_tailnet(tailnet)
    if node_cidr:
        tailscale.validate_cidr(node_cidr)
    authkey = tailscale.validate_authkey(read_authkey(tailscale_authkey_file))
elif tailnet or tailscale_authkey_file or node_cidr:
    raise ValidationError("--tailnet/--tailscale-authkey-file/--node-cidr need --tailscale")
```

After `repo = init_cluster(request, Runner())`:

```python
if authkey is not None:
    context = Context.open(repo)
    print_notes(tailscale.enable(context, tailnet=tailnet, authkey=authkey, node_cidr=node_cidr))
    print_notes(tailscale.lockdown(Context.open(repo)))
```

- [ ] **Step 5: README**

Insert before `## Admin changes`:

````markdown
## Tailscale as the only admin path

    talkub init homelab … --tailscale --tailnet tail1234.ts.net     # new cluster: both steps at once
    # existing cluster:
    talkub tailscale enable --tailnet tail1234.ts.net   # auth key via prompt or --authkey-file
    talkub upgrade talos && talkub apply                # installs + configures the extension (via LAN)
    talkub tailscale lockdown                           # firewall + tailnet names for talosctl/kubectl
    talkub apply                                        # activates the firewall (via the tailnet)

The nodes keep their LAN/public cluster endpoint and do not use Tailscale DNS; only admin
clients go through the tailnet. Use a tagged, reusable, pre-approved auth key.
````

- [ ] **Step 6: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_cli_tailscale.py -q`, then `uv run pytest -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/talkub/cli/tailscale.py src/talkub/cli/setup.py src/talkub/cli/main.py README.md tests/test_cli_tailscale.py
git commit -m "feat: tailscale CLI and init --tailscale"
```

---

### Task 4: `sops edit` follow-ups

**Files:**
- Modify: `src/talkub/crypto/sops.py` (`Sops.edit`), `src/talkub/cli/sops.py`
- Test: `tests/test_sops_edit.py`

**Interfaces:**
- Consumes: `runner.require_tool`, `yamlio.load_all`, `ephemeral.private_file`.
- Produces: `Sops.edit(path, public_key, *, retry: Callable[[str], bool] = lambda message: False) -> int` — returns 1 when invalid YAML was not re-opened (nothing written).

- [ ] **Step 1: Write the failing tests**

`tests/test_sops_edit.py`:

```python
import pytest

from helpers import need
from talkub.context import Context
from talkub.errors import ToolMissing
from talkub.fsutil import atomic_write

pytestmark = need("age", "age-keygen", "sops")

# First run writes broken YAML, every later run writes valid YAML; counts runs in a file.
EDITOR = """n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
if [ "$n" = 1 ]; then printf 'a: [unclosed\\n' > "$1"; else printf 'a: fixed\\n' > "$1"; fi
"""


@pytest.fixture
def secret(keyed_repo):
    ctx = Context.open(keyed_repo)
    path = keyed_repo.root / "x.sops.yaml"
    atomic_write(
        path,
        ctx.sops.encrypt(b"a: 1\n", filename=path.name, public_key=ctx.keyring.public_key()),
    )
    return ctx, path


def _editor(fake_bin, tmp_path, monkeypatch):
    fake_bin("fake-editor", EDITOR.format(counter=tmp_path / "count"))
    monkeypatch.setenv("EDITOR", "fake-editor")
    return tmp_path / "count"


def test_invalid_yaml_reopens_editor(secret, fake_bin, tmp_path, monkeypatch):
    ctx, path = secret
    counter = _editor(fake_bin, tmp_path, monkeypatch)
    questions = []
    code = ctx.sops.edit(path, ctx.keyring.public_key(), retry=lambda q: questions.append(q) or True)
    assert code == 0
    assert counter.read_text().strip() == "2"
    assert "not valid YAML" in questions[0]
    assert ctx.sops.decrypt(path) == b"a: fixed\n"


def test_declined_reopen_writes_nothing(secret, fake_bin, tmp_path, monkeypatch, capfd):
    ctx, path = secret
    _editor(fake_bin, tmp_path, monkeypatch)
    before = path.read_bytes()
    assert ctx.sops.edit(path, ctx.keyring.public_key(), retry=lambda q: False) == 1
    assert path.read_bytes() == before
    assert "discarded" in capfd.readouterr().err


def test_missing_editor_is_reported_before_decrypting(keyed_repo, monkeypatch):
    ctx = Context.open(keyed_repo)
    path = keyed_repo.root / "broken.sops.yaml"
    path.write_text("not: encrypted\n")  # decrypting this would fail with DecryptFailed
    monkeypatch.setenv("EDITOR", "definitely-not-an-editor-xyz")
    with pytest.raises(ToolMissing):
        ctx.sops.edit(path, ctx.keyring.public_key())


def test_cli_declined_reopen_exits_nonzero(secret, fake_bin, tmp_path, monkeypatch):
    import io
    import sys

    from talkub.cli.main import main

    ctx, path = secret
    _editor(fake_bin, tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ctx.repo.root), "sops", "edit", str(path)])
    assert exc.value.code == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sops_edit.py -q`
Expected: failures (`edit()` has no `retry` keyword; the missing-editor test gets `DecryptFailed`).

- [ ] **Step 3: Implement in `src/talkub/crypto/sops.py`**

Add imports `import sys`, `from ruamel.yaml.error import YAMLError`, `from talkub import yamlio`, `from talkub.runner import Runner, require_tool` (extend the existing runner import). Replace `edit` with:

```python
def edit(
    self,
    path: Path,
    public_key: str,
    *,
    retry: Callable[[str], bool] = lambda message: False,
) -> int:
    """Edit `path` in $SOPS_EDITOR/$EDITOR; a missing file starts out empty.

    Not `sops edit`: sops would start the editor with SOPS_AGE_KEY in its environment.
    The editor is checked before anything is decrypted. Invalid YAML re-opens the
    editor while `retry` says so; otherwise nothing is written and 1 is returned.
    """
    editor = shlex.split(os.environ.get("SOPS_EDITOR") or os.environ.get("EDITOR") or "vi")
    require_tool(editor[0])
    original = self.decrypt(path) if path.exists() else b""
    with private_file(path.name, original) as plaintext:
        while True:
            code = self.runner.call([*editor, str(plaintext)])
            if code != 0:
                return code
            edited = plaintext.read_bytes()
            if edited == original:
                return 0
            problem = _yaml_problem(edited)
            if problem is None:
                break
            if not retry(f"the edited file is not valid YAML ({problem}); re-open the editor?"):
                print("talkub: changes discarded, nothing was written", file=sys.stderr)
                return 1
        ciphertext = self.encrypt(edited, filename=path.name, public_key=public_key)
    atomic_write(path, ciphertext)
    return 0
```

and add the module-level helper:

```python
def _yaml_problem(data: bytes) -> str | None:
    try:
        yamlio.load_all(data.decode())
    except (YAMLError, UnicodeDecodeError) as err:
        return str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
    return None
```

In `src/talkub/cli/sops.py`, change the `edit` command body to:

```python
context = ctx.obj.context()
code = context.sops.edit(
    file.resolve(),
    context.keyring.public_key(),
    retry=lambda question: typer.confirm(question, default=True),
)
raise typer.Exit(code)
```

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

Run: `uv run pytest tests/test_sops_edit.py -q`, then `uv run pytest -q`
Expected: all passed (existing edit tests keep working because `retry` defaults to "do not re-open").

- [ ] **Step 5: Commit**

```bash
git add src/talkub/crypto/sops.py src/talkub/cli/sops.py tests/test_sops_edit.py
git commit -m "feat: sops edit re-opens the editor on invalid YAML and checks the editor first"
```

---

## Out of scope

Tailscale Kubernetes Operator (pod access to the tailnet, tailnet ingress) — a later step (spec §13).
Real-node verification of the phase-2b items (talosctl output formats, apply settle, kubeconfig re-minting).
