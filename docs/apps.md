# Deploying apps with Argo CD

This guide covers the app side of a talkub cluster: adding a Helm chart or plain manifests,
secrets and namespaces for it, rolling out and updating, reaching it, and removing it again. The
examples use an app `podinfo` in namespace `podinfo` and the repo `git@github.com:me/homelab.git`.

## 0. How it fits together

```
gitops/root.yaml ──▶ Argo CD app "root" ──▶ every gitops/apps/*.yaml ──▶ one Application per file
                                                        │
                           chart apps: Helm chart + gitops/values/<app>.yaml
                           directory apps: every manifest under gitops/manifests/<app>/
```

`talkub bootstrap` applies `gitops/root.yaml` once; from then on Argo CD follows the repo. Adding,
changing or removing an app is a commit and a push, never `kubectl apply`.

| Path                       | Contents                                                                  |
| -------------------------- | ------------------------------------------------------------------------- |
| `gitops/apps/<app>.yaml`   | the Argo CD Application: chart, **version** (`targetRevision`), namespace |
| `gitops/values/<app>.yaml` | Helm values of a chart app                                                |
| `gitops/manifests/<app>/`  | plain manifests: Namespaces, SealedSecrets, ConfigMaps, CRs, Ingresses    |

Argo CD reads the **remote**, not your working tree: nothing happens until you push. Pushed changes
are applied within about three minutes (Argo CD's polling interval); `selfHeal` reverts changes made
by hand in the cluster.

## 1. A Helm chart

There is no `chart add`. Write two files, with the pattern of the apps `init` wrote:

`gitops/apps/podinfo.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: podinfo
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io # removing the file also deletes the app's resources (see 7)
spec:
  project: default
  sources:
    - repoURL: https://stefanprodan.github.io/podinfo
      chart: podinfo
      targetRevision: "6.7.1" # the only place the chart version lives; quoted
      helm:
        valueFiles:
          - $values/gitops/values/podinfo.yaml
    - repoURL: git@github.com:me/homelab.git # the repo's git URL, as in the other apps
      targetRevision: HEAD
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: podinfo
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true, ServerSideApply=true]
```

`gitops/values/podinfo.yaml`: only the values you change. `helm show values podinfo --repo
https://stefanprodan.github.io/podinfo` lists the defaults.

- The file name without `.yaml` is the app's name for `talkub versions` and `talkub chart upgrade`;
  keep it equal to `metadata.name`.
- `talkub lint`, `versions` and `chart upgrade` understand the pattern above: `repoURL` +
  `chart` + `targetRevision`, values via `$values/…`. Other value file paths are skipped with a
  warning. Charts from an OCI registry work too, written the way Argo CD wants them: `repoURL:
  code.forgejo.org/forgejo-helm` without the `oci://` scheme (talkub also accepts `oci://`).
- Namespaces that must be `privileged` or need labels: create them yourself
  ([3](#3-namespaces-and-pod-security)) instead of `CreateNamespace=true`.

## 2. Plain manifests

For your own Deployments, CRs of an operator, or the secrets a chart references, use a directory
app. `gitops/apps/podinfo-config.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: podinfo-config
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: git@github.com:me/homelab.git
    targetRevision: HEAD
    path: gitops/manifests/podinfo
    directory: { recurse: true }
  destination:
    server: https://kubernetes.default.svc
    namespace: podinfo # default for manifests without metadata.namespace
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [ServerSideApply=true]
```

Every manifest under `gitops/manifests/podinfo/` (subdirectories included) belongs to this app.
Keep chart apps and directory apps apart: a chart app named `podinfo` and its manifests in
`podinfo-config` (the pattern `tailscale-operator` / `tailscale-operator-config` uses).

**Secrets** go in sealed, never as `kind: Secret` (`talkub check` refuses that):

```bash
talkub secret seal podinfo-db -n podinfo --prompt password -o gitops/manifests/podinfo/db.sealed.yaml
```

and the chart references it by name, usually via a value like `existingSecret: podinfo-db`. Until
the controller has unsealed it, pods referencing it fail with `CreateContainerConfigError` and
recover on their own. More in [handling secrets](secrets.md#5-sealed-secrets-app-secrets).

**CRs of an operator installed in the same push** (e.g. a `Certificate` before cert-manager runs)
fail the dry run because the CRD does not exist yet. Add `SkipDryRunOnMissingResource=true` to the
directory app's `syncOptions`; the automatic retries pick them up once the CRD is there.

## 3. Namespaces and Pod Security

Talos enforces the Pod Security `baseline` level in every namespace. Most apps run with it. Node
agents, storage drivers and anything with `hostPath`, `hostNetwork` or `privileged` need a labeled
namespace. Put it into the directory app and drop `CreateNamespace=true` from the chart app:

```yaml
# gitops/manifests/storage/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: storage
  labels:
    pod-security.kubernetes.io/enforce: privileged
```

Only the namespaces that need it. Rejected pods: [debugging Kubernetes](debugging-kubernetes.md#3a-pod-security).

## 4. Roll out

```bash
talkub lint                                   # yamllint, kubeconform on gitops/, helm template per chart app
talkub check                                  # no plaintext Secrets, SOPS files encrypted
git add gitops && git commit -m "feat: podinfo" && git push
talkub status                                 # Argo CD apps: Synced/Healthy, or the error of a failing app
talkub versions                               # TARGET vs. RUNNING per chart app
```

The pre-commit hook runs `check` and `lint --offline` on every commit; the full `talkub lint` also
renders every chart, so run it before pushing a chart change. An app that does not show up or
stays `OutOfSync`: [debugging Kubernetes](debugging-kubernetes.md#4-argo-cd).

## 5. Updates

**Values**: edit `gitops/values/<app>.yaml`, `talkub lint`, commit, push.

**Chart version**:

```bash
talkub versions                               # `*` marks apps with a newer chart
talkub chart upgrade podinfo                  # latest; shows appVersion and a diff of the chart's default values
talkub chart upgrade podinfo --version 6.8.0  # a specific version
talkub lint && git commit -am "chore: podinfo 6.8.0" && git push
```

Read the chart's changelog for major versions: a changed default in the diff often means a value
you set has moved or been renamed.

**Renovate** opens the version bumps as PRs (`talkub renovate init`, then enable the Renovate app
for the repo). Argo CD deploys a merged PR right away, so run `talkub lint` on the PR branch
**before** merging. Order and rules for Talos and Kubernetes: [upgrading](upgrading.md).

**Roll back**: `git revert <commit>` and push. Argo CD returns to the previous chart version and
values; data migrations the new version ran are not undone.

## 6. Reach the app

| Need                                | How                                                                                                                                                                                   |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Now and then, from your machine     | a `portForward` shortcut: `podinfo: { portForward: { namespace: podinfo, service: podinfo, ports: ["9898:9898"] } }`, then `talkub run podinfo`                                       |
| Permanently, for the tailnet        | `talkub tailscale expose podinfo --service podinfo/podinfo --port 9898` → `https://podinfo.<tailnet>` (needs the [Tailscale operator](tailscale.md#2b-ingress-an-app-in-the-tailnet)) |
| Chart with its own ingress settings | configure the chart's `ingress` values with `ingressClassName: tailscale` ([Tailscale](tailscale.md#2b-ingress-an-app-in-the-tailnet))                                                |
| Public                              | an ingress controller as a chart app, and nodes with `--public-ingress` for ports 80/443                                                                                              |

## 7. Remove an app

Delete its files and push:

```bash
git rm gitops/apps/podinfo.yaml gitops/apps/podinfo-config.yaml gitops/values/podinfo.yaml
git rm -r gitops/manifests/podinfo
git commit -m "chore: remove podinfo" && git push
```

The root app prunes the Application objects. What happens to the app's resources depends on the
finalizer from [1](#1-a-helm-chart):

- **with** `resources-finalizer.argocd.argoproj.io`: Argo CD deletes the resources first, then the
  Application.
- **without** it (the apps `init` and `tailscale operator enable` write have none): only the
  Application disappears; Deployments, Services and so on keep running, unmanaged. Delete them by
  hand, e.g. `talkub k delete ns podinfo`, or add the finalizer and push _before_ removing the file.

Either way, some things stay: a namespace created by `CreateNamespace=true`, and PVCs of
StatefulSets (with their volumes). Delete them yourself once you no longer need the data:
`talkub k delete ns podinfo`.

Do not remove `argocd`, `sealed-secrets` or `coredns` this way: the cluster depends on them.

## Troubleshooting

| Symptom                                                    | Cause and fix                                                                                            |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| App does not appear in Argo CD                             | Not pushed, or `root` fails: `talkub status`, [debugging Kubernetes](debugging-kubernetes.md#4-argo-cd). |
| `ComparisonError`, values file not found                   | The `$values/…` path or the `ref: values` source's `repoURL` does not match the repo.                    |
| `talkub versions` shows `not deployed`                     | Argo CD has not created the Application; usually `root` cannot sync.                                     |
| `lint`: skipped value file warning                         | A value file path without `$values/`; lint and `chart upgrade` cannot render that app.                   |
| CR sync fails with `could not find the requested resource` | CRD not installed yet: `SkipDryRunOnMissingResource=true` ([2](#2-plain-manifests)).                     |
| Pods rejected: `violates PodSecurity "baseline"`           | [3](#3-namespaces-and-pod-security).                                                                     |
| Change made with `kubectl` is reverted                     | `selfHeal`: change it in git instead.                                                                    |
| Resources still running after the app file was removed     | No finalizer ([7](#7-remove-an-app)): delete them by hand.                                               |
