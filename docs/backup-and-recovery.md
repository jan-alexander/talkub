# Backup and disaster recovery

This guide covers what a talkub cluster repo already backs up, what it does not, how to take the
missing backups, and how to bring a cluster back after losing a node, etcd or everything. The
examples use cluster `homelab` with control plane `cp-01` (address `10.0.0.10`) and worker `w-01`.

## 0. What the repo holds, and what it does not

The cluster repo is most of the backup. A clone of it plus one working age key is enough to rebuild
every node's configuration and every app declared in `gitops/`.

| In the repo (pushed = backed up)                                         | Not in the repo                                                               |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| Talos secrets bundle: all CAs, tokens, etcd encryption key (`talos/`)    | **etcd**: every Kubernetes object not declared in git                         |
| Node configs, patches, volumes (`talos/`, `.talkub.yaml`)                | **Data** in PersistentVolumes, user volumes and `hostPath` directories        |
| talosconfig, kubeconfig (SOPS)                                           | Your age identity (`~/.config/talkub/identities/`) and the offline backup key |
| Sealed Secrets controller key (`bootstrap/sealed-secrets-key.sops.yaml`) | Tailscale devices and auth keys, DNS records, the git host itself             |
| Argo CD repo credentials, all apps and values (`bootstrap/`, `gitops/`)  |                                                                               |

Because the secrets bundle is in the repo, a rebuilt cluster gets the **same CAs**: the
talosconfig in the repo keeps working, and so do kubeconfigs issued before the loss.

What etcd holds beyond git: objects created by hand (`kubectl apply`, `kubectl create`), Secrets
that operators generate (e.g. the Tailscale operator's device state, cert-manager certificates),
ServiceAccount tokens, PV/PVC bindings, Argo CD's sync history. With everything declared in
`gitops/`, losing etcd costs little; the more you did by hand, the more a snapshot matters.

## 1. Backups to take

### 1a. The repo

- `git push` after every change. `talkub bootstrap`, `rotate-ca`, `kubeconfig`, `keys rotate` and
  `secret backup-sealing-key` all change files that must be committed; a change that exists only
  in your working tree is not backed up.
- Keep a second copy outside the git host (a mirror clone, or the host's own backup). A lost
  remote is annoying, not fatal: any clone can be pushed to a new one (see [3f](#3f-the-git-repo-is-gone)).
- The offline backup key from [handling secrets](secrets.md#3-admins) is part of this backup.
  Without any working age key, the repo's SOPS files cannot be decrypted.

### 1b. etcd snapshots

```bash
talkub t -n 10.0.0.10 etcd snapshot homelab-etcd-$(date +%F).snapshot
age -R keys/recipients.txt -o homelab-etcd-$(date +%F).snapshot.age homelab-etcd-$(date +%F).snapshot
rm homelab-etcd-$(date +%F).snapshot
```

- `-n` takes a control plane's address (`mgmt`, else `ip` in `.talkub.yaml`), not its name.
- A snapshot contains every Kubernetes object. Talos encrypts Secrets in etcd with a key from the
  secrets bundle, but treat the whole snapshot as sensitive: encrypt it (above, to the same admins
  as the master key; `keys/recipients.txt` is readable by `age -R`) and store it **outside the
  repo**.
- Take one before every upgrade ([upgrading](upgrading.md)) and regularly, e.g. daily from a
  machine with talkub access. A global shortcut saves typing:

  ```yaml
  # ~/.config/talkub/shortcuts.yaml
  shortcuts:
    etcd-snapshot:
      description: etcd snapshot to the given file
      run: [t, -n, "{node}", etcd, snapshot]
  ```

  `talkub run etcd-snapshot cp-01 homelab-etcd.snapshot`

### 1c. Application data

Neither the repo nor etcd contains the data in volumes. Back it up at the application level:

- databases with their own dump tools, e.g. a CronJob running `pg_dump` to storage off the
  cluster; a `job` shortcut (`talkub run --follow backup`) triggers it by hand before risky changes;
- files with a backup tool that runs in the cluster (e.g. restic or Velero with a file-system
  backup) and writes to storage **outside** the cluster.

Encrypted user volumes written by `talkub node volumes` use a key bound to the node's identity and
its STATE partition (`nodeID`, `lockToState`). After a reinstall or a reset of that node their
contents are unreadable, even though the disk is untouched. Only an application-level backup helps.

### 1d. Check your backups

A backup nobody restored is a guess. Now and then:

```bash
TALKUB_IDENTITY=/path/to/backup-homelab.txt talkub sops decrypt talos/talosconfig.sops.yaml >/dev/null && echo ok
age -d -i /path/to/backup-homelab.txt homelab-etcd-….snapshot.age >/dev/null && echo ok
```

and restore one application backup into a scratch namespace.

## 2. Before you recover

- Work from an up-to-date clone: `git pull`, `talkub check`.
- Decide what you restore: **configuration only** (a fresh `talkub bootstrap`; Argo CD redeploys
  everything from git) or **configuration plus etcd** (a snapshot restore; brings back everything
  from [0](#0-what-the-repo-holds-and-what-it-does-not) that git does not have). A snapshot restore
  also brings back objects that no longer match reality (old Node objects, pods on dead nodes);
  Kubernetes cleans most of them up by itself.
- With Tailscale lockdown: a reinstalled node joins the tailnet again with the auth key from
  `talos/patches/tailscale.sops.yaml`. That key must still be valid and reusable (a new one:
  `talkub sops edit talos/patches/tailscale.sops.yaml`). Delete the old device in the admin console
  first, or the node gets a name with a `-1` suffix and a new tailnet IP. With
  `lockdown --use-ips`, `mgmt` in `.talkub.yaml` holds the old tailnet IP: set it to the new one
  (`tailscale status`) after the node has joined.

## 3. Scenarios

### 3a. A worker is lost

The cluster keeps running; its pods are rescheduled on other nodes (except those bound to local
volumes on that node).

```bash
talkub k delete node w-01                 # the old Node object
# boot the new or wiped machine into maintenance mode (same address, or adjust .talkub.yaml)
talkub node volumes w-01 --insecure       # only if the disks differ from talos/nodes/w-01.yaml
talkub apply --insecure w-01
talkub status
```

### 3b. One control plane of several is lost

etcd keeps quorum with two of three members, but a second failure stops the API. Replace the lost
member soon:

```bash
talkub t -n 10.0.0.11 etcd members        # from a healthy control plane; note the lost member's ID
talkub t -n 10.0.0.11 etcd remove-member <member id>
talkub k delete node cp-01
# boot the replacement into maintenance mode
talkub apply --insecure cp-01             # joins the existing etcd; no bootstrap
```

Do **not** run `talkub bootstrap` for this: the other control planes already have etcd. (It would
detect that and skip the etcd step, but there is nothing else to do.)

### 3c. The only control plane lost its etcd, the disk is fine

etcd data is corrupted, but Talos boots. Restore from a snapshot:

```bash
age -d -i ~/.config/talkub/identities/yubikey.txt -o etcd.snapshot homelab-etcd-….snapshot.age
talkub t -n 10.0.0.10 reset --graceful=false --system-labels-to-wipe EPHEMERAL --reboot
talkub t -n 10.0.0.10 bootstrap --recover-from=./etcd.snapshot
rm etcd.snapshot
talkub status
```

Wiping EPHEMERAL removes etcd's data directory; Talos and its config stay. Without a snapshot, run
`talkub bootstrap` instead of the `bootstrap --recover-from` line: a fresh etcd, and Argo CD
redeploys `gitops/`.

### 3d. The whole cluster is gone

All nodes lost or wiped. The repo and an age key are all you need.

```bash
git clone <repo> homelab && cd homelab && talkub check
# 1. boot every machine into maintenance mode (same addresses as in .talkub.yaml, or adjust them)
talkub apply --insecure cp-01             # control planes first, then the workers, one at a time
talkub apply --insecure w-01
# 2a. configuration only:
talkub bootstrap
# 2b. or with etcd from a snapshot, on the first control plane:
talkub t -n 10.0.0.10 bootstrap --recover-from=./etcd.snapshot
talkub bootstrap                          # skips etcd, then kubeconfig, Sealed Secrets key, Argo CD, root app
git add -A && git commit -m "chore: rebuild" && git push
```

`talkub bootstrap` restores the Sealed Secrets controller key from
`bootstrap/sealed-secrets-key.sops.yaml` **before** the controller starts, so every SealedSecret in
`gitops/` decrypts as before. It then fetches a new kubeconfig into
`kubernetes/kubeconfig.sops.yaml` (commit it) and hands over to Argo CD. Then restore application
data ([1c](#1c-application-data)).

If `bootstrap/sealed-secrets-key.sops.yaml` is missing, the new controller creates a new key and
none of the existing SealedSecrets decrypt: seal every secret again with `talkub secret seal`, then
commit `bootstrap/`.

New addresses: change `ip`/`mgmt` in `.talkub.yaml`, the endpoints and nodes in the talosconfig
(`talkub sops edit talos/talosconfig.sops.yaml`) and, if the Kubernetes endpoint moved, the cluster
endpoint. A new endpoint changes the API server certificate, so plan it like a new cluster.

### 3e. Your age key is lost

Another admin or the offline backup key restores your access: see
[handling secrets](secrets.md#8-when-something-happens). If **every** key is lost, the SOPS files
cannot be decrypted: the running cluster keeps working, but the repo can no longer manage it.
Fetch what you can while the cluster runs (a talosconfig from `~/.talos/config` if you have one,
`talosctl kubeconfig`) and plan a new repo.

### 3f. The git repo is gone

Push any clone to a new remote, then point the repo and Argo CD at it:

```bash
git remote set-url origin <new url> && git push -u origin main
talkub repo set-url <new url>
talkub repo credentials                   # new deploy key or token for the new host
git add -A && git commit -m "chore: move repo" && git push
talkub repo apply
```

## Troubleshooting

| Symptom                                                      | Cause and fix                                                                                                                                     |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bootstrap --recover-from` fails with a hash mismatch        | The snapshot was copied from the data directory, not taken with `etcd snapshot`: add `--recover-skip-hash-check`.                                 |
| `talkub bootstrap` waits on health until it times out        | A node boots an old image (e.g. with an unconfigured extension): [getting started](getting-started.md#2a-the-node-already-has-talos-on-its-disk). |
| SealedSecrets: `no key could decrypt secret` after a rebuild | The key backup was missing or older than the key used: [debugging Kubernetes](debugging-kubernetes.md#5-sealed-secrets).                          |
| Node unreachable via its tailnet name after a rebuild        | The old device still holds the name, or `mgmt` holds an old tailnet IP: [2](#2-before-you-recover).                                               |
| A volume is empty after a reinstall                          | Encrypted user volumes are bound to the node's STATE; restore from an application backup ([1c](#1c-application-data)).                            |
| Pods of a lost node stay `Terminating`                       | `talkub k delete node <node>`; for a StatefulSet pod also `talkub k delete pod <pod> --force --grace-period=0`.                                   |
