# Tailscale

This guide covers both ways talkub uses Tailscale: as the **only admin path** to the Talos and
Kubernetes APIs (a Talos extension on every node plus a firewall), and as the **Tailscale
operator** in the cluster, which puts apps into the tailnet and lets pods reach tailnet hosts. The
examples use cluster `homelab`, tailnet `tail1234.ts.net`, nodes `cp-01` (`10.0.0.10`) and `w-01`.

| Part              | Gives you                                                          | Commands                                        |
| ----------------- | ------------------------------------------------------------------ | ----------------------------------------------- |
| Admin path        | 50000 (Talos) and 6443 (Kubernetes) reachable only via the tailnet | `tailscale enable`, `tailscale lockdown`        |
| Operator: ingress | `https://<app>.tail1234.ts.net` for a Service                      | `tailscale operator enable`, `tailscale expose` |
| Operator: egress  | pods reach tailnet hosts by MagicDNS name                          | `tailscale egress add`                          |

The operator needs the admin path's `tailscale enable` (it takes the tailnet from there), not the
lockdown.

## 0. In the Tailscale admin console

- **Your admin machine** is in the tailnet.
- **An auth key for the nodes**: tagged (e.g. `tag:talos`), reusable, pre-approved. Disable key
  expiry for the node devices once they have joined, or they drop out of the tailnet after 180 days.
- **For the operator**: an OAuth client with the scopes Devices Core, Auth Keys and Services
  (write), tag `tag:k8s-operator`, and in the ACL:

  ```json
  "tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}
  ```

- **For `expose`**: MagicDNS and HTTPS certificates enabled (DNS page).

## 1. The admin path

### 1a. A new cluster

```bash
talkub init homelab … --tailscale --tailnet tail1234.ts.net   # asks for the auth key (or --tailscale-authkey-file)
```

`init` then does both steps below in the repo right away; the nodes install with the extension and
come up locked down.

### 1b. An existing cluster

```bash
talkub tailscale enable --tailnet tail1234.ts.net   # auth key via hidden prompt or --authkey-file
talkub apply                                        # configures the extension (via LAN)
talkub upgrade talos                                # installs it: new schematic with siderolabs/tailscale
tailscale status                                    # wait until every node is listed
talkub tailscale lockdown                           # firewall patch, clients switch to tailnet names
talkub apply                                        # activates the firewall (now via the tailnet)
git add -A && git commit -m "feat: tailscale lockdown" && git push
```

What the two steps write:

- **`enable`**: `talos/patches/tailscale.sops.yaml` (the auth key, SOPS-encrypted), the extension in
  every schematic in use, and `tailscale` in `.talkub.yaml`. Addresses stay on the LAN, so the
  cluster can still be configured and upgraded as before.
- **`lockdown`**: sets `mgmt` of every node to `<node>.tail1234.ts.net` (or, with `--use-ips`, to
  its tailnet IP), points the talosconfig endpoints and the kubeconfig `server` at the tailnet, and
  writes `talos/patches/firewall.yaml`. On a bootstrapped cluster it first checks that every node
  answers via the tailnet and refuses otherwise, showing talosctl's error.

### 1c. What the firewall allows

`talos/patches/firewall.yaml` blocks all incoming traffic by default, then allows:

| Ports                                    | From                                                   | For                            |
| ---------------------------------------- | ------------------------------------------------------ | ------------------------------ |
| 50000, 6443/tcp                          | the tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`)   | admins: talosctl, kubectl      |
| 50000, 50001, 6443, 2379-2380, 10250/tcp | each node's `ip` (`/32`), pod and service networks     | cluster traffic: etcd, kubelet |
| 4789/udp                                 | the nodes' own addresses                               | Flannel VXLAN                  |
| 80, 443/tcp                              | anywhere, only on nodes added with `--public-ingress`  | a public ingress controller    |

The nodes keep their LAN/public addresses for the cluster itself and do not use Tailscale DNS
(`TS_ACCEPT_DNS=false`); only admin clients go through the tailnet. Everything else, NodePorts
included, is blocked from outside. The rules name each node's `ip`, so a node needs a fixed address
(DHCP reservation or `--static`) and `ip` must be an IP, not a name (`mgmt` may be a name).
`talkub node add` rewrites `talos/patches/firewall.yaml` with the new node; after changing a node's
`ip` run `talkub tailscale firewall`. Both need `talkub apply` afterwards. The file is generated:
put extra rules into `talos/nodes/<node>.yaml` (like `--public-ingress`) or another patch, edits in
`firewall.yaml` itself are overwritten.

Repos written by talkub 0.1.7 or older allowed a whole network instead (`tailscale.nodeCidr`,
usually the `/24` around the first node, which on a hosted server is shared with strangers). The key
is ignored now; run `talkub tailscale firewall` and `talkub apply` once to narrow the rules.

### 1d. Clients without MagicDNS

talosctl resolves names itself and can miss MagicDNS names that `tailscale status` and the OS
resolve (seen on macOS with the open-source `tailscaled`). If `lockdown` fails with a name
resolution error, run `talkub tailscale lockdown --use-ips`: `mgmt` becomes each node's tailnet IP.
The IPs stay the same as long as the device is not deleted from the tailnet.

### 1e. After the lockdown

- **New nodes**: `talkub node add` sets `mgmt` to `<node>.tail1234.ts.net` itself; with
  `--use-ips`, set it to the node's tailnet IP after the first install. The first install
  (`apply --insecure`) still uses `--ip`: maintenance mode has no firewall. See [nodes](nodes.md).
- **A node's `ip` changed**: update it in `.talkub.yaml`, then `talkub tailscale firewall` and
  `talkub apply`.
- **A new auth key** (expired or revoked): `talkub sops edit talos/patches/tailscale.sops.yaml`,
  then `talkub apply`. Nodes already in the tailnet keep their device; the key matters for new and
  reinstalled nodes.
- **A reinstalled node**: delete its old device in the admin console first, or it joins as
  `<node>-1` with a new IP ([backup and recovery](backup-and-recovery.md#2-before-you-recover)).

### 1f. Rollback

Back to LAN access, e.g. when the tailnet is not an option any more:

1. Delete `talos/patches/firewall.yaml`.
2. In `.talkub.yaml`, set `mgmt` back to the LAN addresses (or remove the entries).
3. Point the talosconfig endpoints/nodes and the kubeconfig `server` back at the LAN:
   `talkub sops edit talos/talosconfig.sops.yaml` and `talkub sops edit kubernetes/kubeconfig.sops.yaml`.
4. `talkub apply` via the LAN. While the firewall is still active, 50000 is open only to the tailnet
   and the nodes' own addresses, so run this from a node or from inside the tailnet.
5. Commit.

To remove the extension as well, remove `siderolabs/tailscale` from the schematics
(`talkub image remove siderolabs/tailscale`, with `--platform` for each platform in use), delete `talos/patches/tailscale.sops.yaml` and the
`tailscale` block in `.talkub.yaml`, then `talkub upgrade talos`.

## 2. The Tailscale operator

### 2a. Enable

Prerequisites: `talkub tailscale enable` (the tailnet is known) and CoreDNS managed by Argo CD
(new repos have it; older ones run `talkub coredns takeover` first, see the [reference](reference.md#coredns)).

```bash
talkub tailscale operator enable       # OAuth client via prompt, or --client-id and --client-secret-file
git add gitops && git commit -m "feat: tailscale operator" && git push
talkub status                          # tailscale-operator and tailscale-operator-config Synced/Healthy
```

It writes:

- `gitops/apps/tailscale-operator.yaml` and its values: the operator's Helm chart;
- `gitops/apps/tailscale-operator-config.yaml`: a directory app for `gitops/manifests/tailscale/`;
- `gitops/manifests/tailscale/operator-oauth.sealed.yaml`: the OAuth client, sealed;
- a `DNSConfig` and a CoreDNS stub zone for `tail1234.ts.net` that points at the operator's
  nameserver, so pods resolve MagicDNS names.

The `tailscale` namespace runs with `pod-security.kubernetes.io/enforce: privileged`: the proxy pods
need a privileged init container and NET_ADMIN. Only that namespace.

A new OAuth client: `talkub tailscale operator enable --rotate-credentials` replaces only the
sealed secret. Without the flag, a second `enable` refuses ("already enabled").

### 2b. Ingress: an app in the tailnet

```bash
talkub tailscale expose grafana --service monitoring/grafana --port 80            # https://grafana.tail1234.ts.net
talkub tailscale expose grafana --service monitoring/grafana --port 80 --path /api  # only one path
talkub tailscale unexpose grafana
git add gitops && git commit -m "feat: expose grafana" && git push
```

`expose` writes `gitops/manifests/tailscale/ingress/<name>.yaml`: an Ingress with
`ingressClassName: tailscale` in the Service's namespace. `--service` is `<namespace>/<service>`,
`--port` a number or the Service's named port. The Tailscale proxy terminates TLS, so the backend
must speak **HTTP**.

For an app with its own ingress settings in its chart, use those instead. Observed working with the
Argo CD chart:

```yaml
configs:
  params:
    server.insecure: true # argocd-server would otherwise redirect HTTP to HTTPS and loop,
    # since the Tailscale proxy terminates TLS and talks HTTP to it
server:
  ingress:
    enabled: true
    ingressClassName: tailscale
    hostname: argocd
    tls: true
```

Cluster-internal clients cannot reach the Ingress by its MagicDNS name by default; see the
operator's docs on `tailscale.com/experimental-forward-cluster-traffic-via-ingress`.

### 2c. Egress: tailnet hosts for pods

```bash
talkub tailscale egress add db --fqdn db.tail1234.ts.net
talkub tailscale egress add db --ip 100.64.1.2 --namespace app   # the namespace must already exist
talkub tailscale egress remove db
git add gitops && git commit -m "feat: egress db" && git push
```

`egress add` writes `gitops/manifests/tailscale/egress/<name>.yaml`: an `ExternalName` Service
annotated `tailscale.com/tailnet-fqdn` (or `tailscale.com/tailnet-ip`); the operator starts a proxy
and points the Service at it. Pods then reach the host by its MagicDNS name (`db.tail1234.ts.net`,
via the CoreDNS stub zone) and by the Service (`db.tailscale.svc.cluster.local`). The operator
rewrites `spec.externalName`, so the config app ignores that field instead of reverting it.

## Troubleshooting

| Symptom                                                                  | Cause and fix                                                                                                                                                                              |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `lockdown`: `not reachable via the tailnet yet`                          | Extension not installed or node not joined: finish `apply` + `upgrade talos`, check `tailscale status`.                                                                                    |
| `lockdown`: name resolution error, but `tailscale status` lists the node | talosctl misses MagicDNS on this machine: `lockdown --use-ips` ([1d](#1d-clients-without-magicdns)).                                                                                       |
| Node boot hangs at `service "ext-tailscale" to be up`                    | Extension without a valid auth key (reused image, expired key): new key via `sops edit`, or [getting started](getting-started.md#2a-the-node-already-has-talos-on-its-disk).               |
| talosctl/kubectl time out after `apply`                                  | Firewall active, but your machine is not in the tailnet, or `mgmt` is wrong: `tailscale status`, `.talkub.yaml`.                                                                           |
| Node drops out of the tailnet after months                               | Key expiry: disable it for the node devices; re-auth with a new key ([1e](#1e-after-the-lockdown)).                                                                                        |
| New node `NotReady`, flannel errors                                      | The existing nodes still run the old firewall: `talkub apply`. A node whose `ip` changed: `talkub tailscale firewall`, `talkub apply`.                                                      |
| `https://<app>.<tailnet>` has no certificate                             | HTTPS certificates not enabled for the tailnet; the first request can take a minute while the certificate is issued.                                                                       |
| Redirect loop behind `expose`                                            | The backend redirects HTTP to HTTPS; switch it to plain HTTP (e.g. `server.insecure` for Argo CD).                                                                                         |
| Egress: the Service works, the MagicDNS name returns NXDOMAIN            | The operator's nameserver missed an update (seen with 1.102.4): if `talkub k -n tailscale get cm dnsrecords -o yaml` lists the name, `talkub k -n tailscale delete pod -l app=nameserver`. |
| Proxy pods rejected by Pod Security                                      | The `tailscale` namespace lost its `privileged` label: check `gitops/manifests/tailscale/namespace.yaml`.                                                                                  |
