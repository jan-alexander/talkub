# talkub – Tailscale-Ingress (`expose`) und Schnellbefehle (`run`) – Design

Stand: 2026-09-26. Ergänzt `2026-09-25-talkub-tailscale-operator-design.md`.

## 1. Ziel

- Cluster-Dienste per Befehl ins Tailnet veröffentlichen (`https://<name>.<tailnet>`), GitOps-konform.
- Wiederkehrende Handgriffe als Schnellbefehle in `.talkub.yaml`: Secret auslesen, Job aus CronJob
  starten, beliebiger kubectl-/talosctl-Aufruf, Port-Forward.

Erfolg: `talkub tailscale expose grafana --service monitoring/grafana --port 80` + commit/push → Dienst
unter `https://grafana.<tailnet>`; `talkub run argocd-password` legt das Passwort in die Zwischenablage,
ohne es auszugeben.

Beobachtet am echten Cluster (2026-09-25): Argo CD über das Chart (`server.ingress` mit
`ingressClassName: tailscale`, `configs.params.server.insecure: true`) funktioniert; das `host`-Feld der
Regel stört den Tailscale-Ingress nicht; Zertifikat von Let's Encrypt für `<name>.<tailnet>`.

## 2. `talkub tailscale expose` / `unexpose`

```
talkub tailscale expose <name> --service <namespace>/<service> --port <number|name> [--path /]
talkub tailscale unexpose <name>
```

- Vorbedingung: Operator aktiviert (`gitops/apps/tailscale-operator.yaml`).
- `name`: DNS-1123-Label (≤ 63), wird zum Tailnet-Gerätenamen.
- `--service` genau `ns/svc` (beide DNS-1123); `--port` Zahl 1–65535 oder Portname (DNS-1123-Label).
- Datei `gitops/manifests/tailscale/ingress/<name>.yaml` (existiert → Fehler, Hinweis `unexpose`):

```yaml
# Managed by talkub (talkub tailscale expose).
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tailscale-<name>
  namespace: <namespace>
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: <service>
      port: {number: <n>}      # oder {name: <portname>}
  tls:
    - hosts: [<name>]
```
  Mit `--path` statt `defaultBackend` eine Regel ohne `host` mit diesem Pfad (`pathType: Prefix`).
- Ausgabe: URL `https://<name>.<tailnet>`, Hinweis „commit and push gitops/“, Hinweis: Backend muss HTTP
  sprechen (TLS endet am Proxy), HTTPS-Zertifikate müssen im Tailnet aktiviert sein.
- `unexpose`: Datei löschen (fehlt → Fehler).
- README: Abschnitt „Ingress“ ersetzt das bisherige „Not automated“ durch `expose`; Argo CD über
  Chart-Werte als Beispiel (die beiden Werte oben).

## 3. Schnellbefehle

### Datenmodell (`.talkub.yaml`)

```yaml
shortcuts:
  <name>:
    description: <optional text>
    # genau eine der folgenden Arten:
    secret: {namespace: <ns>, name: <secret>, key: <key>}
    job: {namespace: <ns>, cronjob: <cronjob>, follow: <bool, default false>}
    run: [<k|kubectl|t|talosctl>, <arg>...]
    portForward: {namespace: <ns>, service: <svc>, ports: ["<local>:<remote>", ...]}
```
- `Settings.shortcuts: dict[str, Shortcut]` (fehlt → leer, wird nur geschrieben wenn nicht leer).
- Validierung beim Laden (`ValidationError` mit Pfad `shortcuts.<name>.…`): Name DNS-1123-Label; genau eine
  Art; Pflichtfelder nicht leer; `run` nicht leer und erstes Element in {k, kubectl, t, talosctl};
  `ports` nicht leer, jedes `<1-65535>:<1-65535>` oder nur `<1-65535>`; `follow` bool.

### CLI

```
talkub run                       # Liste: Name, Art, Beschreibung
talkub run [--print] [--follow] <name> [weitere Argumente…]
```
- `run` wird wie `t`/`k` in `cli/main.py` roh geparst (keine Typer-Optionen hinter dem Namen, damit z. B.
  `-f` an kubectl weitergeht). `--print`/`--follow` nur vor dem Namen. Unbekannter Name → Fehler mit Liste.
- Weitere Argumente: bei `run` angehängt; bei den anderen Arten → Fehler („takes no extra arguments“).

### Verhalten je Art (`src/talkub/shortcuts.py`)

- **secret**: `kubectl -n <ns> get secret <name> -o json` in `kube_session`, `data.<key>` base64-dekodiert im
  Speicher (fehlt → Fehler mit vorhandenen Keys, ohne Werte).
  - Standard: Zwischenablage. Werkzeug in dieser Reihenfolge: `pbcopy` (macOS), `wl-copy`,
    `xclip -selection clipboard`; keins → Fehler mit Hinweis `--print`. Wert über stdin.
  - Ausgabe `copied <name> (<n> chars); the clipboard is cleared in 45 s`.
  - Leeren: abgelöster Hilfsprozess `python -m talkub.clipboard_clear` (über `Runner.spawn_detached`,
    neue Session, stdin/stdout/stderr getrennt), bekommt über stdin den SHA-256 des Werts, schläft 45 s,
    liest die Zwischenablage (`pbpaste`/`wl-paste`/`xclip -o`), leert sie nur bei gleichem Hash
    (`pbcopy`/`wl-copy`/`xclip` mit leerer Eingabe). Hash und Wert nie in argv/env.
  - `--print`: Wert auf stdout (ohne zusätzliche Ausgabe; Zeilenumbruch nur wenn stdout ein Terminal ist).
- **job**: Name `<cronjob>-manual-<YYYYMMDDHHMMSS>` (auf 63 Zeichen gekürzt, ohne `-` am Ende),
  `kubectl -n <ns> create job <name> --from=cronjob/<cronjob>`; Ausgabe des Namens.
  - Mit `follow` (Konfig oder `--follow`): `kubectl -n <ns> wait --for=condition=Ready pod -l job-name=<name>
    --timeout=5m` (Fehler toleriert, falls der Pod schon fertig ist), dann `kubectl -n <ns> logs -f job/<name>`
    (interaktiv durchgereicht), dann Status per `kubectl -n <ns> get job <name> -o json`: `succeeded ≥ 1` → 0,
    sonst 1 mit Hinweis.
- **run**: `access.kubectl(ctx, args)` bzw. `access.talosctl(ctx, args)` mit den konfigurierten plus weiteren
  Argumenten; Exit-Code durchreichen.
- **portForward**: `kubectl -n <ns> port-forward svc/<svc> <ports…>` interaktiv in `kube_session` (Ausgabe
  durchgereicht); Strg-C beendet sauber (Temp-Verzeichnis wird aufgeräumt).

## 4. Architektur

- `src/talkub/repo.py`: `Shortcut`-Datenklassen (`SecretShortcut`, `JobShortcut`, `RunShortcut`,
  `PortForwardShortcut` oder eine Klasse mit `kind` + Feldern), Parsing/Validierung, `to_dict`.
- `src/talkub/shortcuts.py`: Ausführung je Art, Zwischenablage-Helfer.
- `src/talkub/clipboard_clear.py`: `python -m`-Einstieg für den Leer-Prozess.
- `src/talkub/runner.py`: `spawn_detached(argv, *, input: bytes) -> None` (einziger neuer Prozessstart-Pfad).
- `src/talkub/tailscale_operator.py`: `ingress(...)`-Renderer, `expose`/`unexpose`.
- `cli/main.py`: `run` als roher Durchreich-Befehl; `cli/tailscale.py`: `expose`/`unexpose`.
- README: Abschnitte „Shortcuts“ und „Ingress“ (aktualisiert).

## 5. Tests

- Settings: gültige/ungültige Schnellbefehle (jede Regel), Round-trip, fehlender Block bleibt weg.
- `expose`/`unexpose`: Renderer (defaultBackend mit Zahl/Name, `--path`-Variante), Validierung,
  Vorbedingung, vorhandene Datei, `talkub check` sauber.
- secret: Spy-kubectl liefert Secret-JSON; Spy-pbcopy erhält den Wert nur über stdin; Wert nicht in
  capfd/argv/Dateien; `spawn_detached` erhält nur den Hash über stdin; fehlender Key; kein Clipboard-Tool →
  Fehler; `--print`.
- clipboard_clear: gleicher Hash → leeren, anderer → nichts (Spy-pbpaste/pbcopy, Schlafzeit per Env
  `TALKUB_CLIPBOARD_CLEAR_SECONDS` in Tests auf 0).
- job: Name/Kürzung, create-Aufruf, follow-Pfad mit Erfolg/Misserfolg, extra Argumente → Fehler.
- run: k/t-Routing, angehängte Argumente, Exit-Code.
- portForward: argv, Aufräumen.
- CLI: `talkub run` Liste, unbekannter Name, `--print`/`--follow` vor dem Namen, `-f` hinter dem Namen geht
  an kubectl.
