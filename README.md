# talkub

Admin CLI for a Talos Linux cluster repo: ArgoCD app-of-apps, SOPS/age for
infrastructure secrets, Sealed Secrets for app secrets. The tool lives outside
the cluster repo and is installed with uv.

## Install

From a GitHub release (built wheel, no build step on install):

    gh release download --repo jan-alexander/talkub --pattern '*.whl' --dir /tmp/talkub
    uv tool install --compile-bytecode /tmp/talkub/talkub-*.whl

Or as a single compiled binary (Nuitka; no Python needed; macOS arm64, Linux amd64/arm64):

    gh release download --repo jan-alexander/talkub --pattern 'talkub-*-darwin-arm64' --dir /tmp/talkub
    install -m 755 /tmp/talkub/talkub-*-darwin-arm64 ~/.local/bin/talkub

The binary unpacks itself once per build into `~/.cache/talkub/bin/` (the first start is slower). The
Linux binaries need glibc 2.35 or newer. Build one locally with `scripts/build-binary.sh` (needs a C
compiler; writes `dist/talkub-<version>-<os>-<arch>`).

Or straight from source:

    uv tool install git+https://github.com/jan-alexander/talkub.git

    # needs: age, sops, talosctl (v1.14 or newer; init refuses older), helm (and kubectl, kubeseal later)
    # optional: age-plugin-yubikey, age-plugin-se

Releases are created by the manually triggered `Release` workflow (Actions tab, or
`gh workflow run release.yml`): it takes the version from `pyproject.toml`, aborts if release
`v<version>` already exists, runs lint and tests, then publishes wheel, sdist, the binaries and
`SHA256SUMS`. Bump the version first with `uv version --bump patch|minor|major`.

Shell completion (commands, options and shortcut names for `talkub run`):

    eval "$(talkub completion zsh)"     # in ~/.zshrc (after compinit)
    eval "$(talkub completion bash)"    # in ~/.bashrc
    talkub completion fish > ~/.config/fish/completions/talkub.fish

Arguments after `talkub t`/`talkub k` are not completed; after a shortcut name only the node of a
shortcut with `{node}` placeholders is.

## Key model

- Every `*.sops.yaml` is encrypted to one **cluster master key**.
- `keys/master.age` holds that key, encrypted to all admins in `keys/recipients.txt`.
- Each admin has a personal identity (software key, YubiKey or Secure Enclave)
  in `~/.config/talkub/identities/`.
- Lookup order: `SOPS_AGE_KEY` / `SOPS_AGE_KEY_FILE` (the master key itself, for CI)
  → `TALKUB_IDENTITY` → `~/.config/talkub/identities/`.
- Keep at least two recipients (e.g. YubiKey + offline backup key). There is no passphrase recovery.

## Quick start

Step by step from a node in maintenance mode to a running cluster (repo, identity, charts,
Tailscale): [docs/getting-started.md](docs/getting-started.md).

    talkub identity new --yubikey          # or --se / --software
    talkub init homelab --endpoint https://10.0.0.10:6443 \
        --node cp-01=10.0.0.10 --git-url https://git.example.com/homelab.git
    cd homelab
    talkub admin add age1... --name backup
    talkub check && git init && git add -A && git commit -m "init cluster repo"

The target must be empty (a `.git` is fine) and the root of its own git repository: `init` runs
`git init -b main` only when there is none yet, leaves an existing repo's checked-out branch alone, and
refuses a directory inside another git repository, since Argo CD reads `gitops/apps` relative to the
repo root.

## Daily use

    talkub t get members               # talosctl, config decrypted only for this call
    talkub k get pods -A               # kubectl
    talkub shell                       # subshell with TALOSCONFIG/KUBECONFIG, removed on exit
    talkub sops edit talos/secrets.sops.yaml

`talkub k` needs `kubernetes/kubeconfig.sops.yaml`. It will be created by
`talkub bootstrap` (phase 2, not yet available); until then place a kubeconfig
with `talkub sops edit kubernetes/kubeconfig.sops.yaml`.

`talkub sops edit` decrypts into a private temp file and opens it in
`$SOPS_EDITOR` or `$EDITOR` (default `vi`); the editor never sees the master key.

Inside `talkub shell`, `TALKUB_SESSION` holds the cluster name. zsh prompt example:

    PROMPT='${TALKUB_SESSION:+(talkub:$TALKUB_SESSION) }'$PROMPT

## Shortcuts

Frequent commands can be named in `.talkub.yaml` (or in the global pool, see below) and run with
`talkub run`:

```yaml
shortcuts:
  db-password:
    description: password of the app database
    secret: {namespace: prod, name: db-creds, key: password}
  headlamp-token:
    description: fresh Headlamp login token
    token: {namespace: headlamp, serviceAccount: headlamp, duration: 8h}
  backup:
    job: {namespace: prod, cronjob: backup, follow: true}
  argocd-apps:
    run: [k, -n, argocd, get, applications]
  extensions:
    run: [t, -n, "{node}", get, extensions]
  extension-names:
    run: [t, -n, "{node}", get, extensions, -o, json]
    pipe:
      - [jq, -r, .spec.metadata.name]
      - [sort]
  grafana:
    portForward: {namespace: monitoring, service: grafana, ports: ["3000:80"]}
  k9s:
    kubernetes: [k9s, --readonly]
```

    talkub run                              # list: name, kind, description, origin
    talkub run db-password                  # copy the secret value to the clipboard
    talkub run --print db-password          # write it to stdout instead
    talkub run headlamp-token               # copy a fresh ServiceAccount token to the clipboard
    talkub run --follow backup              # start the job, stream its logs
    talkub run argocd-apps -o wide          # arguments after the name are passed on verbatim
    talkub run extensions cp-01             # a {node} shortcut takes the node first
    talkub run --save-global argocd-apps    # move it from .talkub.yaml to the global pool

Each shortcut has exactly one kind:

- `secret`: reads the Secret with kubectl and copies the base64-decoded key to the clipboard
  (`pbcopy`, `wl-copy` or `xclip`, value on stdin only). A detached helper clears the clipboard
  after 45 s (`TALKUB_CLIPBOARD_CLEAR_SECONDS`), but only if it still holds that value; the helper
  gets only a SHA-256 digest of it. `--print` writes the bare value to stdout instead (a newline
  only on a terminal), e.g. `talkub run --print db-password | ...`. A missing key is reported with
  the names of the keys that exist. Clipboard history managers (e.g. Raycast, Maccy, Alfred) may
  keep their own copy of the value; use `--print`, or exclude talkub/the terminal in those tools.
- `token`: creates a token for the ServiceAccount with `kubectl create token` (`--duration` only if
  `duration` is set, e.g. `8h` or `1h30m`; otherwise the API server's default applies) and delivers it
  like `secret`: to the clipboard, cleared after 45 s, or with `--print` to stdout.
- `job`: creates `<cronjob>-manual-<UTC timestamp>` from the CronJob. With `follow: true` or
  `--follow`, waits (up to 5 min) for its pod to start, streams the logs and exits 0 only if the
  job completed; a failed job, or one still running a minute after the logs end, exits 1.
- `run`: `k`/`kubectl` or `t`/`talosctl` with the configured arguments, as with `talkub k`/`talkub t`;
  further arguments (including options like `-f`) are appended.
- `portForward`: `kubectl port-forward svc/<service> <ports...>` until Ctrl-C.
- `kubernetes`: any program (e.g. `k9s`, `helm`, `stern`) with the configured arguments and the
  decrypted kubeconfig in `KUBECONFIG`; `{kubeconfig}` in a configured argument is replaced by its
  path (e.g. `[mytool, --config={kubeconfig}]`), appended arguments are passed on verbatim. The
  kubeconfig is removed when the program exits, so it must run in the foreground (GUI apps that
  detach, like `open -a Lens`, do not work). Such a shortcut hands cluster credentials to the
  program it names, from `.talkub.yaml` as well as from the global pool: check shortcuts in repos
  you did not write.

`run` and `kubernetes` shortcuts can refer to a node without naming it, so they also work as
global shortcuts. In the configured arguments, `{node}` (or `{node.addr}`) becomes the node's
`mgmt` address, or its `ip` if `mgmt` is not set; `{node.name}`, `{node.ip}`, `{node.mgmt}` and
`{node.role}` give the single field (`{node.mgmt}` is an error for a node without one). The node
is the first argument after the shortcut name (`talkub run extensions cp-01 -o yaml`); it may be
left out in a cluster with a single node. Other braces, like kubectl's `-o jsonpath={.items[0]}`,
are left alone; an unknown `{node.<field>}` is reported when the shortcuts are loaded. The listing
shows such shortcuts as `extensions <node>`.

`run` and `kubernetes` shortcuts can feed their output through further programs with `pipe:`, a list
of stages in the same list form (`{node}` placeholders work there too). Arguments after the name go
to the first stage. Only the first stage gets the cluster credentials: in the others `KUBECONFIG`
and `TALOSCONFIG` point at a missing file, so a `kubectl` or `talosctl` there has no cluster
configured (kubectl falls back to an unauthenticated `localhost:8080`, talosctl finds no endpoints)
instead of reaching whatever cluster your own configuration selects; `{kubeconfig}` is not allowed
in `pipe:`.
If a later stage really needs the cluster, use a `kubernetes` shortcut with `[sh, -c, "…"]`. The
exit code is that of the rightmost failing stage (like `set -o pipefail`); an earlier stage that
stops only because a later one closed its input (`… | head`) does not count as failing. The listing
shows such shortcuts with the kind `run |` or `kubernetes |`. For one-off pipes, pipe
`talkub run <name>` in your own shell.

`--print` and `--follow` go before the name; everything after the name belongs to the shortcut,
and only `run` and `kubernetes` shortcuts accept anything there.

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

## Nodes and machine configs

    talkub node add w-01 --ip 10.0.0.11 --role worker \
        --static 10.0.0.11/24 --gateway 10.0.0.1 --mac aa:bb:cc:dd:ee:ff
    talkub image add siderolabs/intel-ucode      # extensions per platform (metal, hcloud)
    talkub image show --platform hcloud --arch arm64   # installer image + boot medium URL
    talkub apply --insecure w-01                  # first install (maintenance mode, node's --ip)
    talkub plan                                   # Talos dry-run for all nodes
    talkub apply --mode no-reboot                 # asks per node; --yes to skip

Once the node is booted (maintenance mode), `talkub node volumes w-01` lists its disks and asks for
the install disk, an optional EPHEMERAL limit and user volumes (partition, raw or whole disk,
optionally LUKS2 with a node-bound key). It suggests the most portable CEL selector that matches
exactly the chosen disk, simulates where Talos will place each volume and how large it becomes,
and writes the documents into `talos/nodes/w-01.yaml`. On an installed node it reads the disks via
the talosconfig and only adds volumes.

Machine configs are rendered on the fly from `talos/secrets.sops.yaml`, `talos/patches/*.yaml`
(`controlplane*` / `worker*` are role-specific, `*.sops.yaml` patches are decrypted) and
`talos/nodes/<node>.yaml`; they are never written to the repo.

Before `apply --insecure` sends anything, talkub asks the maintenance API whether Talos is already
on the node's disk (`systemdisk`). If so, Talos does not install the installer image and keeps
booting the image on disk, e.g. from a reused Hetzner snapshot. talkub then prints that image's
version, schematic and extensions. If they differ from the target, it warns and asks
`apply the config to <node> and keep the image on its disk?` (default no). Fix: rewrite the disk
with the boot media, or run `upgrade talos --force-unhealthy` afterwards; see
[getting started](docs/getting-started.md#2a-the-node-already-has-talos-on-its-disk).

Once the cluster is bootstrapped, `apply --mode auto|try` and `upgrade talos` are rolling: they
check `talosctl health` once before the first change (and change nothing if the cluster is
unhealthy; `--force-unhealthy` skips this check when the pending change is the fix, e.g. an
upgrade that removes an extension the node is stuck booting on), then wait for health after each
node before moving on. Health always runs through a
control plane (another one than the node just changed, if there is one). After `apply` talkub first
waits 30 seconds so the node's reboot has started (`TALKUB_APPLY_SETTLE_SECONDS` overrides this).
`upgrade talos` installs with `talosctl upgrade --no-reboot`, triggers the reboot with
`talosctl reboot --wait=false` and polls until the node runs the target version and schematic
(10 minutes; `reboot --wait` breaks when the node stops its Tailscale extension, which the
connection goes through). With more than one node it first drains the node itself with the repo
kubeconfig (`kubectl drain`, 5 minutes timeout) and uncordons it once the cluster is healthy again;
talosctl's own drain (part of its reboot) is not used because it goes through the cluster endpoint,
which `talkub tailscale lockdown` firewalls.
Before `talkub bootstrap`, apply nodes one at a time. If `apply` fails, the error lists the nodes
already applied in this run. The rolling behavior is only tested against a fake `talosctl`; verify
it on a real cluster before relying on it.

Version skew: repos written by this talkub version (`schematics` instead of `installImage` in
`.talkub.yaml`) cannot be read by older talkub versions — all admins should upgrade together.

## Bootstrap, upgrades and status

    talkub bootstrap                 # etcd, kubeconfig, Sealed Secrets, Argo CD, root app (re-runnable)
    talkub status                    # target vs. actual per node + Argo CD sync/health and app errors
    talkub upgrade talos             # nodes whose Talos version or schematic differs, one at a time
    talkub upgrade k8s               # shows the talosctl dry-run first

Chart versions live only in the Argo CD apps (`gitops/apps/<chart>.yaml`: `repoURL`, `chart`,
`targetRevision`, `helm.valueFiles`, `destination.namespace`); `bootstrap` and `repo apply` install
exactly what those files say. An old `charts` block in `.talkub.yaml` is ignored and dropped on the
next write.

## Versions and updates

    talkub versions                            # target vs. running vs. latest: Talos, Kubernetes, every chart app
    talkub versions --offline                  # skip the latest-version lookups (no network)
    talkub versions set talos v1.15.0          # then: talkub upgrade talos
    talkub versions set kubernetes 1.38.0      # then: talkub upgrade k8s
    talkub chart upgrade coredns                # bump one chart app to its latest version
    talkub chart upgrade coredns --version 1.2.3 --yes
    talkub chart upgrade --all                  # every chart app

The Argo CD app files (`gitops/apps/*.yaml`) are the single source of chart versions — `versions`
reads `targetRevision` from them and shows it next to what is actually running (via `kubectl get
application`) and the latest release upstream. RUNNING is `not deployed` when Argo CD has not
created the Application (often because the root app cannot sync — `talkub status` shows why),
`pending` when it exists but has not synced yet, and `?` when the cluster cannot be queried. `*`
marks a row whose latest differs from its target. `chart upgrade` writes the new `targetRevision`, shows a diff of the chart's default values
(`helm show values` old vs. new) and the chart's `appVersion`, and asks for confirmation unless
`--yes`; after it, commit and push `gitops/` so Argo CD picks it up. `versions set` only updates
the target in `.talkub.yaml` — run
`talkub upgrade talos`/`talkub upgrade k8s` afterwards to actually change the nodes.

    talkub renovate init          # writes renovate.json in the repo root
    talkub renovate init --force  # overwrite an existing one

Generates a Renovate config that opens PRs for chart bumps (via the `argocd` manager, matching
`gitops/apps/*.yaml`) and for Talos/Kubernetes bumps (via two custom regex managers, matching
`.talkub.yaml`). After writing it, commit `renovate.json` and enable the Renovate GitHub app for
this repo: <https://github.com/apps/renovate>. Argo CD rolls out a merged chart PR as soon as it
merges, so run `talkub lint` on the PR branch *before* merging it; a merged Talos/Kubernetes PR
only updates `.talkub.yaml` — run `talkub upgrade talos` or `talkub upgrade k8s` afterwards to
apply it to the nodes.

## Sealed Secrets

    talkub secret seal db -n app --from-literal password=… -o gitops/manifests/app/db.sealed.yaml
    talkub secret seal ~/secret.yaml -o gitops/manifests/app/db.sealed.yaml   # manifest outside the repo
    talkub secret backup-sealing-key # done by bootstrap; commit bootstrap/ afterwards

Sealing uses the controller's live certificate and falls back to `bootstrap/sealed-secrets.pem`
when the cluster is unreachable.

## Tailscale as the only admin path

    talkub init homelab … --tailscale --tailnet tail1234.ts.net     # new cluster: both steps at once
    # existing cluster:
    talkub tailscale enable --tailnet tail1234.ts.net   # auth key via prompt or --authkey-file
    talkub apply && talkub upgrade talos                # configures, then installs the extension (via LAN)
    talkub tailscale lockdown                           # firewall + tailnet names for talosctl/kubectl
    talkub tailscale lockdown --use-ips                 # …or tailnet IPs, for clients without MagicDNS
    talkub apply                                        # activates the firewall (via the tailnet)

The nodes keep their LAN/public cluster endpoint and do not use Tailscale DNS; only admin
clients go through the tailnet. Use a tagged, reusable, pre-approved auth key.
On a running cluster, `lockdown` first checks that every node answers via its tailnet
name, and refuses if one does not, showing talosctl's error. talosctl resolves names itself
and can miss MagicDNS names that `tailscale status` and the OS see (e.g. on macOS); if the
error is a name resolution failure, use `--use-ips`. `lockdown --node-cidr` must cover every node's IP.

Rollback: delete `talos/patches/firewall.yaml`, reset `mgmt` in `.talkub.yaml` to the
LAN addresses (or remove the entries), point the talosconfig endpoints/nodes and the
kubeconfig `server` back at the LAN with `talkub sops edit`, then run `talkub apply` via LAN.

## CoreDNS

    talkub coredns takeover              # moves CoreDNS from Talos to Argo CD (DNS keeps working)
    talkub coredns takeover --resume     # finish an interrupted takeover

Talos manages CoreDNS by default (the `kube-system/coredns` ConfigMap and Deployment, behind the
`kube-dns` Service). `takeover` stops Talos from managing it (`talos/patches/controlplane-coredns.yaml`,
`KubeCoreDNSConfig` `enabled: false`) and installs CoreDNS as a Helm release (release `coredns`,
Deployment `coredns-talkub`) that Argo CD manages instead. The `kube-dns` Service keeps its
ClusterIP and selector throughout, so DNS keeps working during the switch; its ports become the
chart's `udp-53`/`tcp-53`, and the metrics port 9153 is no longer exposed on `kube-dns`. The
takeover refuses to start while any node's Talos config differs from the repo (`talkub plan`),
so its Talos apply (mode `no-reboot`, confirmed per node unless `--yes`) carries only the CoreDNS
patch. New clusters get
CoreDNS this way from the start: `talkub init` writes the patch, Argo CD app and Helm values, and
`talkub bootstrap` installs the Helm release before Sealed Secrets and Argo CD.

If a takeover is interrupted partway, finish it with `talkub coredns takeover --resume` before
running `talkub apply` or `talkub upgrade` again: Talos health fails while both CoreDNS
deployments exist. `talkub tailscale operator enable` also
requires the takeover to be done first (it extends the CoreDNS values with a stub zone for the
tailnet).

## Tailscale operator

    talkub tailscale operator enable                      # OAuth client via prompt, --client-id, --client-secret-file
    talkub tailscale operator enable --rotate-credentials  # replace the OAuth client, keep everything else

Prerequisites: `talkub tailscale enable` (the tailnet is known) and `talkub coredns takeover`
(Argo CD manages CoreDNS, so its values can carry the operator's stub zone).

In the Tailscale admin console, create an OAuth client with scopes Devices Core, Auth Keys and
Services (write), tagged `tag:k8s-operator`, and add to the tailnet ACL:

    "tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}

`operator enable` seals the OAuth client into
`gitops/manifests/tailscale/operator-oauth.sealed.yaml` and writes the Helm release
(`tailscale-operator`), its manifests directory app and a `DNSConfig` that points the CoreDNS
stub zone for `<tailnet>` at the operator's nameserver. Run it again without
`--rotate-credentials` and it refuses ("already enabled"); with it, only the sealed secret is
replaced. The `tailscale` namespace runs with `pod-security.kubernetes.io/enforce: privileged`
because the operator's egress/ingress proxy pods need a privileged init container and NET_ADMIN
(only that namespace).

### Egress

    talkub tailscale egress add db --fqdn db.tail1234.ts.net
    talkub tailscale egress add db --ip 100.64.1.2 --namespace app   # namespace must already exist
    talkub tailscale egress remove db

Writes (or removes) `gitops/manifests/tailscale/egress/<name>.yaml`: an `ExternalName` Service
annotated `tailscale.com/tailnet-fqdn` (or `tailscale.com/tailnet-ip`), which the operator points
at the tailnet target. After commit, push and sync, pods reach it both by its MagicDNS name
(`db.tail1234.ts.net`, resolved via the CoreDNS stub zone) and via the cluster Service
(`db.tailscale.svc.cluster.local`). The operator rewrites the Services' `spec.externalName`, so
the `tailscale-operator-config` app ignores that field (`ignoreDifferences`) instead of reverting it.

If the cluster Service works but the MagicDNS name still returns NXDOMAIN after the egress proxy is
running, the operator's nameserver may have missed the update of its records (observed with
operator 1.102.4: it read an empty `dnsrecords` ConfigMap and did not reload). Check the records
with `talkub k -n tailscale get cm dnsrecords -o yaml`; if the name is listed, restart the
nameserver: `talkub k -n tailscale delete pod -l app=nameserver`.

### Ingress

    talkub tailscale expose grafana --service monitoring/grafana --port 80
    talkub tailscale expose grafana --service monitoring/grafana --port 80 --path /api
    talkub tailscale unexpose grafana

Writes (or removes) `gitops/manifests/tailscale/ingress/<name>.yaml`: an Ingress
(`ingressClassName: tailscale`) that publishes the Service at `https://<name>.<tailnet>`, either as
a `defaultBackend` or, with `--path`, a single path-based rule. `--service` is `<namespace>/<service>`
(a Service in another namespace than the Ingress's own is not supported by Kubernetes); `--port` is
a number (1-65535) or the Service's named port. The backend must speak HTTP — the Tailscale proxy
terminates TLS in front of it — and HTTPS certificates must be enabled for the tailnet in the admin
console. After writing the file, commit and push `gitops/`.

For an app installed via its own Helm chart (e.g. Argo CD itself), configure the ingress through the
chart's values instead of `talkub tailscale expose`. Observed working with the Argo CD chart:

    configs:
      params:
        server.insecure: true      # argocd-server would otherwise redirect HTTP to HTTPS and loop,
                                    # since the Tailscale proxy terminates TLS and talks HTTP to it
    server:
      ingress:
        enabled: true
        ingressClassName: tailscale
        hostname: argocd
        tls: true

See the operator's own docs for `tailscale.com/experimental-forward-cluster-traffic-via-ingress`
if cluster-internal clients also need to reach the Ingress by its MagicDNS name.

## Rotating the Talos CA

    talkub rotate-ca            # asks for confirmation, then rotates the Talos API CA
    talkub rotate-ca --yes      # no prompt

Do this when the Talos API CA or a client certificate may have been exposed. It runs
`talosctl rotate-ca`, then writes the new talosconfig and CA cert/key into the repo
(`talos/talosconfig.sops.yaml`, `certs.os` in the secrets bundle). The previous encrypted
talosconfig is kept as `talos/talosconfig.previous.sops.yaml` while a rotation is in
progress; its presence means `talkub rotate-ca --finish` is needed to complete it.

If a rotation is interrupted after `talosctl rotate-ca` has already run, `talkub rotate-ca
--finish` retries bringing the repo in line. If the new talosconfig itself could not be
stored, its plaintext is kept under a printed temp path; pass it explicitly with
`talkub rotate-ca --finish --talosconfig <path>`.

Before you start:

- Commit first. Git history then keeps the old talosconfig and secrets bundle.
- Every other talosconfig for this cluster stops working after the rotation, for example
  `~/.talos/config` or copies held by other admins. Hand out the new one from the repo.
- Do not interrupt the rotation. `talosctl rotate-ca` can take several minutes. On Ctrl-C,
  SIGTERM or a closed terminal, talkub keeps waiting for talosctl to finish (even if it can no
  longer print) and keeps its output before it stops. If talkub is killed hard (SIGKILL), talosctl
  keeps running on its own.
- If the rotation is interrupted or fails, the printed temp directory holds `rotate-ca.out`
  (talosctl's full output, including the new keys) and, if talosctl got that far, the new
  talosconfig; after a successful run it is deleted.

Commit `talos/` afterwards.

## Lint and pre-commit hook

    talkub lint                 # yamllint, kubeconform, helm template — style, syntax, valid manifests
    talkub lint --offline       # same, but skip checks that need network access

`--offline` is not a guarantee of zero network: kubeconform may still fetch a schema that is not
yet cached, and a schema download that fails while offline just counts as skipped, not an error.

Three checks, in order, each key-free:

- yamllint (`.yamllint` if present, else a relaxed default managed by talkub) over every
  `*.yaml`/`*.yml` in the repo.
- `gitops/apps/**` and `gitops/manifests/**` manifests validated with `kubeconform` (a `skipped:`
  line if `kubeconform` is not installed; `--offline` skips entirely if the local schema cache is
  still empty).
- every Argo CD Application's Helm chart rendered with `helm template` and validated the same way
  (skipped entirely with `--offline`, or a `skipped:` line if `helm` is not installed).

    talkub hooks install            # install .git/hooks/pre-commit
    talkub hooks install --force    # overwrite a pre-commit hook not managed by talkub
    talkub hooks uninstall          # remove it again

The hook runs `talkub check && talkub lint --offline` before every commit; `talkub init` installs
it automatically. Bypass it once with `git commit --no-verify`.

## Admin changes

    talkub admin remove alice && talkub keys rotate

## Development

    uv run pytest
    uv run ruff check src tests
    uv run lint-imports

Enable the pre-commit hook (import layers, ruff check, ruff format) once per clone:

    git config core.hooksPath .githooks

### Code layout

`src/talkub/` is split by domain. `cli/` holds one thin module per command group and calls into
the packages below it. Each package's `__init__.py` says what it covers.

| Package | What lives there |
|---|---|
| `cli/` | Typer commands, argument parsing, prompts, console output |
| `scaffold.py` | `talkub init`: writes a new cluster repo from `templates/` |
| `overview/` | `status`, `versions`: read-only views across the cluster |
| `gitops/` | Flows that change what Argo CD deploys: bootstrap, chart upgrade, git repo, CoreDNS, Tailscale operator, Renovate |
| `workstation/` | The admin's machine: credential shells, shortcuts, clipboard |
| `quality/` | `lint`, `check`, pre-commit hook |
| `helm/` | Chart catalog and `helm` command lines |
| `talos/` | Machine configs, node lifecycle, upgrades, CA rotation, installer images, Tailscale on the hosts |
| `argocd/` | Rendering, parsing and editing `gitops/apps/*.yaml` |
| `kube/` | kubectl/helm sessions, kubeseal |
| `context.py` | `Context`: repo + runner + keys, passed to every flow |
| `secrets/` | age identities, recipients, master key, SOPS |
| `config/` | The repo on disk and `.talkub.yaml` (`Repo`, `Settings`, specs) |
| `core/` | Errors, running tools, YAML, atomic writes, temp files |

The table is ordered top to bottom by layer: a package may import the ones below it, never the
ones above. `uv run lint-imports` enforces this (contract in `pyproject.toml`).

Adding a command: put the logic into the package that owns the domain, add a thin module in
`cli/`, register it in `cli/main.py`, and put the tests into the matching folder under `tests/`.
