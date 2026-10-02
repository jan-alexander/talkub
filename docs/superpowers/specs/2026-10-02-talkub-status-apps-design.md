# `talkub status --apps`: per-application detail, parallel and live

**Date:** 2026-10-02
**Status:** implemented

## Goal

`talkub status` used to print the node table and one aggregated Argo CD line
(`argocd: 12 Synced/Healthy, 1 OutOfSync/Progressing`). That tells you *that* something is
progressing or unhealthy, not *which* app or why. The command also ran every lookup sequentially.

Two changes, one feature:

1. A flag `--apps` (`-a`) that adds a table with one line per Argo CD Application.
2. `collect` looks nodes and the Kubernetes side up concurrently and reports progress through
   callbacks, so the CLI can drive a `rich.live` view the way `talkub versions` does.

## Data

Everything per app comes from the single `kubectl get applications.argoproj.io -n argocd -o json`
call `status` already makes; there is nothing to fetch per app. `talos/inspect.application_details`
parses it into `ApplicationDetail` records:

| field | source |
|---|---|
| `sync` | `status.sync.status` (`Unknown` if missing) |
| `health` | `status.health.status` (`Unknown` if missing) |
| `operation` | `status.operationState.phase`, `-` if no operation ran |
| `revision` | `status.sync.revision`, or `status.sync.revisions` comma-joined; 40-hex SHAs cut to 7 |
| `errors` | `<type>: <message>` per `*Error` condition (shared with `application_errors`) |
| `resources` | `Kind/name: <health> (<message>)` per `status.resources[]` entry whose health is not Healthy |

## Collecting

`overview/status.collect(ctx, *, on_step, on_node, on_kube)`:

- `prepare` renders the machine configs first (sequential, as before).
- One Talos session is opened once; a `ThreadPoolExecutor` runs one worker per node (`version`,
  `get extensions`, `apply-config --dry-run`, up to `MAX_WORKERS` at a time) plus one worker for the
  Kubernetes side (`get nodes`, `get applications`), which has its own `kube_session`.
- `on_step(subject, step)` fires before every call; `subject` is the node name or `"kubernetes"`.
  `on_node(NodeStatus)` fires when a node's Talos cells are complete (kubelet still unknown),
  `on_kube(KubeStatus)` once with kubelet versions, the summary, the error list and the details.
- The returned `StatusReport` is unchanged for existing callers, plus `application_details`; the
  kubelet versions are merged into the nodes after both sides finished.

Error handling is unchanged: an unreachable node is `unreachable`, a failing or undecryptable
kubeconfig gives `kube="unavailable"`, a missing kubeconfig `not bootstrapped`.

## Rendering

The overview layer owns the cell contents (`node_cells`, `app_cells`, `argocd_line`) so the plain
text renderer and the rich one cannot drift. `render(report, apps=True)` prints the app table
(`APP SYNC HEALTH OPERATION REVISION NOTE`, NOTE joined with `; `) instead of the per-app error
lines; without the flag the output is exactly what it was.

`cli/status.py` on a terminal: a transient `Live` with the node table (every node listed up front,
pending cells dimmed, spinner plus the running call while a node is being looked up, KUBELET
pending until the Kubernetes side is in) and an `argocd:` line with a spinner until the kubectl
calls return. Then the final result is printed: mismatches and drift in bold yellow; app states
coloured (OutOfSync/Progressing/Running/Unknown yellow, Degraded/Missing/Failed/Error red). On a
pipe, the plain text renderer runs once at the end, as `versions` does.

## Out of scope

Per-pod detail (would need extra kubectl calls), filtering the app table, and `--only`.
