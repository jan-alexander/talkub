# Getting started: from a node in maintenance mode to a running cluster

This guide starts with no cluster repo at all and ends with a Talos node running Argo CD, your own
charts and Tailscale as the only admin path. The examples use cluster `homelab`, node `cp-01` and
LAN IP `10.0.0.10`; the node has booted the Talos ISO and waits in maintenance mode.

## 0. Prerequisites

- talkub plus `age`, `sops`, `talosctl`, `helm`, `kubectl`, `kubeseal` (optional:
  `age-plugin-yubikey`, `age-plugin-se`).
- An **empty** git repo at your git host (e.g. GitHub); Argo CD pulls from it later.
- The node's IP (shown on its console). A DHCP reservation keeps it stable across the install.

## 1. Create the cluster repo

### 1a. Your talkub identity (only if you have none yet)

    ls ~/.config/talkub/identities/          # empty or missing → create one
    talkub identity new --yubikey            # or --se (Secure Enclave) / --software

It prints your public key (`age1…`). Also create an offline backup key for the repo; there is no
passphrase recovery:

    age-keygen -o backup-homelab.txt         # keep offline, never in the repo

Joining an existing repo instead? Send your public key to an admin, who runs
`talkub admin add age1… --name <you>` and commits and pushes.

### 1b. Scaffold the repo

    talkub init homelab \
        --endpoint https://10.0.0.10:6443 \
        --node cp-01=10.0.0.10 \
        --git-url git@github.com:me/homelab.git
    cd homelab
    talkub admin add age1… --name backup     # public key from backup-homelab.txt

`init` creates the master key, the SOPS-encrypted Talos secrets and talosconfig, `gitops/` (root
app, Argo CD, Sealed Secrets, CoreDNS), runs `git init`, sets `origin` and installs the pre-commit
hook. For a static address add `--static 10.0.0.10/24 --gateway 10.0.0.1 --mac …`; for Hetzner
Cloud `--platform hcloud`.

### 1c. Argo CD access to a private repo

    talkub repo credentials                  # SSH URL: creates a deploy key and prints its public key

Add the public key as a **read-only deploy key** at your git host. With an HTTPS URL it asks for a
user name and token instead.

## 2. Bring up the node

    talkub image add siderolabs/intel-ucode  # optional: extensions BEFORE the first install
    talkub node volumes cp-01 --insecure     # install disk, EPHEMERAL limit, user volumes
    talkub check && git add -A && git commit -m "init cluster repo" && git push -u origin main
    talkub apply --insecure cp-01            # first install via the maintenance API
    talkub bootstrap                         # etcd, kubeconfig, CoreDNS, Sealed Secrets, Argo CD, root app
    git add -A && git commit -m "bootstrap" && git push   # keeps kubernetes/ and bootstrap/

- `node volumes` reads the disks from the node and writes `talos/nodes/cp-01.yaml`.
- Push before `bootstrap`: the root app pulls `gitops/apps` from the remote, not from your
  working tree.
- `bootstrap` can be re-run after a failure; finished steps are skipped.

Check the result:

    talkub status                            # target vs. actual per node, Argo CD sync/health
    talkub k get nodes

More nodes: `talkub node add w-01 --ip … --role worker`, then `talkub node volumes w-01 --insecure`
and `talkub apply --insecure w-01` — one node at a time.

## 3. Install charts

There is no `chart add`: a chart is an Argo CD app file in `gitops/apps/`, which the root app picks
up automatically. Two files:

`gitops/apps/podinfo.yaml` (same pattern as the apps `init` writes):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: podinfo
  namespace: argocd
spec:
  project: default
  sources:
    - repoURL: https://stefanprodan.github.io/podinfo
      chart: podinfo
      targetRevision: "6.7.1"               # the only place the chart version lives; quoted
      helm:
        valueFiles:
          - $values/gitops/values/podinfo.yaml
    - repoURL: git@github.com:me/homelab.git   # same URL as --git-url
      targetRevision: HEAD
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: podinfo
  syncPolicy:
    automated: {prune: true, selfHeal: true}
    syncOptions: [CreateNamespace=true, ServerSideApply=true]
```

`gitops/values/podinfo.yaml`: the Helm values.

Secrets are only committed sealed:

    talkub secret seal podinfo-db -n podinfo --prompt password -o gitops/manifests/podinfo/db.sealed.yaml

Manifests under `gitops/manifests/<app>/` need their own app with
`source.path: gitops/manifests/<app>` and `directory: {recurse: true}`.

Roll out:

    talkub lint                              # yamllint, kubeconform, helm template
    git add gitops && git commit -m "feat: podinfo" && git push
    talkub versions                          # target / running / latest per chart

Argo CD syncs on its own after the push. Later updates: `talkub chart upgrade podinfo` (shows a
values diff), then commit and push; `talkub renovate init` automates update PRs.

## 4. Tailscale as the only admin path

Goal: the Talos API (50000) and the Kubernetes API (6443) are reachable only via the tailnet.

In the Tailscale admin console: create a tagged, reusable, pre-approved auth key and disable key
expiry for the nodes. Your admin machine must be in the tailnet.

### 4a. Lock down the admin APIs

    talkub tailscale enable --tailnet tail1234.ts.net   # extension + auth key; addresses stay on the LAN
    talkub apply && talkub upgrade talos                # configure, then install the extension (via LAN)
    tailscale status                                    # wait until every node is listed
    talkub tailscale lockdown                           # firewall patch, talosctl/kubectl via tailnet names
    talkub apply                                        # activate the firewall (now via the tailnet)
    git add -A && git commit -m "feat: tailscale lockdown" && git push

- No MagicDNS on your client: `lockdown --use-ips`.
- `lockdown` refuses if a node does not answer via the tailnet.
- The nodes keep their LAN endpoint; only admin clients go through the tailnet.
- For a new cluster, `talkub init … --tailscale --tailnet tail1234.ts.net` does 4a right away.
- Rollback: see [Tailscale as the only admin path](../README.md#tailscale-as-the-only-admin-path).

### 4b. Apps in the tailnet (optional, Tailscale operator)

In the admin console, create an OAuth client (scopes Devices Core, Auth Keys, Services write; tag
`tag:k8s-operator`) and add to the ACL:

    "tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}

Then:

    talkub tailscale operator enable                    # seals the OAuth client, writes the Helm app
    talkub tailscale expose grafana --service monitoring/grafana --port 80   # https://grafana.tail1234.ts.net
    talkub tailscale egress add db --fqdn db.tail1234.ts.net                 # pods reach tailnet hosts
    git add gitops && git commit -m "feat: tailscale operator" && git push

The operator needs the CoreDNS takeover (new repos have it; older ones run `talkub coredns takeover`).
`expose` needs HTTPS certificates enabled for the tailnet. Details: [Tailscale
operator](../README.md#tailscale-operator).
