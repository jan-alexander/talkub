# talkub – `lint` und Pre-Commit-Hook – Design

Stand: 2026-09-26.

## 1. Ziel

Fehler im Cluster-Repo vor dem Commit bzw. Push finden: YAML-Stil/Syntax, ungültige Kubernetes-Manifeste und
Helm-Values, die nicht rendern. Ein Git-Hook führt die schnellen, schlüssellosen Prüfungen vor jedem Commit aus.

Erfolg: ein frisch per `init` angelegtes Repo besteht `talkub lint` ohne Fehler; `talkub hooks install` in
`homelab_2` bewirkt, dass `git commit` bei einem unverschlüsselten Secret oder kaputtem YAML abbricht.

## 2. `talkub lint [--offline]`

Drei Prüfungen in dieser Reihenfolge; jede meldet Befunde als `<datei>:<zeile>: <level>: <text>` bzw. je Datei,
am Ende eine Zusammenfassung (`lint: N errors, M warnings, K skipped`). Exit 1 bei mindestens einem Fehler.

### 2.1 yamllint

- Python-Abhängigkeit `yamllint` (in `pyproject.toml`), Aufruf über `yamllint.linter.run(...)` mit
  `yamllint.config.YamlLintConfig`; kein Subprozess.
- Dateien: alle `*.yaml`/`*.yml` im Repo außer `.git/` (über `Repo.root.rglob`, versteckte Verzeichnisse außer
  `.git` eingeschlossen, z. B. `.talkub.yaml`, `.sops.yaml`).
- Konfiguration: `<repo>/.yamllint` falls vorhanden, sonst `DEFAULT_YAMLLINT`:
  ```yaml
  # Managed by talkub. yamllint configuration for this cluster repo.
  extends: relaxed
  ignore: |
    *.sops.yaml
  rules:
    line-length: disable
  ```
  Die `ignore`-Muster der Konfiguration gelten (yamllint-Semantik).
- Level `error` → Fehler, `warning` → Warnung.

### 2.2 Manifeste mit kubeconform

- Dateien: `gitops/apps/**/*.y*ml` und `gitops/manifests/**/*.y*ml` (keine `*.sops.yaml`, nicht `gitops/values`).
- Aufruf (über `Runner.run`, check=False):
  `kubeconform -output json -summary -strict -kubernetes-version <ver> -cache <cache> -schema-location default
   -schema-location '<datreeio>' [-ignore-missing-schemas] <files…>`
  - `<ver>` = `settings.kubernetes_version` (ohne führendes `v`). Gibt es dafür (noch) keine Schemas, wird mit
    `master` wiederholt und ein Hinweis ausgegeben (Erkennung über die JSON-Ausgabe: alle Ressourcen „could not find
    schema“ bzw. Download-Fehler für Core-Typen).
  - `<datreeio>` = `https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json`
  - `<cache>` = `$XDG_CACHE_HOME/talkub/kubeconform` bzw. `~/.cache/talkub/kubeconform` (angelegt).
  - Ohne `--offline`: fehlende Schemas → Warnung („no schema for <kind>“), kein Fehler (`-ignore-missing-schemas`
    immer; fehlende Schemas als `skipped` zählen).
  - Mit `--offline`: zusätzlich darf kubeconform kein Netz brauchen — nur Cache; Schemas, die nicht im Cache
    sind, gelten als übersprungen. (Umsetzung: wenn der Cache-Ordner leer ist → ganze Prüfung „skipped (no schema
    cache yet; run `talkub lint` online once)“; sonst kubeconform mit Cache — Download-Fehler werden als skipped
    gewertet.)
- `kubeconform` nicht installiert → Warnung `kubeconform not found — skipped (brew install kubeconform)`,
  2.2 und 2.3 werden übersprungen.

### 2.3 Helm-Apps rendern (nur ohne `--offline`)

- Für jede Application in `gitops/apps/*.yaml` mit einer Quelle, die `chart` hat: Chart, `repoURL`,
  `targetRevision`, Ziel-Namespace (`spec.destination.namespace`), Release-Name = `metadata.name`
  (bzw. `helm.releaseName`), Value-Dateien = `helm.valueFiles` mit Präfix `$values/` → lokaler Pfad im Repo
  (fehlende Datei → Fehler); inline `helm.values`/`valuesObject` werden als temporäre Datei (privates Temp-Verzeichnis)
  mitgegeben.
- `helm template <release> <chart> --repo <repo> --version <v> -n <ns> -f <values…>` (OCI-Repos wie in
  `bootstrap.chart_ref`); Fehler → Lint-Fehler mit helm-stderr (gekürzt).
- Ausgabe per stdin an `kubeconform … -` (gleiche Optionen wie 2.2); Befunde mit Präfix `<app>:`.
- `helm` nicht installiert → Warnung, übersprungen.

## 3. Pre-Commit-Hook

```
talkub hooks install [--force]
talkub hooks uninstall
```

- Ziel: `<git-dir>/hooks/pre-commit` (Git-Dir über `git rev-parse --git-dir` im Repo-Root; kein Git-Repo →
  Fehler mit Hinweis `git init`).
- Inhalt (Mode 0755):
  ```sh
  #!/bin/sh
  # talkub pre-commit hook (managed by talkub; reinstall with `talkub hooks install`)
  TALKUB='<absoluter Pfad>'
  [ -x "$TALKUB" ] || TALKUB=talkub
  "$TALKUB" check && "$TALKUB" lint --offline
  ```
  `<absoluter Pfad>` = das laufende talkub-Programm (`shutil.which(sys.argv[0])` bzw. `Path(sys.argv[0]).resolve()`;
  Fallback `talkub`). Der Pfad wird shell-sicher gequotet.
- Vorhandene Datei: enthält die Markerzeile `talkub pre-commit hook` → überschreiben; sonst Fehler
  „a pre-commit hook already exists“, Hinweis `--force` (dann überschreiben).
- `uninstall`: nur den eigenen Hook löschen (Marker), sonst Fehler.
- Außerdem: fehlt `<repo>/.yamllint`, legt `hooks install` sie mit `DEFAULT_YAMLLINT` an (Hinweis „commit .yamllint“).
- `talkub init`: nach `git_init` den Hook installieren und `.yamllint` schreiben (Fehler nur als Warnung).

## 4. Architektur

- `src/talkub/lint.py`: `run(ctx, *, offline) -> LintReport`, je Prüfung eine Funktion; Parser für kubeconform-JSON.
- `src/talkub/hooks.py`: `install(repo, runner, *, force)`, `uninstall(repo, runner)`, `render_hook(path)`.
- `cli/lint.py` (`lint`), `cli/hooks.py` (`hooks install/uninstall`), Registrierung in `cli/main.py`.
- `runner.py`: `INSTALL_HINTS` für `kubeconform` (`brew install kubeconform`).
- `pyproject.toml`: Abhängigkeit `yamllint`.
- `scaffold.py`: `.yamllint` schreiben; `cli/setup.py`: Hook nach `git_init`.
- README: Abschnitt „Lint and pre-commit hook“.

## 5. Tests

- yamllint: Fehler/Warnung/Ignore von `*.sops.yaml`, eigene `.yamllint` hat Vorrang, frisches `init`-Repo ohne Fehler.
- kubeconform: Spy liefert JSON-Ausgabe → Befunde/Zusammenfassung; argv (Version, Cache, Schema-Locations,
  Dateiauswahl ohne values/sops); fehlendes Tool → Warnung; `--offline` mit leerem Cache → skipped; Version-Fallback
  auf `master`.
- Helm: Application-Parsing (valueFiles → lokale Pfade, inline values, releaseName, Namespace), helm-Fehler,
  stdin an kubeconform, `--offline` überspringt.
- Hooks: Installation in echtem `git init`-Repo, Inhalt/Mode, Marker-Überschreiben, fremder Hook ohne/mit
  `--force`, uninstall, kein Git-Repo; ein echter `git commit` in einem Test-Repo mit Spy-talkub, das bei
  Fehlerstatus den Commit verhindert.
- Mit echtem kubeconform/helm (need(...), Netz → skip): `init`-Repo besteht `lint`.
