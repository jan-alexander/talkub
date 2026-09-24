# talkub Phase 2 – Talos-Lebenszyklus, Tailscale, Schematics – Design

Datum: 2026-09-23
Status: Entwurf zur Review
Baut auf: `2026-09-23-talkub-design.md` (Phase-1-Spec, weiterhin bindend) und dem Phase-1-Code auf `main`.

## 1. Ziel

Phase 2 macht aus dem Cluster-Repo einen laufenden Cluster und hält ihn aktuell:
Nodes beschreiben, Machine-Configs rendern und anwenden, Cluster bootstrappen (inkl. Sealed Secrets und
ArgoCD), Talos/Kubernetes upgraden, Soll/Ist anzeigen, Sealed Secrets erzeugen. Dazu kommen optional
Tailscale als einziger Admin-Zugang und konfigurierbare Image-Factory-Schematics.

Erfolgskriterien:

- Vom leeren Repo bis zu synchronisierendem ArgoCD mit `init` → `apply --insecure` → `bootstrap`.
- Jede Config-Änderung ist vorher per `plan` als Talos-Dry-Run sichtbar.
- Mit `--tailscale` sind Talos-API (50000) und Kube-API (6443) von außen nur über das Tailnet erreichbar.
- Extensions stehen lesbar im Repo; das Installer-Image wird daraus abgeleitet.
- Kein Klartext-Secret auf Platte außerhalb der privaten Temp-Verzeichnisse (Phase-1-Regeln gelten).

## 2. Getroffene Entscheidungen

| Thema | Entscheidung |
|---|---|
| Plattformen | `metal` (Bare Metal) und `hcloud` (Hetzner Cloud). Plattform pro Node, Cluster-Default in `.talkub.yaml`. |
| Netzwerk | Pro Node DHCP (Default) oder statisch (`--static CIDR --gateway IP [--mac] [--dns]`). |
| Multi-Standort | Nicht in Phase 2 (KubeSpan später). Die Struktur (Plattform pro Node) lässt es zu. |
| Tailscale | Optional (`init --tailscale`, `tailscale enable`). Dann Talos-Firewall `ingress: block` mit Freigaben. |
| Öffentlicher Ingress | Pro Node `--public-ingress` → 80/443 tcp von überall. |
| Schematics | Extensions/KernelArgs pro Plattform in `.talkub.yaml`; ID per Image-Factory-API berechnet und gecacht. |
| Extensions ändern | Über `upgrade talos` (Image-Tausch), nicht über `apply`. |
| Bootstrap | Idempotente Schritte, erneut ausführbar; kein Fortschritts-State. |
| Sealed Secrets erzeugen | Bevorzugt `--from-literal/--from-file` im Speicher; Datei-Eingabe nur von außerhalb des Repos. |

## 3. Datenmodell (`.talkub.yaml`)

```yaml
name: homelab
endpoint: https://10.0.0.10:6443           # Cluster-Endpoint (Nodes); Admins gehen über mgmt
talosVersion: v1.14.0
kubernetesVersion: 1.37.0
platform: metal                      # Cluster-Default
schematics:
  metal:
    extensions: [siderolabs/tailscale, siderolabs/intel-ucode]
    kernelArgs: []
    id: 188cef51…                    # abgeleitet (Cache)
    hash: 3f9a…                      # sha256 der Customization, zu der `id` gehört
  hcloud:
    extensions: [siderolabs/tailscale]
    kernelArgs: []
    id: …
    hash: …
tailscale:                           # nur mit --tailscale
  tailnet: tail1234.ts.net
  nodeCidr: 10.0.0.0/24              # Cluster-interne Freigaben
nodes:
  cp-01:
    ip: 10.0.0.10                    # LAN/öffentlich: Erstinstallation (--insecure)
    mgmt: cp-01.tail1234.ts.net      # optional: Adresse für alles nach der Installation
    role: controlplane
    platform: metal                  # optional, sonst Cluster-Default
    arch: amd64                      # optional, amd64|arm64 (nur für Download-URLs)
charts: { … }                        # unverändert aus Phase 1
```

Regeln:

- Management-Adresse eines Nodes = `mgmt`, sonst `ip`.
- Installer-Image = `factory.talos.dev/<platform>-installer/<schematics[platform].id>:<talosVersion>`.
- Migration: Ein Phase-1-`installImage` wird beim Laden gelesen; `write_settings` schreibt nur noch
  `schematics` (für die Cluster-Plattform mit leerer Extension-Liste und der ID aus dem alten Image).
- `schematics[p].id` ist gültig, solange `hash` = sha256 der aktuell beschriebenen Customization ist;
  sonst berechnet talkub die ID neu (Netzwerk nötig) und schreibt `id`/`hash` zurück.
- Unbekannte Plattform / fehlende Schematic für eine genutzte Plattform → `ValidationError`.

## 4. Patches und Rendering

### Patch-Dateien

- `talos/patches/*.yaml` und `talos/patches/*.sops.yaml`, sortiert nach Dateiname:
  - Name beginnt mit `controlplane` → nur Control-Plane-Nodes (`--config-patch-control-plane`),
  - Name beginnt mit `worker` → nur Worker (`--config-patch-worker`),
  - sonst → alle Nodes (`--config-patch`).
- `talos/nodes/<node>.yaml` zuletzt (`--config-patch`).
- `*.sops.yaml`-Patches werden zusammen mit `secrets.sops.yaml` in **ein** `decrypted()`-Verzeichnis entschlüsselt.

### Node-Patch (`node add`, `init --node`)

Immer `HostnameConfig` (`auto: "off"`, `hostname: <node>`). Optional:

- `--static <CIDR> --gateway <IP> [--mac <MAC>] [--dns <IP>…]`:
  `LinkAliasConfig` `net0` mit Selector `mac(link.permanent_addr) == "<mac>"`; ohne `--mac` der
  Selector `link.type == 1 && link.kind == ""` (physisches Ethernet, ARPHRD_ETHER; validiert) plus
  Warnung, dass Nodes mit mehreren NICs `--mac` brauchen. Dazu `LinkConfig` `net0` (`up`, `addresses`,
  `routes`) und `ResolverConfig` (`nameservers`) nur mit `--dns`.
- `--disk-selector <CEL>`: `UnattendedInstallConfig.provisioning.diskSelector.match` (vollständiger
  `provisioning`-Block, da Talos das Dokument sonst ersetzt).
- `--public-ingress`: `NetworkRuleConfig` `public-ingress` für 80/443 tcp von `0.0.0.0/0` und `::/0`
  (nur sinnvoll mit Tailscale-Firewall; ohne Firewall ist alles ohnehin offen → Warnung).

Verifiziert mit talosctl 1.14: `HostnameConfig`, `LinkAliasConfig` (mit `mac(...)`), `LinkConfig`,
`ResolverConfig`, `UnattendedInstallConfig.provisioning`, `ExtensionServiceConfig`,
`NetworkDefaultActionConfig`, `NetworkRuleConfig`. Ein Patch, der nur `installer.image` setzt, ersetzt das
ganze `UnattendedInstallConfig` → das Image wird deshalb per `--install-image` übergeben.

### Rendering (`talos/render.py`)

Pro Node (Klartext nur im privaten Temp-Verzeichnis, Ergebnis im Speicher):

```
talosctl gen config <name> <endpoint>
  --with-secrets <tmp>/secrets.yaml
  --talos-version <talosVersion major.minor>
  --kubernetes-version <kubernetesVersion>
  --install-image <installer image>
  --additional-sans <mgmt aller Control-Plane-Nodes, falls gesetzt>
  -t <controlplane|worker> -o - --with-docs=false --with-examples=false
  <patch-Argumente gemäß Reihenfolge oben>
```

Danach `talosctl validate --mode <metal|cloud>` (hcloud → `cloud`) auf eine private Temp-Datei.
Fehler → `ValidationError` mit der talosctl-Meldung, es wird nichts gesendet.

## 5. Tailscale

- `init --tailscale --tailnet <tailnet> [--tailscale-authkey-file <f>] [--node-cidr <cidr>]` macht beide Schritte.
  Bestehende Repos in zwei Schritten (Korrektur 2026-09-24, sonst wären laufende Nodes vor der Extension
  unerreichbar): `tailscale enable --tailnet … [--authkey-file …] [--node-cidr …]` (Auth-Key-Patch +
  Extension in den Schematics, Adressen unverändert) → `upgrade talos` + `apply` über LAN →
  `tailscale lockdown [--node-cidr …]` (firewall.yaml, mgmt, Client-Configs) → `apply` über das Tailnet.
- Auth-Key: ohne `--tailscale-authkey-file` interaktiv per `getpass` (kein Echo).
- Schreibt:
  - `talos/patches/tailscale.sops.yaml`: `ExtensionServiceConfig` `tailscale` mit
    `TS_AUTHKEY=<key>` und `TS_ACCEPT_DNS=false` (Hostname kommt aus `HostnameConfig`).
  - `talos/patches/firewall.yaml` (Klartext, lesbare Diffs):
    `NetworkDefaultActionConfig` `ingress: block`;
    `NetworkRuleConfig` `admin-via-tailnet`: tcp 50000, 6443 von `100.64.0.0/10`, `fd7a:115c:a1e0::/48`;
    `NetworkRuleConfig` `cluster-internal`: tcp 50000, 50001, 6443, 2379-2380, 10250 von `nodeCidr`
    sowie Pod- und Service-Subnetz (Talos-Defaults `10.244.0.0/16`, `10.96.0.0/12`);
    `NetworkRuleConfig` `flannel-vxlan`: udp 4789 von `nodeCidr`.
- Fügt `siderolabs/tailscale` zu jeder genutzten Plattform-Schematic hinzu.
- **Endpoints (Korrektur 2026-09-24):** Der Cluster-Endpoint (`endpoint`, den Nodes/Kubelet/Join benutzen)
  bleibt die LAN-/öffentliche Adresse des ersten CP — Nodes lösen mit `TS_ACCEPT_DNS=false` keine
  `*.ts.net`-Namen auf. Nur die **Client-Seite** geht übers Tailnet: talosconfig-Endpoints = `mgmt`-Adressen
  der CPs, kubeconfig `server:` = `https://<mgmt des ersten CP>:6443` (bootstrap schreibt das beim Speichern um,
  `tailscale enable` schreibt eine vorhandene kubeconfig um). Die `mgmt`-Namen stehen per `--additional-sans`
  im API-Zertifikat. Jeder Node ohne `mgmt` bekommt `mgmt: <node>.<tailnet>` (auch neue Nodes bei `node add`).
- `nodeCidr`-Default: `/24` um die `ip` des ersten Nodes; wird angezeigt.
- Hinweis-Ausgabe: getaggten, wiederverwendbaren, vorab genehmigten Auth-Key verwenden;
  bei `tailscale enable` auf laufenden Nodes: `apply` blockt danach LAN-Zugriff auf 50000 —
  Tailscale muss nach dem Apply laufen (Image mit Extension via `upgrade talos` zuerst).

## 6. Schematics / Image Factory

- API: `POST https://factory.talos.dev/schematics` mit YAML
  `customization: {systemExtensions: {officialExtensions: [...]}, extraKernelArgs: [...]}` → `{"id": …}`.
  Liste: `GET https://factory.talos.dev/version/<talosVersion>/extensions/official`.
- HTTP über `urllib.request` (keine neue Abhängigkeit), Basis-URL per `TALKUB_FACTORY_URL` überschreibbar
  (für Tests/Mirror). Netzwerkfehler → `CommandFailed`-artiger Fehler mit Hinweis.
- Befehle:

| Befehl | Verhalten |
|---|---|
| `image extensions [--platform p]` | Offizielle Extensions der Soll-Version; aktive markiert. |
| `image add <ext…> [--platform p]` | Namen gegen offizielle Liste prüfen, eintragen, ID neu berechnen. |
| `image remove <ext…> [--platform p]` | Austragen, ID neu berechnen. |
| `image show [--platform p] [--arch a]` | Installer-Image; Boot-Medium: `metal` → `https://factory.talos.dev/image/<id>/<ver>/metal-<arch>.iso`, `hcloud` → `…/hcloud-<arch>.raw.xz`. |

Ohne `--platform` gilt die Cluster-Plattform.

## 7. Befehle

| Befehl | Verhalten |
|---|---|
| `node add <name> --ip <ip> [--role controlplane\|worker] [--platform p] [--arch a] [--mgmt addr] [Netz-/Disk-/Ingress-Optionen]` | Node-Patch schreiben, `nodes:` ergänzen; Name bereits vorhanden → Fehler. |
| `init … --node n=ip` | Wie Phase 1; die `node add`-Optionen sind nur bei genau einem `--node` erlaubt. Zusätzlich `--platform`, `--tailscale …`. |
| `plan [node…]` | Pro Node (CP vor Worker): rendern, validieren, `talosctl -n <mgmt> -e <mgmt> apply-config -f <cfg> --dry-run`, Ausgabe anzeigen. |
| `apply [node…] [--mode auto\|no-reboot\|staged\|try] [--yes]` | Wie `plan`, dann pro Node Bestätigung (außer `--yes`) und Anwenden. Fehler stoppt die Schleife, Exit ≠ 0. |
| `apply --insecure <node> [--yes]` | Erstinstallation gegen `ip` ohne talosconfig; zeigt vorher Installer-Image und Disk-Auswahl, verlangt Bestätigung. |
| `bootstrap` | Schritte in §8. |
| `upgrade talos [node…] [--yes]` | Pro Node sequenziell: Soll-Version/-Schematic vs. Ist; bei Abweichung `talosctl -n <mgmt> upgrade --image <img> --wait`, dann `health`. Fehler stoppt. |
| `upgrade k8s [--yes]` | `talosctl -n <erster CP> upgrade-k8s --to <v> --dry-run` anzeigen, bestätigen, ausführen. |
| `status` | Tabelle pro Node: erreichbar, Talos Ist/Soll, Schematic Ist/Soll (`talosctl get extensions`, Eintrag `schematic`), Config-Drift (Dry-Run), K8s-Version; danach ArgoCD-Applications nach Sync/Health. Nicht erreichbare Nodes werden markiert, kein Abbruch. |
| `secret seal -n <ns> <name> (--from-literal k=v \| --from-file k=path)… -o <out>` | Secret im Speicher (`kubectl create secret generic --dry-run=client -o yaml`) → `kubeseal --format yaml`; `<out>` muss auf `.sealed.yaml` enden. |
| `secret seal <datei\|-> -o <out>` | Nur `kind: Secret`; Eingabedatei innerhalb des Repos → Fehler mit Hinweis auf `--from-literal`. |
| `secret backup-sealing-key` | Secrets mit Label `sealedsecrets.bitnami.com/sealed-secrets-key` aus `kube-system`, Laufzeitfelder entfernt (`resourceVersion`, `uid`, `creationTimestamp`, `managedFields`, `ownerReferences`) → `bootstrap/sealed-secrets-key.sops.yaml`; Cert (`kubeseal --fetch-cert`) → `bootstrap/sealed-secrets.pem`. |
| `tailscale enable …` | §5. |
| `image …` | §6. |

Zertifikat für `secret seal`: live per `kubeseal --fetch-cert` (kubeconfig ephemer), bei Fehler
Fallback `bootstrap/sealed-secrets.pem` mit Hinweis.

## 8. Bootstrap

Jeder Schritt prüft den Ist-Zustand; erneutes Ausführen setzt nach einem Abbruch fort.

1. `talosctl -n <mgmt erster CP> -e <…> bootstrap`; Meldung „already bootstrapped/AlreadyExists“ = Erfolg.
2. `talosctl health --wait-timeout 15m` (gegen denselben Node).
3. `talosctl kubeconfig - --merge=false` → SOPS → `kubernetes/kubeconfig.sops.yaml` (überschreibt nur,
   wenn sich der Inhalt geändert hat).
4. Falls `bootstrap/sealed-secrets-key.sops.yaml` existiert: entschlüsselt per `kubectl apply -f`.
5. `helm upgrade --install sealed-secrets <repo/chart> --version <v> -n kube-system -f gitops/values/sealed-secrets.yaml --wait`.
6. `helm upgrade --install argocd <repo/chart> --version <v> -n argocd --create-namespace -f gitops/values/argocd.yaml --wait`.
7. `kubectl apply -f gitops/root.yaml`.
8. Kein Backup vorhanden → `secret backup-sealing-key`, Hinweis „jetzt committen“.

Helm und kubectl laufen mit ephemer entschlüsselter kubeconfig (`KUBECONFIG` im Kind-Env).

## 9. Nachzügler aus Phase 1

- `sops edit`: Ist das Ergebnis kein gültiges YAML, fragt talkub „re-open editor? [Y/n]“ und öffnet
  dieselbe Temp-Datei erneut; bei „n“ wird nichts geschrieben (Hinweis, dass die Änderung verworfen wurde).
- `sops edit`: `$SOPS_EDITOR`/`$EDITOR`-Programm vor dem Entschlüsseln prüfen (`require_tool`).

## 10. Architektur

Neue/geänderte Module (Phase-1-Muster: `runner.py` bleibt einziger Subprocess-Aufrufer, CLI nur Parsing):

```
src/talkub/
├── repo.py                # Settings erweitert (platform, schematics, tailscale, nodes.mgmt/platform/arch), Migration
├── nodes.py               # NodeOptions → Node-Patch (render_node_patch ersetzt scaffold-Version), node add
├── factory.py             # Image-Factory-Client (urllib), Schematic-Hash/ID, Extensions-Liste, URLs
├── talos/render.py        # Patch-Auswahl, gen config, validate
├── talos/client.py        # talosctl-Aufrufe mit ephemerer talosconfig (apply/dry-run/bootstrap/health/upgrade/version/extensions)
├── lifecycle.py           # plan/apply/upgrade/status-Abläufe
├── bootstrap.py           # §8
├── tailscale.py           # §5
├── sealing.py             # secret seal / backup-sealing-key
└── cli/{nodes,lifecycle,image,tailscale,secret}.py
```

## 11. Fehlerbehandlung

- Wie Phase 1 (`TalkubError` mit Hinweis, Tracebacks nur mit `--debug`).
- Schleifen über Nodes brechen beim ersten Fehler ab und nennen den Node.
- Destruktive/unterbrechende Aktionen (`apply --insecure`, `apply`, `upgrade`) fragen nach (außer `--yes`).
- Netzwerkfehler zur Image Factory: Hinweis, dass gecachte IDs ohne Netzwerk genutzt werden können.

## 12. Tests

- Unit: Settings-Migration, Patch-Auswahl/Reihenfolge, Node-Patch-Erzeugung, Schematic-Hash, URLs.
- Rendering + `talosctl validate` gegen das echte talosctl (inkl. Tailscale-/Firewall-/Static-Patches).
- Abläufe (plan/apply/bootstrap/upgrade/status/seal) über Fake-Binaries auf `PATH`, die argv/env protokollieren.
- Image Factory über einen lokalen HTTP-Stub (`TALKUB_FACTORY_URL`), kein Netzwerk.
- Optional, per Marker deaktiviert: E2E mit `talosctl cluster create docker` (Durchreichen, `status`).

## 13. Nicht in Phase 2

KubeSpan/Multi-Standort, Tailscale Kubernetes Operator (Pod-Zugriff aufs Tailnet per Egress-Services; eigener späterer Schritt), `node remove`/`talosctl reset`, automatische Versionsermittlung für
Talos/Kubernetes-Updates, Cilium o. a. CNI-Wechsel.
