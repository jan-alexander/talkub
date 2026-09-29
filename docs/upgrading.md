# Upgrading Talos, Kubernetes and charts

This guide covers the three kinds of upgrades a talkub cluster sees: the Talos OS on the nodes,
the Kubernetes control plane and kubelets, and the Helm charts Argo CD deploys. The examples use
cluster `homelab` with nodes `cp-01` and `w-01`.

Where each target version lives:

| What       | Source of truth                                 | Changed by                                    | Rolled out by             |
| ---------- | ----------------------------------------------- | --------------------------------------------- | ------------------------- |
| Talos      | `talosVersion` + `schematics` in `.talkub.yaml` | `talkub versions set talos`, `talkub image …` | `talkub upgrade talos`    |
| Kubernetes | `kubernetesVersion` in `.talkub.yaml`           | `talkub versions set kubernetes`              | `talkub upgrade k8s`      |
| Charts     | `targetRevision` in `gitops/apps/<app>.yaml`    | `talkub chart upgrade`                        | Argo CD, after `git push` |

Changing a target only edits the repo. Nothing on the cluster changes until the matching
`upgrade` command runs (Talos, Kubernetes) or the change is pushed (charts).

## 0. Before you start

```bash
talkub status                            # every node healthy, every Argo CD app Synced/Healthy
talkub versions                          # target vs. running vs. latest; `*` marks available updates
git pull                                 # work on the latest repo state
```

- Read the upstream release notes for every version you skip over: Talos
  (<https://github.com/siderolabs/talos/releases>), Kubernetes (the changelog's _Urgent Upgrade
  Notes_ and removed APIs), and each chart's changelog.
- Update your local `talosctl` to the target Talos version (or newer). `kubectl` should be within
  one minor version of the target Kubernetes version.
- Take an etcd snapshot from a control plane:

  ```bash
  talkub talosctl -n 10.0.0.10 etcd snapshot homelab-etcd.snapshot   # keep it outside the repo
  ```

  `-n` takes cp-01's address (`mgmt`, else `ip` in `.talkub.yaml`), not its name: talosctl does
  not know the node names.

- One kind of upgrade at a time. The recommended order is **Talos → Kubernetes → charts**: a new
  Talos release supports newer Kubernetes versions, and charts sometimes need a newer Kubernetes.
  If a chart upgrade is required _before_ a Kubernetes upgrade (e.g. it drops an API the new
  Kubernetes removes), do that chart first.

## 1. Upgrade Talos

Talos supports upgrades from the previous minor release only. To go from v1.13 to v1.15, upgrade to
the latest v1.14 patch release first, then to v1.15. Patch releases within a minor can be skipped.

```bash
talkub versions set talos v1.15.0        # only edits .talkub.yaml
talkub upgrade talos                     # asks per node; --yes to skip
git add .talkub.yaml && git commit -m "chore: talos v1.15.0" && git push
```

What `upgrade talos` does:

1. Resolves the schematic ID for each platform at the Image Factory and saves it in `.talkub.yaml`
   if it changed.
2. Goes through the nodes **control planes first**, one at a time. A node already running the
   target version _and_ schematic is reported as `up to date` and skipped.
3. Before the first change it checks `talosctl health` once; on an unhealthy cluster it changes
   nothing.
4. Per node (clusters with more than one node): `kubectl drain`, `talosctl upgrade --no-reboot`,
   `talosctl reboot`, then it polls up to 10 minutes until the node runs the target version and
   schematic, waits for cluster health, and uncordons the node.

On a single-node cluster the node is not drained (the pods have nowhere to go), so expect a short
outage of everything running on it.

Only some nodes: `talkub upgrade talos cp-01`. The same command also rolls out schematic changes
without a version bump, e.g. after `talkub image add siderolabs/intel-ucode` or `talkub tailscale
enable`.

If it fails midway, the error names the nodes already upgraded and those not touched yet; a
drained node stays cordoned until you run `talkub kubectl uncordon <node>`. Fix the cause and run
`talkub upgrade talos` again; upgraded nodes are skipped.

`--force-unhealthy` skips the health check. Use it only when the upgrade itself is the fix, e.g.
a node stuck booting on an extension that the new schematic removes.

## 2. Upgrade Kubernetes

Kubernetes must be upgraded one minor version at a time (1.36 → 1.37 → 1.38), and the target must
be supported by the Talos version the nodes run (see the support matrix in the Talos docs). Upgrade
Talos first if it is not.

```bash
talkub versions set kubernetes 1.38.0    # only edits .talkub.yaml
talkub lint                              # validates all manifests and charts against 1.38.0
talkub upgrade k8s                       # dry-run first, then asks; --yes to skip
git add .talkub.yaml && git commit -m "chore: kubernetes 1.38.0" && git push
```

Run `talkub lint` right after `versions set`: kubeconform validates against the Kubernetes version
in `.talkub.yaml`, so it flags manifests and chart templates that still use APIs the target
version removed. Upgrade those charts (step 3) before running `upgrade k8s`.

`upgrade k8s` runs `talosctl upgrade-k8s --dry-run` against the first control plane and prints
its output (this takes a while). Check it for deprecated resources, then confirm. talosctl then
upgrades the API server, controller manager, scheduler, kube-proxy and the kubelet on every node
in turn. After `talkub tailscale lockdown`, talkub points talosctl at the Kubernetes API via the
tailnet automatically.

Afterwards `talkub versions` should show the new version as running, and `talkub status` all nodes
`Ready`.

## 3. Upgrade charts

```bash
talkub versions                          # `*` marks chart apps with a newer release
talkub chart upgrade podinfo             # to the latest release
talkub chart upgrade podinfo --version 6.8.0
talkub chart upgrade --all               # every chart app, one after another
```

For each app, `chart upgrade`:

1. Resolves the target (default: the latest release; it never steps back if the app is pinned to
   something newer).
2. Prints `chart: old → new`, the chart's `appVersion` change and a diff of the chart's default
   values (`helm show values`, old vs. new). Compare it with `gitops/values/<app>.yaml`: renamed or
   removed keys are the usual cause of a broken upgrade.
3. Asks for confirmation (`--yes` skips it), writes the new `targetRevision` and lints the app
   (`helm template` + kubeconform). A lint error leaves the change in place so you can fix the
   values or revert it.

Then roll it out:

```bash
talkub lint
git add gitops && git commit -m "chore(podinfo): chart 6.8.0" && git push
talkub status                            # watch the app go back to Synced/Healthy
```

Argo CD rolls out the new chart after the push, including its own chart (`argocd`), which it
upgrades itself. There is no separate talkub step for that.

Notes:

- Read the chart's changelog for major version bumps; the values diff does not show changed
  behavior or required migration steps (CRD updates, one-off jobs).
- `--all` does not stop at the first failure: a failing app is reported and the next one continues. The summary at
  the end lists how many succeeded.
- Apps with more than one chart source are skipped by `--all`; edit their `targetRevision` by
  hand.
- Rolling back is a `git revert` of the commit and a push.

## 4. Automate with Renovate

```bash
talkub renovate init
git add renovate.json && git commit -m "chore: renovate" && git push
```

Enable the Renovate GitHub app for the repo (<https://github.com/apps/renovate>). It opens PRs for
chart bumps and for Talos/Kubernetes bumps in `.talkub.yaml`.

- **Chart PRs**: Argo CD rolls them out as soon as they merge. Check out the PR branch, run
  `talkub lint` and read the values diff (`talkub chart upgrade <app> --version <new>` on a clean
  checkout shows it) _before_ merging.
- **Talos/Kubernetes PRs**: merging only changes the target. Pull, then run `talkub upgrade talos`
  or `talkub upgrade k8s` as above. Watch the minor-version rules: Renovate may suggest skipping a
  minor release.

## Troubleshooting

| Symptom                                           | Cause / fix                                                                                                                                                    |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `the cluster is not healthy; nothing was changed` | Fix the cluster first (`talkub status`), or `--force-unhealthy` if the upgrade is the fix                                                                      |
| `drain failed for node …`                         | A PodDisruptionBudget blocks the eviction (5 minutes timeout); scale the workload or relax the PDB, then retry. Uncordon with `talkub kubectl uncordon <node>` |
| `node … did not come back on … within 600s`       | Look at the boot log: `talkub talosctl -n <node address> dmesg`. The node may be booting the old image after a failed install                                  |
| `upgrade-k8s dry-run failed`                      | Usually a Kubernetes version the running Talos does not support, or skipping a minor version                                                                   |
| Chart app `OutOfSync`/`Degraded` after a push     | `talkub status` shows the Argo CD error; fix the values or `git revert`                                                                                        |
