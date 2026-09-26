# CoreDNS via Argo CD and Tailscale Operator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `talkub coredns takeover` moves CoreDNS from Talos to an Argo CD-managed Helm release (new clusters get it from `init`/`bootstrap`); `talkub tailscale operator enable` installs the Tailscale operator via GitOps with a sealed OAuth secret, a pinned DNSConfig nameserver and a CoreDNS stub zone; `talkub tailscale egress add/remove` manages egress services.

**Architecture:** Pure renderers + flows in `src/talkub/coredns.py` and `src/talkub/tailscale_operator.py`; a shared Argo CD Helm-app renderer in `src/talkub/apps.py`; optional charts in `charts.OPTIONAL_CHARTS`; CLI in `cli/coredns.py` and new subgroups in `cli/tailscale.py`; `bootstrap` installs CoreDNS first when configured.

**Tech Stack:** Python ≥ 3.12, Typer, ruamel.yaml, pytest; kubectl, helm, kubeseal, talosctl.

**Spec:** `docs/superpowers/specs/2026-09-25-talkub-tailscale-operator-design.md`

## Global Constraints

- English code, comments, CLI output. `runner.py` is the only subprocess starter; argv lists only. talosctl/kubectl output parsing lives in `talos/inspect.py` / existing helpers.
- The OAuth client secret is never on argv, never printed, never plaintext on disk: built in memory with `sealing.secret_manifest(..., prompted={...})` and sealed with `sealing.seal(ctx, manifest)`; read via `cli/prompts.read_secret` (file or hidden prompt).
- Constants (in `coredns.py`): `CLUSTER_DNS_IP = "10.96.0.10"`, `TS_NAMESERVER_IP = "10.96.0.53"`, `DEPLOYMENT = "coredns-talkub"`, `RELEASE = "coredns"`, `NAMESPACE = "kube-system"`.
- Chart sources: `coredns` → repo `https://coredns.github.io/helm`, chart `coredns`; `tailscale-operator` → repo `https://pkgs.tailscale.com/helmcharts`, chart `tailscale-operator`. Versions resolved with `charts.latest_version` when first written, then pinned in `.talkub.yaml` `charts`.
- Talos patch file: `talos/patches/controlplane-coredns.yaml` (the `controlplane*` prefix applies it to control planes only, see `talos/render.select_patches`), content: `apiVersion: v1alpha1` / `kind: KubeCoreDNSConfig` / `enabled: false`.
- Generated Argo CD apps use the git URL from `settings.git.url`; missing → `ValidationError(hint="talkub repo set-url <url>")`.
- Paths: `gitops/apps/coredns.yaml`, `gitops/values/coredns.yaml`, `gitops/apps/tailscale-operator.yaml`, `gitops/values/tailscale-operator.yaml`, `gitops/apps/tailscale-operator-config.yaml`, `gitops/manifests/tailscale/operator-oauth.sealed.yaml`, `gitops/manifests/tailscale/dnsconfig.yaml`, `gitops/manifests/tailscale/egress/<name>.yaml`.
- Commits end with `Co-Authored-By: <model> <noreply@anthropic.com>`. `uv run pytest`.

## Review Focus

1. **DNS gap during takeover:** the old Talos pods keep serving until the new release is rolled out; old objects are deleted only after `rollout status` succeeded — test in Task 2.
2. **Wrong cluster DNS IP:** `kube-dns` ClusterIP ≠ `10.96.0.10` → abort before any change — test in Task 2.
3. **OAuth secret leakage:** never in argv/stdout/any plaintext file — test in Task 4.
4. **Idempotence:** stub block added once; `operator enable` twice → "already enabled"; `takeover` after success → "already taken over" — tests in Tasks 1, 2, 4.
5. **Unrelated Services untouched:** takeover adopts only `kube-system/kube-dns` and deletes only the listed Talos object names — test in Task 2.

---

### Task 1: Helm app renderer and CoreDNS renderers

**Files:** Create `src/talkub/apps.py`, `src/talkub/coredns.py` (renderers only); Modify `src/talkub/charts.py`; Tests `tests/test_apps.py`, `tests/test_coredns.py`.

**Interfaces (produced):**
- `charts.OPTIONAL_CHARTS: dict[str, tuple[str, str]]` = `{"coredns": ("https://coredns.github.io/helm", "coredns"), "tailscale-operator": ("https://pkgs.tailscale.com/helmcharts", "tailscale-operator")}`; `charts.resolve(runner, key) -> ChartSpec` (latest version for one key from DEFAULT_CHARTS or OPTIONAL_CHARTS).
- `apps.helm_app(name: str, chart: ChartSpec, git_url: str, values_file: str, namespace: str, *, create_namespace: bool = False, server_side: bool = True) -> str` — same shape as `templates/repo/gitops/apps/sealed-secrets.yaml.tmpl` (Multi-Source: chart source with `helm.valueFiles: [$values/<values_file>]` + git source `ref: values`, `targetRevision: HEAD`), `metadata.namespace: argocd`, `project: default`, automated prune+selfHeal, `syncOptions` containing `CreateNamespace=true` if requested and `ServerSideApply=true` if server_side. Leading comment line `# Managed by talkub.`
- `apps.directory_app(name: str, git_url: str, path: str, namespace: str, *, skip_dry_run_on_missing: bool = False) -> str` — `source: {repoURL, targetRevision: HEAD, path, directory: {recurse: true}}`; syncOptions `ServerSideApply=true` and, if requested, `SkipDryRunOnMissingResource=true`.
- `coredns.talos_patch() -> str`; `coredns.values(tailnet: str | None = None) -> str` (spec §4 values; with `tailnet` includes the stub block); `coredns.add_stub_zone(values_text: str, tailnet: str) -> tuple[str, bool]` (round-trip via yamlio; returns (text, changed); unchanged when a server with that zone exists).

Values structure (write exactly; `servers` uses the chart's format `zones: [{zone: .}]`, `port: 53`, `plugins: [{name, parameters?, configBlock?}]`):

```python
TALOS_DEFAULT_PLUGINS = [
    {"name": "errors"},
    {"name": "health", "configBlock": "lameduck 5s"},
    {"name": "ready"},
    {"name": "log", "parameters": ".", "configBlock": "class error"},
    {"name": "prometheus", "parameters": ":9153"},
    {
        "name": "kubernetes",
        "parameters": "cluster.local in-addr.arpa ip6.arpa",
        "configBlock": "pods insecure\nfallthrough in-addr.arpa ip6.arpa\nttl 30",
    },
    {
        "name": "forward",
        "parameters": ". /etc/resolv.conf",
        "configBlock": "max_concurrent 1000",
    },
    {
        "name": "cache",
        "parameters": 30,
        "configBlock": "disable success cluster.local\ndisable denial cluster.local",
    },
    {"name": "loop"},
    {"name": "reload"},
    {"name": "loadbalance"},
]


def _stub_server(tailnet: str) -> dict:
    return {
        "zones": [{"zone": tailnet}],
        "port": 53,
        "plugins": [
            {"name": "errors"},
            {"name": "cache", "parameters": 30},
            {"name": "forward", "parameters": f". {TS_NAMESERVER_IP}"},
        ],
    }
```

Top-level values: `fullnameOverride: coredns-talkub`, `k8sAppLabelOverride: kube-dns`, `replicaCount: 2`, `priorityClassName: system-cluster-critical`, `tolerations: [{key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule}]`, `service: {name: kube-dns, clusterIP: 10.96.0.10, selector: {k8s-app: kube-dns}}`, `servers: [{zones: [{zone: .}], port: 53, plugins: TALOS_DEFAULT_PLUGINS}, (stub)]`. First line comment `# Helm values for CoreDNS (chart coredns). Used by talkub and by Argo CD.`

- [ ] Tests first: `helm_app` output parses, has both sources, `$values/gitops/values/x.yaml`, syncOptions per flags; `directory_app` recurse + skip flag; `values()` without/with tailnet (stub block present exactly once, forward to 10.96.0.53); `add_stub_zone` idempotent and keeps the comment line; `talos_patch()` parses to the three keys; **render check with the real chart** (mark `need("helm")`, skip on network error): `helm template coredns coredns --repo https://coredns.github.io/helm --version 1.47.1 -f <values>` → the rendered ConfigMap Corefile contains `lameduck 5s`, `forward . /etc/resolv.conf`, and with tailnet `forward . 10.96.0.53`; the Service has clusterIP 10.96.0.10 and selector exactly `{k8s-app: kube-dns}`; the Deployment is named `coredns-talkub`.
- [ ] Implement, PASS, full suite, commit `feat: Argo CD app and CoreDNS renderers`.

---

### Task 2: `talkub coredns takeover`

**Files:** Modify `src/talkub/coredns.py`; Create `src/talkub/cli/coredns.py`; Modify `src/talkub/cli/main.py`; Test `tests/test_coredns_takeover.py`.

**Interfaces:** `coredns.takeover(ctx, *, resume: bool = False, confirm: Callable[[str], bool]) -> list[str]`; CLI `talkub coredns takeover [--resume] [--yes]` (Typer group `coredns`).

Flow (spec §4, implement in this order; each kubectl/helm through `kube_session(ctx)`; talosctl apply through `lifecycle.apply(ctx, [], confirm=lambda q: True)`):
1. Pre-checks: bootstrapped (`repo.kubeconfig` exists); `settings.git` set; `kubectl -n kube-system get svc kube-dns -o jsonpath={.spec.clusterIP}` == `10.96.0.10` else `ValidationError`; if `gitops/apps/coredns.yaml` exists and not `resume` → `ValidationError("CoreDNS is already managed by talkub", hint="--resume if the takeover was interrupted")`; if `resume` and the file does not exist → error.
2. `confirm("Move CoreDNS from Talos to Argo CD now? DNS keeps working during the switch.")` else abort (nothing changed).
3. Unless resuming: resolve chart (`charts.resolve(runner, "coredns")`), write settings `charts.coredns`, `talos/patches/controlplane-coredns.yaml`, `gitops/values/coredns.yaml` (with stub block if an operator app already exists — not in practice), `gitops/apps/coredns.yaml` (`apps.helm_app("coredns", chart, git_url, "gitops/values/coredns.yaml", "kube-system")`).
4. `lifecycle.apply(ctx, [], confirm=...)`; then `kubectl -n kube-system get deploy coredns -o jsonpath={.status.readyReplicas}` — if missing/0: print a warning and continue.
5. Adopt Service: `kubectl -n kube-system annotate svc kube-dns --overwrite meta.helm.sh/release-name=coredns meta.helm.sh/release-namespace=kube-system` and `kubectl -n kube-system label svc kube-dns --overwrite app.kubernetes.io/managed-by=Helm`.
6. Helm: reuse `bootstrap._helm_install(kube, repo, "coredns", "coredns", chart, "kube-system", False)` — promote it to a public `bootstrap.helm_install` (keep the old name as alias only if needed by tests) — then `kubectl -n kube-system rollout status deploy/coredns-talkub --timeout 5m`.
7. Delete old Talos objects only now, each with `--ignore-not-found`: `deploy/coredns`, `configmap/coredns`, `serviceaccount/coredns` (namespace kube-system), `clusterrolebinding/system:coredns`, `clusterrole/system:coredns`.
8. Verify: `kubectl -n kube-system get endpoints kube-dns -o json` → every `targetRef.name` starts with `coredns-talkub-` (parser in `talos/inspect.py`? no — kubectl JSON helper next to existing `inspect.kubelet_versions`); otherwise `CommandFailed` with hint. Notes: `["CoreDNS now runs as coredns-talkub (Helm release coredns)", "commit and push gitops/ and talos/ — Argo CD manages CoreDNS from now on"]`.

- [ ] Tests (spies for kubectl/helm; monkeypatch `lifecycle.apply` to record the call): happy path call order (apply → annotate → label → helm upgrade --install → rollout status → 5 deletes → get endpoints); files written; wrong ClusterIP aborts with no files and no kubectl mutations; already taken over → error; `--resume` skips file writing but runs 4–8; declined confirm changes nothing; failing `rollout status` → no delete calls; endpoints still pointing at old pods → CommandFailed. CLI: `--yes` skips the prompt.
- [ ] Implement, PASS, full suite, commit `feat: talkub coredns takeover`.

---

### Task 3: CoreDNS for new clusters (`init` + `bootstrap`)

**Files:** Modify `src/talkub/scaffold.py`, `src/talkub/bootstrap.py`, `src/talkub/charts.py` (init resolves coredns too); Tests `tests/test_scaffold.py`, `tests/test_bootstrap.py`.

- `init_cluster`: resolve `coredns` chart alongside the defaults (network failure aborts before writing, like today); write the Talos patch, values (no stub) and `gitops/apps/coredns.yaml` via the Task 1 renderers (the git URL is the request's).
- `bootstrap.run`: when `settings.charts` has `coredns`, install it first (`helm_install(... "coredns", "coredns", chart, "kube-system", False)`) right after the kubeconfig is stored and before sealed-secrets; step line `== installing coredns`.
- Existing clusters without `charts.coredns` behave exactly as before.
- [ ] Tests: init writes the three files and `charts.coredns`; `talkub check` stays clean; bootstrap with `charts.coredns` → helm order coredns, sealed-secrets, argocd; without → unchanged (existing tests).
- [ ] Implement, PASS, full suite, commit `feat: new clusters get CoreDNS from talkub`.

---

### Task 4: `talkub tailscale operator enable`

**Files:** Create `src/talkub/tailscale_operator.py`; Modify `src/talkub/cli/tailscale.py` (subgroup `operator`); Test `tests/test_tailscale_operator.py`.

**Interfaces:** `tailscale_operator.enable(ctx, *, client_id: str, client_secret: str, rotate_credentials: bool = False) -> list[str]`; renderers `values(cluster: str) -> str`, `dnsconfig() -> str`.

- Preconditions: `settings.tailscale` set; `gitops/apps/coredns.yaml` exists (hint `talkub coredns takeover`); `settings.git` set; `client_id`/`client_secret` non-empty single lines.
- Already enabled (`gitops/apps/tailscale-operator.yaml` exists): unless `rotate_credentials` → `ValidationError("the Tailscale operator is already enabled", hint="--rotate-credentials to replace the OAuth client")`; with it: only re-seal the secret.
- Seal: `manifest = sealing.secret_manifest("operator-oauth", "tailscale", [], [], {"client_id": client_id, "client_secret": client_secret})`; `sealed = sealing.seal(ctx, manifest)`; `sealing.write_sealed(sealed, repo.gitops_dir / "manifests/tailscale/operator-oauth.sealed.yaml")`.
- Write: chart `tailscale-operator` into settings; `gitops/values/tailscale-operator.yaml` (`installCRDs: true`, `operatorConfig: {hostname: <cluster>-operator, defaultTags: [tag:k8s-operator]}`, `proxyConfig: {defaultTags: tag:k8s}`, `apiServerProxyConfig: {mode: "false"}`); `gitops/apps/tailscale-operator.yaml` (`helm_app(..., namespace="tailscale", create_namespace=True)`); `gitops/apps/tailscale-operator-config.yaml` (`directory_app("tailscale-operator-config", git_url, "gitops/manifests/tailscale", "tailscale", skip_dry_run_on_missing=True)`); `gitops/manifests/tailscale/dnsconfig.yaml` (`apiVersion: tailscale.com/v1alpha1`, `kind: DNSConfig`, `metadata.name: ts-dns`, `spec.nameserver.service.clusterIP: 10.96.0.53`); CoreDNS values: `coredns.add_stub_zone(..., settings.tailscale.tailnet)`.
- Notes: ACL `tagOwners` snippet `{"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}`; OAuth client scopes (Devices Core, Auth Keys, Services — write) with tag `tag:k8s-operator`; "commit and push gitops/ — Argo CD installs the operator".
- CLI: `talkub tailscale operator enable [--client-id ID] [--client-secret-file PATH] [--rotate-credentials]`; client id prompted visibly if missing (`typer.prompt`), secret via `read_secret(path, "OAuth client secret: ", repo_root)`.
- [ ] Tests: files and contents; secret sealed via spy kubeseal (`read_stdin` rule) — the secret appears only in kubeseal's stdin, not in argv, stdout/stderr or any repo file; preconditions (no tailscale, no coredns app, no git url); twice → error; `--rotate-credentials` re-seals only; stub block added once; CLI with `--client-secret-file` outside the repo.
- [ ] Implement, PASS, full suite, commit `feat: talkub tailscale operator enable`.

---

### Task 5: Egress helpers and docs

**Files:** Modify `src/talkub/tailscale_operator.py`, `src/talkub/cli/tailscale.py` (subgroup `egress`), `README.md`; Test `tests/test_tailscale_operator.py`.

- `egress_add(ctx, name, *, fqdn=None, ip=None, namespace="tailscale") -> Path`: operator enabled required; `name` DNS-1123 label (reuse `nodes.validate_node_name` rules if identical, else a local regex `[a-z0-9]([-a-z0-9]*[a-z0-9])?`, ≤ 63); exactly one of `fqdn`/`ip`; `fqdn` must end with `.<tailnet>` (case-insensitive); `ip` must be in 100.64.0.0/10 or fd7a:115c:a1e0::/48; file exists → error. Writes a Service: `apiVersion: v1`, `kind: Service`, `metadata: {name, namespace, annotations: {tailscale.com/tailnet-fqdn: <fqdn>} or {tailscale.com/tailnet-ip: <ip>}}`, `spec: {type: ExternalName, externalName: placeholder}`. Note when namespace ≠ tailscale: "the namespace must exist".
- `egress_remove(ctx, name) -> Path` (missing → error).
- CLI: `talkub tailscale egress add NAME (--fqdn F | --ip I) [--namespace NS]`, `talkub tailscale egress remove NAME`; both print "commit and push gitops/".
- README: sections "CoreDNS" (takeover, what changes) and "Tailscale operator" (enable, ACL/OAuth, egress examples with the MagicDNS name, ingress examples: Service `loadBalancerClass: tailscale`, Ingress `ingressClassName: tailscale`).
- [ ] Tests for validation, file contents, remove; PASS; full suite; commit `feat: tailscale egress add/remove and docs`.
