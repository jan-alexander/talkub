# Adding, replacing and removing nodes

This guide covers a node's life in a talkub cluster: adding workers and control planes, growing
from one to three control planes, replacing a machine or a disk, changing an address, and removing
a node for good. The examples use cluster `homelab` with control plane `cp-01` (`10.0.0.10`) and
worker `w-01` (`10.0.0.11`).

Where a node is defined:

| What                                                           | File                                                           |
| -------------------------------------------------------------- | -------------------------------------------------------------- |
| Name, role, first-install address `ip`, `mgmt`, platform, arch | `nodes.<name>` in `.talkub.yaml`                               |
| Network, install disk, volumes of this node                    | `talos/nodes/<name>.yaml`                                      |
| Settings for all nodes / one role                              | `talos/patches/*.yaml`, `controlplane*.yaml`, `worker*.yaml`   |
| Extensions (installer image)                                   | `schematics` per platform in `.talkub.yaml` (`talkub image …`) |

talkub reaches a node at its `mgmt` address, or at `ip` if `mgmt` is not set. `ip` is only used for
the first install via the maintenance API (`apply --insecure`).

## 1. Add a worker

```bash
talkub node add w-01 --ip 10.0.0.11 --role worker     # DHCP; add --static/--gateway/--mac for a fixed address
# boot the machine from the boot media into maintenance mode: `talkub image show` prints the URL
talkub node volumes w-01 --insecure                   # install disk, EPHEMERAL limit, user volumes
talkub check && git add -A && git commit -m "feat: node w-01" && git push
talkub apply cp-01                                    # Tailscale lockdown only: existing nodes allow w-01 (node add says which)
talkub apply --insecure w-01
talkub status
```

- One node at a time: wait until `talkub status` shows it healthy before the next one.
- `--platform`/`--arch` default to the cluster's platform; set them for a mixed cluster (e.g. an
  arm64 Hetzner CAX node). A platform the cluster does not use yet needs a schematic first
  (`talkub image show --platform <platform>`); `apply` refuses without one.
- With Tailscale lockdown, `node add` sets `mgmt` to `<name>.<tailnet>` itself (with
  `lockdown --use-ips` it warns: set `mgmt` to the tailnet IP once the node has joined). It also
  rewrites `talos/patches/firewall.yaml` to allow the new node's `ip` (which must be an IP address,
  not a name). Apply that to the existing nodes first (`talkub apply <existing nodes>`, the warning
  names them); a plain `talkub apply` would also try the new, not yet installed node.
- With Tailscale enabled, `node add` also pins the Kubernetes node IP to the node's address
  (`KubeNodeConfig` with `nodeIP.validSubnets`, the static address if given, else `ip`). Without
  the pin Talos may pick the tailscale0 address, and the API server endpoint and the kubelet would
  depend on tailscaled; see [tailscale](tailscale.md#1c-what-the-firewall-allows).
- If the machine already has Talos on its disk, `apply --insecure` shows that and asks: see
  [getting started](getting-started.md#2a-the-node-already-has-talos-on-its-disk).

The first worker changes where pods run: `talos/patches/controlplane.yaml`, written by `init`,
removes the control plane taint so a single-node cluster can run workloads. Keep it if the control
plane should keep running pods; delete it and `talkub apply` to reserve the control plane for the
control plane.

## 2. Add control planes (HA)

etcd needs a majority of its members. **One or three control planes, never two**: with two, losing
either stops the API, which is worse than one. Five only for large clusters.

```bash
talkub node add cp-02 --ip 10.0.0.12 --role controlplane
talkub node volumes cp-02 --insecure
git add -A && git commit -m "feat: node cp-02" && git push
talkub apply --insecure cp-02                         # joins etcd on its own; no bootstrap
talkub t -n 10.0.0.10 etcd members                    # cp-02 listed?
# then cp-03 the same way
```

Never run `talkub bootstrap` for an additional control plane. (It would find the existing etcd
and skip the etcd step, but there is nothing to bootstrap.)

**The Kubernetes endpoint.** `--endpoint` from `talkub init` is still cp-01's address; if cp-01 is
down, the API is unreachable for kubelets and admins even though cp-02 and cp-03 run. For real HA,
the endpoint must be an address that survives one node: a Talos shared VIP (nodes in one L2
network, see the Talos docs on _Virtual (shared) IP_), a load balancer (Hetzner Cloud: a Load
Balancer on 6443), or a DNS name pointing at all control planes. talkub has no option for this; a
VIP goes into a `talos/patches/controlplane-*.yaml` patch. Changing the endpoint of a running
cluster touches the API server certificate and every kubeconfig: decide on it before bootstrap if
you can, and add the new name to the certificate SANs first if you cannot.

## 3. Replace a machine

The node keeps its name; the machine or its disk is new.

```bash
talkub k drain w-01 --ignore-daemonsets --delete-emptydir-data   # if the old machine still runs
talkub k delete node w-01
# control plane only, from another control plane:
talkub t -n 10.0.0.12 etcd members
talkub t -n 10.0.0.12 etcd remove-member <member id of cp-01>
# boot the new machine into maintenance mode
talkub node volumes w-01 --insecure                   # the disks differ: choose them again
talkub apply --insecure w-01
```

- A new address: set `ip` (and `mgmt`) in `.talkub.yaml` first, and `--static` values in
  `talos/nodes/<name>.yaml`. For a control
  plane, also update the talosconfig endpoints with `talkub sops edit talos/talosconfig.sops.yaml`.
- Pods with local volumes on the old machine lose their data: restore it from an application
  backup ([backup and recovery](backup-and-recovery.md#1c-application-data)).
- With Tailscale: delete the old device in the admin console first, so the new one gets the same
  name ([backup and recovery](backup-and-recovery.md#2-before-you-recover)).
- The only control plane of the cluster: that is a cluster rebuild, see
  [backup and recovery](backup-and-recovery.md#3d-the-whole-cluster-is-gone).

## 4. Add or replace a disk

A new disk in a running node, or a user volume on it: [debugging Talos, a new disk](debugging-talos.md#6-a-new-disk)
and `talkub node volumes <node>` (on an installed node it reads the disks via the talosconfig and
only adds volumes). Using the volume from pods: [debugging Kubernetes](debugging-kubernetes.md#6b-using-the-new-disk-from-pods).

The install disk itself cannot be swapped in place: treat it as a new machine ([3](#3-replace-a-machine)).

## 5. Change an address

| Change                                         | Steps                                                                                                                                                                                                |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mgmt` only (how talkub reaches the node)      | Edit `nodes.<name>.mgmt` in `.talkub.yaml`. A name (not an IP) goes into the node's certificate: `talkub apply <name>`. A control plane's `mgmt` goes into every node's certificate: `talkub apply`. |
| The node's IP (DHCP reservation or `--static`) | Change `--static`/`--gateway` in `talos/nodes/<name>.yaml` (or the DHCP reservation), `talkub apply <name>`, then update `ip`/`mgmt`. With Tailscale lockdown: `talkub tailscale firewall` and `talkub apply`, so the other nodes allow the new address. |
| A control plane's IP                           | As above, plus the talosconfig endpoints (`talkub sops edit talos/talosconfig.sops.yaml`); if it is the Kubernetes endpoint, see [2](#2-add-control-planes-ha).                                      |

Reach the node through its old address for the `apply`, and expect the connection to drop when the
new address takes effect.

## 6. Remove a node for good

```bash
talkub k drain w-01 --ignore-daemonsets --delete-emptydir-data
talkub t -n 10.0.0.11 reset --graceful --reboot       # control plane: leaves etcd cleanly; wipes the node
talkub k delete node w-01
```

Then remove it from the repo:

1. Delete `nodes.w-01` from `.talkub.yaml` and the file `talos/nodes/w-01.yaml`.
2. Control plane: remove its address from the talosconfig endpoints
   (`talkub sops edit talos/talosconfig.sops.yaml`). Check with `talkub t -n <other cp> etcd members`
   that it left etcd; if the node was already dead, remove it with `etcd remove-member`.
3. Anything that names the node: `nodeSelector`s and local PersistentVolumes in `gitops/`, global
   shortcuts, the Tailscale device in the admin console.
4. `talkub check && git add -A && git commit -m "chore: remove w-01" && git push`.

Going from three control planes to one: remove two, one at a time, checking `etcd members` in
between. Never stop at two.

## Troubleshooting

| Symptom                                                       | Cause and fix                                                                                                     |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `node '<name>' already exists`                                | The name is still in `.talkub.yaml`. For a replacement, keep it and go on with `node volumes`/`apply --insecure`. |
| New node does not appear in `kubectl get nodes`               | Talos side first: [debugging Talos](debugging-talos.md); kubelet logs with `talkub t -n <addr> logs kubelet`.     |
| New control plane stuck, etcd `waiting to join`               | A stale member with the same name or address: `etcd members`, `etcd remove-member`, then reset the new node.      |
| Old Node object stays `NotReady`                              | `talkub k delete node <name>`.                                                                                    |
| Node unreachable after `apply` changed its address            | Set `mgmt` in `.talkub.yaml` to the new address.                                                                  |
| API unreachable while cp-01 is down, other control planes run | The endpoint is cp-01's address: [2](#2-add-control-planes-ha).                                                   |
