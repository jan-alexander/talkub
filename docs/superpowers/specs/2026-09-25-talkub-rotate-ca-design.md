# talkub rotate-ca – Talos API CA rotation – Design

Stand: 2026-09-25. Ergänzt die bisherigen talkub-Specs.

## 1. Ziel

Die Talos-Maschinen-CA (`machine.ca`, im Secrets-Bundle `certs.os`) kann kompromittiert sein – konkret
stand ihr privater Schlüssel einmal in einer ungeschwärzten `plan`-Ausgabe. `talkub rotate-ca` rotiert sie
mit `talosctl rotate-ca` und zieht das Repo nach, damit talkubs deklaratives Rendering die neue CA
verwendet. Erfolg: nach dem Befehl zeigt `talkub plan` für alle Nodes „No changes“, die talosconfig im Repo
funktioniert, und kein Klartext-Schlüssel wurde ausgegeben oder dauerhaft gespeichert.

Nicht enthalten: Kubernetes-CA-Rotation (`--kubernetes`), spätere Erweiterung möglich.

## 2. Beobachtetes Verhalten (echter Dry-Run, Talos 1.14, 2026-09-25)

- Ausgabe enthält den **aktuellen und neuen CA-Schlüssel** sowie die komplette neue talosconfig inkl.
  Client-Key → jede Ausgabe von `rotate-ca` läuft über `TalosSession.call_redacted` / `redact_diff`.
- Phasen: Konnektivität prüfen → neue CA als accepted → neue CA issuing, alte accepted → alte CA entfernt →
  Konnektivität prüfen. Endzustand: keine alte CA in `acceptedCAs`.
- Die neue talosconfig übernimmt Kontext und Endpoints der aktuellen.
- Im Dry-Run wird die `-o`-Datei nicht geschrieben.
- Flags: `--talos`, `--kubernetes`, `--dry-run` (Default true), `--control-plane-nodes`,
  `--worker-nodes`, `-o`, `--with-docs`, `--with-examples` (Defaults true).

## 3. Befehl

```
talkub rotate-ca [--yes]
talkub rotate-ca --finish
```

### Ablauf (`talkub rotate-ca`)

1. **Vorprüfung** (bricht ab, nichts geändert):
   - Cluster gesund (`talosctl health` über den ersten Control-Plane-`mgmt`).
   - Für jeden Node: Dry-Run-Apply der gerenderten Config ohne Änderungen („No changes“) – sonst ist das
     Repo nicht die Wahrheit. Hinweis: erst `talkub apply`.
   - Kein `talos/talosconfig.previous.sops.yaml` vorhanden (sonst läuft/lief eine Rotation → `--finish`).
2. **Dry-Run** `talosctl rotate-ca --talos=true --kubernetes=false --dry-run=true --with-docs=false
   --with-examples=false --control-plane-nodes <cp mgmt…> [--worker-nodes <worker mgmt…>]`
   (Endpoints aus der talosconfig), Ausgabe geschwärzt. Rückfrage „Rotate the Talos CA now?“
   (Default nein; `--yes` überspringt).
3. **Sicherung**: aktuelle verschlüsselte talosconfig byte-gleich nach
   `talos/talosconfig.previous.sops.yaml` kopieren.
4. **Rotation**: gleicher Aufruf mit `--dry-run=false -o <private_tempdir>/talosconfig`, Ausgabe geschwärzt.
5. **Neue talosconfig sichern** – sofort, vor allem anderen: lesen, Endpoints/Nodes wie `lockdown` bzw. die
   `mgmt`-Adressen setzen (`rewrite_talosconfig`), verschlüsselt nach `talos/talosconfig.sops.yaml`.
6. **`--finish`-Teil** (siehe unten).

### Fehlerfälle

- Fehler in 1–3: nichts rotiert; bei Fehler nach 3 wird die Sicherung wieder gelöscht.
- Fehler in 4 (talosctl Exit ≠ 0) oder 5: das private Temp-Verzeichnis wird **nicht** gelöscht (einzige
  Ausnahme vom Klartext-Grundsatz, weil sein Verlust aussperren kann). Ausgabe: Pfad, Hinweis
  „if the rotation completed, only this talosconfig works: `talkub rotate-ca --finish --talosconfig <path>`“,
  und dass die Datei danach zu löschen ist. `talos/talosconfig.previous.sops.yaml` bleibt liegen.
- Fehler in 6: Repo hat bereits die neue talosconfig; Hinweis `talkub rotate-ca --finish`.

### `talkub rotate-ca --finish [--talosconfig <plaintext path>]`

Setzt voraus, dass `talos/talosconfig.previous.sops.yaml` existiert (sonst Fehler „no rotation in progress“).

1. Mit `--talosconfig`: diese Datei (Klartext, außerhalb des Repos) wie Schritt 5 übernehmen und danach
   den Nutzer auffordern, sie zu löschen (talkub löscht Dateien außerhalb seines Temp-Verzeichnisses nicht).
2. Mit der talosconfig aus dem Repo: `talosctl get machineconfig -o yaml` am ersten Control Plane,
   `machine.ca.crt`/`machine.ca.key` im Speicher entnehmen (Parser in `talos/inspect.py`, nichts ausgeben).
   Plausibilität: `crt` beginnt dekodiert mit `-----BEGIN CERTIFICATE-----`, `key` mit `-----BEGIN`.
3. Secrets-Bundle entschlüsseln, `certs.os.crt`/`certs.os.key` ersetzen, verschlüsselt zurückschreiben
   (Round-trip über `yamlio`, andere Felder unverändert).
4. Abschluss: `talosctl version` an allen Nodes, dann Dry-Run aller Nodes = „No changes“.
   Erfolg → `talos/talosconfig.previous.sops.yaml` löschen, Meldung „commit talos/“.
   Sonst → Sicherung bleibt, Hinweis mit den abweichenden Nodes.

## 4. Architektur

- `src/talkub/rotation.py`: `rotate(ctx, confirm)`, `finish(ctx, talosconfig_path=None)`, Hilfen für
  Sicherung, Übernahme der talosconfig, Bundle-Update.
- `talos/inspect.py`: `machine_ca(text: str) -> tuple[str, str]` aus `get machineconfig -o yaml`
  (Ressource mit `spec:` als YAML-String oder Mapping – beide Formen unterstützen).
- `talos/client.py`: `TalosSession` kann mit einer alternativen talosconfig (Klartextpfad) geöffnet werden
  (`talos_session_from(path)`), für `--finish --talosconfig`.
- `lifecycle.py`: die bestehende Dry-Run-Logik liefert pro Node „changes / no changes“ als Rückgabewert
  (für Vorprüfung und Abschluss wiederverwendet, statt Text zu parsen an zwei Stellen).
- `cli/rotate.py` bzw. Eintrag in `cli/main.py`: `rotate-ca` mit `--yes`, `--finish`, `--talosconfig`.

## 5. Tests

- `machine_ca` gegen realistische `get machineconfig -o yaml`-Ausgabe (beide `spec`-Formen); Fehler bei fehlender CA.
- Bundle-Update: nur `certs.os` ändert sich, Rest byte-gleich nach Round-trip; verschlüsselt.
- `rotate` mit Spy-talosctl: Reihenfolge Vorprüfung → Dry-Run → Rückfrage → Sicherung → Rotation →
  talosconfig geschrieben (vor dem Bundle-Update) → Bundle → Abschluss; Ablehnung ändert nichts;
  Vorprüfung „changes“ bricht ab; `previous` vorhanden → Abbruch mit Hinweis `--finish`.
- Fehler in Rotation: Temp-Verzeichnis bleibt, Pfad in der Ausgabe, `previous` bleibt.
- Kein Secret in der Ausgabe: Spy gibt CA-Key/Client-Key-Zeilen aus → erscheinen nicht.
- `--finish` ohne laufende Rotation → Fehler; `--finish --talosconfig` übernimmt die Datei.
