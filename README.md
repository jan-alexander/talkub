# talkub 🛰️

**Your whole Talos Kubernetes cluster in one git repo, and one CLI that runs it safely.**

talkub is an admin CLI for [Talos Linux](https://www.talos.dev) clusters that are run the GitOps
way. One repo holds everything: machine configs, secrets, and every app Argo CD deploys. talkub
takes care of the tedious and dangerous parts: key handling, rendering configs, bootstrapping,
upgrades and access control. That leaves you with `git push` and a handful of commands you can
remember.

```bash
talkub init homelab --endpoint https://10.0.0.10:6443 --node cp-01=10.0.0.10 --git-url git@github.com:me/homelab.git
talkub apply --insecure cp-01     # install Talos on the node
talkub bootstrap                  # etcd, kubeconfig, CoreDNS, Sealed Secrets, Argo CD, the root app
git push                          # from now on, Argo CD deploys whatever is in gitops/
```

## 🤯 The problem

Talos is a great OS for Kubernetes: immutable, minimal, API-driven. A cluster built on it still
leaves you with a lot of loose ends:

- 🔑 **Secrets everywhere.** The secrets bundle, `talosconfig` and `kubeconfig` are keys to the
  kingdom. They end up in `~/Downloads`, in shell history, or in git by accident.
- 🧩 **Glue scripts.** `talosctl gen config`, patches per node, image factory schematics, Helm
  installs before Argo CD exists: every cluster grows its own fragile `Makefile`.
- 👥 **Sharing access.** A second admin means passing a file around. Revoking one means hoping
  they deleted it.
- 🎢 **Scary upgrades.** Which node runs which Talos version, schematic and config? What happens
  if the second node does not come back?
- 🌍 **Open ports.** The Talos and Kubernetes APIs listen on the internet, or you build a VPN setup
  by hand.

## ✨ What talkub gives you

|                                                       |                                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 📦 **One repo, one source of truth**                  | Talos configs, versions, schematics, Argo CD apps and chart versions all live in the repo. `talkub init` scaffolds it; nothing important lives on a laptop.                                                                                                                                                              |
| 🔐 **Secrets that never touch the disk in plaintext** | SOPS + age for infrastructure secrets, Sealed Secrets for app secrets. Keys on a **YubiKey** or in the **Secure Enclave**. Decrypted configs exist only in private temp dirs for the duration of one command.                                                                                                            |
| 👥 **Team access without passing files around**       | `talkub admin add` / `admin remove` plus `keys rotate`. Nobody ever handles the cluster master key directly.                                                                                                                                                                                                             |
| 🧠 **Configs rendered, never stored**                 | Machine configs are built on the fly from the secrets bundle, shared patches and per-node files. `talkub plan` shows what would change as a coloured diff with secrets redacted (word-level highlighting through [delta](https://github.com/dandavison/delta) if you have it); `talkub apply` rolls it out node by node. |
| 🚀 **Bootstrap in one command**                       | etcd, kubeconfig, CoreDNS, Sealed Secrets (including a key backup) and Argo CD with an app of apps. Safe to re-run.                                                                                                                                                                                                      |
| 🔄 **Upgrades with guard rails**                      | `talkub versions` compares target, running and latest for Talos, Kubernetes and every chart. Upgrades run one node at a time with drain and health checks. `talkub renovate init` sets up update PRs.                                                                                                                    |
| 🛡️ **Tailscale lockdown**                             | Two steps, and the Talos and Kubernetes APIs are reachable only from your tailnet. The Tailscale operator puts apps at `https://app.your-tailnet.ts.net`.                                                                                                                                                                |
| 💽 **Disk assistant**                                 | `talkub node volumes` reads the node's disks, suggests a portable selector, simulates where Talos places each volume and writes the config.                                                                                                                                                                              |
| ⚡ **Shortcuts for daily work**                       | `talkub run db-password` copies a secret to the clipboard and clears it after 45 s. Fresh tokens, port forwards, CronJob runs and `k9s` with the right kubeconfig work the same way.                                                                                                                                     |
| ✅ **Mistakes caught before the commit**              | A pre-commit hook blocks unencrypted secrets, plain Kubernetes Secrets and invalid manifests (`yamllint`, `kubeconform`, `helm template`).                                                                                                                                                                               |
| 🧯 **A plan for bad days**                            | `rotate-ca` for a leaked Talos CA, `keys rotate` when an admin leaves, and guides for rebuilding a node or the whole cluster from the repo.                                                                                                                                                                              |

## 🎯 Is talkub for you?

**Yes, if you …**

- ✅ run Talos on bare metal, in a homelab or on **Hetzner Cloud**, with one to a handful of nodes
- ✅ want GitOps with Argo CD, without writing the plumbing yourself
- ✅ share admin access with a few people and want hardware-backed keys
- ✅ would rather type `talkub upgrade talos` than read the Talos upgrade docs every time

**Probably not, if you …**

- ❌ use managed Kubernetes (EKS, GKE, AKS) or a distribution other than Talos
- ❌ need to provision the machines themselves (talkub configures nodes, it does not create them)
- ❌ run a fleet of clusters (have a look at Sidero Omni) or several clusters from one GitOps repo

## 🗺️ How it works

```
homelab/                         ← your cluster repo (talkub lives outside it)
├── .talkub.yaml                 ← nodes, Talos/Kubernetes versions, schematics
├── keys/                        ← master key, encrypted to every admin's age key
├── talos/
│   ├── secrets.sops.yaml        ← Talos secrets bundle (SOPS)
│   ├── talosconfig.sops.yaml
│   ├── patches/                 ← shared machine config patches
│   └── nodes/<node>.yaml        ← per-node network, disks, volumes
├── kubernetes/kubeconfig.sops.yaml
├── bootstrap/                   ← Sealed Secrets key backup, repo credentials (SOPS)
└── gitops/
    ├── root.yaml                ← Argo CD app of apps
    ├── apps/*.yaml              ← one Argo CD Application per app; chart versions live here
    ├── values/*.yaml            ← Helm values
    └── manifests/               ← plain manifests and SealedSecrets
```

Your YubiKey (or another age identity) unlocks the master key, and the master key unlocks the SOPS
files. talkub hands the decrypted `talosconfig`/`kubeconfig` to `talosctl`, `kubectl` or `helm`
for a single call and deletes them afterwards. Argo CD pulls `gitops/` from your git remote and
keeps the cluster in sync.

## ⏱️ A tour in 60 seconds

```bash
talkub status                          # every node: Talos version, schematic, config drift, kubelet; every Argo CD app
talkub k get pods -A                   # kubectl with the decrypted kubeconfig, just for this call
talkub t -n 10.0.0.10 dashboard        # talosctl, same idea
talkub shell                           # a subshell with KUBECONFIG/TALOSCONFIG set, removed on exit

talkub plan && talkub apply            # change a patch, see the coloured diff (secrets redacted), roll it out node by node
talkub versions                        # target vs. running vs. latest: Talos, Kubernetes, every chart
talkub versions --outdated             # only what has an update
talkub chart upgrade podinfo           # bump a chart and diff its default values
talkub secret seal db -n app --prompt password -o gitops/manifests/app/db.sealed.yaml

talkub run db-password                 # a shortcut: secret to the clipboard, cleared after 45 s
talkub tailscale expose grafana --service monitoring/grafana --port 80   # https://grafana.<tailnet>
```

## 📥 Install

With [uv](https://docs.astral.sh/uv/), from the latest GitHub release (a built wheel, no Python
project setup needed):

```bash
gh release download --repo jan-alexander/talkub --pattern '*.whl' --dir /tmp/talkub
uv tool install --compile-bytecode /tmp/talkub/talkub-*.whl
```

Or straight from source: `uv tool install git+https://github.com/jan-alexander/talkub.git`. More
options and shell completion: [reference](docs/reference.md#install-and-shell-completion).

talkub drives the standard tools: [`age`](https://github.com/FiloSottile/age),
[`sops`](https://github.com/getsops/sops), [`talosctl`](https://github.com/siderolabs/talos) (v1.14+),
[`helm`](https://helm.sh), [`kubectl`](https://kubernetes.io/docs/reference/kubectl/),
[`kubeseal`](https://github.com/bitnami-labs/sealed-secrets), and optionally
[`age-plugin-yubikey`](https://github.com/str4d/age-plugin-yubikey) /
[`age-plugin-se`](https://github.com/remko/age-plugin-se),
[`kubeconform`](https://github.com/yannh/kubeconform) and
[`delta`](https://github.com/dandavison/delta) (colours the `plan`/`apply` diffs; red/green built in
without it).

## 🚀 Quick start

```bash
talkub identity new --yubikey          # your personal key (or --se / --software)
talkub init homelab --endpoint https://10.0.0.10:6443 \
    --node cp-01=10.0.0.10 --git-url git@github.com:me/homelab.git
cd homelab
talkub admin add age1… --name backup   # a second key, kept offline: there is no passphrase recovery
git add -A && git commit -m "init cluster repo" && git push -u origin main
```

Then boot the node into maintenance mode and follow [getting started](docs/getting-started.md),
from `talkub apply --insecure` to your first app.

## 📚 Guides

| Guide                                                   | For                                                                     |
| ------------------------------------------------------- | ----------------------------------------------------------------------- |
| 🏁 [Getting started](docs/getting-started.md)           | from a node in maintenance mode to a running cluster                    |
| 📦 [Apps](docs/apps.md)                                 | Helm charts and manifests with Argo CD: add, update, reach, remove      |
| 🔐 [Handling secrets](docs/secrets.md)                  | admins, SOPS files, Sealed Secrets, rotation, leaks                     |
| 🖥️ [Nodes](docs/nodes.md)                               | add workers and control planes, replace, re-address, remove nodes       |
| 🛡️ [Tailscale](docs/tailscale.md)                       | the tailnet as the only admin path; the operator for ingress and egress |
| 🔄 [Upgrading](docs/upgrading.md)                       | Talos, Kubernetes and chart upgrades                                    |
| 🧯 [Backup and recovery](docs/backup-and-recovery.md)   | etcd snapshots, app data, rebuilding a node or the cluster              |
| 🔍 [Debugging Talos](docs/debugging-talos.md)           | nodes, boot, disks, etcd                                                |
| 🔍 [Debugging Kubernetes](docs/debugging-kubernetes.md) | pods, Argo CD, Sealed Secrets, storage, DNS                             |
| 📖 [Command reference](docs/reference.md)               | every command in detail                                                 |

## 🧰 Commands at a glance

| Area               | Commands                                                                                      |
| ------------------ | --------------------------------------------------------------------------------------------- |
| Cluster access     | `t`/`talosctl`, `k`/`kubectl`, `shell`, `run` (shortcuts), `kubeconfig`                       |
| Repo and nodes     | `init`, `node add`, `node volumes`, `image`, `plan`, `apply`, `bootstrap`, `status`           |
| Versions           | `versions`, `upgrade talos`, `upgrade k8s`, `chart upgrade`, `renovate init`                  |
| Secrets and access | `identity`, `admin`, `keys rotate`, `sops edit`/`decrypt`, `secret seal`, `rotate-ca`, `repo` |
| Networking         | `tailscale enable`/`lockdown`/`operator`/`expose`/`egress`, `coredns takeover`                |
| Quality            | `check`, `lint`, `hooks install`                                                              |

Details for each: [command reference](docs/reference.md).

## 🧪 Status

talkub is young (0.1.x) and runs a real homelab cluster on Hetzner Cloud. Some paths, like
rolling upgrades across several nodes, are so far tested against a fake `talosctl` only: take an
etcd snapshot before big changes (see [backup and recovery](docs/backup-and-recovery.md)).

## 🛠️ Development

```bash
uv run pytest && uv run ruff check src tests && uv run lint-imports
```

Code layout, layer rules and releases: [development](docs/development.md).
