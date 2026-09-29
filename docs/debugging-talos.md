# Debugging Talos

This guide is for the node side of a talkub cluster: a node that is unreachable, does not boot,
drifts from the repo, has a broken service or etcd, or just got a new disk. It starts with where to
look, then walks through the common cases. For pods, Argo CD apps, DNS and volumes inside
Kubernetes see [debugging Kubernetes](debugging-kubernetes.md).

The examples use cluster `homelab` with control plane `cp-01` (`10.0.0.10`) and worker `w-01`
(`10.0.0.11`).

## 0. Addressing a node

`talkub t` (alias of `talkub talosctl`) runs talosctl with the decrypted talosconfig and passes
every argument through verbatim. talosctl does not know the node names from `.talkub.yaml`, so
`-n` takes the node's address: its `mgmt` address if it has one (after `talkub tailscale
lockdown`, the tailnet name or IP), otherwise its `ip`.

```bash
N=10.0.0.11                               # the node you are looking at
talkub t -n $N version
```

To use node names instead, put the commands you need often into global shortcuts
(`~/.config/talkub/shortcuts.yaml`); there `{node}` is replaced by the right address:

```yaml
shortcuts:
  tlogs:
    description: logs of a Talos service
    run: [t, -n, "{node}", logs]
  tdmesg:
    run: [t, -n, "{node}", dmesg]
  tvolumes:
    run: [t, -n, "{node}", get, volumestatus]
```

```bash
talkub run tlogs w-01 kubelet -f
```

For a longer session, `talkub shell` sets `TALOSCONFIG` and `KUBECONFIG` for plain `talosctl` and
`kubectl` and removes both files on exit.

## 1. First look

```bash
talkub status                             # per node: reachable?, Talos, schematic, config drift, kubelet
talkub t -n 10.0.0.10 health              # control plane view: etcd, apid, kubelet, nodes Ready, …
talkub t -n $N dashboard                  # live TUI: services, logs, CPU/memory (q to quit)
```

How to read `talkub status`:

| Column      | Value              | Meaning                                                                              |
| ----------- | ------------------ | ------------------------------------------------------------------------------------ |
| `TALOS`     | `unreachable`      | talosctl got no answer from the node's address, see [2](#2-node-unreachable)         |
| `TALOS`     | `v1.14.2 (want …)` | the node runs another version than `.talkub.yaml`: `talkub upgrade talos`            |
| `SCHEMATIC` | `abc… (want def…)` | other extensions than the target: `talkub upgrade talos` rolls them out              |
| `CONFIG`    | `changes`          | the rendered config differs from the node's: `talkub plan <node>` shows the diff     |
| `CONFIG`    | `unknown`          | the configs could not be rendered (error above the table) or the dry-run failed      |
| `KUBELET`   | `-`                | Kubernetes is not bootstrapped or not reachable; the line below the table says which |

`health` runs through a control plane and checks the whole cluster. It hangs at the first failing
check; that check is your lead.

## 2. Gathering information

Everything below is read-only.

| Question                              | Command (`talkub t -n $N …`)                                                                              |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Which services run, which failed?     | `services`, then `service <id>` for its state history                                                     |
| Logs of a Talos service               | `logs machined`, `logs kubelet`, `logs etcd`, `logs apid`, `logs containerd` (`-f` follows, `--tail 100`) |
| Kernel and boot log                   | `dmesg` (`-f` follows)                                                                                    |
| What is Talos doing right now?        | `events`, `get machinestatus -o yaml`                                                                     |
| Version and extensions                | `version`, `get extensions`                                                                               |
| Network                               | `get addresses`, `get routes`, `get links`, `get resolvers`, `netstat -tlnp`                              |
| Time (certificates break without)     | `get timestatus`, `get timeservers`                                                                       |
| Disks and volumes                     | `get disks`, `get systemdisk`, `get discoveredvolumes`, `get volumestatus`, `get mountstatus`             |
| Disk usage                            | `usage /var -d 1`                                                                                         |
| Cluster membership / discovery        | `get members`                                                                                             |
| etcd (control planes only)            | `etcd members`, `etcd status`, `etcd alarm list`                                                          |
| Kubernetes static pods and containers | `containers -k`, `logs -k <container id from containers -k>`                                              |
| Files on the node (read-only)         | `ls -l /var/mnt`, `read /proc/meminfo`                                                                    |

`get <resource> -o yaml` shows the full resource, including error messages that the table view
leaves out. `get rd` lists every resource type the node knows.

For a bug report or a second opinion, `talkub t -n $N support` writes a `support.zip` with logs and
resources of the node. It contains network details and parts of the configuration: write it outside
the repo and read it before sharing it.

Two commands to avoid in a shared terminal or recording: `get machineconfig -o yaml` and
`read /system/state/config.yaml` print the node's full machine config, including its keys and
tokens. `talkub plan` shows the difference to the repo with secrets redacted.

## 3. Node unreachable

`talkub status` shows `unreachable`, or talosctl fails with `connection refused`, `i/o timeout` or
`certificate signed by unknown authority`.

1. **Is it up?** Look at the console (the physical screen, the Hetzner Cloud console, IPMI). Talos
   shows its dashboard there: the stage (`maintenance`, `booting`, `running`), the IP addresses and
   the last log lines. Without console access: `ping` and `nc -vz <address> 50000`.
2. **Maintenance mode** (console shows it, or `talkub t -n $N version --insecure` answers): the
   node booted the ISO/PXE image or was reset and has no config. Reinstall it with
   `talkub apply --insecure <node>`.
3. **Wrong address.** The node got another IP via DHCP (use a reservation or `--static`), or
   `mgmt` in `.talkub.yaml` points at a tailnet name that does not resolve. talosctl resolves names
   itself and can miss MagicDNS names that the OS resolves (macOS); try the tailnet IP.
4. **Firewall after `tailscale lockdown`.** The Talos API then only answers on the tailnet. If
   Tailscale on the node is down, `talkub t` via the LAN fails too. Check on the console that the
   `ext-tailscale` service runs and the node is online in the Tailscale admin console.
5. **Certificate errors** (`x509: …`): the talosconfig belongs to another CA (an interrupted
   `rotate-ca`: `talkub rotate-ca --finish`), or the node's clock is far off (look at the console,
   fix NTP in a patch).

## 4. Config problems

```bash
talkub plan w-01                          # Talos dry-run: what apply would change, secrets redacted
talkub apply w-01 --mode try              # apply, roll back automatically unless confirmed (1 minute)
```

- **`plan` shows changes you did not make**: someone applied a config by hand, or the rendering
  inputs changed (`talos/patches/*`, `talos/nodes/w-01.yaml`, `talos/secrets.sops.yaml`,
  `.talkub.yaml`). `git log -p talos/` shows what changed in the repo. The repo is the truth:
  `talkub apply` brings the node back.
- **`apply` rejects the config** (validation error): the message names the field. A patch for
  another Talos version is the usual cause; check the field in the Talos docs for the target
  version.
- **Change needs a reboot**: `--mode auto` (default) reboots when Talos says it must. Use
  `--mode no-reboot` to fail instead of rebooting, `--mode staged` to apply it on the next reboot.
- **`the cluster is not healthy; nothing was changed`**: `apply` and `upgrade talos` check health
  once before the first change. Fix the cluster first, or use `--force-unhealthy` when this change is
  the fix.

## 5. Node does not boot or keeps rebooting

1. Read the boot log on the console, or once apid answers: `talkub t -n $N dmesg`.
2. **After an upgrade**: Talos keeps the previous image. `talkub t -n $N rollback` boots it again.
   Then find the cause before upgrading again (release notes, an extension that does not exist for
   the new version).
3. **Stuck on an extension** (the service of an extension fails and blocks boot): remove it with
   `talkub image remove <extension>`, then `talkub upgrade talos --force-unhealthy <node>`.
4. **Disk full** (`no space left on device` in `dmesg`, services crash-looping): see
   [7](#7-disk-full-ephemeral).
5. **Nothing helps**: reset the node and install it again (see [9](#9-resetting-a-node)).

## 6. A new disk

The node is installed and running; you added a disk (or a new virtual disk in Hetzner/Proxmox) and
want it as storage.

### 6a. Make sure Talos sees it

```bash
talkub t -n $N get disks                  # the new disk with size, model, serial, transport
talkub t -n $N get discoveredvolumes      # partitions and filesystems Talos found on each disk
```

Hot-plugged disks (virtual disks, most SATA/NVMe backplanes) show up without a reboot. If it is
missing, check `talkub t -n $N dmesg | grep -i -E 'sd[a-z]|nvme'`, then reboot the node
(`talkub t -n $N reboot`; in a multi-node cluster drain it first with
`talkub k drain w-01 --ignore-daemonsets --delete-emptydir-data`, afterwards `talkub k uncordon w-01`).

### 6b. Wipe it if it was used before

Talos only creates volumes in free space. A disk that still has partitions (an old Talos, another
OS) leaves no room. `discoveredvolumes` shows them. If the data can go:

```bash
talkub t -n $N wipe disk sdb              # the disk's name from `get disks`
```

talosctl refuses to wipe the system disk or disks with volumes that are in use.

### 6c. Describe the volume in the repo

```bash
talkub node volumes w-01                  # reads the disks via the talosconfig
```

It prints a disk table and the current volume plan, then asks per new volume:

- **name**: the volume is mounted at `/var/mnt/<name>`; Talos calls it `u-<name>` (raw: `r-<name>`).
- **type**: `partition` (with a filesystem, the usual choice), `raw` (no filesystem, e.g. for Ceph)
  or `disk` (the whole disk, must be empty).
- **disk** and **selector**: talkub suggests CEL selectors that match exactly this disk, the most
  portable (serial, WWID) first. Prefer those over `/dev/sdb`: device names can change between
  boots.
- **size**: fixed, rest of the disk, a percentage or all but X. `minSize` only picks the disk;
  without `maxSize` the volume takes the largest free region.
- **filesystem** (`xfs`, `ext4`, `btrfs` depending on the Talos version) and **encryption** (LUKS2
  with a key bound to this node; unreadable after a reset of the node, only new empty volumes).

It simulates where Talos will place the volume and how big it gets, then writes the documents into
`talos/nodes/w-01.yaml`. It never changes existing volume documents; edit those by hand.

### 6d. Roll it out

```bash
talkub plan w-01                          # shows the new UserVolumeConfig
talkub apply w-01                         # volume configs do not need a reboot
talkub t -n $N get volumestatus           # u-<name> should reach phase `ready`
talkub t -n $N get mountstatus            # …and be mounted at /var/mnt/<name>
git add talos/nodes/w-01.yaml && git commit -m "feat(w-01): data volume" && git push
```

If the volume does not reach `ready`, `get volumestatus u-<name> -o yaml` shows why. Common causes:

| Status / error                                    | Cause / fix                                                                            |
| ------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `waiting`, no matching disk                       | the selector matches no disk: compare it with `get disks -o yaml`                      |
| not enough space                                  | the disk still has partitions (6b) or `minSize` is bigger than the free region         |
| `disk` type fails                                 | the disk is not empty (6b)                                                             |
| mounted, but the filesystem is wrong or too small | Talos does not reformat or shrink existing volumes: wipe the disk (6b) and apply again |

Using the volume from pods is a Kubernetes question: see
[debugging Kubernetes, storage](debugging-kubernetes.md#6-storage-and-the-new-disk).

## 7. Disk full (EPHEMERAL)

EPHEMERAL (`/var`) holds container images, logs, etcd data and `emptyDir` volumes. When it fills up,
kubelet reports `DiskPressure` and evicts pods; at 100 % etcd and containerd fail.

```bash
talkub t -n $N get volumestatus EPHEMERAL -o yaml   # size of the volume
talkub t -n $N usage /var -d 2                       # what takes the space
```

- Container images: kubelet removes unused ones on its own above 85 %. Old images from many chart
  upgrades are the usual reason.
- Pods writing large `emptyDir` volumes or logs: find them with the Kubernetes guide and give them
  a `sizeLimit` or a real volume.
- etcd (control planes): `talkub t -n 10.0.0.10 etcd status` shows the DB size; `etcd defrag`
  shrinks it after many deletes.

Resizing EPHEMERAL on an installed node needs a wipe of EPHEMERAL (`talkub node volumes` says so
too): set the new limit in `talos/nodes/<node>.yaml`, apply, then
`talkub t -n $N reset --system-labels-to-wipe EPHEMERAL --reboot`. Everything on `/var` is gone
afterwards (images are pulled again; etcd data on a control plane too, so do this only with a healthy
etcd quorum elsewhere or a snapshot).

## 8. etcd

Only control planes run etcd. Symptoms: the Kubernetes API hangs or answers `etcdserver: …`,
`health` stops at the etcd check.

```bash
talkub t -n 10.0.0.10 service etcd        # state and recent events
talkub t -n 10.0.0.10 logs etcd --tail 100
talkub t -n 10.0.0.10 etcd members        # all expected members listed?
talkub t -n 10.0.0.10 etcd status         # leader, DB size, errors per member
talkub t -n 10.0.0.10 etcd alarm list     # NOSPACE after a full disk
```

- **`NOSPACE` alarm**: free space (7), `etcd defrag`, then `etcd alarm disarm`.
- **A dead member** (node gone for good) blocks quorum in a three-node control plane once a second
  one fails. Remove it: `etcd remove-member <member id>` from a healthy control plane, then reset or
  replace the node.
- **Single control plane lost its data**: restore from a snapshot with
  `talkub t -n 10.0.0.10 bootstrap --recover-from=<snapshot>` (see the Talos disaster recovery docs).
  This is why [upgrading](upgrading.md) takes a snapshot first; take one now and then:
  `talkub t -n 10.0.0.10 etcd snapshot homelab-etcd.snapshot`, stored outside the repo.

## 9. Resetting a node

The last resort for a node in a state you cannot repair, and the way to hand a disk back to
maintenance mode.

```bash
talkub k drain w-01 --ignore-daemonsets --delete-emptydir-data   # multi-node clusters
talkub t -n $N reset --graceful --reboot  # leaves etcd cleanly (control plane), wipes, reboots
```

The node comes back in maintenance mode. Then `talkub apply --insecure w-01` as in
[getting started](getting-started.md), and `talkub k delete node w-01` if the old Node object is
still listed. Encrypted user volumes of the node are unreadable after a reset. On a single-node
cluster a reset loses etcd: restore it from a snapshot or bootstrap again.

## Troubleshooting

| Symptom                                                 | Where to look / fix                                                                          |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `talkub status` shows `unreachable`                     | [3](#3-node-unreachable): console, address, Tailscale, certificates                          |
| `CONFIG` shows `changes`                                | `talkub plan <node>`, then `talkub apply <node>`                                             |
| `connection refused` on port 50000 right after a reboot | apid starts late; wait, watch the console                                                    |
| `kubelet` service fails                                 | `talkub t -n $N logs kubelet`; often a patch with a wrong kubelet option or a full `/var`    |
| Node `NotReady`, Talos services all `Running`           | Kubernetes side (CNI, kubelet registration): [debugging Kubernetes](debugging-kubernetes.md) |
| Boot loop after `upgrade talos`                         | `talkub t -n $N rollback`; [5](#5-node-does-not-boot-or-keeps-rebooting)                     |
| New disk missing in `get disks`                         | `dmesg`, then reboot the node                                                                |
| Volume stays `waiting`                                  | `get volumestatus u-<name> -o yaml`; selector or free space ([6d](#6d-roll-it-out))          |
| API server hangs, `etcdserver: request timed out`       | [8](#8-etcd), and disk latency/space on the control plane                                    |
| `x509: certificate signed by unknown authority`         | interrupted `rotate-ca` (`talkub rotate-ca --finish`) or node clock                          |
