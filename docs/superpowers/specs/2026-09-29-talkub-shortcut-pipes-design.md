# talkub – Pipes in Schnellbefehlen – Design

Stand: 2026-09-29. Ergänzt `2026-09-26-talkub-expose-and-shortcuts-design.md`,
`2026-09-27-talkub-global-shortcuts-design.md` und `2026-09-29-talkub-shortcut-node-placeholders-design.md`.

## 1. Ziel

Die Ausgabe eines `run`- oder `kubernetes`-Shortcuts soll durch weitere Programme geleitet werden können,
auch in mehreren Stufen, und das als benannter, wiederverwendbarer (auch globaler) Befehl.

Erfolg: Mit

```yaml
shortcuts:
  ext-names:
    description: extension names
    run: [t, -n, "{node}", get, extensions, -o, json]
    pipe:
      - [jq, -r, .spec.metadata.name]
      - [sort]
```

gibt `talkub run ext-names cp-01` die sortierten Extension-Namen von `cp-01` aus; `jq` und `sort` sehen dabei
keine Zugangsdaten.

Abgrenzung: Ad-hoc-Pipes in der eigenen Shell (`talkub run pods | grep x`) funktionieren bereits, weil talkub
stdout durchreicht. Braucht eine spätere Stufe selbst den Cluster (`… | xargs kubectl delete`), bleibt der
`kubernetes`-Shortcut mit `sh -c "…"` der Weg; dort ist die Weitergabe der kubeconfig eine bewusste
Entscheidung.

Nicht-Ziele (YAGNI):
- Zugangsdaten für spätere Stufen (weder generell noch pro Stufe freischaltbar).
- Umleitung in Dateien oder in die Zwischenablage, stderr-Umleitung, Verzweigungen (`tee`-Semantik).
- `pipe:` für `secret`, `job`, `portForward`.
- Shell-Syntax (Strings mit `|`, Quoting): Stufen sind argv-Listen, es gibt kein `sh -c`.

## 2. Format

- `pipe:` ist nur neben `run` oder `kubernetes` erlaubt: eine nicht-leere Liste von nicht-leeren Listen aus
  Strings. Jede innere Liste ist eine Stufe (`[programm, args...]`) und darf nicht mit einem leeren Programm
  beginnen.
- `{node…}`-Platzhalter sind in `pipe:`-Stufen erlaubt, werden beim Laden geprüft wie in der ersten Stufe und
  beim Aufruf mit demselben Node gefüllt. Ein `{node…}` nur in `pipe:` genügt, damit der Shortcut einen Node
  braucht.
- `{kubeconfig}` in einer `pipe:`-Stufe ist ein Fehler beim Laden (nur die erste Stufe hat Zugangsdaten).
- Fehler beim Laden nennen den Pfad, z. B.
  `.talkub.yaml: shortcuts.ext-names.pipe[1] must be a non-empty list of strings`,
  `.talkub.yaml: shortcuts.x.pipe[0]: {kubeconfig} is only available in the first stage`,
  `.talkub.yaml: shortcuts.x.pipe is only allowed with run or kubernetes`.
- `shortcut_to_dict` schreibt `pipe:` zurück; `talkub run --save-global` übernimmt es damit unverändert.

## 3. Ausführung

- Extra-Argumente vom Aufruf werden an die erste Stufe angehängt (wie bisher). Der Node (erstes Argument bei
  `{node…}`) wird wie bisher vorher abgetrennt.
- Alle Programme aller Stufen werden mit `require_tool` geprüft, bevor etwas entschlüsselt wird.
- Die erste Stufe läuft mit derselben Umgebung wie bisher (`TALOSCONFIG` bzw. `KUBECONFIG` der
  entschlüsselten Datei; bei `kubernetes` wird `{kubeconfig}` in der ersten Stufe ersetzt).
- Alle späteren Stufen bekommen `child_env` ohne Schlüsselmaterial; zusätzlich zeigen `KUBECONFIG` und
  `TALOSCONFIG` auf eine nicht existierende Datei im privaten Temp-Verzeichnis. Ein `kubectl`/`talosctl` in
  einer späteren Stufe schlägt damit laut fehl, statt still den Standard-Kontext eines anderen Clusters zu
  benutzen (wie bei `talkub shell` ohne kubeconfig).
- stdin der ersten Stufe und stdout der letzten Stufe sind die des Terminals; stderr aller Stufen geht aufs
  Terminal.
- Exit-Code wie `set -o pipefail`: der Code der am weitesten rechts stehenden Stufe mit Code ≠ 0, sonst 0.
  Ein durch Signal N beendeter Prozess zählt als 128 + N. Ausnahme: Eine Stufe, die nicht die letzte ist und
  an SIGPIPE stirbt (141), zählt als Erfolg (z. B. `… | head -20`).
- Ctrl-C erreicht alle Stufen über die Prozessgruppe; talkub wartet auf alle und liefert den Code nach obiger
  Regel. Bei anderen Ausnahmen werden alle noch laufenden Stufen beendet.
- Das Listing zeigt die Art eines Shortcuts mit `pipe:` als `run |` bzw. `kubernetes |`.

## 4. Umsetzung

**`core/runner.py`**
- `pipe_status(codes: Sequence[int]) -> int` – reine Funktion für die Regel aus Abschnitt 3.
- `Runner.pipeline(stages: Sequence[Sequence[str]], *, env=None, rest_env=None) -> int` – erste Stufe mit
  `child_env(env)`, weitere mit `child_env(rest_env)`; Verkettung über `stdout=PIPE`; der Elternprozess
  schließt nach jedem Start sein Ende der vorigen Pipe; Warten wie in `call()`. `call()` bleibt unverändert.

**`config/shortcuts.py`**
- `Shortcut.pipe: tuple[tuple[str, ...], ...] = ()`; Parsen, Prüfen (Abschnitt 2), Serialisieren.
- `needs_node` berücksichtigt `pipe`.

**`workstation/access.py`**
- `talosctl`, `kubectl`, `with_kubeconfig` bekommen `pipe: Sequence[Sequence[str]] = ()`; ist es nicht leer:
  `require_tool` für jede Stufe vor dem Entschlüsseln, dann `runner.pipeline(...)` mit `rest_env` aus
  Abschnitt 3.

**`workstation/shortcuts.py`**
- `fill_node` auch für die `pipe`-Stufen; `pipe` an `access` durchreichen; Listing-Anzeige.

**README.md**: Abschnitt Shortcuts um `pipe:` ergänzen (Beispiel, Extras an die erste Stufe, Zugangsdaten nur
in der ersten Stufe, pipefail mit SIGPIPE-Ausnahme, Hinweis auf `sh -c` im `kubernetes`-Shortcut).

Keine neuen Pakete oder Abhängigkeiten; Schichtenvertrag unverändert.

## 5. Tests

- `tests/core/test_runner.py` (echte Prozesse): Ausgabe fließt durch zwei und drei Stufen; Fehler der ersten
  Stufe wird nicht von einer erfolgreichen letzten verdeckt; rechteste Fehlerstufe gewinnt; `yes | head -n 1`
  endet (mit Timeout-Schutz) mit 0; Umgebung erste vs. spätere Stufe; Signaltod → 128 + N;
  `pipe_status` als Tabelle (leer nicht erlaubt, 0/0, 1/0, 0/1, 141/0, 141/1, 0/141, 2/3).
- `tests/config/test_config_shortcuts.py`: gültiges `pipe`; Round-Trip; alle Fehler aus Abschnitt 2;
  ungültige `{node.…}` in `pipe`; `needs_node` bei `{node}` nur in `pipe`.
- `tests/workstation/`: `run` mit talosctl-/kubectl-Spy und `pipe: [[tr, a-z, A-Z]]`; Extras an erster Stufe;
  `{node}` in `pipe` gefüllt; fehlendes Programm in `pipe` → `ToolMissing` vor dem Entschlüsseln; spätere
  Stufe sieht keine existierende `KUBECONFIG`/`TALOSCONFIG`; `kubernetes` mit `pipe`; Listing zeigt `run |`;
  `--save-global` erhält `pipe`.
