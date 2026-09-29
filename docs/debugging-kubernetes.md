# Debugging Kubernetes

This guide is for the Kubernetes side of a talkub cluster: pods that do not start, Argo CD apps that
do not sync, Sealed Secrets that do not unseal, DNS and Services, storage (including a new disk)
and nodes that are `NotReady`. When the cause is the node itself (Talos services, boot, disks,
etcd), continue in [debugging Talos](debugging-talos.md).

The examples use cluster `homelab`, nodes `cp-01` and `w-01` and an app `podinfo` in namespace
`podinfo`.

## 0. Tools

`talkub k` (alias of `talkub kubectl`) runs kubectl with the decrypted kubeconfig; every argument is
passed through. For many commands in a row, or for other tools (`k9s`, `stern`, `helm`), open
`talkub shell`: it sets `KUBECONFIG` and `TALOSCONFIG` and removes both files on exit. Tools you use
in every cluster fit into `kubernetes` shortcuts (see the README), e.g. `k9s: { kubernetes: [k9s] }`.

## 1. First look

```bash
talkub status                                     # nodes, Argo CD summary and the errors of failing apps
talkub k get nodes -o wide                        # Ready?, versions, internal IPs
talkub k get pods -A | grep -v -E 'Running|Completed'
talkub k get events -A --sort-by=.lastTimestamp | tail -30
```

Events are the fastest way to the cause: scheduling failures, image pulls, probes, volume mounts
and evictions all end up there. They are kept for one hour only.

## 2. Gathering information

| Question                          | Command (`talkub k …`)                                                         |
| --------------------------------- | ------------------------------------------------------------------------------ |
| Why is this pod in this state?    | `-n podinfo describe pod <pod>` (the _Events_ at the bottom)                   |
| Logs                              | `-n podinfo logs <pod>` (`-c <container>`, `-f`, `--since 10m`)                |
| Logs of the crashed run           | `-n podinfo logs <pod> --previous`                                             |
| Logs of all pods of a Deployment  | `-n podinfo logs deploy/podinfo --all-containers`                              |
| Everything in a namespace         | `-n podinfo get all,pvc,ingress,sealedsecret`                                  |
| The live object                   | `-n podinfo get deploy podinfo -o yaml` (`status` and `conditions` at the end) |
| Who owns this pod?                | `-n podinfo get pod <pod> -o jsonpath='{.metadata.ownerReferences}'`           |
| Node state                        | `describe node w-01` (_Conditions_, _Allocated resources_, _Events_)           |
| API server health                 | `get --raw='/readyz?verbose'`                                                  |
| Permissions                       | `auth can-i --list --as=system:serviceaccount:podinfo:default -n podinfo`      |
| A shell in a running pod          | `-n podinfo exec -it <pod> -- sh`                                              |
| A shell next to a pod without one | `-n podinfo debug -it <pod> --image=busybox:1.36 --target=<container>`         |

`kubectl top` needs metrics-server, which a new talkub cluster does not have. `kubectl debug node/…`
creates a privileged pod that Pod Security rejects in most namespaces; on Talos, the node side is
better inspected with `talkub t` (see the Talos guide).

## 3. Pods

| State                               | Typical cause                                                         | Look at / fix                                                                                           |
| ----------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `Pending`                           | no node fits: resources, `nodeSelector`/affinity, taints, unbound PVC | `describe pod` → `FailedScheduling` names the reason                                                    |
| `ContainerCreating` for minutes     | volume cannot be mounted, missing Secret/ConfigMap                    | `describe pod` → `FailedMount`                                                                          |
| `CreateContainerConfigError`        | referenced Secret, ConfigMap or key does not exist                    | `describe pod`; for a SealedSecret see [5](#5-sealed-secrets)                                           |
| `ErrImagePull` / `ImagePullBackOff` | wrong image or tag, private registry without `imagePullSecrets`       | `describe pod` shows the registry's answer                                                              |
| `CrashLoopBackOff`                  | the process exits                                                     | `logs --previous`; exit code under _Last State_ in `describe pod`                                       |
| `OOMKilled` (in _Last State_)       | memory limit too low                                                  | raise `resources.limits.memory` in `gitops/values/<app>.yaml`                                           |
| `Running` but not `Ready`           | readiness probe fails                                                 | `describe pod` → `Unhealthy` events, then the probe's endpoint                                          |
| `Evicted`                           | node under `DiskPressure`/`MemoryPressure`                            | `describe node`; disk: [debugging Talos, disk full](debugging-talos.md#7-disk-full-ephemeral)           |
| `Terminating` forever               | a finalizer or an unreachable node                                    | `get pod -o yaml` → `finalizers`; `delete pod --grace-period=0 --force` only if the node is really gone |
| Rejected: `violates PodSecurity`    | the namespace enforces `baseline`, the pod needs more                 | [3a](#3a-pod-security)                                                                                  |

Fix the cause in the repo (values, manifests), not with `kubectl edit`: Argo CD runs with
`selfHeal`, so a live edit of a managed object is reverted within minutes. A live edit is fine for
a quick experiment as long as you expect it to disappear.

### 3a. Pod Security

Talos enables the Pod Security admission with `baseline` as the default for every namespace. That
rejects pods with `hostPath` volumes, `hostNetwork`, `privileged` and some capabilities. The error
names the violated rule. If the workload really needs it (node agents, storage drivers, the
Tailscale operator), label its namespace in the repo:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: storage
  labels:
    pod-security.kubernetes.io/enforce: privileged
```

Keep this to the namespaces that need it.

## 4. Argo CD

```bash
talkub status                                         # sync/health summary and each failing app's error
talkub k -n argocd get applications                   # SYNC STATUS and HEALTH per app
talkub k -n argocd describe application podinfo       # conditions, last sync result, resources
```

| State                        | Meaning / fix                                                                                                                |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `Unknown`, `ComparisonError` | Argo CD cannot render the app: wrong chart version/repo, broken values, repo not reachable. The condition message says which |
| `OutOfSync`, not changing    | a sync fails: `describe application` → _Operation State_; often an invalid manifest or a missing CRD                         |
| `Synced` but `Degraded`      | deployed, but a resource is unhealthy: find it with `describe application` → _Resources_, then [3](#3-pods)                  |
| `Progressing` for long       | rollout waits for pods that do not become ready ([3](#3-pods))                                                               |
| app missing entirely         | the root app has not picked up `gitops/apps/<app>.yaml`: not pushed, or `root` itself fails                                  |

Things to check, in this order:

1. **Pushed?** Argo CD reads the remote, not your working tree: `git status`, `git log origin/main..`.
2. **Lint** catches most rendering errors before they reach Argo CD: `talkub lint`.
3. **Repo access** (`authentication required`, `repository not found` on `root` or every app): the
   deploy key or token is missing or revoked. `talkub repo credentials`, then `talkub repo apply`.
4. **Force a refresh** after fixing something outside git (credentials, a chart repo):
   `talkub k -n argocd annotate application podinfo argocd.argoproj.io/refresh=hard --overwrite`.
5. **Argo CD's own logs**: `talkub k -n argocd logs deploy/argocd-repo-server` for rendering and git
   errors, `talkub k -n argocd logs statefulset/argocd-application-controller` for sync errors.

A bad change is undone with `git revert` and a push. `prune: true` removes resources that were
deleted from the repo; an app whose file is removed from `gitops/apps` is deleted with its
resources.

## 5. Sealed Secrets

A SealedSecret that does not become a Secret shows it in its status:

```bash
talkub k -n podinfo get sealedsecret
talkub k -n podinfo get sealedsecret db -o jsonpath='{.status.conditions}'
talkub k -n kube-system logs deploy/sealed-secrets-controller
```

- **`no key could decrypt secret`**: it was sealed with another controller key. Either it was
  sealed for another cluster, or the controller got a new key (a reinstall without the backup). On a
  rebuilt cluster, `talkub bootstrap` restores the key from `bootstrap/sealed-secrets-key.sops.yaml`;
  if that file is missing, seal the secret again with `talkub secret seal`.
- **Wrong name or namespace**: a SealedSecret is bound to the name and namespace it was sealed for.
  Seal it again with the right `-n` and name.
- **Secret exists but the pod does not see a change**: most apps read secrets only at start;
  `talkub k -n podinfo rollout restart deploy/podinfo`.

To read a value while debugging, use a `secret` shortcut (`talkub run --print …`) rather than
`get secret -o yaml`, which leaves it in your terminal scrollback.

## 6. Storage and the new disk

### 6a. PVC stays `Pending`

```bash
talkub k -n podinfo describe pvc data             # the events name the reason
talkub k get storageclass                         # is there one, is it the default?
```

- **no StorageClass / no default**: a new talkub cluster has no storage provisioner. Name a
  `storageClassName` in the PVC, or install one (below).
- **`WaitForFirstConsumer`**: normal; the PVC binds once a pod using it is scheduled.
- **no matching PersistentVolume** with static local PVs: size, access mode or `storageClassName`
  do not match any free PV.

### 6b. Using the new disk from pods

After [debugging Talos, a new disk](debugging-talos.md#6-a-new-disk), the volume is mounted at
`/var/mnt/<name>` on that node. kubelet can use paths below `/var/mnt` without extra mounts. Three
ways to hand it to pods, from simplest to most flexible:

**hostPath**, for a single pod on that node. Needs a `privileged` namespace ([3a](#3a-pod-security))
and a `nodeSelector` on the node:

```yaml
volumes:
  - name: data
    hostPath:
      path: /var/mnt/data/podinfo
      type: DirectoryOrCreate
```

**Local PersistentVolume**, for regular PVCs without a provisioner. Works with the default
`baseline` Pod Security. One PV per directory, pinned to the node:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-data
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: w-01-data-podinfo
spec:
  capacity:
    storage: 50Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-data
  local:
    path: /var/mnt/data/podinfo # the directory must exist
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [w-01]
```

The directory must exist before the pod starts, e.g. created once by an init container of a
privileged helper, or use the whole volume as `path: /var/mnt/data`.

**A provisioner**, when PVCs should just work: local-path-provisioner or OpenEBS LocalPV (pointed at
`/var/mnt/<name>`) for node-local volumes, Rook/Ceph on `raw` volumes for replicated storage. Add
it as a chart app like any other (`gitops/apps/`, `gitops/values/`); its namespace usually needs
`privileged` ([3a](#3a-pod-security)).

Put the manifests under `gitops/manifests/`, run `talkub lint`, commit and push.

### 6c. Pods lose data or the volume is empty

- The pod runs on another node than the disk: local volumes only exist on one node. Pin the pod
  (`nodeSelector`) or use local PVs, whose `nodeAffinity` does that for you.
- The Talos volume is not mounted (not `ready`): the pod then writes into the directory on
  EPHEMERAL. Check `talkub t -n <address> get mountstatus`.

## 7. Networking and DNS

```bash
talkub k run -it --rm dnstest --image=busybox:1.36 --restart=Never -- nslookup kubernetes.default
talkub k -n podinfo get endpointslices -l kubernetes.io/service-name=podinfo
talkub k -n podinfo port-forward svc/podinfo 9898:9898          # bypass Ingress and DNS
```

- **DNS fails in pods**: `talkub k -n kube-system get pods -l app.kubernetes.io/name=coredns` and
  their logs. After `talkub coredns takeover`, CoreDNS is the Argo CD app `coredns`; an interrupted
  takeover is finished with `talkub coredns takeover --resume`.
- **Service has no endpoints**: its `selector` does not match the pod labels, or no pod is `Ready`.
- **Port-forward works, Ingress does not**: the problem is in front of the Service (Ingress,
  Tailscale proxy, certificates). For Tailscale ingress/egress see the README's Tailscale operator
  section.
- **Pods cannot reach anything, nodes `NotReady`**: the CNI (Flannel on Talos by default) is not
  running: `talkub k -n kube-system get pods -o wide | grep -i flannel` and its logs.

## 8. Nodes `NotReady` and pressure

```bash
talkub k describe node w-01                       # Conditions: Ready, DiskPressure, MemoryPressure, PIDPressure
```

| Condition                        | Look at                                                                                       |
| -------------------------------- | --------------------------------------------------------------------------------------------- |
| `Ready=Unknown` (no heartbeat)   | the node or kubelet is down: [debugging Talos](debugging-talos.md) (`status`, `logs kubelet`) |
| `Ready=False`, network not ready | CNI ([7](#7-networking-and-dns))                                                              |
| `DiskPressure`                   | EPHEMERAL full: [debugging Talos, disk full](debugging-talos.md#7-disk-full-ephemeral)        |
| `MemoryPressure`                 | pods without memory limits; `describe node` → _Allocated resources_                           |
| `SchedulingDisabled`             | still cordoned after a drain: `talkub k uncordon w-01`                                        |

## 9. Control plane

If `talkub k` itself hangs or fails:

```bash
talkub k get --raw='/readyz?verbose'              # which check fails
talkub t -n 10.0.0.10 containers -k               # kube-apiserver, controller-manager, scheduler
talkub t -n 10.0.0.10 logs -k <container id>      # logs of one of them
```

- **`Unable to connect to the server`**: the kubeconfig points at an address you cannot reach (after
  `tailscale lockdown` only via the tailnet), or the API server is down. `talkub t … health` tells
  them apart.
- **`etcdserver: request timed out`**: etcd, see [debugging Talos, etcd](debugging-talos.md#8-etcd).
- **`x509` / `Unauthorized`**: the kubeconfig in the repo is outdated. `talkub kubeconfig` fetches
  a fresh one from a control plane; commit `kubernetes/` afterwards.

## Troubleshooting

| Symptom                                          | Where to look / fix                                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Change in git does not show up                   | pushed?, `talkub status`, hard refresh ([4](#4-argo-cd))                                    |
| Live fix is reverted after a few minutes         | `selfHeal`: make the change in the repo                                                     |
| App `Unknown` / `ComparisonError`                | `describe application`; `talkub lint`; repo credentials                                     |
| Pod `Pending`                                    | `describe pod` → `FailedScheduling`; PVC without StorageClass ([6a](#6a-pvc-stays-pending)) |
| Pod rejected with `violates PodSecurity`         | [3a](#3a-pod-security)                                                                      |
| `CreateContainerConfigError` with a SealedSecret | [5](#5-sealed-secrets)                                                                      |
| New disk not usable from pods                    | Talos volume `ready`? then [6b](#6b-using-the-new-disk-from-pods)                           |
| Pods evicted, node `DiskPressure`                | [debugging Talos, disk full](debugging-talos.md#7-disk-full-ephemeral)                      |
| DNS fails inside pods                            | CoreDNS pods and logs ([7](#7-networking-and-dns))                                          |
| `kubectl` hangs                                  | [9](#9-control-plane)                                                                       |
