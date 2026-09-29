# talkub – Node-Platzhalter in Schnellbefehlen – Design

Stand: 2026-09-29. Ergänzt `2026-09-26-talkub-expose-and-shortcuts-design.md` und
`2026-09-27-talkub-global-shortcuts-design.md`.

## 1. Ziel

`run`- und `kubernetes`-Shortcuts sollen sich auf einen Node beziehen können, ohne ihn hart zu kodieren.
Damit werden node-bezogene Befehle (z. B. `talosctl -n <node> get extensions`) auch als globale Shortcuts
nutzbar: der Node wird beim Aufruf gewählt.

Erfolg: Mit

```yaml
shortcuts:
  ext:
    description: installed extensions
    run: [t, -n, "{node}", get, extensions]
```

führt `talkub run ext cp-01 -o yaml` in jedem Cluster-Repo `talosctl -n <mgmt-oder-ip von cp-01> get
extensions -o yaml` aus; in einem Repo mit genau einem Node genügt `talkub run ext`.

Nicht-Ziele (YAGNI):
- Feste Auswahl in der Definition (`{nodes[0]}`, `{nodes.controlplane}`, Mengen/Listen).
- Cluster-weite Platzhalter (`{cluster}`, `{tailnet}` …).
- Interaktive Node-Auswahl per Prompt.
- Platzhalter in `secret`/`job`/`portForward`-Shortcuts.
- Pipes (Ausgabe in weitere Programme leiten): eigener Spec, folgt.

## 2. Platzhalter

Nur in `run`- und `kubernetes`-Shortcuts, nur in den konfigurierten Argumenten (nie in den beim Aufruf
angehängten Extras):

| Platzhalter    | Wert                                                                  |
|----------------|-----------------------------------------------------------------------|
| `{node}`       | Kurzform für `{node.addr}`                                            |
| `{node.addr}`  | `mgmt`, falls gesetzt, sonst `ip` (wie `Settings.mgmt_address`)       |
| `{node.name}`  | Node-Name (Schlüssel unter `nodes:`), z. B. `k get node {node.name}`  |
| `{node.ip}`    | `ip`                                                                  |
| `{node.mgmt}`  | `mgmt`; nicht gesetzt → Fehler beim Aufruf (kein stiller Fallback)    |
| `{node.role}`  | `controlplane` bzw. `worker`                                          |
| `{kubeconfig}` | unverändert: Pfad der entschlüsselten kubeconfig, nur bei `kubernetes`|

Ein Argument darf mehrere Platzhalter enthalten und sie mit Text kombinieren (`--nodes={node}`,
`{node.name}.log`).

**Abgrenzung zu kubectl-Syntax.** jsonpath (`{.items[0].metadata.name}`) und Go-Templates
(`{{.status}}`) enthalten ebenfalls geschweifte Klammern. Ersetzt wird deshalb ausschließlich das Muster
`\{node(?:\.([a-z]+))?\}` (plus das bestehende `{kubeconfig}`); alles andere bleibt Literal. Beim Laden
wird jedes `{node.<irgendwas>}` geprüft, damit Tippfehler wie `{node.mgmt_ip}` oder `{node.ADDR}` nicht
still als Literal bei talosctl landen.
Bestehende Shortcuts bleiben ohne Änderung gültig.

**Prüfung beim Laden.** Ein `{node.<feld>}` mit unbekanntem Feld ist ein `ValidationError` beim Laden
von `.talkub.yaml` bzw. des globalen Pools, z. B.
`.talkub.yaml: shortcuts.ext.run: unknown placeholder {node.adr}` mit
`hint: one of {node}, {node.addr}, {node.name}, {node.ip}, {node.mgmt}, {node.role}`.
Ob `mgmt` gesetzt ist, zeigt sich erst beim Aufruf.

## 3. Node-Wahl beim Aufruf

Ein Shortcut *braucht einen Node*, wenn mindestens eines seiner konfigurierten Argumente einen
`{node…}`-Platzhalter enthält. Dann gilt beim Aufruf `talkub run <name> [args...]`:

1. Ist das erste Extra-Argument vorhanden und beginnt es nicht mit `-`, ist es der Node-Name.
   Unbekannter Name → `error: unknown node 'cp-9'`, `hint: one of cp-01, cp-02, w-01`.
   Ein Argument mit führendem `-` ist eine Option (Node-Namen sind DNS-Labels und beginnen nie mit
   `-`); dann gilt der Node als weggelassen, z. B. `talkub run ext -o yaml` im Ein-Node-Cluster.
2. Fehlt es und hat der Cluster genau einen Node, wird dieser genommen.
3. Fehlt es und hat der Cluster mehrere (oder keine) Nodes →
   `error: shortcut ext needs a node`, `hint: one of cp-01, cp-02, w-01`
   (ohne Nodes: `hint: add one with talkub node add`).
4. Die übrigen Extras werden wie bisher unverändert angehängt.

Shortcuts ohne `{node…}` verhalten sich exakt wie bisher (das erste Extra-Argument ist kein Node).

Die Node-Platzhalter werden aufgelöst, bevor irgendetwas entschlüsselt wird; ein falscher Node oder
fehlendes `mgmt` wird also gemeldet, ohne dass ein Hardware-Key berührt werden muss. `{kubeconfig}` wird
weiterhin erst nach dem Entschlüsseln in `access.with_kubeconfig` ersetzt.

## 4. Anzeige und Completion

- `talkub run` (Liste) zeigt Shortcuts mit Node-Parameter als `ext <node>`.
- `talkub run <name> <TAB>` schlägt Node-Namen des aktuellen Repos vor, wenn `<name>` einen Node braucht;
  sonst keine Vorschläge (wie bisher).
- Usage-Text: `talkub run [--print] [--follow] <name> [<node>] [args...]`.

## 5. Umsetzung

**`config/shortcuts.py`** (nur Syntax, kennt keine `Settings`):
- `NODE_PLACEHOLDER` (kompilierte Regex) und `NODE_FIELDS = ("addr", "name", "ip", "mgmt", "role")`.
- `shortcut_from_dict` prüft bei `run` und `kubernetes` jedes Argument auf unbekannte Felder (Abschnitt 2).
- `needs_node(shortcut) -> bool`.

**`workstation/shortcuts.py`**:
- `pick_node(settings, shortcut_name, extra) -> tuple[str, list[str]]` – Regeln aus Abschnitt 3.
- `fill_node(argv, settings, node) -> list[str]` – ersetzt die Platzhalter; `{node.mgmt}` ohne `mgmt` →
  `ValidationError(..., hint="use {node.addr} or set mgmt on the node")`.
- `run()`: bei `needs_node` vor der Verzweigung `pick_node` + `fill_node`, dann weiter wie bisher über
  `access.talosctl` / `access.kubectl` / `access.with_kubeconfig`.
- `listing()`: Anzeige `name <node>`.

**`cli/main.py`, `cli/completion.py`**: Usage-Text; in `_run_help` ein zweites, nur für Completion
deklariertes Argument mit Node-Vorschlägen (`completion.node_names`).

**README.md**: Abschnitt Shortcuts um Platzhalter, Node-Wahl und Beispiel ergänzen; den Satz zur
Completion nach dem Shortcut-Namen anpassen.

Keine neuen Pakete oder Abhängigkeiten; Schichtenvertrag unverändert (`config` → Syntax,
`workstation` → Auflösung gegen `Settings`).

## 6. Tests

- `tests/config/test_shortcuts.py`: gültige Platzhalter in `run`/`kubernetes`; unbekanntes Feld →
  Fehler mit Pfad; jsonpath `{.items[0]}` und `{{.x}}` bleiben gültig und unverändert; Round-Trip über
  `shortcut_to_dict`; `needs_node`.
- `tests/workstation/test_shortcuts.py` (FakeRunner bzw. `spy`): Node als erstes Argument + Extras
  dahinter; unbekannter Node; ein Node ohne Argument; mehrere Nodes ohne Argument → Fehler mit Liste;
  `{node}`/`{node.addr}` mit und ohne `mgmt`; `{node.mgmt}` fehlt → Fehler vor dem Entschlüsseln;
  `{node.name}`, `{node.ip}`, `{node.role}`; mehrere Platzhalter in einem Argument; `kubernetes` mit
  `{node}` und `{kubeconfig}`; globaler Shortcut mit Node in einem Repo; Shortcut ohne Platzhalter
  unverändert; Listing zeigt `<node>`.
- `tests/cli/`: Completion schlägt Node-Namen nur für Shortcuts mit Node vor.
