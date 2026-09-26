# talkub – Versionen: eine Quelle, `versions`, `chart upgrade`, Renovate – Design

Stand: 2026-09-26.

## 1. Ziel

Chart-Versionen (und Repo/Chart) stehen heute doppelt: `.talkub.yaml` `charts` und `targetRevision` in
`gitops/apps/*.yaml`. Drift führt zu Downgrades bei `bootstrap`/`repo apply`. Ziel: eine Quelle (die Argo-CD-App),
Überblick über Soll/Ist/Neueste, gezieltes Anheben und automatische PRs per Renovate.

Erfolg: `.talkub.yaml` enthält keinen `charts`-Block mehr; `talkub versions` zeigt Talos, Kubernetes und alle
Chart-Apps; `talkub chart upgrade coredns` hebt die Version in `gitops/apps/coredns.yaml` an und prüft sie;
`talkub renovate init` erzeugt eine Konfiguration, mit der Renovate PRs für Charts und Talos/Kubernetes öffnet.

## 2. Eine Quelle für Charts

- `Settings.charts` entfällt. `from_dict` ignoriert einen vorhandenen `charts`-Block (keine Fehlermeldung),
  `to_dict` schreibt ihn nie → beim nächsten Schreiben verschwindet er (Migration ohne Befehl).
- Einzige Quelle: die Application in `gitops/apps/<key>.yaml` (Chart-Quelle mit `repoURL`, `chart`,
  `targetRevision`, `helm.valueFiles`/`releaseName`, `destination.namespace`).
- Gemeinsamer Parser: `lint.helm_sources` wird zu einem Modul `talkub/argoapps.py` (`HelmSource`,
  `helm_sources(repo)`, `app_source(repo, key) -> HelmSource`), von `lint`, `bootstrap`, `repo apply`, `versions`,
  `chart upgrade` genutzt. `app_source` lädt `gitops/apps/<key>.yaml`, genau eine Chart-Quelle erwartet; fehlt die
  Datei/Quelle → `ValidationError` mit Hinweis.
- Standardtabelle im Code (`charts.CATALOG`: key → repo, chart; bisher DEFAULT_CHARTS + OPTIONAL_CHARTS) dient nur
  noch zum **Erzeugen** neuer App-Dateien (`init`, `coredns takeover`, `tailscale operator enable`) mit der neuesten
  Version (`charts.latest_version`).
- `bootstrap`: installiert in fester Reihenfolge `coredns` (nur wenn `gitops/apps/coredns.yaml` existiert),
  `sealed-secrets`, `argocd` — jeweils per `helm_install` mit den Angaben aus `app_source` (Release, Namespace,
  Values-Dateien, `--create-namespace` für argocd wie bisher). Fehlt `sealed-secrets` oder `argocd` → Fehler.
- `repo apply` (Argo-CD-Upgrade für extraHosts) liest die argocd-Quelle aus der App-Datei.
- `init`: schreibt die App-Dateien mit den aufgelösten Versionen (wie bisher), aber keinen `charts`-Block.

## 3. `talkub versions`

Tabelle `COMPONENT  TARGET  RUNNING  LATEST  NOTE`, Zeilen:

- `talos`: TARGET `settings.talos_version`; RUNNING über `talosctl version` wie `status` (nur wenn talosconfig
  entschlüsselbar und Nodes erreichbar, sonst `?`; bei mehreren Nodes verschiedene Versionen komma-getrennt);
  LATEST = `tag_name` von `https://api.github.com/repos/siderolabs/talos/releases/latest`.
- `kubernetes`: TARGET `settings.kubernetes_version`; RUNNING kubelet-Versionen wie `status`; LATEST aus
  `https://dl.k8s.io/release/stable.txt` (ohne `v`); NOTE „check the Talos support matrix before upgrading“, wenn
  LATEST > TARGET.
- je Chart-App (alle `gitops/apps/*.yaml` mit Chart-Quelle, Name = App-Name): TARGET `targetRevision`; RUNNING aus
  `kubectl -n argocd get application <name> -o json` (`status.sync.revision` bzw. `status.sync.revisions[i]` passend
  zur Chart-Quelle; nicht verfügbar → `?`); LATEST per `charts.latest_version(repo, chart)`; NOTE `appVersion a → b`
  wenn neuer.
- Netz-/Clusterfehler je Zeile → `?`, nie Abbruch. `--offline`: LATEST überall `-`.
- Markierung: LATEST ≠ TARGET → Zeile mit `*` am Anfang; Exit immer 0.

## 4. Aktualisieren

### `talkub chart upgrade (<app> | --all) [--version X] [--yes]`

1. Quelle aus der App-Datei; Ziel = `--version` oder neueste. Gleich → „already at X“.
2. Anzeige: `chart: old → new`, `appVersion: a → b` (per `helm show chart --version`), Diff der Standard-Values
   (`helm show values` alt vs. neu, unified diff, gekürzt auf 200 Zeilen mit Hinweis), Rückfrage (Default ja,
   `--yes` überspringt).
3. `targetRevision` der Chart-Quelle round-trip setzen (Kommentare bleiben).
4. `lint` nur für diese App (Helm-Rendern + kubeconform, online). Fehler → Meldung, Datei bleibt geändert,
   Exit 1 mit Hinweis „fix the values or revert the change“.
5. Hinweis: commit + push — Argo CD rollt aus. Für `argocd` selbst: Hinweis auf Selbstverwaltung.
- `--all`: alle Chart-Apps nacheinander (je Schritt 2–4), Zusammenfassung am Ende.
- `--version` nur mit einer App.

### `talkub versions set (talos | kubernetes) <version>`

- Validierung: talos `v<major>.<minor>.<patch>`, kubernetes `<major>.<minor>.<patch>` (führendes `v` wird
  entfernt). Schreibt `.talkub.yaml`; Hinweis „run `talkub upgrade talos`“ bzw. „`talkub upgrade k8s`“.

## 5. Renovate: `talkub renovate init [--force]`

Schreibt `renovate.json` im Repo-Root (existiert → Fehler ohne `--force`):

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended"],
  "argocd": { "managerFilePatterns": ["/^gitops/apps/.+\\.ya?ml$/"] },
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": ["/^\\.talkub\\.yaml$/"],
      "matchStrings": ["talosVersion:\\s*\"?(?<currentValue>v[0-9.]+)\"?"],
      "datasourceTemplate": "github-releases",
      "depNameTemplate": "siderolabs/talos"
    },
    {
      "customType": "regex",
      "managerFilePatterns": ["/^\\.talkub\\.yaml$/"],
      "matchStrings": ["kubernetesVersion:\\s*\"?(?<currentValue>[0-9.]+)\"?"],
      "datasourceTemplate": "github-releases",
      "depNameTemplate": "kubernetes/kubernetes",
      "extractVersionTemplate": "^v(?<version>.*)$"
    }
  ],
  "packageRules": [
    {
      "matchFileNames": [".talkub.yaml"],
      "prBodyNotes": ["After merging, run `talkub upgrade talos` or `talkub upgrade k8s` — merging alone does not change the nodes."]
    }
  ]
}
```

- Vor der Implementierung gegen die aktuelle Renovate-Doku prüfen (`managerFilePatterns` vs. älteres `fileMatch`).
- Ausgabe: Hinweis, die Renovate-GitHub-App für das Repo zu aktivieren (https://github.com/apps/renovate),
  und dass gemergte Chart-PRs sofort von Argo CD ausgerollt werden → vorher `talkub lint` auf dem PR-Branch.
- `talkub lint` (yamllint) muss `renovate.json` nicht prüfen (kein YAML).

## 6. Architektur

- `src/talkub/argoapps.py`: HelmSource, helm_sources, app_source, set_target_revision (round-trip).
- `src/talkub/charts.py`: CATALOG, latest_version, show_chart/show_values (helm), resolve(runner, key) für neue Apps.
- `src/talkub/versions.py`: Tabelle (collect + render), set.
- `src/talkub/chart_upgrade.py`: upgrade-Fluss.
- `src/talkub/renovate.py`: Renderer + write.
- `repo.py`: `charts` raus (Parsing ignoriert, nicht mehr geschrieben).
- Anpassungen: bootstrap, gitrepo/cli repo apply, coredns, tailscale_operator, scaffold, lint (Parser-Import).
- CLI: `versions` (Gruppe mit Default-Anzeige + `set`), `chart upgrade`, `renovate init`.
- HTTP für GitHub/dl.k8s.io über `urllib.request` mit Timeout (wie der Factory-Client), kein Subprozess.

## 7. Tests

- Settings: alter `charts`-Block wird ignoriert und nicht zurückgeschrieben.
- argoapps: bestehende helm_sources-Tests verschoben; app_source Fehlerfälle; set_target_revision round-trip.
- bootstrap/repo apply: Versionen/Values aus App-Dateien (Spy-helm argv); fehlende App → Fehler.
- init/coredns/operator: App-Dateien mit aufgelösten Versionen, kein `charts` in `.talkub.yaml`.
- versions: Spies (helm, kubectl, talosctl) + lokaler HTTP-Stub für GitHub/dl.k8s.io; `?`-Fälle; `--offline`.
- chart upgrade: Spy-helm (show chart/values/template), Diff-Ausgabe, Rückfrage, `--all`, lint-Fehler → Exit 1.
- versions set: Validierung, Schreiben.
- renovate init: Inhalt (JSON gültig), `--force`, vorhandene Datei.
