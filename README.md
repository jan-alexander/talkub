# talkub

Admin CLI for a Talos Linux cluster repo: ArgoCD app-of-apps, SOPS/age for
infrastructure secrets, Sealed Secrets for app secrets. The tool lives outside
the cluster repo and is installed with uv.

## Install

    uv tool install git+https://<your-git-host>/talkub.git
    # needs: age, sops, talosctl, helm (and kubectl, kubeseal later)
    # optional: age-plugin-yubikey, age-plugin-se

## Key model

- Every `*.sops.yaml` is encrypted to one **cluster master key**.
- `keys/master.age` holds that key, encrypted to all admins in `keys/recipients.txt`.
- Each admin has a personal identity (software key, YubiKey or Secure Enclave)
  in `~/.config/talkub/identities/`.
- Lookup order: `SOPS_AGE_KEY` / `SOPS_AGE_KEY_FILE` (the master key itself, for CI)
  → `TALKUB_IDENTITY` → `~/.config/talkub/identities/`.
- Keep at least two recipients (e.g. YubiKey + offline backup key). There is no passphrase recovery.

## Quick start

    talkub identity new --yubikey          # or --se / --software
    talkub init homelab --endpoint https://10.0.0.10:6443 \
        --node cp-01=10.0.0.10 --git-url https://git.example.com/homelab.git
    cd homelab
    talkub admin add age1... --name backup
    talkub check && git init && git add -A && git commit -m "init cluster repo"

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

## Nodes and machine configs

    talkub node add w-01 --ip 10.0.0.11 --role worker \
        --static 10.0.0.11/24 --gateway 10.0.0.1 --mac aa:bb:cc:dd:ee:ff
    talkub image add siderolabs/intel-ucode      # extensions per platform (metal, hcloud)
    talkub image show --platform hcloud --arch arm64   # installer image + boot medium URL
    talkub apply --insecure w-01                  # first install (maintenance mode, node's --ip)
    talkub plan                                   # Talos dry-run for all nodes
    talkub apply --mode no-reboot                 # asks per node; --yes to skip

Machine configs are rendered on the fly from `talos/secrets.sops.yaml`, `talos/patches/*.yaml`
(`controlplane*` / `worker*` are role-specific, `*.sops.yaml` patches are decrypted) and
`talos/nodes/<node>.yaml`; they are never written to the repo.

**Warning:** `apply` does not wait for a rebooting node to become healthy before it moves on
to the next one. With several control planes, use `--mode no-reboot` / `--mode staged`, or
apply them one at a time (`talkub apply cp-01`) and check health in between
(`talkub t health`). If `apply` fails, the error lists the nodes already applied in this run.

## Admin changes

    talkub admin remove alice && talkub keys rotate

## Development

    uv run pytest
