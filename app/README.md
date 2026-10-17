# Recipe Vault – Web-App

Minimale Oberfläche für die Rezepte unter `recipes/` im privaten Daten-Repo (`GITEA_REPO`): Suche, Filter (Kategorie, Schlagwort, max. kcal, min. Eiweiß), Sortierung, Karten- oder Index-Ansicht (nur Titel, A–Z), Rezepte hinzufügen (**+ Rezept**: Datei oder Link – verarbeitet vom Importer), Detailansicht mit Portionsrechner und Kochansicht (Schritt für Schritt, Zutaten abhaken, Bildschirm bleibt an). Der Portionsrechner gilt für Detail- und Kochansicht gemeinsam und rechnet auch Mengen in Beschreibung und Zubereitungsschritten um – nur „Zahl + Einheit“ (g, ml, EL, Prise …) oder „Zahl + Zutat dieses Rezepts“; Zeiten, Temperaturen und Größen bleiben. Rezepte ohne Portionsangabe skalieren mit Faktor (½×, 1×, 1½× …).

Die Markdown-Dateien werden nur gelesen, nie verändert. Neue Rezepte erscheinen beim nächsten Neuladen der Seite – kein Redeploy nötig.

## Aufbau

| Datei | Zweck |
|---|---|
| `index.html` | Die ganze App: HTML, CSS, JavaScript (keine Abhängigkeiten) |
| `nginx.conf.template` | Liefert `index.html` aus und reicht vier **nur lesende** Gitea-Aufrufe durch |
| `favicon.svg`, `icons/`, `manifest.webmanifest` | Favicon und Icon für den Home-Bildschirm (iOS/Android). `icons/icon-square.svg` ist die Vorlage für die PNGs |
| `Dockerfile` | `nginx:alpine` + die Dateien oben |
| `docker-compose.yml` | Stack für Portainer |

Der Browser lädt `/gitea/archive` – den Ordner `recipes/` als ein `.tar.gz` (~100 KB, ein Request) – und entpackt ihn selbst. Fotos liegen unter `images/` (Feld `image` im Frontmatter) und kommen einzeln und erst bei Bedarf über `/gitea/media/images/…`; der Dateiname enthält einen Inhalts-Hash, daher dürfen Browser sie dauerhaft zwischenspeichern. Nur wenn das scheitert, holt er wie früher `/gitea/tree` (Dateiliste) und `/gitea/blob/<sha>` (einzelne Dateien). nginx hängt dabei das Gitea-Token an – das Token kommt nie im Browser an. Alles andere als `GET` auf diese Pfade wird abgewiesen. Der letzte Stand liegt im Browser (localStorage): Die App zeigt ihn sofort an und aktualisiert im Hintergrund.

## Deployment (Portainer)

1. In Gitea ein eigenes Token nur für die App anlegen: *Einstellungen → Anwendungen*, Berechtigung **repository: read**.
2. Portainer → *Stacks → Add stack → Repository*
   - Repository URL: Clone-URL dieses **Code**-Repos, z. B. `https://github.com/OWNER/recipe-vault.git` (Authentication nur, wenn es privat ist)
   - Reference: `refs/heads/main`
   - Compose path: `app/docker-compose.yml`
3. *Environment variables* (Vorlage mit Beispielwerten: [`.env.example`](.env.example)):
   - Pflicht: `GITEA_URL` (aus Sicht des Containers, also LAN-IP statt `localhost`), `GITEA_REPO` = das Daten-Repo (`owner/repo`, z. B. `dein-user/recipe-vault-data`), `GITEA_TOKEN` = das Token aus Schritt 1
   - optional: `APP_PORT` (Standard `8095`), `GITEA_BRANCH` (Standard `main`), `IMPORTER_URL`
4. Deploy → `http://<host>:<APP_PORT>`

Änderungen an der App selbst (`app/`) kommen per *Pull and redeploy* in Portainer.

## Lokal testen

```bash
cp .env.example .env   # Werte eintragen, .env nie committen
docker compose up --build
```

## Rezepte hinzufügen

**+ Rezept** schickt eine Datei (`/api/import/file`) oder einen Link (`/api/import/url`) über nginx an den Importer (`IMPORTER_URL`, z. B. `http://192.168.1.10:8099`; ohne Importer antwortet **+ Rezept** mit einem Fehler). Dort läuft dieselbe Verarbeitung wie für Dateien im Eingangsordner; die App fragt den Auftrag alle 2 s ab und zeigt am Ende einen Link zum neuen Rezept. Kein Login – wie die ganze App nur im Heimnetz erreichbar; der Importer begrenzt auf 20 Importe pro Stunde und 30 MB pro Datei.
