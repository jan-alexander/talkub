# talkub – `node volumes`: Assistent für Install-Disk, EPHEMERAL und User Volumes – Design

Stand: 2026-09-27.

## 1. Ziel

Beim Einrichten eines Talos-Nodes liest talkub die tatsächlich verbauten Disks und führt interaktiv durch
Install-Disk, EPHEMERAL-Größe und User Volumes. Der Assistent schreibt korrekte CEL-Selektoren (die er selbst gegen
die echten Disks auswertet), übersetzt Größenwünsche in Talos' `minSize`/`maxSize`/`grow`-Semantik und zeigt vorab per
Simulation, welches Volume auf welcher Disk mit welcher Größe landet.

Erfolg: `talkub node volumes cp-01` gegen einen Node im Maintenance Mode schreibt `UnattendedInstallConfig`,
`VolumeConfig` (EPHEMERAL), `UserVolumeConfig`/`RawVolumeConfig` in `talos/nodes/cp-01.yaml`; `talkub apply --insecure
cp-01` installiert, und `talosctl get volumestatus` zeigt alle Volumes `ready` mit den simulierten Größen.

Deterministisch, offline (außer dem Lesen der Disks vom Node), keine neue Abhängigkeit, kein LLM.

## 2. Talos-Semantik (Grundlage, Stand Talos 1.14, aus dem Quellcode verifiziert)

Quellen: `siderolabs/talos` v1.11.0–v1.14.0 (`pkg/machinery/cel/celenv`, `pkg/machinery/config/types/block`,
`internal/app/machined/pkg/controllers/block/internal/volumes`), Doku `docs.siderolabs.com/talos/v1.14/…`
(disk-management, disk-encryption, unattended-install).

**CEL (`celenv.DiskLocator`)**
- Variablen: `disk` (DiskSpec: `size`/`io_size`/`sector_size` uint64; `readonly`, `cdrom`, `rotational` bool;
  `model`, `serial`, `wwid`, `transport`, `dev_path`, `bus_path`, … string; `symlinks` list; ab 1.14
  `firmware_version`) und `system_disk` (bool).
- Einheiten sind uint: `100u * GiB` (ohne `u` Typfehler). Binär `KiB…EiB`, dezimal `kB…EB`.
- Nur Standard-CEL plus `glob(pattern, string)`; keine String-Extensions.
- `talosctl validate` prüft Syntax/Typ, wertet aber nicht gegen Disks aus. Kein Talos-Tool tut das.

**Install-Disk (`UnattendedInstallConfig`, ab 1.14)**: 0 Treffer → `pending`; mehrere → Warnung im Log, **erster**
Treffer; readonly/cdrom werden **nicht** gefiltert; `system_disk` ist dabei immer `false`; `provisioning.wipe`
Standard `true`. Die Install-Disk wird zur Systemdisk.

**Volume-Provisioning (User/Raw/VolumeConfig, `partition`)**
- Kandidaten: alle nicht-readonly Disks, die der Selektor trifft; die Systemdisk ist **nicht** ausgeschlossen.
- Reihenfolge der Volumes: ohne provisioning zuerst; dann wave (Systemvolumes −1, User/Raw 0; spätere Wave wartet auf
  `ready` der früheren); nicht-grow vor grow; kleinere absolute `maxSize` zuerst (ohne/Prozent = maximal); dann ID.
- Pro Volume: erste Kandidaten-Disk (Disk-ID-Reihenfolge), deren größter zusammenhängender freier Bereich ≥ `minSize`
  ist (leere Disk: ≥ `minSize` + GPT-Overhead). Nicht-GPT-Signatur → „wrong format“. Kein Platz → `failed`,
  Retry alle 30 s.
- Größe beim Anlegen: `available` = größter zusammenhängender Freiraum. `maxSize` fehlt oder ≥ available → available;
  sonst `maxSize`. Prozent (ab 1.12) = `available × p / 100`; negativ (ab 1.13) = `available − |x|`.
  **`minSize` bestimmt nur die Disk-Wahl, nicht die Größe.** Standard-`minSize` 100 MiB; mindestens eins von
  `minSize`/`maxSize` ist Pflicht.
- `grow` (Standard false, EPHEMERAL true): wächst später in den direkt folgenden Freiraum bis zur **absoluten**
  `maxSize`; bei Prozent-`maxSize` ohne Obergrenze.
- Nach dem Anlegen wird das Volume per Label (`u-<name>`, `r-<name>`) gefunden; Selektor-/Größenänderungen wirken
  nicht mehr; kein Verkleinern. Dokument entfernen → unmount, Daten bleiben.

**`volumeType: disk`** (ab 1.12, nur UserVolumeConfig): ganze, leere Disk; kein min/max/grow; mehrere Treffer →
harter Fehler. **RawVolumeConfig** (ab 1.11): unformatierte Partition `r-<name>`, gleiche Größenregeln; negative
`maxSize` wird in 1.14 falsch (als positiv) behandelt → nicht anbieten.

**EPHEMERAL**: Standard Selektor `system_disk`, `minSize` 2 GiB, kein `maxSize`, `grow: true` → nimmt die ganze
restliche Systemdisk. Begrenzen nur beim ersten Provisioning wirksam.

**Namen**: 1–34 Zeichen `[A-Za-z0-9-]`; Mount `/var/mnt/<name>`.

**Versionsgrenzen**: `volumeType` und Prozent ab 1.12; negative Größen und `mount` ab 1.13; `UnattendedInstallConfig`,
`btrfs`, `allowDiscards` ab 1.14. Unter 1.14 wird die Install-Disk über `machine.install` gewählt → dieser Befehl
verlangt `talosVersion` ≥ 1.14 für Schritt 2 und ≥ 1.11 für Volumes (sonst `ValidationError` mit Hinweis).

## 3. Befehl und Ablauf

`talkub node volumes <name> [--insecure | --installed]`

Die Node muss in `.talkub.yaml` existieren (`node add`). Zugriffsweg:
- `--insecure`: `talosctl get disks --insecure -n <ip>` (Maintenance Mode, vor `apply --insecure`).
- `--installed`: normale `TalosSession` über `mgmt`/`ip` (`get disks`, `get systemdisk`, `get discoveredvolumes`).
- ohne Flag: zuerst Session versuchen; schlägt sie fehl (Node nicht installiert), `--insecure`. Der gewählte Weg wird
  ausgegeben.

Schritte:

1. **Disks lesen** und als Tabelle zeigen: Index, Gerät, Größe, Transport, SSD/HDD, Modell, Serial, vorhandene
   Partitionen/Dateisysteme (aus `discoveredvolumes`, sofern lesbar). readonly/cdrom werden markiert.
2. **Install-Disk** (nur Maintenance Mode): Auswahl per Index; der Assistent schlägt den portabelsten Selektor vor, der
   genau diese Disk trifft (§4), zeigt ihn samt Treffer und fragt nach Bestätigung oder Alternative (nächst-exaktere
   Stufe). Ergebnis ersetzt ein vorhandenes `UnattendedInstallConfig` (bestehendes `installer`/`wipe` bleibt; ohne
   Dokument: nur `provisioning`, das Installer-Image setzt talkub beim Rendern wie bisher). Installierter Node: Schritt
   entfällt, Systemdisk aus `get systemdisk`.
3. **EPHEMERAL**: „ganze Systemdisk“ (kein Dokument) oder begrenzen (Größen-Dialog §5, ohne „Rest der Disk“).
   Installierter Node mit bereits provisioniertem EPHEMERAL: nur Hinweis, dass eine Änderung einen Wipe braucht; kein
   Dokument wird geschrieben.
4. **Volumes** in einer Schleife: Name → Art (`partition` mit Dateisystem / `raw` / ganze Disk `disk`) → Ziel-Disk per
   Index (Selektor-Vorschlag §4) → Größe (§5, nicht bei `disk`) → Dateisystem (`xfs` Standard, `ext4`, `btrfs` ab 1.14;
   nicht bei `raw`) → Verschlüsselung (§6). Nach jedem Volume: Simulation (§7). Vorhandene Volume-Dokumente im Patch
   werden angezeigt und in die Simulation einbezogen, aber nicht bearbeitet; ein neuer Name, der schon existiert, wird
   abgelehnt.
5. **Abschluss**: YAML-Vorschau aller neuen/ersetzten Dokumente, Bestätigung, Schreiben in `talos/nodes/<name>.yaml`.
   Hinweis auf den nächsten Schritt (`apply --insecure <name>` bzw. `plan`/`apply <name>`).

`node add --disk-selector` bleibt unverändert (nicht-interaktiver Weg); `node add` gibt als `next:` zusätzlich
`talkub node volumes <name>` aus.

## 4. Selektoren

Ein `Selector` ist eine UND-Liste von Prädikaten; `to_cel()` erzeugt den Ausdruck, `matches(disk, system_disk)` wertet
ihn in Python mit derselben Semantik aus.

Prädikate: `transport == "<t>"`, `rotational` / `!rotational`, Größenbereich `size >= Au * GiB && size <= Bu * GiB`
(auf ganze GiB gerundet, ±10 % um die Zielgröße, nicht überlappend mit anderen Disks), `model == "<m>"`,
`serial == "<s>"`, `wwid == "<w>"`, `system_disk` / `!system_disk`, sowie die Standard-Ausschlüsse.

**Standard-Ausschlüsse** (Install-Disk und Volumes): `!disk.readonly && !disk.cdrom && disk.transport != "usb"`.
Ist die gewählte Disk selbst USB, entfällt `transport != "usb"` und es gibt eine Warnung.

**`suggest(disks, target, system_disk)`**: probiert Stufen in dieser Reihenfolge und nimmt die erste, die unter den
Kandidaten genau `target` trifft: (1) Transport, (2) + SSD/HDD, (3) + Größenbereich, (4) + Modell, (5) Serial,
(6) WWID. Bei Volumes ist `!system_disk` bzw. `system_disk` Teil jeder Stufe, wenn die Systemdisk bekannt ist. Hat
`target` weder Serial noch WWID und ist nach (4) nicht eindeutig: Fehler mit Hinweis auf eigenen Ausdruck.

**Eigener Ausdruck**: erlaubt; wird nur bei `talosctl validate` (beim Rendern) geprüft. Der Assistent kann ihn nicht
auswerten, zeigt „Treffer nicht verifiziert“ und lässt das Volume aus der Simulation heraus (mit Warnung). Bei
`volumeType: disk` sind nur generierte Selektoren erlaubt.

## 5. Größen-Dialog

Gefragt wird nach der Absicht; der Assistent übersetzt:

| Auswahl | erzeugt | Erklärung im Dialog |
|---|---|---|
| Feste Größe X | `minSize: X`, `maxSize: X` | min = max: nie verkleinert auf einer knappen Disk |
| Rest der Disk | `minSize: <Untergrenze, Standard 1GiB>`, ohne `maxSize` | größter *zusammenhängender* Freiraum beim Anlegen |
| Prozent (ab 1.12) | `maxSize: "N%"`, `minSize: 100MiB`, `grow: false` | N % des *freien* Bereichs, nicht der Disk |
| Alles bis auf X (ab 1.13, nicht `raw`) | `maxSize: "-X"`, `minSize: 100MiB`, `grow: false` | vom freien Bereich abgezogen |

„Später wachsen?“ (`grow: true`) wird nur bei „Rest der Disk“ angeboten: das Volume wächst dann in Platz, der direkt
dahinter frei wird (z. B. vergrößerte VM-Disk). Bei fester Größe hätte `grow` keine Wirkung (Obergrenze = Startgröße),
bei Prozent/negativ wüchse es unbegrenzt → dort immer `grow: false`. EPHEMERAL bekommt beim Begrenzen explizit
`grow: false` (Talos-Standard ist `true`).

Eingaben werden wie Talos geparst (go-humanize): `GB` dezimal, `GiB` binär; der Dialog zeigt beides an
(„100GB = 93.1 GiB“).

## 6. Verschlüsselung

Optional pro Volume und für EPHEMERAL: `provider: luks2`, ein Key `slot: 0`, `nodeID: {}`, `lockToState: true`.
Erklärung im Dialog: schützt Daten auf ausgebauten Disks; nach STATE-Wipe (Reset) nicht mehr entsperrbar (gewollt).
Installierter Node: Hinweis, dass nur neu angelegte, leere Volumes verschlüsselt werden.

Nicht angeboten (Folgearbeit):
- **TPM**: Talos versiegelt TPM-Keys mit dem UKI-PCR-11-Key; talkub erzeugt nur Nicht-SecureBoot-Installer
  (`<platform>-installer`). Kommt mit SecureBoot-Support.
- **static**: die Passphrase stünde im Klartext im Node-Patch (Git). Bräuchte SOPS-verschlüsselte Node-Patches.
- **kms**: braucht einen KMS-Server.

## 7. Simulation

`simulate(disks, system_disk, install, volumes, version) -> Plan` bildet §2 nach:
- Install-Disk (Maintenance Mode, `wipe: true`): als leer angenommen bis auf das Talos-Systemlayout (EFI, BIOS, BOOT,
  META, STATE; Konstanten aus dem Talos-Quellcode der Zielversion, Anzeige mit „≈“). Andere Disks: belegte Bereiche aus
  `discoveredvolumes` (Offset/Größe, ab 1.12); fehlen Offsets → belegte Partitionen zählen als Block am Anfang und die
  Anzeige sagt „Simulation ungenau“.
- Reihenfolge und Disk-Wahl wie Talos; 1 MiB-Ausrichtung und GPT-Overhead berücksichtigt.
- Ausgabe pro Disk: Belegung in Talos-Reihenfolge, Systemdisk mit ★; darunter Fehler und Warnungen.

**Harte Fehler** (Volume wird nicht übernommen, Dialog fragt neu): Selektor trifft 0 Disks; `disk`-Typ trifft mehrere
oder eine nicht leere Disk; kein Platz; Feature nicht in `talosVersion`; ungültiger Name; min > max.

**Warnungen** (bestätigen): Selektor trifft mehrere Disks, Ergebnis hängt an der Reihenfolge; Volume liegt auf der
Systemdisk und EPHEMERAL ist nicht begrenzt (→ Volume bekommt keinen Platz, wird zum Fehler); Ziel-Disk hat
Nicht-GPT-Inhalt (Volume wird dort nicht angelegt); installierter Node: bestehende Volumes ändern sich nicht.

## 8. Module

Innerhalb der bestehenden Paketgrenzen (import-linter):

- `talos/inspect.py`: `disks(text) -> list[Disk]`, `system_disk(text) -> str | None`,
  `discovered_volumes(text) -> list[Partition]` aus dem JSON-Stream (`json_documents`). Fehlende `omitempty`-Felder →
  `False`/`""`; unbrauchbare Dokumente werden übersprungen.
- `talos/volumes/` (reine Logik, keine I/O):
  - `model.py`: `Disk`, `Partition`, `Size` (bytes | percent | negative), `Encryption`, `VolumeSpec`
    (name, kind `partition|raw|disk|ephemeral`, selector, min, max, grow, filesystem, encryption), `InstallSpec`.
  - `selector.py`: `Predicate`, `Selector` (`to_cel`, `matches`), `DEFAULT_EXCLUSIONS`, `suggest`.
  - `sizes.py`: Parsen/Formatieren wie go-humanize, Absicht → min/max/grow (§5).
  - `validate.py`: Namen, Feldkombinationen, Versionsgrenzen gegen `talosVersion`.
  - `simulate.py`: §7.
  - `render.py`: `VolumeSpec`/`InstallSpec` → Dokument-Dicts.
- `talos/nodes.py`: `upsert_documents(path, docs)`: liest den Multi-Doc-Patch, ersetzt Dokumente mit gleichem
  (`kind`, `name`) bzw. das einzige `UnattendedInstallConfig`, hängt neue an; Header-Kommentar und übrige Dokumente
  bleiben; atomar geschrieben.
- `talos/disks.py`: Disks, Systemdisk und Partitionen lesen über `TalosSession` bzw. `--insecure`.
- `cli/volumes.py`: Dialog mit `typer.prompt`/`typer.confirm`, als `node volumes` in `cli/nodes.py` registriert.

## 9. Tests

TDD, Struktur gespiegelt (`tests/talos/volumes/…`, `tests/cli/test_cli_volumes.py`):
- `inspect`: Fixtures für `get disks`/`systemdisk`/`discoveredvolumes` (auch mit fehlenden Feldern, kaputtem Stream);
  möglichst von einem echten Node aufgenommen.
- `selector`: `to_cel` (immer `u`-Suffix, Standard-Ausschlüsse, USB-Ausnahme); `suggest` trifft genau eine Disk und
  wählt die portabelste Stufe; `matches` stimmt mit `to_cel` überein.
- `sizes`: GB/GiB, Prozent, negativ, Absicht → Felder.
- `simulate`: EPHEMERAL frisst Systemdisk; begrenztes EPHEMERAL + Volume auf Systemdisk; Prozent vom Freiraum; negativ;
  Reihenfolge (wave, grow, maxSize); kein Platz; `disk`-Typ mehrfach/nicht leer; bestehende Partitionen.
- `validate`: Versionsgrenzen 1.11–1.14, Namen, verbotene Kombinationen.
- `render` + `upsert_documents`: Golden-YAML; Header und fremde Dokumente bleiben; `UnattendedInstallConfig` ersetzt.
- CLI: Fake-Runner (`tests/helpers.py`) mit vorgegebenen Eingaben; Wegwahl Session/`--insecure`.
- Integration (übersprungen ohne talosctl): `talosctl validate --mode metal` auf einem mit den erzeugten Dokumenten
  gerenderten Config.

## 10. Nicht im Umfang

Bearbeiten/Löschen bestehender Volume-Dokumente; `volumeType: directory`; ExistingVolumeConfig; Swap; dedizierte
ETCD/CRI/KUBELET/LOG-Volumes (1.14); TPM/static/kms-Keys; `machine.install` für Talos < 1.14; Auswertung frei
eingegebener CEL-Ausdrücke.
