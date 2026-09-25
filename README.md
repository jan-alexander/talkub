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
    talkub apply && talkub upgrade talos                # configures, then installs the extension (via LAN)
    talkub tailscale lockdown                           # firewall + tailnet names for talosctl/kubectl
    talkub tailscale lockdown --use-ips                 # …or tailnet IPs, for clients without MagicDNS
    talkub apply                                        # activates the firewall (via the tailnet)

The nodes keep their LAN/public cluster endpoint and do not use Tailscale DNS; only admin
clients go through the tailnet. Use a tagged, reusable, pre-approved auth key.
On a running cluster, `lockdown` first checks that every node answers via its tailnet
name, and refuses if one does not. `lockdown --node-cidr` must cover every node's IP.

Rollback: delete `talos/patches/firewall.yaml`, reset `mgmt` in `.talkub.yaml` to the
LAN addresses (or remove the entries), point the talosconfig endpoints/nodes and the
kubeconfig `server` back at the LAN with `talkub sops edit`, then run `talkub apply` via LAN.

## CoreDNS

    talkub coredns takeover              # moves CoreDNS from Talos to Argo CD (DNS keeps working)
    talkub coredns takeover --resume     # finish an interrupted takeover

Talos manages CoreDNS by default (the `kube-system/coredns` ConfigMap and Deployment, behind the
`kube-dns` Service). `takeover` stops Talos from managing it (`talos/patches/controlplane-coredns.yaml`,
`KubeCoreDNSConfig` `enabled: false`) and installs CoreDNS as a Helm release (release `coredns`,
Deployment `coredns-talkub`) that Argo CD manages instead. The `kube-dns` Service keeps its
ClusterIP and selector throughout, so DNS keeps working during the switch; its ports become the
chart's `udp-53`/`tcp-53`, and the metrics port 9153 is no longer exposed on `kube-dns`. The
takeover refuses to start while any node's Talos config differs from the repo (`talkub plan`),
so its Talos apply (mode `no-reboot`, confirmed per node unless `--yes`) carries only the CoreDNS
patch. New clusters get
CoreDNS this way from the start: `talkub init` writes the patch, Argo CD app and Helm values, and
`talkub bootstrap` installs the Helm release before Sealed Secrets and Argo CD.

If a takeover is interrupted partway, finish it with `talkub coredns takeover --resume` before
running `talkub apply` or `talkub upgrade` again: Talos health fails while both CoreDNS
deployments exist. `talkub tailscale operator enable` also
requires the takeover to be done first (it extends the CoreDNS values with a stub zone for the
tailnet).

## Tailscale operator

    talkub tailscale operator enable                      # OAuth client via prompt, --client-id, --client-secret-file
    talkub tailscale operator enable --rotate-credentials  # replace the OAuth client, keep everything else

Prerequisites: `talkub tailscale enable` (the tailnet is known) and `talkub coredns takeover`
(Argo CD manages CoreDNS, so its values can carry the operator's stub zone).

In the Tailscale admin console, create an OAuth client with scopes Devices Core, Auth Keys and
Services (write), tagged `tag:k8s-operator`, and add to the tailnet ACL:

    "tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}

`operator enable` seals the OAuth client into
`gitops/manifests/tailscale/operator-oauth.sealed.yaml` and writes the Helm release
(`tailscale-operator`), its manifests directory app and a `DNSConfig` that points the CoreDNS
stub zone for `<tailnet>` at the operator's nameserver. Run it again without
`--rotate-credentials` and it refuses ("already enabled"); with it, only the sealed secret is
replaced. The `tailscale` namespace runs with `pod-security.kubernetes.io/enforce: privileged`
because the operator's egress/ingress proxy pods need a privileged init container and NET_ADMIN
(only that namespace).

### Egress

    talkub tailscale egress add db --fqdn db.tail1234.ts.net
    talkub tailscale egress add db --ip 100.64.1.2 --namespace app   # namespace must already exist
    talkub tailscale egress remove db

Writes (or removes) `gitops/manifests/tailscale/egress/<name>.yaml`: an `ExternalName` Service
annotated `tailscale.com/tailnet-fqdn` (or `tailscale.com/tailnet-ip`), which the operator points
at the tailnet target. After commit, push and sync, pods reach it both by its MagicDNS name
(`db.tail1234.ts.net`, resolved via the CoreDNS stub zone) and via the cluster Service
(`db.tailscale.svc.cluster.local`). The operator rewrites the Services' `spec.externalName`, so
the `tailscale-operator-config` app ignores that field (`ignoreDifferences`) instead of reverting it.

If the cluster Service works but the MagicDNS name still returns NXDOMAIN after the egress proxy is
running, the operator's nameserver may have missed the update of its records (observed with
operator 1.102.4: it read an empty `dnsrecords` ConfigMap and did not reload). Check the records
with `talkub k -n tailscale get cm dnsrecords -o yaml`; if the name is listed, restart the
nameserver: `talkub k -n tailscale delete pod -l app=nameserver`.

### Ingress

    talkub tailscale expose grafana --service monitoring/grafana --port 80
    talkub tailscale expose grafana --service monitoring/grafana --port 80 --path /api
    talkub tailscale unexpose grafana

Writes (or removes) `gitops/manifests/tailscale/ingress/<name>.yaml`: an Ingress
(`ingressClassName: tailscale`) that publishes the Service at `https://<name>.<tailnet>`, either as
a `defaultBackend` or, with `--path`, a single path-based rule. `--service` is `<namespace>/<service>`
(a Service in another namespace than the Ingress's own is not supported by Kubernetes); `--port` is
a number (1-65535) or the Service's named port. The backend must speak HTTP — the Tailscale proxy
terminates TLS in front of it — and HTTPS certificates must be enabled for the tailnet in the admin
console. After writing the file, commit and push `gitops/`.

For an app installed via its own Helm chart (e.g. Argo CD itself), configure the ingress through the
chart's values instead of `talkub tailscale expose`. Observed working with the Argo CD chart:

    configs:
      params:
        server.insecure: true      # argocd-server would otherwise redirect HTTP to HTTPS and loop,
                                    # since the Tailscale proxy terminates TLS and talks HTTP to it
    server:
      ingress:
        enabled: true
        ingressClassName: tailscale
        hostname: argocd
        tls: true

See the operator's own docs for `tailscale.com/experimental-forward-cluster-traffic-via-ingress`
if cluster-internal clients also need to reach the Ingress by its MagicDNS name.

## Rotating the Talos CA

    talkub rotate-ca            # asks for confirmation, then rotates the Talos API CA
    talkub rotate-ca --yes      # no prompt

Do this when the Talos API CA or a client certificate may have been exposed. It runs
`talosctl rotate-ca`, then writes the new talosconfig and CA cert/key into the repo
(`talos/talosconfig.sops.yaml`, `certs.os` in the secrets bundle). The previous encrypted
talosconfig is kept as `talos/talosconfig.previous.sops.yaml` while a rotation is in
progress; its presence means `talkub rotate-ca --finish` is needed to complete it.

If a rotation is interrupted after `talosctl rotate-ca` has already run, `talkub rotate-ca
--finish` retries bringing the repo in line. If the new talosconfig itself could not be
stored, its plaintext is kept under a printed temp path; pass it explicitly with
`talkub rotate-ca --finish --talosconfig <path>`.

Before you start:

- Commit first. Git history then keeps the old talosconfig and secrets bundle.
- Every other talosconfig for this cluster stops working after the rotation, for example
  `~/.talos/config` or copies held by other admins. Hand out the new one from the repo.
- Do not interrupt the rotation. `talosctl rotate-ca` can take several minutes. On Ctrl-C,
  SIGTERM or a closed terminal, talkub keeps waiting for talosctl to finish (even if it can no
  longer print) and keeps its output before it stops. If talkub is killed hard (SIGKILL), talosctl
  keeps running on its own.
- If the rotation is interrupted or fails, the printed temp directory holds `rotate-ca.out`
  (talosctl's full output, including the new keys) and, if talosctl got that far, the new
  talosconfig; after a successful run it is deleted.

Commit `talos/` afterwards.

## Admin changes

    talkub admin remove alice && talkub keys rotate

## Development

    uv run pytest
