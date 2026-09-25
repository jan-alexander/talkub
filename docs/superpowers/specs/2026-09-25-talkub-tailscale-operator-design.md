# talkub – CoreDNS über ArgoCD und Tailscale Kubernetes Operator – Design

Stand: 2026-09-25. Ergänzt die bisherigen Specs (Phase 2 §5 Tailscale, §13 „Operator später“).

## 1. Ziel

Pods sollen Dienste im Tailnet unter ihren echten MagicDNS-Namen erreichen (Egress + Tailnet-DNS), und
Cluster-Dienste sollen sich ins Tailnet veröffentlichen lassen (Ingress). Die Nodes bleiben bei
`TS_ACCEPT_DNS=false`. API-Server-Proxy ist nicht Teil davon.

Erfolg: nach `talkub coredns takeover` und `talkub tailscale operator enable` + commit/push löst ein Pod
`<host>.<tailnet>` für ein per `talkub tailscale egress add` angelegtes Ziel auf und erreicht es; ein
Service mit Tailscale-Annotation erscheint als Gerät im Tailnet.

## 2. Beobachtungen (echter Cluster / Charts, 2026-09-25)

- Die ConfigMap `kube-system/coredns` gehört Talos (managedFields: `talos` / Apply, Manifest `11-core-dns`);
  Corefile ohne `import`. Ein Eingriff per ArgoCD würde mit Talos kollidieren → CoreDNS muss von Talos zu
  ArgoCD wechseln.
- `kube-dns`-Service: ClusterIP `10.96.0.10`, Selector `{k8s-app: kube-dns}`; Talos-Deployment `coredns`
  mit demselben Selector.
- Chart `coredns` (https://coredns.github.io/helm) 1.47.1: `fullnameOverride`, `k8sAppLabelOverride`,
  `service.name`, `service.clusterIP`, `service.selector` (überschreibt den Standard-Selector),
  `servers` (Corefile-Blöcke), `isClusterService: true`.
- Chart `tailscale-operator` (https://pkgs.tailscale.com/helmcharts) 1.102.4: ohne `oauth.*` wird das
  Secret `operator-oauth` erwartet; `installCRDs: true`; `operatorConfig.defaultTags`,
  `proxyConfig.defaultTags` (`tag:k8s`), `apiServerProxyConfig.mode` (`"false"`).
- `DNSConfig.spec.nameserver.service.clusterIP` setzt die Nameserver-IP fest → CoreDNS-Stub-Zone kann statisch
  im Repo stehen.

## 3. Datenmodell

- `.talkub.yaml` `charts` bekommt bei Bedarf `coredns` und `tailscale-operator` (Version beim Anlegen per
  `charts.latest_version` ermittelt und gepinnt, wie argocd/sealed-secrets).
- Konstanten: `CLUSTER_DNS_IP = "10.96.0.10"`, `TS_NAMESERVER_IP = "10.96.0.53"` (unteres, für statische
  Zuweisung reserviertes Band des Default-Service-CIDR `10.96.0.0/12`).

## 4. CoreDNS über ArgoCD

### Repo-Dateien (Renderer in `src/talkub/coredns.py`)

- `talos/patches/coredns.yaml`: `apiVersion: v1alpha1`, `kind: KubeCoreDNSConfig`, `enabled: false`.
- `gitops/apps/coredns.yaml`: Application `coredns`, Multi-Source wie argocd (Chart + `$values`), Namespace
  `kube-system`, `ServerSideApply=true`, automated sync (prune, selfHeal).
- `gitops/values/coredns.yaml`:
  - `fullnameOverride: coredns-talkub`, `k8sAppLabelOverride: kube-dns`, `replicaCount: 2`,
    `priorityClassName: system-cluster-critical`, Tolerations für `node-role.kubernetes.io/control-plane`.
  - `service: {name: kube-dns, clusterIP: 10.96.0.10, selector: {k8s-app: kube-dns}}` — derselbe Selector wie
    Talos' Pods, damit während der Übernahme alte und neue Pods bedient werden.
  - `servers`: Talos' Standard-Corefile als Chart-`servers`-Block (errors, health lameduck 5s, ready,
    log class error, prometheus :9153, kubernetes cluster.local in-addr.arpa ip6.arpa mit pods insecure,
    fallthrough, ttl 30; forward . /etc/resolv.conf max_concurrent 1000; cache 30 mit disable success/denial
    cluster.local; loop; reload; loadbalance).
  - Mit Operator zusätzlich ein Block `zones: [{zone: <tailnet>}]`, `port: 53`, Plugins errors, cache 30,
    `forward . 10.96.0.53`.

### `talkub coredns takeover` (laufender Cluster)

1. Vorprüfung: bootstrapped, `kube-dns` hat ClusterIP `10.96.0.10`, `gitops/apps/coredns.yaml` noch nicht
   vorhanden (sonst „already taken over“), Charts-Version auflösbar.
2. Repo-Dateien schreiben (Patch, App, Values, Chart in `.talkub.yaml`).
3. `talkub apply` für alle Nodes (Talos wendet 11-core-dns nicht mehr an). Danach prüfen: Deployment
   `kube-system/coredns` existiert noch und hat bereite Pods; sonst Warnung und sofort weiter mit 4.
4. `kube-dns`-Service für Helm übernehmen: Annotations `meta.helm.sh/release-name: coredns`,
   `meta.helm.sh/release-namespace: kube-system`, Label `app.kubernetes.io/managed-by: Helm`.
5. `helm upgrade --install coredns coredns --repo … --version … -n kube-system -f gitops/values/coredns.yaml`
   (wie `bootstrap._helm_install`), dann `kubectl -n kube-system rollout status deploy/coredns-talkub`.
6. Alte Talos-Objekte löschen (nur diese Namen, jeweils `--ignore-not-found`): Deployment `coredns`,
   ConfigMap `coredns`, ServiceAccount `coredns`, ClusterRoleBinding/ClusterRole `system:coredns`.
7. Abschluss: Endpoints von `kube-dns` enthalten nur noch Pods von `coredns-talkub`; Hinweis „commit, push —
   ArgoCD manages CoreDNS from now on“.
Fehler in 4–6: klare Meldung, welcher Schritt; der Zustand bleibt bedienbar (alte oder neue Pods hinter
dem Service). Wiederholbar: 4–6 sind idempotent; 1 lässt `--resume` zu, wenn die Repo-Dateien existieren,
der Helm-Release aber fehlt.

### Neue Cluster

- `init` schreibt `talos/patches/coredns.yaml`, `gitops/apps/coredns.yaml`, `gitops/values/coredns.yaml`
  und den Chart gleich mit.
- `bootstrap` installiert CoreDNS per Helm als erstes Release (vor sealed-secrets und argocd), wenn
  `charts.coredns` existiert; bestehende Cluster ohne Takeover bleiben unverändert.

## 5. Tailscale Operator

### `talkub tailscale operator enable [--client-id <id>] [--client-secret-file <path>]`

Vorbedingungen: `tailscale` in `.talkub.yaml` (Tailnet bekannt), CoreDNS übernommen
(`gitops/apps/coredns.yaml` existiert), `bootstrap/sealed-secrets.pem` existiert.

- Client-ID per Option oder sichtbarem Prompt, Secret per `--client-secret-file` oder verstecktem Prompt
  (`cli/prompts.read_secret`).
- Offline versiegeln: Secret `tailscale/operator-oauth` mit `client_id`, `client_secret`, im Speicher gebaut
  (`sealing.secret_manifest`), `kubeseal --cert bootstrap/sealed-secrets.pem`, geschrieben nach
  `gitops/manifests/tailscale/operator-oauth.sealed.yaml`.
- `gitops/apps/tailscale-operator.yaml`: Helm-App (Multi-Source), Namespace `tailscale`, `CreateNamespace=true`,
  `ServerSideApply=true`.
- `gitops/values/tailscale-operator.yaml`: `operatorConfig.hostname: <cluster>-operator`,
  `operatorConfig.defaultTags: [tag:k8s-operator]`, `proxyConfig.defaultTags: tag:k8s`,
  `apiServerProxyConfig.mode: "false"`, `installCRDs: true`.
- `gitops/apps/tailscale-operator-config.yaml`: Verzeichnis-App auf `gitops/manifests/tailscale`
  (`recurse: true`), `SkipDryRunOnMissingResource=true` (CRDs kommen vom Operator-Chart).
- `gitops/manifests/tailscale/dnsconfig.yaml`: `DNSConfig` `ts-dns`, `spec.nameserver.service.clusterIP: 10.96.0.53`.
- CoreDNS-Values: Stub-Block für `<tailnet>` ergänzen (idempotent).
- Ausgabe: ACL-Ausschnitt `"tagOwners": {"tag:k8s-operator": [], "tag:k8s": ["tag:k8s-operator"]}`,
  benötigte OAuth-Scopes (Devices Core, Auth Keys, Services — write) mit Tag `tag:k8s-operator`,
  „commit, push“.
- Erneuter Aufruf: Fehler „already enabled“, außer `--rotate-credentials` (nur das SealedSecret neu).

### `talkub tailscale egress add <name> --fqdn <host>.<tailnet> [--namespace <ns>]`

- `name` DNS-1123-Label, `fqdn` muss auf `.<tailnet>` enden (oder `--ip <tailnet-ip>` statt `--fqdn`).
- Schreibt `gitops/manifests/tailscale/egress/<name>.yaml`: Service `type: ExternalName`,
  `externalName: placeholder`, Annotation `tailscale.com/tailnet-fqdn` (bzw. `tailscale.com/tailnet-ip`),
  Namespace default `tailscale` (anderer Namespace muss existieren; Hinweis).
- `egress remove <name>` löscht die Datei.
- Pods erreichen das Ziel unter `<host>.<tailnet>` (CoreDNS → Operator-Nameserver) oder
  `<name>.<ns>.svc.cluster.local`.

### Ingress

Nur Dokumentation (README): Service mit `loadBalancerClass: tailscale` oder Annotation `tailscale.com/expose`,
Ingress mit `ingressClassName: tailscale`; für MagicDNS-Namen aus dem Cluster
`tailscale.com/experimental-forward-cluster-traffic-via-ingress` laut Operator-Doku.

## 6. Architektur

- `src/talkub/coredns.py`: Renderer (Patch, App, Values inkl. Stub-Block), `takeover(ctx, resume=False)`.
- `src/talkub/tailscale_operator.py`: `enable(...)`, `egress_add/remove(...)`, Renderer.
- `src/talkub/sealing.py`: Offline-Siegeln mit `--cert` (falls noch nicht vorhanden) wiederverwenden.
- `src/talkub/charts.py`: `DEFAULT_CHARTS` bleibt argocd/sealed-secrets; neue Einträge über eine
  `OPTIONAL_CHARTS`-Tabelle.
- `bootstrap.py`: CoreDNS-Release zuerst, wenn konfiguriert.
- CLI: `cli/coredns.py` (`takeover`), `cli/tailscale.py` bekommt Untergruppe `operator` und `egress`.
- Templates für `init` (Patch/App/Values CoreDNS).

## 7. Tests

- Renderer: Values/Corefile-Blöcke (Talos-Standard + Stub), Apps, DNSConfig, Egress-Service; YAML gültig.
- `takeover` mit Spy-kubectl/helm/talosctl: Reihenfolge (apply → Service-Adoption → helm → rollout → delete),
  Abbruch bei falscher ClusterIP, bereits übernommen, `--resume`.
- `operator enable`: Offline-Siegeln mit Spy-kubeseal (Secret nie im Klartext auf Platte/argv), Vorbedingungen,
  Stub-Block idempotent, erneuter Aufruf.
- `egress add/remove`: Validierung von Name/FQDN/IP, Datei-Inhalt.
- `bootstrap`: CoreDNS als erstes Release, wenn `charts.coredns` gesetzt; unverändert sonst.
- `init`: neue Dateien vorhanden, `talkub check` sauber.

## 8. Am echten Cluster zu prüfen (vor Merge nicht testbar)

- Talos entfernt bei `enabled: false` die CoreDNS-Objekte nicht (Schritt 3 prüft das und reagiert).
- ArgoCD übernimmt den Helm-Release ohne Neuerstellung (gleiche Namen, SSA).
- Operator-Nameserver erhält `10.96.0.53`; Pod-Auflösung eines Egress-Ziels.
