# Recipe Vault

A private, self-hosted recipe collection. Every recipe is a plain **Markdown file** in a Git repository, and a small **web app** makes it pleasant to browse, search, scale and cook from, on your phone, tablet or PC.

An optional **AI importer** turns PDFs, photos, Word/Excel files and web links into properly formatted recipe files and commits them automatically.

**This repository contains only the code.** Your recipes live in a separate, **private** data repository on your own Gitea server. Recipes and photos you import usually belong to their original authors, so they should not be published. A template for the data repo, with two sample recipes, is in [`examples/data-repo/`](examples/data-repo/).

```
                ┌──────────────────────────────┐
  PDF / photo / │  Importer (optional)         │   commit + push
  link / DOCX ─▶│  Python + Claude API         │──────────────┐
                └──────────────────────────────┘              ▼
                                                     ┌─────────────────┐
                                                     │ private data    │
                                                     │ repo on Gitea   │
                                                     │  recipes/*.md   │
                                                     │  images/*.webp  │
                                                     └────────┬────────┘
                                                              │ read-only API
                ┌──────────────────────────────┐              │
  Browser  ◀───▶│  Web app (nginx + index.html)│◀─────────────┘
                └──────────────────────────────┘
```

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Recipe file format](#recipe-file-format)
- [Installation (step by step)](#installation-step-by-step)
  - [What you need](#what-you-need)
  - [Step 1 – Install Docker](#step-1--install-docker)
  - [Step 2 – Get a Gitea server](#step-2--get-a-gitea-server)
  - [Step 3 – Get the code and create your recipe repo](#step-3--get-the-code-and-create-your-recipe-repo)
  - [Step 4 – Create a read-only access token](#step-4--create-a-read-only-access-token)
  - [Step 5 – Start the web app](#step-5--start-the-web-app)
  - [Step 6 (optional) – Set up the AI importer](#step-6-optional--set-up-the-ai-importer)
- [Deploying with Portainer](#deploying-with-portainer)
- [Adding recipes](#adding-recipes)
- [Keeping INDEX.md up to date](#keeping-indexmd-up-to-date)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [License](#license)

---

## Features

**Web app**

- **Search and filter** by text, category, keyword, maximum calories and minimum protein. Results can be sorted.
- **Card view** with photos, or an **index view** (titles only, A–Z).
- **Detail view** with ingredients, steps, nutrition, source link and photo.
- **Portion calculator.** It rescales the ingredient list, and also quantities inside the description and the steps, such as "200 g", "2 EL" or "3 eggs". Times, temperatures and sizes stay unchanged. Recipes without a serving count scale by a factor (½×, 1×, 1½× …).
- **Cooking mode.** Shows the steps one at a time, lets you tick off ingredients, and keeps the screen on while you cook.
- **Add recipes from the app** (**+ Rezept**): upload a file, paste a link, or paste the recipe text. The importer then processes it (requires the optional importer).
- **Edit recipes from the app** (**Bearbeiten** on a recipe): change the Markdown directly, or tell Claude what to change ("2 Knoblauchzehen ergänzen") and review the result before saving. Add or replace the photo: upload your own, search one (source page, then Wikimedia Commons), or generate one with Google Gemini. Suggested photos are only saved after you accept them (requires the importer; generating needs `GEMINI_API_KEY`).
- **Fast and offline-friendly.** All recipes are fetched as one small archive (about 100 KB) and cached in the browser. The app shows the cached state immediately and refreshes in the background. Photos load only when needed and are cached permanently.
- **Installable on your home screen** (iOS/Android) with its own icon.
- **No database and no build step.** The whole app is a single `index.html` with no dependencies.

**Importer (optional)**

- Watches a folder (for example a OneDrive folder synced to a NAS) and imports new files every 5 minutes.
- Understands **PDF, photos (including HEIC), DOCX, XLSX, TXT, MD**, and **web links**: recipe websites, YouTube and TikTok video descriptions.
- Splits collection PDFs into individual recipes and skips non-recipes and duplicates.
- Finds a matching **photo**: the page's preview image or the largest photo in the PDF.
- Makes **one Git commit per recipe** and writes a report (`IMPORT-REPORT.md`) listing anything it skipped or that failed.

---

## How it works

1. **Recipes live in their own Git repo.** Every recipe is one Markdown file under `recipes/<year>/` in your private data repo, and its photo is under `images/`. Git gives you history, backups and easy editing. The code (this repo) and your recipes are kept apart, so you can update the code without touching your recipes.
2. **The web app never writes to Git itself.** It is an nginx container that serves `index.html` and forwards a handful of **read-only** requests to the Gitea API. nginx adds the Gitea access token on the server side, so **the token never reaches the browser.** Any request other than `GET` on these paths is refused. Adding and editing recipes goes through the importer (below), which does the writing.
3. **New recipes show up after a page reload.** There is nothing to rebuild or redeploy.
4. **The importer is a separate container.** It sends sources to Claude (Anthropic API), validates the Markdown it gets back, and pushes the result to the data repo with an SSH deploy key.

---

## Repository layout

**This repo (code, public):**

| Path | What it is |
|---|---|
| `app/` | The web app: `index.html`, nginx config, Dockerfile, compose file, icons ([details](app/README.md)) |
| `importer/` | The AI importer: Python service, prompt, Dockerfile, compose file ([details](importer/README.md)) |
| `scripts/build_index.py` | Regenerates `INDEX.md` of a data repo (Python standard library only). Also shipped inside the importer image |
| `examples/data-repo/` | Template for your private data repo, with two sample recipes ([details](examples/data-repo/README.md)) |

**Your data repo (recipes, private):**

| Path | What it is |
|---|---|
| `recipes/<year>/*.md` | The recipes (one file each) |
| `images/*.webp` | Recipe photos. The file name contains a content hash, so a photo can be cached forever |
| `INDEX.md` | Auto-generated A–Z list of all recipes, readable directly in Gitea |
| `IMPORT-REPORT.md` | Written by the importer: files it skipped or could not process |
| `scripts/build_index.py`, `.githooks/`, `.gitea/workflows/` | Keep `INDEX.md` up to date on manual edits (optional) |

---

## Recipe file format

Each recipe is Markdown with a YAML header ("frontmatter"). Here is a shortened example:

```markdown
---
title: "Mango Sticky Rice"
slug: mango-sticky-rice
source_url: "https://www.example.com/mango-sticky-rice"
source_site: example.com
captured_at: "2026-10-07T14:16:47+02:00"
language: de
servings: 4
prep_time: PT5M
cook_time: PT40M
total_time: PT1H
cuisine: thai
category: dessert
keywords: [klebreis, mango, kokosmilch, dessert]
nutrition:
  serving_size: "1 Portion"
  calories: 208
  protein_g: 2.2
  carbs_g: 44.6
  fat_g: 2
image: images/mango-sticky-rice-72a089fd.webp
---

# Mango Sticky Rice

Short description of the dish.

## Zutaten

### Für den Sticky Rice

- 200 g Klebreis
- 200 ml Kokosmilch

## Zubereitung

1. Rinse the rice …
2. …

## Notizen

Optional tips and variations.
```

Rules the app relies on:

- **Section headings must match exactly:** `## Zutaten` / `## Ingredients` and `## Zubereitung` / `## Instructions`. `### Subgroups` inside the ingredients are optional.
- **One ingredient per line**, with the quantity first (`200 g Mehl`). This is what makes the portion calculator work.
- **`category`** must be one of: `breakfast`, `main`, `side`, `salad`, `soup`, `snack`, `sauce`, `bread`, `baking`, `dessert`, `ice-cream`, `drink`.
- **Times** use the ISO 8601 duration format: `PT20M` = 20 minutes, `PT1H30M` = 1½ hours.
- **`nutrition`** values are per serving. They are optional, but the calorie and protein filters need them.
- **`image`** is optional and points to a file under `images/`.

The full specification used by the importer is in [`importer/prompt.md`](importer/prompt.md).

---

## Installation (step by step)

This guide assumes no prior experience with Docker or Gitea. If you already run both, skip ahead to [Step 4](#step-4--create-a-read-only-access-token).

### What you need

| Required | Optional (only for the AI importer) |
|---|---|
| A computer or NAS that can run **Docker** | An **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com)) |
| A **Gitea** server (self-hosted Git; you can run it in Docker too) | A folder for incoming recipe files (for example a synced OneDrive folder) |
| **Git** on your PC | |

> **Why Gitea?** The web app reads recipes through the Gitea API. GitHub or GitLab will **not** work without changing `app/nginx.conf.template`.

### Step 1 – Install Docker

- **Windows / macOS:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
- **Linux:** follow the [official guide](https://docs.docker.com/engine/install/) for your distribution. It includes the Compose plugin.
- **Synology NAS:** install **Container Manager** from the Package Center. [Portainer](https://www.portainer.io/) on top of it is convenient (see [Deploying with Portainer](#deploying-with-portainer)).

Check that it works. Open a terminal (PowerShell on Windows) and run:

```bash
docker compose version
```

You should see a version number.

### Step 2 – Get a Gitea server

If you already have Gitea, skip this step.

The quickest way to start one with Docker:

```bash
docker run -d --name gitea --restart unless-stopped -p 3000:3000 -p 2222:22 -v gitea-data:/data gitea/gitea:latest
```

Then:

1. Open `http://localhost:3000` in your browser.
2. Keep the defaults on the setup page (SQLite is fine), then click **Install Gitea**.
3. Register: **the first account you create becomes the administrator.**

> Write down your computer's **LAN IP address** (for example `192.168.1.10`). You will need it in Step 5. On Windows, run `ipconfig` and look for "IPv4 Address". On macOS/Linux, run `ip a` or check your network settings.

### Step 3 – Get the code and create your recipe repo

You need two things: this **code** on the machine that runs Docker, and a **private recipe repo** in Gitea that the app reads from.

1. **Get the code.** Replace `OWNER` with the account you got the code from:

   ```bash
   git clone https://github.com/OWNER/recipe-vault.git
   ```

   ```bash
   cd recipe-vault
   ```

2. **Create the recipe repo in Gitea.** Click **+ → New Repository**, name it, for example, `recipe-vault-data`, tick **Make Repository Private**, and leave it **empty** (no README, no .gitignore).

3. **Fill it from the template.** [`examples/data-repo/`](examples/data-repo/) contains the folder layout, two sample recipes and the tools that keep `INDEX.md` up to date. Copy it next to the code folder and add the index script:

   ```bash
   cp -r examples/data-repo ../recipe-vault-data
   ```

   ```bash
   mkdir -p ../recipe-vault-data/scripts && cp scripts/build_index.py ../recipe-vault-data/scripts/
   ```

   (Windows PowerShell: `Copy-Item -Recurse examples/data-repo ../recipe-vault-data`, then `New-Item -ItemType Directory ../recipe-vault-data/scripts` and `Copy-Item scripts/build_index.py ../recipe-vault-data/scripts/`.)

4. **Push it to Gitea.** Replace `YOUR-USER` and the address with your own:

   ```bash
   cd ../recipe-vault-data
   ```

   ```bash
   git init -b main
   ```

   ```bash
   git add . && git commit -m "Initial recipe data"
   ```

   ```bash
   git remote add origin http://localhost:3000/YOUR-USER/recipe-vault-data.git
   ```

   ```bash
   git push -u origin main
   ```

   Git asks for your Gitea username and password.

5. **Recommended:** in the data repo, turn on the Git hook that keeps `INDEX.md` up to date on every commit:

   ```bash
   git config core.hooksPath .githooks
   ```

   The hook needs Python 3 to be available as `python3`.

Then go back to the code folder (`cd ../recipe-vault`) for the next steps.

### Step 4 – Create a read-only access token

The web app needs a token so it can read your private recipe repository.

1. In Gitea, click your avatar → **Settings → Applications**.
2. Under **Manage Access Tokens**, enter a name such as `recipe-vault-app`.
3. Under permissions, set **repository** to **Read**. Leave everything else at *No access*.
4. Click **Generate Token** and **copy the token now**. Gitea shows it only once.

> Use a **separate token just for the app**, with read permission only. Even if it leaked, nobody could change your recipes with it.

### Step 5 – Start the web app

1. Go to the `app` folder:

   ```bash
   cd app
   ```

2. Copy the template [`app/.env.example`](app/.env.example) to a new file named **`.env`** in the same folder, and fill in your own values:

   ```bash
   cp .env.example .env
   ```

   (Windows PowerShell: `Copy-Item .env.example .env`.) It looks like this:

   ```ini
   # Address of Gitea as seen FROM INSIDE the container: use the LAN IP, not localhost
   GITEA_URL=http://192.168.1.10:3000
   # owner/repository
   GITEA_REPO=YOUR-USER/recipe-vault-data
   GITEA_BRANCH=main
   # The token from step 4
   GITEA_TOKEN=paste-your-token-here
   # Port the app is reachable on
   APP_PORT=8095
   ```

   > ⚠️ **Do not commit `.env`.** It contains your token.
   >
   > ⚠️ **Do not use `localhost` in `GITEA_URL`.** Inside the container, "localhost" means the container itself, not your computer.

3. Build and start the app:

   ```bash
   docker compose up -d --build
   ```

4. Open **`http://localhost:8095`**, or `http://<LAN-IP>:8095` from your phone on the same network. The recipes should appear.

To stop the app, run `docker compose down`. To update after changing the code, run `docker compose up -d --build` again.

**Install on your phone:** open the app in Safari (iOS) or Chrome (Android) and choose **Add to Home Screen**.

### Step 6 (optional) – Set up the AI importer

The importer needs three things: your **Anthropic API key**, an **SSH deploy key** with write access to the recipe data repository, and a **folder** it watches for new files. Full details are in [`importer/README.md`](importer/README.md).

1. **Create the working folders** on the Docker host. By default they are created next to `importer/docker-compose.yml`; set `BASE` to keep them somewhere else, for example `/srv/recipe-importer` or, on a Synology NAS, a folder under `/volume1/docker/`.

   ```bash
   mkdir -p /srv/recipe-importer/secrets /srv/recipe-importer/data
   ```

2. **Store the API key** in a file that only its owner can read:

   ```bash
   cd /srv/recipe-importer/secrets
   ```

   ```bash
   printf '%s' 'sk-ant-...' > anthropic_api_key && chmod 600 anthropic_api_key
   ```

3. **Create an SSH deploy key** (no passphrase):

   ```bash
   ssh-keygen -t ed25519 -N "" -C recipe-importer -f deploy_key && chmod 600 deploy_key
   ```

   In Gitea, open the **data** repository (`recipe-vault-data`) → **Settings → Deploy Keys → Add Deploy Key**. Paste the contents of `deploy_key.pub` and tick **Enable write access**.

4. **Configure and start** the importer. Copy [`importer/.env.example`](importer/.env.example) to `importer/.env` and adjust it to your setup:

   ```ini
   # SSH clone URL of your recipe data repo (Gitea → repo page → "SSH")
   REPO_URL=ssh://git@192.168.1.10:2222/YOUR-USER/recipe-vault-data.git
   # Folder the importer watches for new files (mounted read-only)
   INBOX_HOST_PATH=/path/to/your/recipe-inbox
   # Where secrets/ and data/ live
   BASE=/srv/recipe-importer
   ```

   ```bash
   cd importer
   ```

   ```bash
   docker compose up -d --build
   ```

5. **Connect the app to the importer.** Add the importer's address to `app/.env` (it listens on port `8099` by default), then restart the app with `docker compose up -d`:

   ```ini
   IMPORTER_URL=http://192.168.1.10:8099
   ```

   The **+ Rezept** button in the app now works.

**Try it without changing anything first.** This runs one scan, writes nothing and pushes nothing:

```bash
DRY_RUN=1 ONCE=1 SETTLE_SECONDS=0 INBOX=./inbox DATA=./data REPO_URL=... ANTHROPIC_API_KEY_FILE=./key python importer.py
```

> **Costs:** every import is a paid Claude API call. Built-in limits: at most 10 files per scan, 20 app imports per hour and 30 MB per file. You can also set a monthly spending limit in the Anthropic console.

---

## Deploying with Portainer

This is the recommended setup on a NAS. Portainer pulls the compose file straight from Gitea, so updates take one click.

**Web app**

1. Portainer → **Stacks → Add stack → Repository**.
2. **Repository URL:** the URL of this **code** repo, e.g. `https://github.com/OWNER/recipe-vault.git` or your own copy in Gitea. Enable *Authentication* only if that repository is private.
3. **Reference:** `refs/heads/main`. **Compose path:** `app/docker-compose.yml`.
4. **Environment variables:** `GITEA_URL`, `GITEA_REPO` (your data repo) and `GITEA_TOKEN` (required). Optionally also `APP_PORT`, `GITEA_BRANCH` and `IMPORTER_URL`. See [`app/.env.example`](app/.env.example).
5. **Deploy the stack**, then open `http://<nas>:<APP_PORT>`.

**Importer:** follow the same steps with compose path `importer/docker-compose.yml` and the variables from [Step 6](#step-6-optional--set-up-the-ai-importer).

After changing code in `app/` or `importer/`, use **Pull and redeploy** on the stack. Recipe changes do **not** need a redeploy; they appear on the next page reload.

---

## Adding recipes

| Method | How |
|---|---|
| **From the app** | Click **+ Rezept**, then upload a file, paste a link, or paste the recipe text (tab **Text**). The app shows progress and links to the new recipe when it is done. *(Needs the importer.)* |
| **Drop a file** | Copy a PDF, photo or document into the importer's inbox folder. It is picked up within about 5 minutes. *(Needs the importer.)* |
| **By hand** | In your data repo, create `recipes/<year>/<yyyy-mm-dd>-<slug>.md` following the [format](#recipe-file-format), optionally add a photo under `images/`, then commit and push. |
| **In the Gitea web UI** | Open the data repository → `recipes/` → **New File**, paste the Markdown, and commit. |

To fix a recipe, open it in the app and tap **Bearbeiten** (needs the importer), or edit its Markdown file and push the change. Edits from the app are refused if the file was changed elsewhere in the meantime, so nothing is overwritten silently.

---

## Keeping INDEX.md up to date

`INDEX.md` in your data repo is generated by `scripts/build_index.py`. The importer regenerates it with every recipe it commits. For changes you make yourself there are two automatic triggers (both come with [`examples/data-repo/`](examples/data-repo/)):

- **Locally:** the pre-commit hook, after running `git config core.hooksPath .githooks` once per clone of the data repo.
- **On the server:** the Gitea Actions workflow `.gitea/workflows/index.yml` in the data repo runs on every push that changes `recipes/`. It needs a Gitea Actions runner with the label `host` and a repository secret `GITEA_TOKEN` with write access. The runner clones the repo from Gitea's own URL (`ROOT_URL`). If the runner should use a different address, for example the LAN address instead of a public hostname, set the repository variable `CLONE_BASE_URL` (*Settings → Actions → Variables*) to something like `http://192.168.1.10:3000`.

To regenerate it manually, from the code repo:

```bash
python3 scripts/build_index.py ../recipe-vault-data
```

---

## Configuration reference

**Web app** (`app/docker-compose.yml`)

| Variable | Default | Meaning |
|---|---|---|
| `GITEA_TOKEN` | *(required)* | Gitea token with **repository: read** |
| `GITEA_URL` | *(required)* | Gitea base URL as seen from the container, e.g. `http://192.168.1.10:3000` |
| `GITEA_REPO` | *(required)* | Your recipe data repo as `owner/repository`, e.g. `your-user/recipe-vault-data` |
| `GITEA_BRANCH` | `main` | Branch to read |
| `APP_PORT` | `8095` | Port the app is published on |
| `IMPORTER_URL` | `http://127.0.0.1:8099` (= no importer) | Importer API, e.g. `http://192.168.1.10:8099`. Without an importer, **+ Rezept** shows an error |

**Importer** (`importer/docker-compose.yml`)

| Variable | Default | Meaning |
|---|---|---|
| `REPO_URL` | *(required)* | SSH URL of the data repo the importer pushes to, e.g. `ssh://git@192.168.1.10:2222/your-user/recipe-vault-data.git` |
| `INBOX_HOST_PATH` | `./inbox` | Folder that is watched (mounted read-only) |
| `BASE` | `.` (the `importer/` folder) | Holds `secrets/` and `data/` |
| `API_HOST_PORT` | `8099` | Host port of the import API |
| `INTERVAL` | `300` | Seconds between folder scans |
| `MAX_FILES_PER_SCAN` | `10` | Maximum files processed per scan |
| `MAX_JOBS_PER_HOUR` | `20` | Maximum imports per hour from the app |
| `MODEL` | `claude-opus-5-5` | Claude model used for conversion |
| `EFFORT` | `medium` | Reasoning effort for the model |
| `GEMINI_API_KEY` | *(empty)* | Google Gemini API key for **KI-Bild generieren** when editing a recipe. Without it the button is hidden |
| `IMAGE_MODEL` | `gemini-2.5-flash-image` | Gemini image model |

> Templates with example values: [`app/.env.example`](app/.env.example) and [`importer/.env.example`](importer/.env.example). With Portainer, enter the same variables in the stack's *Environment variables* instead.

---

## Troubleshooting

| Problem | What to check |
|---|---|
| `docker compose up` fails with *"set GITEA_TOKEN …"* | `GITEA_TOKEN` is missing. Put it in `app/.env` or in the Portainer stack variables. |
| The app loads but shows no recipes | Check `GITEA_URL` (no `localhost`!), `GITEA_REPO` (`owner/repo`) and `GITEA_BRANCH`. Then open `http://<app>/gitea/tree` in your browser: it should return JSON, not an error. |
| `401` / `403` on `/gitea/...` | The token is wrong, expired, or lacks **repository: read**. |
| `502 Bad Gateway` | The container cannot reach Gitea. Check that the IP and port are reachable from the Docker host and that no firewall blocks them. |
| New recipe does not show up | Reload the page. The app shows its cached state first and refreshes in the background. Check that the commit is really on the configured branch. |
| Photos are missing | Check that the `image:` path in the frontmatter matches a file under `images/` exactly. |
| **+ Rezept** fails | Check that the importer is running and that `IMPORTER_URL` is reachable from the app container. |
| Importer does nothing | Check `data/importer.log` under `BASE`. Files are processed only after they have been unchanged for 2 minutes. Already processed files are recorded in `data/state.json`; delete an entry there to import that file again. |
| Importer cannot push | The deploy key needs **write access**, and the `REPO_URL` host and port must be the Gitea **SSH** address. |

---

## Security notes

- The app has **no login**. Run it only on your **home network**, or put it behind a reverse proxy with authentication before exposing it to the internet.
- The Gitea token stays inside the nginx container. The browser can reach only four read-only `GET` endpoints.
- Give the app token **read** permission only, and give the importer's deploy key access to **this one repository** only.
- Keep `.env` files and everything under `secrets/` out of Git.
- Anyone who can open the app can also add and **edit** recipes and use the AI features (which cost money). `MAX_JOBS_PER_HOUR` limits the AI jobs; every change is a Git commit and can be reverted.

---

## License

The code in this repository is released under the [MIT License](LICENSE). It contains no recipes apart from the two samples in `examples/data-repo/`, which are free to use. Recipes and photos you import into your own data repo keep the rights of their original authors: keep that repo private.
