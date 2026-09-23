# talkub – Design

Datum: 2026-09-23
Status: Entwurf zur Review

## 1. Ziel

`talkub` ist ein eigenständiges CLI-Tool (Python, verteilt via `uv tool install`) zur Administration
eines Talos-Linux-Clusters, der per ArgoCD (App of Apps) GitOps betrieben wird. Das Tool ist **nicht**
an ein Repo gebunden, sondern arbeitet auf einem "Cluster-Repo", das es selbst anlegt und pflegt.

Erfolgskriterien:

- Ein neues Cluster-Repo inkl. Talos-Secrets, Schlüsselverwaltung und GitOps-Gerüst entsteht mit einem Befehl.
- Talos-Configs werden aus Patches gerendert; Änderungen und Upgrades laufen als `plan`/`apply`.
- `talosctl`/`kubectl` laufen mit Zugangsdaten, die nur für die Dauer des Aufrufs im Klartext existieren.
- Kein Klartext-Secret und kein Klartext-Schlüssel liegt jemals im Repo.

## 2. Getroffene Entscheidungen

| Thema | Entscheidung |
|---|---|
| Schlüsselhierarchie | Ein **Cluster-Master-age-Key** verschlüsselt alle SOPS-Dateien. Der Master-Key liegt als `keys/master.age` im Repo, verschlüsselt an alle Admin-Empfänger. |
| Admin-Identitäten | Pro Admin eine eigene age-Identität: Software-Key, YubiKey (`age-plugin-yubikey`) oder Secure Enclave (`age-plugin-se`). |
| Passphrase-Backup | **Keines.** Wiederherstellung basiert allein auf mehreren Empfängern. Tool warnt bei < 2 Empfängern und verweigert das Entfernen des letzten. |
| SOPS vs. Sealed Secrets | SOPS nur für Infrastruktur außerhalb des Clusters (Talos-Secrets, talosconfig, kubeconfig, Sealing-Key-Backup). Sealed Secrets für alle App-Secrets in ArgoCD. ArgoCD sieht nie einen age-Key. |
| Repo-Zuschnitt | Ein Repo = ein Cluster. Flache Struktur. |
| Talos-Configs | `talosctl gen config` aus `secrets.sops.yaml` + Patches, nur im Speicher gerendert. Keine gerenderten Configs im Repo. |
| Topologie | Start mit einem Control-Plane-Node, der Workloads ausführt (`allowSchedulingOnControlPlanes: true`). Struktur erlaubt weitere Nodes. |
| Ephemere Entschlüsselung | (1) stateless pro Aufruf und (2) `talkub shell`. Kein Agent/Key-Cache in v1. |
| Aliase | Unterbefehle `talkub t` = `talosctl`, `talkub k` = `kubectl`. Keine eigenständigen Shell-Befehle. |
| ArgoCD-Installation | Einmalig per Helm im Bootstrap; danach verwaltet sich ArgoCD über `gitops/apps/argocd.yaml` selbst. |

## 3. Struktur des Cluster-Repos

```
<cluster>/
├── .talkub.yaml                 # Marker + Soll-Zustand (Name, Endpoint, Versionen)
├── .sops.yaml                   # creation_rules: *.sops.yaml → master.pub
├── .gitignore                   # *.dec.*, .talkub-tmp/
├── keys/
│   ├── recipients.txt           # Admin-Public-Keys, "# <name> <typ>" als Kommentar je Eintrag
│   ├── master.age               # Master-Key, verschlüsselt an recipients.txt
│   └── master.pub               # Public Key des Masters
├── talos/
│   ├── secrets.sops.yaml        # talosctl gen secrets
│   ├── talosconfig.sops.yaml
│   ├── patches/common.yaml
│   └── nodes/<node>.yaml
├── kubernetes/
│   └── kubeconfig.sops.yaml
├── bootstrap/
│   ├── sealed-secrets-key.sops.yaml
│   ├── sealed-secrets.pem       # öffentliches Sealing-Cert (Offline-Sealing)
│   └── argocd/values.yaml
└── gitops/
    ├── root.yaml                # Root-Application → gitops/apps/
    ├── apps/{argocd,sealed-secrets,<app>}.yaml
    └── manifests/<app>/{kustomization.yaml,*.sealed.yaml}
```

Konventionen:

- `*.sops.yaml` muss einen `sops:`-Block enthalten (von `talkub check` geprüft).
- `*.sealed.yaml` sind SealedSecrets; unter `gitops/` sind keine `kind: Secret` erlaubt.
- ArgoCD liest ausschließlich `gitops/`.

### `.talkub.yaml` (Beispiel)

```yaml
name: my-cluster
endpoint: https://10.0.0.10:6443
talosVersion: v1.11.0
kubernetesVersion: v1.34.0
charts:
  argocd: 8.x.x
  sealedSecrets: 2.x.x
```

## 4. Identitäts- und Schlüsselauflösung

Reihenfolge beim Beschaffen des Master-Keys:

1. `SOPS_AGE_KEY` oder `SOPS_AGE_KEY_FILE` gesetzt → ist der Master-Key (CI/Automation). `master.age` wird nicht angefasst.
2. `TALKUB_IDENTITY` gesetzt (Pfade, `:`-getrennt) → damit `keys/master.age` entschlüsseln.
3. Standard: alle Identitätsdateien in `~/.config/talkub/identities/` → damit `keys/master.age` entschlüsseln.

Findet keine Stufe einen Schlüssel, bricht das Tool mit `NoIdentity` und Hinweisen ab.

Regeln:

- Der entschlüsselte Master-Key existiert nur im Prozessspeicher und wird ausschließlich via
  `SOPS_AGE_KEY` an `sops`-Kindprozesse übergeben.
- `SOPS_AGE_KEY`, `SOPS_AGE_KEY_FILE` und `TALKUB_IDENTITY` werden aus der Umgebung von `talosctl`,
  `kubectl`, `helm`, `kubeseal` und der Session-Shell entfernt.
- Verschlüsseln (neue SOPS-Dateien, `sops -e`) nutzt nur `master.pub` und benötigt keine Identität.

## 5. Befehle

### Einrichtung & Schlüssel

| Befehl | Verhalten |
|---|---|
| `identity new [--yubikey\|--se\|--software]` | Erzeugt persönliche Identität in `~/.config/talkub/identities/`, gibt Public Key aus. |
| `init <name> --endpoint <url> --node <name>=<ip>` | Legt Struktur an; erzeugt Master-Key (nur RAM) und verschlüsselt ihn an den eigenen Public Key; `talosctl gen secrets` → SOPS; schreibt `.sops.yaml`, `.talkub.yaml`, Patches, Node-Datei, GitOps-Gerüst. Bricht ab, wenn Zielverzeichnis nicht leer ist. |
| `admin add <pubkey> --name <n>` | Fügt Empfänger hinzu, verschlüsselt `master.age` neu. |
| `admin remove <n>` | Entfernt Empfänger, verschlüsselt `master.age` neu; verweigert letzten Empfänger; empfiehlt `keys rotate`. |
| `admin list` | Listet Empfänger; Warnung bei < 2. |
| `keys rotate` | Neuer Master-Key; alle `*.sops.yaml` neu verschlüsseln; `master.age`, `master.pub`, `.sops.yaml` ersetzen. |

### Talos-Lebenszyklus

| Befehl | Verhalten |
|---|---|
| `node add <name> --ip <ip> [--role controlplane\|worker]` | Legt `talos/nodes/<name>.yaml` an. |
| `plan [node]` | Rendert Config im Speicher, `talosctl apply-config --dry-run`, zeigt Diff und Reboot-Bedarf. |
| `apply [node] [--mode auto\|no-reboot\|reboot\|staged] [--yes]` | Wie `plan`, dann nach Bestätigung anwenden. |
| `apply --insecure <node>` | Erstinstallation eines Nodes im Maintenance-Modus. |
| `bootstrap` | `talosctl bootstrap` → Health abwarten → kubeconfig holen, SOPS-verschlüsselt speichern → falls Backup vorhanden, Sealing-Key einspielen → Sealed Secrets + ArgoCD per Helm → `gitops/root.yaml` anwenden → falls kein Backup existierte, `secret backup-sealing-key` ausführen. |
| `upgrade talos` / `upgrade k8s` | Soll-Version aus `.talkub.yaml` vs. Ist; `talosctl upgrade` bzw. `talosctl upgrade-k8s`. |
| `status` | Soll/Ist: Versionen, Config-Drift je Node, ArgoCD-Sync-Status. |

### Durchreichen & Secrets

| Befehl | Verhalten |
|---|---|
| `talosctl …` / `t …` | Ephemer entschlüsselte talosconfig, Argumente unverändert durchreichen, Exit-Code übernehmen. |
| `kubectl …` / `k …` | Wie oben mit kubeconfig. |
| `shell` | Subshell (`$SHELL`) mit `TALOSCONFIG`/`KUBECONFIG`; Prompt-Präfix `(talkub:<name>)`; Aufräumen bei Exit. |
| `secret seal <datei> [-o <out>]` | Secret → `*.sealed.yaml` via `kubeseal`; Cert live aus dem Cluster, Fallback `bootstrap/sealed-secrets.pem`. |
| `secret backup-sealing-key` | Controller-Key(s) aus dem Cluster → `bootstrap/sealed-secrets-key.sops.yaml`; Cert → `bootstrap/sealed-secrets.pem`. |
| `sops edit\|decrypt <datei>` | `sops` mit aufgelöstem Master-Key. |
| `check` | Alle `*.sops.yaml` verschlüsselt; keine `kind: Secret` unter `gitops/`; ≥ 2 Empfänger (Warnung). Exit ≠ 0 bei Fehlern (Pre-Commit geeignet). |

Das Repo wird gefunden, indem ab `cwd` aufwärts nach `.talkub.yaml` gesucht wird; `--repo <pfad>` überschreibt.

## 6. Architektur

Stack: Python ≥ 3.12, `uv`-Projekt, `src/`-Layout, Typer (CLI), ruamel.yaml (kommentarerhaltendes YAML).
Sprache: Code, Bezeichner, Kommentare, Docstrings und CLI-Ausgaben sind durchgehend Englisch.
Externe Binaries: `age` (+ Plugins), `sops`, `talosctl`, `kubectl`, `kubeseal`, `helm`. Vorhandensein
wird beim Aufruf des jeweiligen Befehls geprüft (`ToolMissing`).

```
src/talkub/
├── cli/            # nur Parsing, ruft Services
│   ├── main.py     # App, Aliase t/k
│   └── keys.py admin.py talos.py secrets.py passthrough.py
├── repo.py         # Repo finden, Pfade, .talkub.yaml laden/validieren
├── identity.py     # Auflösungsreihenfolge (Abschnitt 4)
├── crypto/age.py   # encrypt an Empfänger, decrypt mit Identitäten
├── crypto/sops.py  # sops mit SOPS_AGE_KEY nur im Kind-Env
├── ephemeral.py    # Kontextmanager decrypted(repo, *names) → Pfade
├── runner.py       # einziger subprocess-Aufrufer; argv-Listen, Env-Filter, Exit-Codes
├── talos/render.py # gen config aus secrets + patches (im Speicher)
├── talos/lifecycle.py
├── scaffold.py     # init
└── templates/
```

### `ephemeral.decrypted`

- Legt ein Verzeichnis mit Modus `0700` via `tempfile.mkdtemp` an.
- Entschlüsselt die angeforderten Dateien per `sops -d` dorthin, jeweils mit Modus `0600`.
- Löscht das Verzeichnis im `finally`. `SIGINT`/`SIGTERM` werden abgefangen, damit der
  `finally`-Block läuft und Kindprozesse beendet werden.
- `talkub shell` nutzt denselben Kontextmanager um einen langlebigen Kindprozess.

### Pass-Through

Typer-Unterbefehle mit `context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`;
Argumente aus `ctx.args` als Liste an `runner`. Nie Shell-Strings.

## 7. Fehlerbehandlung

- Fehlerklassen: `RepoNotFound`, `NoIdentity`, `DecryptFailed`, `ToolMissing`, `TalosError`, `ValidationError`.
- Ausgabe als kurze Meldung mit Hinweis (z. B. "YubiKey gesteckt?", "Deine Identität steht nicht in recipients.txt").
- Tracebacks nur mit `--debug`.
- Pass-Through-Befehle übernehmen den Exit-Code des Kindprozesses.
- Schreibende Schlüsseloperationen (`admin add/remove`, `keys rotate`) schreiben zunächst in
  temporäre Dateien und ersetzen atomar (`os.replace`), damit ein Abbruch keinen halben Zustand hinterlässt.

## 8. Tests

- Unit: Identitätsauflösung, Env-Filter in `runner`, Rendern mit festen Secrets, Scaffold, `check`.
- Integration mit echten `age`/`sops` und Software-Test-Keys: `init` → `admin add` → `admin remove` → entschlüsseln; `keys rotate`.
- Talos/Kubernetes-Befehle über einen Fake-Runner, der erwartete argv prüft.
- Optional, per Marker deaktiviert: E2E gegen `talosctl cluster create` (Docker).

## 9. Nicht in v1

- Agent / Master-Key-Cache (Ansatz 3).
- Mehrere Cluster pro Repo.
- Passphrase-Recovery.
- SOPS-Entschlüsselung innerhalb von ArgoCD.
- Eigenständige Shell-Befehle für Aliase.
