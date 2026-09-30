# Handling secrets

This guide covers every secret in a talkub cluster repo: who can decrypt what, how to add, change
and read secrets, what to do when an admin leaves or a key leaks, and the guard rails that keep
plaintext out of git. The examples use cluster `homelab` and an app `podinfo` in namespace
`podinfo`.

## 0. Two mechanisms

| Kind                                                         | Mechanism      | Who can decrypt                          | Files                     |
| ------------------------------------------------------------ | -------------- | ---------------------------------------- | ------------------------- |
| Infrastructure: Talos secrets, talosconfig, kubeconfig, keys | SOPS + age     | every admin (via the cluster master key) | `*.sops.yaml`             |
| App secrets: database passwords, API tokens, OAuth clients   | Sealed Secrets | only the controller in the cluster       | `gitops/**/*.sealed.yaml` |

Rule of thumb: what talkub or an admin needs on the workstation is SOPS; what a pod needs in the
cluster is a SealedSecret. Argo CD never sees a SOPS file, and admins cannot unseal a SealedSecret
from the repo (read the live Secret from the cluster instead, see [6](#6-reading-secrets-from-the-cluster)).

The SOPS files in a cluster repo:

| File                                     | Contents                                             | Written by                                             |
| ---------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------ |
| `talos/secrets.sops.yaml`                | Talos secrets bundle (CAs, tokens, etcd encryption)  | `talkub init`, `talkub rotate-ca`                      |
| `talos/talosconfig.sops.yaml`            | talosctl client config                               | `talkub init`, `talkub rotate-ca`                      |
| `talos/patches/*.sops.yaml`              | machine config patches with secrets (e.g. Tailscale) | `talkub tailscale enable`, `talkub sops edit`          |
| `kubernetes/kubeconfig.sops.yaml`        | admin kubeconfig                                     | `talkub bootstrap`, `talkub kubeconfig`                |
| `bootstrap/sealed-secrets-key.sops.yaml` | the Sealed Secrets controller's private key(s)       | `talkub bootstrap`, `talkub secret backup-sealing-key` |
| `bootstrap/repo-credentials.sops.yaml`   | Argo CD's deploy key or HTTPS token for the repo     | `talkub repo credentials`                              |

## 1. The key model

```
your identity (YubiKey / Secure Enclave / age file)   ─┐
backup key (offline)                                   ├─ decrypt ─▶ keys/master.age ─▶ master key ─▶ *.sops.yaml
other admins' identities                              ─┘            (encrypted to keys/recipients.txt)
```

- `keys/master.pub` is the cluster master key's public key; `.sops.yaml` encrypts every
  `*.sops.yaml` to it and nothing else.
- `keys/master.age` holds the master key's private half, encrypted to every recipient in
  `keys/recipients.txt`. Adding or removing an admin rewrites only these two files.
- Nobody ever handles the master key directly. talkub decrypts it in memory for one command and
  passes it to `sops` in that child's environment only; editors, `kubectl`, `talosctl` and other
  tools never get it.
- There is no passphrase recovery. If every recipient's key is lost, the SOPS files are lost too.
  Keep at least two recipients, one of them an offline backup key.

## 2. Your identity

```bash
talkub identity new --yubikey            # or --se (Secure Enclave) / --software (plain age file)
talkub identity new --software --name laptop   # a second one; the file name defaults to the kind
```

The identity lands in `~/.config/talkub/identities/<name>.txt` (mode 0600; for YubiKey and Secure
Enclave the file is only a stub, the private key stays in the hardware). The command prints the
public key (`age1…`); send it to an admin, who runs `talkub admin add` (see [3](#3-admins)). You
cannot add yourself: adding a recipient needs the master key.

Which key talkub uses, first match wins:

1. `SOPS_AGE_KEY` or `SOPS_AGE_KEY_FILE`: the **master key itself**, for CI and automation. It is
   used only if it matches `keys/master.pub`; otherwise talkub stops with an error instead of
   silently falling through (a personal `SOPS_AGE_KEY_FILE` from another project is a common cause:
   unset it for talkub).
2. `TALKUB_IDENTITY`: one or more identity files, separated by `:`.
3. Every file in `~/.config/talkub/identities/`.

All three are stripped from the environment of every program talkub starts.

With a YubiKey, the touch/PIN prompt appears once per talkub command that needs the master key.

## 3. Admins

```bash
talkub admin list                              # name and key; warns with fewer than 2 recipients
talkub admin add age1… --name alice            # re-encrypts keys/master.age for alice too
talkub admin remove alice && talkub keys rotate  # see below
git add keys/ && git commit -m "chore: add alice" && git push
```

Names may contain letters, digits, `.`, `_` and `-`. A key already registered under another name is
refused, and so is removing the last recipient.

**Removing an admin is not enough on its own.** `admin remove` only stops them from decrypting the
_next_ `keys/master.age`; they may still know the current master key. `talkub keys rotate` creates
a new master key and re-encrypts every `*.sops.yaml` with it. Commit all changed files together.

Even then, git history keeps the old ciphertexts, which the old master key still decrypts. If the
person should lose access to the _secrets themselves_ (not just to future changes), change the
secret values too: `talkub rotate-ca` for the Talos API CA, `talkub kubeconfig` for a fresh
kubeconfig, `talkub repo credentials` for the repo deploy key, and new values for app secrets.

A backup key is an ordinary recipient:

```bash
age-keygen -o backup-homelab.txt           # keep it offline (password manager, safe), never in the repo
talkub admin add age1… --name backup       # public key from the comment line of backup-homelab.txt
```

To use it in an emergency: `TALKUB_IDENTITY=/path/to/backup-homelab.txt talkub …`.

## 4. SOPS files

```bash
talkub sops edit talos/patches/extra.sops.yaml      # opens $SOPS_EDITOR / $EDITOR (default vi); creates a missing file
talkub sops decrypt kubernetes/kubeconfig.sops.yaml # to stdout; pipe it, do not redirect it into the repo
```

`sops edit` decrypts into a private temp directory, starts the editor on that copy and encrypts the
result back only if it changed and is valid YAML (on invalid YAML it offers to re-open the editor;
declining discards the changes). The temp copy is removed on exit, including on Ctrl-C, SIGTERM and
SIGHUP. Editors that keep swap or backup files (vim `swapfile`/`backup`, JetBrains local history)
may leave copies elsewhere; turn that off for `$SOPS_EDITOR`, e.g. `SOPS_EDITOR="vim -n -i NONE"`.

A new SOPS file just needs the `.sops.yaml` suffix: `.sops.yaml` in the repo root encrypts every
`*.sops.yaml` to the master key. In `talos/patches/`, a `*.sops.yaml` patch is decrypted on the fly
when machine configs are rendered (`talkub plan`, `talkub apply`); the plaintext never touches the
repo.

Do not decrypt with plain `sops` and a hand-exported master key: it puts the key into your shell
history and environment. In CI, set `SOPS_AGE_KEY` from a CI secret and use talkub.

## 5. Sealed Secrets (app secrets)

### Sealing

```bash
# values from a hidden prompt (preferred)
talkub secret seal podinfo-db -n podinfo --prompt username --prompt password \
    -o gitops/manifests/podinfo/db.sealed.yaml

# values from files outside the repo; the key defaults to the file name
talkub secret seal podinfo-tls -n podinfo --from-file tls.crt=~/tls/cert.pem --from-file tls.key=~/tls/key.pem \
    -o gitops/manifests/podinfo/tls.sealed.yaml

# a complete Secret manifest (e.g. with type or labels) outside the repo, or on stdin
talkub secret seal ~/secrets/podinfo-db.yaml -o gitops/manifests/podinfo/db.sealed.yaml
some-generator | talkub secret seal - -o gitops/manifests/podinfo/db.sealed.yaml
```

- `--from-literal key=value` works too, but the value ends up in `ps` and your shell history; prefer
  `--prompt` or `--from-file`.
- Input files and manifests inside the cluster repo are refused: plaintext must never be there,
  not even untracked.
- The output name must end in `.sealed.yaml`; `talkub check` relies on it.
- A SealedSecret is bound to its **name and namespace**. Copying the file to another namespace or
  renaming it breaks it; seal it again instead.
- talkub seals offline against the controller's live certificate. When the cluster is unreachable
  (or not bootstrapped yet), it falls back to `bootstrap/sealed-secrets.pem` and says so.

Commit the `.sealed.yaml` like any other manifest; Argo CD applies it and the controller creates the
Secret.

### Changing a sealed secret

A SealedSecret cannot be edited in place. Seal the **whole** Secret again with every key, the old
ones included, and overwrite the file. To look up the current values, read the live Secret (next
section).

### The controller key

The sealing key pair lives in the cluster (`kube-system`, label
`sealedsecrets.bitnami.com/sealed-secrets-key`). `talkub bootstrap` backs it up on first install
into `bootstrap/sealed-secrets-key.sops.yaml` (private key, SOPS) and `bootstrap/sealed-secrets.pem`
(certificate), and restores it from there on a rebuilt cluster before installing the controller.
Without that backup, a rebuilt cluster cannot decrypt a single existing SealedSecret.

The controller adds a new key periodically (every 30 days by default) and keeps the old ones. When
`talkub secret seal` notices that the live certificate differs from `bootstrap/sealed-secrets.pem`,
it tells you; then:

```bash
talkub secret backup-sealing-key          # all current controller keys + certificate into bootstrap/
git add bootstrap/ && git commit -m "chore: back up sealed-secrets key" && git push
```

## 6. Reading secrets from the cluster

For a value you need often, add a `secret` shortcut to `.talkub.yaml` (details in the [reference](reference.md#shortcuts)):

```yaml
shortcuts:
  db-password:
    secret: { namespace: podinfo, name: podinfo-db, key: password }
  headlamp-token:
    token: { namespace: headlamp, serviceAccount: headlamp, duration: 8h }
```

```bash
talkub run db-password                    # to the clipboard, cleared after 45 s
talkub run --print db-password | psql …   # to stdout instead
```

Clipboard history tools (Raycast, Maccy, Alfred) keep their own copy; use `--print` or exclude the
terminal there. One-off reads go through kubectl:

```bash
talkub k -n podinfo get secret podinfo-db -o jsonpath='{.data.password}' | base64 -d
```

## 7. Guard rails

`talkub check` runs key-free (no identity needed) and fails on:

- a `*.sops.yaml` that is not encrypted, or not encrypted to `keys/master.pub`
- `.sops.yaml` missing or pointing at another key
- a plain `kind: Secret` anywhere under `gitops/`
- a plaintext Argo CD repository secret in `bootstrap/`
- an `AGE-SECRET-KEY-` anywhere in the repo
- `talos/secrets.yaml`, `talos/talosconfig` or `kubernetes/kubeconfig` (decrypted copies)

It warns about fewer than two recipients and about an interrupted key rotation
(`keys/master.next.age`). The pre-commit hook (`talkub hooks install`; `talkub init` installs it)
runs `talkub check && talkub lint --offline` before every commit. The scaffolded `.gitignore` also
ignores the plaintext names and `*.dec` files.

`git commit --no-verify` skips the hook. Do not use it to get past a `check` error.

## 8. When something happens

| Situation                             | What to do                                                                                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| New admin                             | They run `talkub identity new`; you run `talkub admin add age1… --name <them>`, commit and push `keys/`.                                    |
| Admin leaves                          | `talkub admin remove <name> && talkub keys rotate`, commit everything; change the secret values they knew ([3](#3-admins)).                 |
| Lost YubiKey / laptop (identity only) | Another admin (or the backup key) removes the lost recipient and adds your new one; `keys rotate` if the key could be used by someone else. |
| Master key may have leaked            | `talkub keys rotate`, then change the secret values: old ciphertexts in git history are still readable.                                     |
| Talos API CA or talosconfig exposed   | `talkub rotate-ca` (see the [reference](reference.md#rotating-the-talos-ca)).                                                               |
| kubeconfig exposed                    | `talkub kubeconfig` fetches a fresh one; rotate the Kubernetes CA with Talos if the client cert must stop working.                          |
| Repo deploy key / token exposed       | Revoke it at the git host, then `talkub repo credentials` and `talkub repo apply`.                                                          |
| Tailscale auth key exposed            | Revoke it in the admin console, then `talkub sops edit talos/patches/tailscale.sops.yaml` and `talkub apply`.                               |
| App secret exposed                    | Change it at its source, seal the new value ([5](#changing-a-sealed-secret)), commit and push.                                              |
| Plaintext committed by accident       | Treat the value as leaked and rotate it. Rewriting git history does not help once it was pushed.                                            |

## Troubleshooting

| Message                                                                                | Cause and fix                                                                                                                                                      |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `no age identity found`                                                                | No file in `~/.config/talkub/identities/` and no `TALKUB_IDENTITY`: `talkub identity new`.                                                                         |
| `SOPS_AGE_KEY/SOPS_AGE_KEY_FILE is set but does not contain this cluster's master key` | A key for something else is exported; `unset SOPS_AGE_KEY SOPS_AGE_KEY_FILE` for talkub.                                                                           |
| age cannot decrypt `keys/master.age`                                                   | Your key is not (or no longer) a recipient: `talkub admin list`; ask an admin to add you, then `git pull`.                                                         |
| `a previous key rotation was interrupted`                                              | `keys/master.next.age` holds the newer key. If `git status` shows no changed `*.sops.yaml`, delete it and rotate again; otherwise finish by hand as the hint says. |
| `… contains a plain Secret; seal it with kubeseal`                                     | Replace the file with `talkub secret seal … -o <name>.sealed.yaml` and delete the plaintext.                                                                       |
| `no sealing certificate available`                                                     | Cluster unreachable and `bootstrap/sealed-secrets.pem` missing: run `talkub secret backup-sealing-key` once the cluster is up.                                     |
| SealedSecret does not become a Secret (`no key could decrypt secret`)                  | See [debugging Kubernetes](debugging-kubernetes.md#5-sealed-secrets).                                                                                              |
