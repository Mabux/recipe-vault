# Recipe Vault – Importer

Holt neue Dateien aus einem Eingangsordner (z. B. ein per Cloud-Sync gespiegelter OneDrive-Ordner) und legt sie als Rezepte im privaten **Daten-Repo** an (`REPO_URL`; Vorlage: [`examples/data-repo/`](../examples/data-repo/)). Dazu nimmt er Dateien und Links aus der App entgegen (**+ Rezept**).

```
OneDrive o. Ä. ─(Cloud-Sync, nur Download)→ Eingangsordner ─(dieser Container)→ Claude → Commit/Push → Gitea → App
```

## Ablauf

Alle 5 Minuten (`INTERVAL`) wird der Ordner gescannt. Eine Datei wird verarbeitet, wenn ihr Pfad **und** ihr Inhalt neu sind und sie seit 2 Minuten unverändert ist (Cloud Sync ist fertig).

1. Claude (`claude-opus-5-5`) bekommt die Datei: PDF als Dokument, Fotos als Bild (auch HEIC), DOCX/XLSX/TXT/MD als Text – dazu die Regeln aus `prompt.md` und die Liste der vorhandenen Titel (Duplikatprüfung).
2. Antwort: ein oder mehrere Rezepte (Sammel-PDFs werden aufgeteilt) oder „überspringen“ mit Grund.
3. Prüfung: Frontmatter, Kategorie, Zutaten-Abschnitt, eindeutiger Slug. `source_file` wird erzwungen.
4. Foto: von der Rezeptseite (`source_url`: og:image bzw. YouTube-/TikTok-Vorschaubild) oder das größte Foto auf der Rezeptseite des PDFs (`images.py`). Gibt es keins, bekommt das Rezept keins – Eintrag „ohne Foto“ im Bericht, dann von Hand ergänzen (keine automatische Stockfoto-Suche, ein falsches Gericht wäre schlimmer).
5. Ein Commit pro Rezept (`recipe: add …`) inkl. Foto und `INDEX.md`, dann Push. `INDEX.md` baut `scripts/build_index.py`, das im Image mitgeliefert wird – das Daten-Repo braucht dafür nichts.
6. Übersprungenes, Fehlgeschlagenes und Rezepte ohne Foto stehen in **`IMPORT-REPORT.md`** im Daten-Repo.

Nur neue Dateien: Änderungen oder Löschungen im Eingangsordner ändern nichts am Repo. Netzwerk-/API-Ausfälle werden beim nächsten Scan wiederholt (max. 3 Versuche). Ein Rezept gilt erst als importiert, wenn der Push geklappt hat.

Bremsen: max. 10 Dateien pro Scan (`MAX_FILES_PER_SCAN`), max. 30 MB pro Datei (`MAX_FILE_MB`), dazu das Monatslimit des Anthropic-Workspace.

## Einrichtung

Konfiguration über `.env` bzw. die Stack-Variablen in Portainer – Vorlage mit Beispielwerten: [`.env.example`](.env.example). Pflicht ist `REPO_URL` (SSH-Clone-URL des Daten-Repos, z. B. `ssh://git@192.168.1.10:2222/dein-user/recipe-vault-data.git`). Ändert sich `REPO_URL`, stellt der Importer seine Arbeitskopie beim nächsten Abgleich auf das neue Repo um. Das Image wird aus dem Repo-Wurzelordner gebaut (`build.context: ..`), weil es `scripts/build_index.py` enthält.

| Pfad auf dem Host | Inhalt |
|---|---|
| `INBOX_HOST_PATH` (Standard `./inbox`) | Eingangsordner, z. B. das Ziel einer Cloud-Sync-Aufgabe („nur Remote-Änderungen herunterladen“). Nur lesend eingebunden. |
| `BASE/secrets/anthropic_api_key` | API-Key (Modus 600), als Docker-Secret eingebunden |
| `BASE/secrets/deploy_key` | SSH-Deploy-Key mit Schreibrecht nur für das Daten-Repo |
| `BASE/data/` | `state.json` (was schon verarbeitet ist), `importer.log` und Arbeitskopie des Repos |

`BASE` ist standardmäßig dieser Ordner (`importer/`), z. B. `/srv/recipe-importer` für einen festen Ort.

Optional: `seed-done.txt` im Wurzelordner des Daten-Repos – Dateinamen aus dem Eingangsordner (eine pro Zeile), die schon von Hand importiert wurden. Sie gelten immer als erledigt, auch wenn der Sync sie erst später liefert. Die Liste liegt im Daten-Repo, weil sie private Dateinamen enthält.

## Bedienung

- **Datei erneut importieren:** ihren Eintrag aus `data/state.json` löschen (oder die Datei umbenennen *und* inhaltlich ändern).
- **Logs:** `BASE/data/importer.log` (rotiert, 1 MB × 4). Auf Synology bleibt das Log in Portainer leer (`db`-Log-Treiber) – dann diese Datei lesen.
- **Code-Änderungen:** Portainer → Stack `recipe-vault-importer` → *Pull and redeploy*.
- **Lokal testen** (schreibt/pusht nichts): `DRY_RUN=1 ONCE=1 SETTLE_SECONDS=0 INBOX=… DATA=… REPO_URL=… ANTHROPIC_API_KEY_FILE=… python importer.py`

## Import aus der App

Kleine HTTP-Schnittstelle auf Port 8080 im Container (auf dem Host `API_HOST_PORT`, Standard `8099`), aufgerufen über das nginx der App:

| Aufruf | Zweck |
|---|---|
| `POST /api/import/file` (Body = Datei, Header `X-Filename`) | Datei wie aus OneDrive verarbeiten; `source_file: "App-Upload/<Name>"`, Kopie in `data/uploads/` |
| `POST /api/import/url` (`{"url": "…"}`) | Link: schema.org-Rezeptdaten (JSON-LD) bzw. Seitentext, bei YouTube/TikTok die Videobeschreibung |
| `GET /api/import/jobs/<id>` | Status: `queued` → `running` (mit Schritt) → `done` / `skipped` / `failed` |
| `POST /api/import/edit/ai` (`{"path", "markdown", "instruction"}`) | Rezept nach Anweisung von Claude überarbeiten lassen; Ergebnis (`markdown`, `message`) ist nur ein Vorschlag, gespeichert wird nichts |
| `POST /api/import/edit/save` (`{"path", "base", "markdown"}`) | Bearbeitetes Rezept prüfen, committen, pushen. Abgelehnt, wenn die Datei nicht mehr `base` entspricht (anderswo geändert); `slug` bleibt immer gleich |
| `POST /api/import/image/upload` (Body = Bild, Header `X-Recipe`, `X-Filename`) | Eigenes Foto: als WebP speichern, `image_source: "eigenes Foto"`, sofort committen |
| `POST /api/import/image/find` (`{"path", "markdown", "exclude"}`) | Foto-Vorschlag: Bild der Quellseite, sonst Wikimedia Commons (Suchbegriffe von Claude). `exclude` = bereits gezeigte Quellen |
| `POST /api/import/image/generate` (`{"path", "markdown", "hint"}`) | Foto-Vorschlag von Gemini (`GEMINI_API_KEY`, `IMAGE_MODEL`) |
| `GET /api/import/candidates/<id>.webp` | Vorschaubild eines Vorschlags (liegt 24 h in `data/candidates/`) |
| `POST /api/import/image/accept` (`{"path", "candidate"}`) | Vorschlag als Foto übernehmen und committen; ein altes Foto, das kein anderes Rezept nutzt, wird gelöscht |
| `GET /api/import/health` | `{"ok", "queue", "generate"}`; `generate` = Bild-KI eingerichtet |

Aufträge laufen nacheinander und teilen sich mit dem OneDrive-Scan eine Sperre auf Arbeitskopie und `state.json`. Bereits importierte Dateien (gleicher Inhalt, auch aus OneDrive) und Links werden sofort als „bereits importiert“ beantwortet. Grenzen: `MAX_JOBS_PER_HOUR` (20, zählt Importe und KI-Aufträge beim Bearbeiten), `MAX_FILE_MB` (30). Übersprungenes landet hier nicht in `IMPORT-REPORT.md` – die App zeigt den Grund direkt an.
