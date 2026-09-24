# talkub – Git-Repo-Zugang für Argo CD – Design

Stand: 2026-09-24. Ergänzt `2026-09-23-talkub-design.md` und `2026-09-23-talkub-phase2-design.md`.

## 1. Ziel

Beim ersten echten Cluster (Hetzner, `homelab`) zeigte sich:

- Die Git-URL wird nur einmal von `init` in drei Manifeste geschrieben und ist danach nirgends
  gespeichert. Sie zu ändern heißt, von Hand in `gitops/` zu suchen.
- Private Repos gehen nicht: Argo CD braucht Zugangsdaten, bevor es das Repo lesen kann.
  Ein SealedSecret *im* Repo löst das nicht (Henne-Ei).
- `init` legt kein Git-Repo an; die Schlüssel-Sicherung unter `bootstrap/` landet so leicht
  nirgends.

Erfolg: `talkub repo set-url`, `talkub repo credentials`, `talkub repo apply` bringen ein
bestehendes Cluster-Repo (ohne `git`-Block, mit Platzhalter-URL) auf ein privates Repo
(SSH-Deploy-Key oder HTTPS-Token), ohne dass Klartext-Zugangsdaten ins Repo gelangen.

## 2. Entscheidungen

| Frage | Entscheidung |
|---|---|
| Wo lebt die URL? | `.talkub.yaml` → `git.url` (optional, für ältere Repos). |
| Auth-Arten | SSH-Deploy-Key **und** HTTPS-Token; gewählt anhand der URL. |
| Wo liegen Zugangsdaten? | `bootstrap/repo-credentials.sops.yaml`, SOPS mit dem Master-Key, wie die Sealing-Key-Sicherung. |
| Wer bringt sie in den Cluster? | talkub per `kubectl apply --server-side` (nicht Argo CD). |
| `set-url` ändert den Cluster? | Nein. Nur Dateien; `repo apply` ist ein eigener Schritt (erst pushen). |
| `init` und Git | `git init -b main` + `git remote add origin`, falls noch nicht vorhanden. Kein Commit. |

## 3. Datenmodell

```yaml
git:
  url: git@github.com:jam/homelab.git
```

- `Settings.git: GitSpec | None`, `GitSpec(url: str)`. Fehlt der Block, bleibt er beim
  Schreiben weg (bestehende Repos ändern sich nicht, bis `set-url` läuft).
- `init --git-url` schreibt ihn.
- URL-Klassen (`talkub/gitrepo.py`, `classify_url(url) -> GitUrl`):
  - `ssh`: `git@host:path` (scp-Form) oder `ssh://[user@]host[:port]/path`.
  - `https`: `https://host[:port]/path`.
  - Alles andere (inkl. `http://`, `file://`, leer) → `ValidationError` mit Beispiel.
  - `GitUrl` trägt `kind`, `host`, `port | None`.
- Platzhalter `PLACEHOLDER_URL = "https://example.invalid/CHANGE-ME/<name>.git"` ist eine
  Konstante; `init` ohne echte URL gibt es weiterhin nicht (`--git-url` bleibt Pflicht).

## 4. `talkub repo set-url <url>`

1. `classify_url(url)`.
2. Alte URL = `settings.git.url`, sonst die Platzhalter-URL des Clusters (`name` einsetzen).
3. In `gitops/root.yaml` und jedem `gitops/apps/*.yaml` (Multi-Dokument erlaubt) wird jedes
   `spec.source.repoURL` und jedes `spec.sources[*].repoURL`, das **exakt** der alten URL
   entspricht, auf die neue gesetzt. Round-trip über `yamlio` (Kommentare bleiben). Dateien
   ohne Treffer werden nicht angefasst.
4. Kein einziger Treffer und kein `git`-Block → `ValidationError` (unbekannte alte URL;
   Hinweis: `repoURL` von Hand prüfen).
5. Gibt es `bootstrap/repo-credentials.sops.yaml`: Art (ssh/https) muss zur neuen URL passen,
   sonst `ValidationError` mit Hinweis `talkub repo credentials`. Passt sie, wird `stringData.url`
   per decrypt → ändern → encrypt aktualisiert.
6. `settings.git = GitSpec(url)`; schreiben.
7. Ausgabe: geänderte Dateien, dann `next: commit, push, then talkub repo apply`.

Idempotent: gleiche URL erneut → keine Änderungen, Meldung „already set“.

## 5. `talkub repo credentials`

Braucht `settings.git`. Verhalten nach URL-Art:

**SSH**
- `ssh-keygen -t ed25519 -N "" -C talkub-<name> -f <private_tempdir>/key` (Klartext nur im
  0700-Temp-Verzeichnis).
- Secret (s. u.) mit `sshPrivateKey`.
- Ausgabe: Public Key + „als read-only Deploy Key im Repo eintragen“.
- Known hosts: Ist der Host nicht in Argo CDs Default-Liste (`github.com`, `gitlab.com`,
  `bitbucket.org`, `ssh.dev.azure.com`, `vs-ssh.visualstudio.com`) oder ist ein Port gesetzt:
  `ssh-keyscan [-p port] host`, SHA256-Fingerprints anzeigen, `typer.confirm` (Default nein).
  Bestätigt → Zeilen in `gitops/values/argocd.yaml` unter `configs.ssh.extraHosts`
  (String, Zeilen angehängt, Duplikate vermieden; Host-Präfix `[host]:port` bei Port).
  Abgelehnt → Abbruch, nichts geschrieben.

**HTTPS**
- `username` per Prompt (sichtbar), Token per verstecktem Prompt (`hide_input`), beides
  nicht leer. Optional `--username`/`--token-file` für nicht-interaktive Nutzung.
- Secret mit `username`/`password`.

**Secret** (SOPS-verschlüsselt über die bestehende `.sops.yaml`-Regel – alle Werte, wie bei der Sealing-Key-Sicherung):
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: repo-<name>
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: <git.url>
  sshPrivateKey: |   # or username/password
    ...
```

- Existiert die Datei schon: `typer.confirm("replace existing repo credentials?")`.
- `--remove`: Datei löschen (Cluster unberührt; Hinweis `talkub k -n argocd delete secret repo-<name>`).
- Ausgabe endet mit `next: commit, push, then talkub repo apply`.

## 6. `talkub repo apply`

Gemeinsame Funktion `gitrepo.apply(ctx, kube)`, auch von `bootstrap` genutzt:

1. Gibt es die Credentials-Datei: entschlüsseln (`decrypted`) → `kubectl apply --server-side -f`.
2. `kubectl apply -f gitops/root.yaml`.

`bootstrap` ersetzt seinen bisherigen Schritt „applying gitops/root.yaml“ durch diesen Aufruf
(Credentials vor Root-App). Warnung in `repo apply` und `bootstrap`, wenn `root.yaml` noch die
Platzhalter-URL enthält.

## 7. `init` und Git

Nach dem Befüllen (in `init_cluster`, vor dem Cleanup-Ende):

- Kein `.git` → `git init -b main`.
- `git remote get-url origin` schlägt fehl → `git remote add origin <url>`.
- Git fehlt auf dem System → Warnung, kein Abbruch (`require_tool` nicht hart).
- Kein Commit. Die Abschlussmeldung nennt `git add -A && git commit`.

## 8. `talkub check`

`bootstrap/repo-credentials.sops.yaml` wird wie andere `*.sops.yaml` geprüft (muss SOPS-
verschlüsselt mit dem Master-Key sein). Zusätzlich: eine Datei unter `bootstrap/` mit
`argocd.argoproj.io/secret-type` im Klartext → Finding.

## 9. Architektur

- `src/talkub/gitrepo.py`: `GitUrl`, `classify_url`, `rewrite_repo_urls(repo, old, new) -> list[Path]`,
  `credentials_path(repo)`, `build_secret(...)`, `write_credentials`, `update_credentials_url`,
  `known_host_lines`/`add_extra_hosts`, `apply(ctx, kube)`, `git_init(repo, url, runner)`.
- `src/talkub/cli/repo.py`: Typer-Gruppe `repo` mit `set-url`, `credentials`, `apply`.
- `repo.py`: `GitSpec`, `Settings.git`, `Repo.bootstrap_dir` falls noch nicht vorhanden.
- `runner.py`: `INSTALL_HINTS` für `git`, `ssh-keygen`, `ssh-keyscan`.

## 10. Fehlerbehandlung

- Unbekannte URL-Form, fehlender `git`-Block, Art-Konflikt → `ValidationError` mit Hinweis.
- `ssh-keyscan` ohne Ergebnis → `CommandFailed` („host unreachable or no SSH on port“).
- Kein Kubeconfig bei `repo apply` → wie andere Kube-Befehle („run talkub bootstrap first“).

## 11. Tests

- `classify_url`: scp-Form, `ssh://` mit/ohne Port/User, https, ungültige Formen.
- `rewrite_repo_urls`: Kommentare bleiben, Chart-`repoURL`s unberührt, `source` und `sources`,
  Multi-Dokument, keine Treffer → keine Schreibzugriffe.
- `set-url`: alter Platzhalter ohne `git`-Block, idempotent, Credentials-URL aktualisiert,
  Art-Konflikt.
- `credentials`: Spy `ssh-keygen`/`ssh-keyscan`; Datei ist verschlüsselt, Klartext-Key nie im
  Repo; Default-Host ohne Keyscan; eigener Host mit Port → `[host]:port`; Ablehnung schreibt
  nichts; HTTPS mit `--token-file`.
- `repo apply` / `bootstrap`: Reihenfolge Credentials → root; ohne Credentials nur root.
- `init`: `.git` und `origin` angelegt; vorhandenes `.git`/`origin` bleibt; ohne `git` Warnung.
- `check`: Klartext-Repo-Secret unter `bootstrap/` wird gemeldet.

## 12. Nicht enthalten

- GitHub-App-Auth, Credential-Templates (`repo-creds`) für mehrere Repos.
- Automatisches Committen/Pushen.
- Rotation eines Deploy-Keys beim Hoster (nur lokal neu erzeugen + Hinweis).
