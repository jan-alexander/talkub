# talkub – Token-Schnellbefehle – Design

Stand: 2026-09-29. Ergänzt `2026-09-26-talkub-expose-and-shortcuts-design.md`,
`2026-09-27-talkub-global-shortcuts-design.md` und `2026-09-29-talkub-shortcut-pipes-design.md`.

## 1. Ziel

Ein frisches ServiceAccount-Token (z. B. für den Headlamp-Login) soll mit einem kurzen Eintrag in die
Zwischenablage kommen, über denselben Weg wie `secret`-Shortcuts (Wert nur im Speicher, stdin an das
Clipboard-Tool, automatisches Leeren). Bisher geht das nur umständlich und ohne Leeren:

```yaml
headlamp-token:
  kubernetes: [sh, -c, "kubectl -n headlamp create token headlamp --duration=8h | pbcopy"]
```

Erfolg: Mit

```yaml
shortcuts:
  headlamp-token:
    description: Frisches Headlamp-Login-Token (8h)
    token: {namespace: headlamp, serviceAccount: headlamp, duration: 8h}
```

kopiert `talkub run headlamp-token` ein neues Token in die Zwischenablage, die nach 45 s geleert wird (sofern
sie noch das Token enthält); `talkub run --print headlamp-token` schreibt es nach stdout.

Nicht-Ziele (YAGNI):
- `audience`, `bound-object-*` und andere Optionen von `kubectl create token`.
- Überschreiben der Laufzeit beim Aufruf (`talkub run headlamp-token 1h`); `token` nimmt wie `secret` keine
  Extra-Argumente.
- Eigene Leer-Zeit pro Shortcut; es gilt `TALKUB_CLIPBOARD_CLEAR_SECONDS` wie bei `secret`.
- `pipe:` und `{node…}`-Platzhalter.

## 2. Format

- Neue Art `token`: ein Mapping mit
  - `namespace` (Pflicht, Label),
  - `serviceAccount` (Pflicht, Label),
  - `duration` (optional): eine Go-Dauer aus einem oder mehreren Blöcken `<Zahl><Einheit>` mit den Einheiten
    `h`, `m`, `s` (z. B. `8h`, `30m`, `1h30m`), insgesamt nicht null. Fehlt sie, wird `--duration` nicht
    übergeben; es gilt der Standard des API-Servers (meist 1 h). Die Obergrenze setzt der API-Server.
- Fehler beim Laden nennen den Pfad, z. B.
  `.talkub.yaml: shortcuts.headlamp-token.token must be a mapping`,
  `.talkub.yaml: shortcuts.headlamp-token.token.duration must be a duration like 8h or 1h30m`.
- `pipe:` neben `token` ist ein Fehler (wie bei `secret`).
- `shortcut_to_dict` schreibt `token:` zurück (`duration` nur, wenn gesetzt); `--save-global` übernimmt den
  Shortcut damit unverändert.

## 3. Ausführung

- `kubectl -n <namespace> create token <serviceAccount> [--duration=<duration>]` in einer `kube_session`,
  stdout wird abgefangen (das Token erscheint nie im Terminal, in argv oder env). Nachgestellte Leerzeichen
  und Zeilenumbrüche werden entfernt; ist danach nichts übrig, ist das ein Fehler.
- Schlägt kubectl fehl (z. B. ServiceAccount fehlt, fehlende RBAC-Rechte), meldet der bestehende
  `CommandFailed`-Weg kubectls stderr.
- Die Auslieferung ist dieselbe wie bei `secret`: mit `--print` der blanke Wert nach stdout (Zeilenumbruch nur
  am Terminal), sonst in die Zwischenablage, Leeren nach `TALKUB_CLIPBOARD_CLEAR_SECONDS` durch den
  abgekoppelten Helfer, Meldung `copied <name> (<n> chars); the clipboard is cleared in <s> s`.
- `--print` ist bei `secret` und `token` erlaubt; die Fehlermeldung bei anderen Arten lautet
  `--print only applies to secret and token shortcuts, <name> is <kind>`.
- Extra-Argumente sind ein Fehler (`shortcut <name> (token) takes no extra arguments`).
- Das Listing zeigt die Art `token`.

## 4. Umsetzung

**`config/shortcuts.py`**
- `token` in `_SHORTCUT_KINDS`; `_DURATION`-Regex und Prüfung auf nicht-null.
- `Shortcut` erhält `service_account: str | None` und `duration: str | None`; `namespace` wird mitbenutzt.

**`workstation/shortcuts.py`**
- `_deliver(ctx, name, value, print_value)` aus dem Auslieferungsteil von `_secret` herausgelöst.
- `_token(ctx, shortcut, print_value)` erzeugt das Token und ruft `_deliver`.

**`cli/main.py`**: Hilfetext von `--print` nennt `secret`/`token`.

**README**: `token` in der Liste der Arten und im Beispiel; `--print`-Hinweise angepasst.

## 5. Tests

- Config: Parsen mit und ohne `duration`; fehlende/ungültige `namespace`/`serviceAccount`; ungültige
  Dauern (`8hours`, `0h`, `h`, `8`, `-1h`); `pipe:` neben `token`; Rundreise über `shortcut_to_dict`.
- Ausführung mit `spy("kubectl")`: argv mit und ohne `--duration`; `--print` gibt das Token ohne
  nachgestellten Zeilenumbruch aus; Kopieren über ein gefälschtes `pbcopy` bekommt das Token auf stdin;
  leere kubectl-Ausgabe ist ein Fehler; Extra-Argumente sind ein Fehler.
- Listing zeigt `token`.
