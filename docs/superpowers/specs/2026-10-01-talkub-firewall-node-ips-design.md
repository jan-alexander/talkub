# Firewall rules from node IPs instead of a node network

Date: 2026-10-01
Status: approved

## Problem

`talkub tailscale lockdown` writes `talos/patches/firewall.yaml` with the rules `cluster-internal`
(50000, 50001, 6443, 2379-2380, 10250/tcp) and `flannel-vxlan` (4789/udp) allowed from a single
network `tailscale.nodeCidr`, which `tailscale enable` defaults to the `/24` around the first node's
`ip`. On a hosted server (Hetzner and others) that `/24` is shared with strangers: every machine in
it may reach etcd, the kubelet and the Talos/Kubernetes APIs of the cluster. The only honest use
for a whole network would be nodes whose IP changes (DHCP without reservation), and talkub already
assumes stable node addresses everywhere else (`nodes.<name>.ip`, `--static`, DHCP reservations).

## Decision

`nodeCidr` is removed. The cluster-internal rules list the nodes' own addresses, one `/32`
(`/128` for IPv6) per node from `nodes.<name>.ip` in `.talkub.yaml`, plus the pod and service
networks as before. No `--node-cidr` on `init`, `tailscale enable` or `tailscale lockdown`; no
"outside the node network" check.

## Behavior

- `render_firewall_patch(sources)` renders the patch for a list of source subnets. The header says
  the file is written by `talkub tailscale lockdown`, rewritten by `talkub tailscale firewall` and
  `talkub node add`, and that extra rules belong into `talos/nodes/<node>.yaml` or another patch,
  not here (hand edits are overwritten).
- `node_sources(settings)` derives the subnets from the nodes. A node whose `ip` is a host name is
  an error (`ValidationError`, hint: set `nodes.<name>.ip` to the node's IP address; `mgmt` may stay
  a name), because a firewall rule needs an address.
- `tailscale lockdown` renders from `node_sources`; it fails before writing anything if a node has
  no IP address. The lockdown note says the APIs are open only to the tailnet and the nodes' own
  addresses.
- New command `talkub tailscale firewall`: rewrites `talos/patches/firewall.yaml` from the current
  settings. Refuses when Tailscale is not enabled or the cluster is not locked down (hint: run
  `lockdown`). Notes: the file was rewritten for the listed node addresses; `talkub apply`
  activates it. Use after editing a node's `ip`, and once to migrate a repo written by an older
  talkub (whose rules still allow the old `nodeCidr`).
- `talkub node add` on a locked-down cluster rewrites the firewall patch with the new node (the
  node's `--ip` must be an IP address then) and warns that the existing nodes block cluster traffic
  from the new address until `talkub apply` has run on them. Without lockdown nothing changes.
- `.talkub.yaml`: `tailscale.nodeCidr` is ignored when read (like the legacy `charts` block) and no
  longer written. `tailscale` needs only `tailnet` (`useIps` optional).
- `TailscaleSpec(tailnet, use_ips=False)`.

## Out of scope

- Any other rule in the firewall (admin-via-tailnet, pod/service networks, `--public-ingress`).
- Nodes whose address changes under them: documented as unsupported, as the rest of talkub assumes.
- A version bump or release.

## Documentation

`docs/tailscale.md` (1a, 1c table, 1e, 1f, troubleshooting), `docs/nodes.md` (node add after
lockdown, change an address), `docs/reference.md` (Tailscale commands, version skew note for the
dropped key). README needs no change (it never mentioned the node network).

## Tests

- Render: two nodes (IPv4 and IPv6) give `/32` and `/128` entries plus pod and service networks;
  the talosctl validation test renders from node sources.
- `node_sources`: host name `ip` raises with the hint naming the node.
- Flows: `enable` writes no `nodeCidr`; `lockdown` renders the nodes' addresses and refuses a
  host-name node before writing; `firewall` refuses without lockdown, rewrites after it and
  returns the apply note.
- `node add` after lockdown: firewall contains the new address, warning mentions `apply`; host
  name `--ip` is refused; without lockdown nothing is rewritten.
- Settings: legacy `nodeCidr` is read without error and gone after a write; `tailscale` without
  `tailnet` is still rejected.
- CLI: `tailscale firewall` runs the flow; `init`'s failure hint no longer carries `--node-cidr`;
  `lockdown --use-ips` passes only `use_ips`.
