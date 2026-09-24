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

Once the cluster is bootstrapped, `apply --mode auto|try` and `upgrade talos` are rolling: they
check `talosctl health` once before the first change (and change nothing if the cluster is
unhealthy), then wait for health after each node before moving on. Health always runs through a
control plane (another one than the node just changed, if there is one). After `apply` talkub first
waits 30 seconds so the node's reboot has started (`TALKUB_APPLY_SETTLE_SECONDS` overrides this).
Before `talkub bootstrap`, apply nodes one at a time. If `apply` fails, the error lists the nodes
already applied in this run. The rolling behavior is only tested against a fake `talosctl`; verify
it on a real cluster before relying on it.

Version skew: repos written by this talkub version (`schematics` instead of `installImage` in
`.talkub.yaml`) cannot be read by older talkub versions — all admins should upgrade together.

## Bootstrap, upgrades and status

    talkub bootstrap                 # etcd, kubeconfig, Sealed Secrets, Argo CD, root app (re-runnable)
    talkub status                    # target vs. actual per node + Argo CD sync/health
    talkub upgrade talos             # nodes whose Talos version or schematic differs, one at a time
    talkub upgrade k8s               # shows the talosctl dry-run first

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
    talkub upgrade talos && talkub apply                # installs + configures the extension (via LAN)
    talkub tailscale lockdown                           # firewall + tailnet names for talosctl/kubectl
    talkub apply                                        # activates the firewall (via the tailnet)

The nodes keep their LAN/public cluster endpoint and do not use Tailscale DNS; only admin
clients go through the tailnet. Use a tagged, reusable, pre-approved auth key.

## Admin changes

    talkub admin remove alice && talkub keys rotate

## Development

    uv run pytest
