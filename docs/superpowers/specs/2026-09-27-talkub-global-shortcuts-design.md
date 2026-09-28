# talkub – Globale Schnellbefehle (`run`) – Design

Stand: 2026-09-27. Ergänzt `2026-09-26-talkub-expose-and-shortcuts-design.md`.

## 1. Ziel

Schnellbefehle nicht nur pro Projekt in `.talkub.yaml`, sondern zusätzlich in einem globalen Pool im
Benutzerverzeichnis. Häufig genutzte Shortcuts sind damit nicht mehr an ein Cluster-Repo gebunden und
lassen sich in jedem Projekt ausführen; der Pool wächst über die Zeit, indem bewährte Projekt-Shortcuts
per Befehl dorthin verschoben werden.

Erfolg: `talkub run --save-global pods` verschiebt `pods` aus `.talkub.yaml` nach
`~/.config/talkub/shortcuts.yaml`; danach funktioniert `talkub run pods` in jedem Cluster-Repo, jeweils
gegen dessen Cluster.

Nicht-Ziele: Hinzufügen/Bearbeiten/Löschen globaler Einträge per CLI (die Datei wird dafür von Hand
bearbeitet); Rückverschieben global → Projekt; weitere globale Einstellungen außer Shortcuts.

## 2. Speicherort und Format

- Datei: `$XDG_CONFIG_HOME/talkub/shortcuts.yaml`; ist `XDG_CONFIG_HOME` nicht gesetzt oder leer,
  `~/.config/talkub/shortcuts.yaml`.
- Format wie der `shortcuts:`-Block in `.talkub.yaml`, mit demselben Wrapper auf oberster Ebene (lässt Raum
  für spätere globale Einstellungen):

```yaml
shortcuts:
  pods:
    description: all pods
    run: [k, get, pods, -A]
```

- Validierung: dieselbe wie im Projekt (`shortcut_from_dict`). Fehler tragen den Dateipfad als Präfix
  statt `.talkub.yaml`, z. B. `~/.config/talkub/shortcuts.yaml: shortcuts.pods.run must …`. Oberste
  Ebene keine Mapping bzw. `shortcuts` keine Mapping → Fehler. Fehlende Datei oder fehlender Block → leer.
- Ausführung immer im Context des aktuellen Cluster-Repos (kubeconfig/talosconfig); ein globaler Shortcut
  ist nur global *gespeichert*.

## 3. Laden und Zusammenführen

Neues Modul `src/talkub/config/user.py`:

- `user_config_dir() -> Path`: XDG-Auflösung wie oben, `…/talkub`.
- `global_shortcuts_file() -> Path`: `user_config_dir() / "shortcuts.yaml"`.
- `load_global_shortcuts() -> dict[str, Shortcut]`.
- `save_global_shortcut(shortcut: Shortcut, *, force: bool = False) -> Path`: validiert zuerst die ganze
  Datei (kaputt → Fehler, nichts wird geschrieben), Konflikt ohne `force` → Fehler; fügt den Eintrag in das
  Round-trip-Dokument ein (Kommentare und übrige Einträge bleiben erhalten), legt das Verzeichnis an,
  schreibt atomar (`atomic_write`).

Die Feld-Helfer (`config/fields.py`) setzen `MARKER` fest in ihre Meldungen. `load_global_shortcuts` fängt
deshalb den `ValidationError` aus `shortcut_from_dict` ab und ersetzt das führende `.talkub.yaml: ` durch
den globalen Pfad (`~` für das Home-Verzeichnis); Projekt-Fehler bleiben unverändert.

Zusammenführen (in `src/talkub/workstation/shortcuts.py`):

- `Origin`: `"project"`, `"global"`, `"project-overrides-global"`.
- `effective_shortcuts(project, global_) -> dict[str, tuple[Shortcut, Origin]]`: globale Einträge, darüber
  die Projekt-Einträge; ein Projekt-Eintrag mit einem Namen, der auch global existiert, bekommt
  `project-overrides-global`.

Die globale Datei wird nur von `talkub run` gelesen. Kein anderer Befehl (auch nicht `talkub check`) lädt
sie; eine kaputte globale Datei stört also nur `run`.

## 4. CLI

```
talkub run                                        # Liste aus Projekt und global
talkub run [--print] [--follow] <name> [args...]  # ausführen
talkub run --save-global [--force] <name>         # Projekt → global verschieben
```

Weiterhin roh geparst in `cli/main.py` (`run_shortcut`); alle Optionen nur vor dem Namen.

**Liste:** Spalten Name, Art, Beschreibung, Herkunft (`project`, `global`, `project (overrides global)`),
nach Namen sortiert und ausgerichtet. Beide Quellen leer →
`no shortcuts defined in .talkub.yaml or <globaler Pfad>`.

**Ausführen:** Auflösung über `effective_shortcuts`; unbekannter Name → Fehler mit allen verfügbaren Namen
aus beiden Quellen. Das Verhalten je Art bleibt unverändert.

**`--save-global <name>`:**

1. Nicht kombinierbar mit `--print`/`--follow`; zusätzliche Argumente hinter dem Namen → Fehler.
2. Name nicht in `.talkub.yaml` → Fehler; existiert er nur global, mit Hinweis
   `<name> is already a global shortcut`.
3. Name existiert global schon → Fehler mit Hinweis `--force`; mit `--force` wird der globale Eintrag
   überschrieben.
4. Reihenfolge: zuerst die globale Datei schreiben, dann den Eintrag aus `.talkub.yaml` entfernen
   (`Repo.write_settings`). Ein Abbruch dazwischen hinterlässt den Shortcut höchstens doppelt, nie
   verloren.
5. Ausgabe: `moved <name> to <globaler Pfad>`.

`RUN_USAGE`, Typer-Hilfe von `run` und README-Abschnitt „Shortcuts“ beschreiben den globalen Pool, die
Präzedenz (Projekt vor global) und `--save-global`.

## 5. Dateien

- `src/talkub/config/user.py` (neu): Pfad, Laden, Schreiben.
- `src/talkub/workstation/shortcuts.py`: `Origin`, `effective_shortcuts`, Liste mit Herkunft, `run` auf
  zusammengeführte Einträge, `save_global`.
- `src/talkub/cli/main.py`: `--save-global`/`--force`, neue Hilfe.
- `README.md`: Abschnitt „Shortcuts“.

## 6. Tests

In allen Tests zeigt `XDG_CONFIG_HOME` per `monkeypatch` auf `tmp_path`; das echte `~/.config` wird nie
berührt.

- `config/user.py`: XDG-Auflösung mit/ohne/leerer Variable; fehlende Datei → leer; ungültiger Eintrag →
  Fehler mit Dateipfad; ungültige oberste Ebene; Round-trip; Verzeichnis wird angelegt.
- `effective_shortcuts`: nur Projekt, nur global, Überdeckung mit korrektem `Origin`.
- Liste: Herkunftsspalte, `(overrides global)`, Meldung bei leeren Quellen.
- Ausführen: globaler `run`-Shortcut läuft im Projekt-Context; bei gleichem Namen gewinnt das Projekt;
  unbekannter Name listet beide Quellen.
- `--save-global`: Erfolg (global vorhanden, aus `.talkub.yaml` entfernt, übrige Settings unverändert);
  Name nicht im Projekt; nur global vorhanden (Hinweis); Konflikt ohne `--force` → Fehler, keine Datei
  geändert; `--force` überschreibt; Zusatzargumente und Kombination mit `--print`/`--follow` → Fehler.
- Kaputte globale Datei: `talkub check` läuft weiter.
