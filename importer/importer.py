#!/usr/bin/env python3
"""recipe-vault importer: watches an inbox folder (e.g. a Cloud-Sync copy of a
OneDrive folder) and turns every new file into recipe Markdown committed to the
private recipe data repo (REPO_URL) on Gitea.

Loop: scan inbox -> pick new, settled files -> Claude converts -> validate ->
one commit per recipe (+ INDEX.md) -> push. Skips/failures go to
IMPORT-REPORT.md in the data repo. State lives in /data/state.json.

Env (all optional except where noted):
  INBOX=/inbox  DATA=/data  INTERVAL=300  SETTLE_SECONDS=120
  MAX_FILES_PER_SCAN=10  MAX_FILE_MB=30  MODEL=claude-opus-5-5  EFFORT=medium
  REPO_URL (required)  REPO_BRANCH=main
  ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_api_key
  DEPLOY_KEY_FILE=/run/secrets/deploy_key
  DRY_RUN=1 -> convert and print, never write state, commit or push
  ONCE=1    -> single scan, then exit
"""
import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import anthropic
import yaml

import images

log = logging.getLogger("importer")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
HERE = Path(__file__).resolve().parent

INBOX = Path(os.environ.get("INBOX", "/inbox"))
DATA = Path(os.environ.get("DATA", "/data"))
REPO = DATA / "repo"
STATE_FILE = DATA / "state.json"
INTERVAL = int(os.environ.get("INTERVAL", "300"))
SETTLE = int(os.environ.get("SETTLE_SECONDS", "120"))
MAX_FILES = int(os.environ.get("MAX_FILES_PER_SCAN", "10"))
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "30")) * 1024 * 1024)
MODEL = os.environ.get("MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EFFORT", "medium")
REPO_URL = os.environ.get("REPO_URL", "")
BRANCH = os.environ.get("REPO_BRANCH", "main")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
MAX_ATTEMPTS = 3

CATEGORIES = {"breakfast", "main", "side", "salad", "soup", "snack", "sauce",
              "bread", "baking", "dessert", "ice-cream", "drink"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
TEXT_TYPES = {".txt", ".md"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["import", "skip"]},
        "reason": {"type": "string", "description": "German, short. Required for skip; may be empty for import."},
        "recipes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "markdown": {"type": "string"},
                },
                "required": ["slug", "markdown"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "reason", "recipes"],
    "additionalProperties": False,
}


class Retryable(Exception):
    """Temporary problem (network, rate limit, overload): try again next scan."""


class Rejected(Exception):
    """The file cannot be imported as-is; record it and don't retry."""


# ---------------------------------------------------------------- helpers --

def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def read_secret(path_env: str, default: str) -> str:
    p = Path(os.environ.get(path_env, default))
    return p.read_text().strip()


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                       env={**os.environ, "GIT_SSH_COMMAND": git_ssh()})
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(s: str) -> str:
    s = s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80].strip("-") or "rezept"


# ------------------------------------------------------------------ state --

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"files": {}, "seeded": False}


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def seed_names() -> set[str]:
    """Optional seed-done.txt at the data repo root: inbox file names (one per line)
    that were imported by hand before the importer ran. Lives in the data repo
    because it names private files."""
    f = REPO / "seed-done.txt"
    if not f.exists():
        return set()
    return {unicodedata.normalize("NFC", l.strip())
            for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}


def seed(state: dict, rel: str, digest: str) -> bool:
    """Files listed in seed-done.txt count as done whenever they show up
    (Cloud Sync may deliver them after the first scan)."""
    if rel not in seed_names():
        return False
    state["files"][rel] = {"sha": digest, "status": "seeded", "at": now().isoformat()}
    log.info("already imported by hand, marking done: %s", rel)
    return True


# ------------------------------------------------------------------ inbox --

def inbox_files():
    for p in sorted(INBOX.rglob("*")):
        rel = unicodedata.normalize("NFC", p.relative_to(INBOX).as_posix())  # macOS NFD vs Linux NFC
        if not p.is_file() or p.name in IGNORED_NAMES:
            continue
        if any(part.startswith((".", "@", "#", "~$")) for part in p.relative_to(INBOX).parts):
            continue  # hidden, Synology @eaDir / #recycle, Office lock files
        yield rel, p


def pending(state: dict):
    known_shas = {f["sha"] for f in state["files"].values()}
    out = []
    for rel, path in inbox_files():
        entry = state["files"].get(rel)
        if entry and (entry["status"] != "retry"):
            continue  # done, skipped or failed: edits to known files are ignored on purpose
        st = path.stat()
        if time.time() - st.st_mtime < SETTLE:
            continue  # Cloud Sync may still be writing it
        digest = sha256(path)
        if not entry and seed(state, rel, digest):
            continue
        if not entry and digest in known_shas:
            state["files"][rel] = {"sha": digest, "status": "duplicate-file", "at": now().isoformat()}
            log.info("same content as an already-known file, ignoring: %s", rel)
            continue
        out.append((rel, path, digest))
    return out


# ------------------------------------------------------ content building --

def pdf_date(raw: str):
    """D:YYYYMMDDHHmmSS[Z|+HH'mm'|-HH'mm'] -> aware datetime (no offset = local time)."""
    m = re.match(r"D:(\d{14})(Z|[+-]\d{2}'?\d{2}'?)?", raw)
    if not m:
        return None
    when = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    tz = m.group(2)
    if not tz:
        return when.replace(tzinfo=TZ)
    if tz == "Z":
        return when.replace(tzinfo=dt.timezone.utc).astimezone(TZ)
    sign = 1 if tz[0] == "+" else -1
    hh, mm = int(tz[1:3]), int(tz.replace("'", "")[3:5] or 0)
    return when.replace(tzinfo=dt.timezone(sign * dt.timedelta(hours=hh, minutes=mm))).astimezone(TZ)


def captured_at(path: Path) -> str:
    """When the recipe was saved: PDF creation date, photo EXIF date, else file mtime."""
    when = dt.datetime.fromtimestamp(path.stat().st_mtime, TZ)
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            when = pdf_date(str((PdfReader(str(path)).metadata or {}).get("/CreationDate", ""))) or when
        elif ext in IMAGE_TYPES:
            from PIL import Image
            if ext in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            exif = Image.open(path).getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
            if raw:
                when = dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").replace(tzinfo=TZ)
    except Exception:
        pass
    return when.replace(microsecond=0).isoformat()


def docx_text(path: Path) -> str:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = ["".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")) for p in root.iter(f"{{{ns['w']}}}p")]
    return "\n".join(paras)


def xlsx_text(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"=== Blatt {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            if any(c is not None for c in row):
                out.append("\t".join("" if c is None else str(c) for c in row))
    return "\n".join(out)


def image_block(path: Path) -> dict:
    from PIL import Image, ImageOps
    if path.suffix.lower() in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((2000, 2000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def source_blocks(path: Path) -> tuple[list, str]:
    """Content blocks for the file plus a short type label for the Quelle line."""
    ext = path.suffix.lower()
    if path.stat().st_size > MAX_BYTES:
        raise Rejected(f"Datei zu groß ({path.stat().st_size // 1048576} MB, Limit {MAX_BYTES // 1048576} MB)")
    try:
        if ext == ".pdf":
            data = base64.standard_b64encode(path.read_bytes()).decode()
            return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}], "PDF"
        if ext in IMAGE_TYPES:
            return [image_block(path)], "Foto"
        if ext == ".docx":
            text, label = docx_text(path), "DOCX"
        elif ext == ".xlsx":
            text, label = xlsx_text(path), "XLSX"
        elif ext in TEXT_TYPES:
            text, label = path.read_text(encoding="utf-8", errors="replace"), ext[1:].upper()
        else:
            raise Rejected(f"Dateityp {ext or '(ohne Endung)'} wird nicht unterstützt")
    except Rejected:
        raise
    except Exception as e:  # corrupt/truncated file
        raise Rejected(f"Datei nicht lesbar ({type(e).__name__}: {e})") from e
    if not text.strip():
        raise Rejected("Datei enthält keinen Text")
    return [{"type": "text", "text": f"<datei name=\"{path.name}\">\n{text}\n</datei>"}], label


# ---------------------------------------------------------------- Claude --

def existing_titles() -> list[str]:
    titles = []
    for f in sorted((REPO / "recipes").rglob("*.md")):
        m = re.search(r"^title:\s*(.+)$", f.read_text(encoding="utf-8"), re.M)
        if m:
            titles.append(m.group(1).strip().strip("\"'"))
    return titles


def titles_hint() -> str:
    return ("Bereits vorhandene Rezepttitel (für die Duplikatprüfung):\n"
            + "\n".join(f"- {t}" for t in existing_titles()))


def file_input(path: Path, source_file: str, origin: str) -> tuple[list, str]:
    """Claude input for a file. origin: wording for the Quelle line, e.g. 'aus OneDrive'."""
    blocks, label = source_blocks(path)
    info = (f"Quelldatei: {path.name}\n"
            f"source_file: \"{source_file}\"\n"
            f"captured_at: \"{captured_at(path)}\"\n"
            f"Heute (importiert am): {now().date().isoformat()}\n"
            f"Herkunft für die Quelle-Zeile: \"{origin} ({label})\"\n\n" + titles_hint())
    return blocks, info


def url_input(url: str) -> tuple[list, str]:
    """Claude input for a recipe link: structured recipe data if the page has it, else page text."""
    import urllib.error
    try:
        page = images.page_content(url)
    except urllib.error.HTTPError as e:
        if e.code in (408, 429) or e.code >= 500:
            raise Retryable(f"Seite antwortet nicht (HTTP {e.code})") from e
        raise Rejected(f"Seite nicht abrufbar (HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Retryable(f"Seite nicht erreichbar ({e})") from e
    parts = [f"Titel: {page['title']}", f"Beschreibung: {page['description']}"]
    if page["recipe_jsonld"]:
        parts.append("Strukturierte Rezeptdaten (schema.org JSON-LD):\n"
                     + json.dumps(page["recipe_jsonld"], ensure_ascii=False)[:60_000])
    if page["text"]:
        parts.append("Seitentext:\n" + page["text"][: 20_000 if page["recipe_jsonld"] else 60_000])
    if not any([page["recipe_jsonld"], page["text"].strip(), page["description"].strip()]):
        raise Rejected("Seite enthält keinen lesbaren Inhalt")
    blocks = [{"type": "text", "text": f"<webseite url=\"{url}\">\n" + "\n\n".join(parts) + "\n</webseite>"}]
    info = (f"Quelle ist ein Link, keine Datei: KEIN source_file-Feld setzen.\n"
            f"source_url: \"{url}\"\n"
            f"captured_at: \"{now().replace(microsecond=0).isoformat()}\"\n"
            f"Heute (importiert am): {now().date().isoformat()}\n"
            f"Herkunft für die Quelle-Zeile: \"per Link in der App\"\n\n" + titles_hint())
    return blocks, info


def convert(client: anthropic.Anthropic, blocks: list, info: str) -> dict:
    system = (HERE / "prompt.md").read_text(encoding="utf-8")
    return claude_json(client, system, [*blocks, {"type": "text", "text": info}], RESULT_SCHEMA)


def claude_json(client: anthropic.Anthropic, system: str, content: list, schema: dict,
                effort: str = EFFORT, max_tokens: int = 128000) -> dict:
    """One Claude call whose answer is JSON matching schema."""
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
        raise Retryable(f"{type(e).__name__}: {e}") from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500 or e.status_code in (408, 409, 429, 529):
            raise Retryable(f"API {e.status_code}: {e.message}") from e
        raise Rejected(f"API-Fehler {e.status_code}: {e.message}") from e

    u = msg.usage
    log.info("  %s: %s in / %s out tokens, stop=%s, message=%s", msg.model, u.input_tokens, u.output_tokens,
             msg.stop_reason, msg.id)
    if msg.stop_reason == "refusal":
        raise Rejected("Modell hat die Verarbeitung abgelehnt")
    if msg.stop_reason == "max_tokens":
        raise Rejected("Antwort zu lang (max_tokens erreicht) – Datei aufteilen")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise Retryable(f"ungültiges JSON vom Modell: {e}") from e


# ------------------------------------------------------------- validation --

def validate(md: str, source_file: str | None = None, source_url: str | None = None) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", md.strip() + "\n", re.S)
    if not m:
        raise ValueError("kein Frontmatter")
    meta = yaml.safe_load(m.group(1))
    body = m.group(2)
    if not isinstance(meta, dict) or not meta.get("title"):
        raise ValueError("Frontmatter ohne title")
    if meta.get("category") not in CATEGORIES:
        raise ValueError(f"unbekannte Kategorie {meta.get('category')!r}")
    if not re.search(r"^## (Zutaten|Ingredients)\s*$", body, re.M):
        raise ValueError("Abschnitt Zutaten/Ingredients fehlt")
    if "<today>" in md or "<Herkunft>" in md:
        raise ValueError("Platzhalter nicht ersetzt")
    # enforce provenance exactly
    for key, expected in (("source_file", source_file), ("source_url", source_url)):
        if expected and meta.get(key) != expected:
            if re.search(rf"^{key}:", md, re.M):
                md = re.sub(rf"^{key}:.*$", f'{key}: "{expected}"', md, count=1, flags=re.M)
            else:
                md = md.replace("\n", f'\n{key}: "{expected}"\n', 1)  # right after the opening ---
            meta[key] = expected
    if not source_file and "source_file" in meta:
        md = re.sub(r"^source_file:.*\n", "", md, count=1, flags=re.M)
        meta.pop("source_file")
    return meta, md.strip() + "\n"


# ------------------------------------------------------------------- repo --

def sync_repo() -> None:
    if not (REPO / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", "--branch", BRANCH, REPO_URL, str(REPO)],
                       check=True, env={**os.environ, "GIT_SSH_COMMAND": git_ssh()})
    elif REPO_URL and git("remote", "get-url", "origin").strip() != REPO_URL:
        # REPO_URL changed (e.g. data repo renamed): never push to the old remote
        log.warning("origin changed, switching working clone to %s", REPO_URL)
        git("remote", "set-url", "origin", REPO_URL)
    git("config", "user.name", "recipe-vault importer")
    git("config", "user.email", "importer@recipe-vault.local")
    git("fetch", "--quiet", "origin", BRANCH)
    git("reset", "--quiet", "--hard", f"origin/{BRANCH}")


def git_ssh() -> str:
    key = os.environ.get("DEPLOY_KEY_FILE", "/run/secrets/deploy_key")
    return (f"ssh -i {key} -o IdentitiesOnly=yes "
            f"-o UserKnownHostsFile={DATA}/known_hosts -o StrictHostKeyChecking=accept-new")


def push() -> None:
    for _ in range(3):
        r = subprocess.run(["git", "-C", str(REPO), "push", "--quiet", "origin", f"HEAD:{BRANCH}"],
                           capture_output=True, text=True, env={**os.environ, "GIT_SSH_COMMAND": git_ssh()})
        if r.returncode == 0:
            return
        log.warning("push rejected, rebasing: %s", r.stderr.strip())
        git("pull", "--quiet", "--rebase", "-X", "theirs", "origin", BRANCH)  # someone pushed meanwhile
        rebuild_index_commit()
    raise RuntimeError("push failed 3 times")


# In the image next to importer.py; when run from a checkout of the code repo: ../scripts/
INDEX_SCRIPT = next((p for p in (HERE / "build_index.py", HERE.parent / "scripts" / "build_index.py")
                     if p.exists()), HERE / "build_index.py")


def build_index() -> None:
    subprocess.run([sys.executable, str(INDEX_SCRIPT), str(REPO)], check=True, capture_output=True)


def rebuild_index_commit() -> None:
    build_index()
    if git("status", "--porcelain", "INDEX.md").strip():
        git("commit", "--quiet", "-m", "index: regenerate INDEX.md", "--", "INDEX.md")


def unique_path(slug: str) -> Path:
    day = now().date().isoformat()
    folder = REPO / "recipes" / day[:4]
    taken = {p.stem.split("-", 3)[-1] for p in (REPO / "recipes").rglob("*.md")}
    base, n = slugify(slug), 2
    candidate = base
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return folder / f"{day}-{candidate}.md"


def pdf_pages(md: str) -> list[int] | None:
    """Pages named in the Quelle line ("PDF, S. 12" or "S. 12–13"), None if not stated."""
    m = re.search(r"S\. (\d+)(?:[–-](\d+))?", md.split("## Quelle")[-1])
    return list(range(int(m[1]), int(m[2] or m[1]) + 1)) if m else None


def inbox_source(meta: dict) -> Path | None:
    """The inbox file a recipe was imported from, if it is still there."""
    rel = str(meta.get("source_file") or "")
    if not rel.startswith("OneDrive Rezepte/"):
        return None  # app uploads are stored under generated names
    path = INBOX / rel.removeprefix("OneDrive Rezepte/")
    return path if path.is_file() else None


def find_image(meta: dict, md: str, source: Path | None):
    """(webp bytes, image_source) from the recipe page or the source PDF, else None.
    No automatic stock-photo search: a wrong dish photo is worse than none."""
    try:
        if meta.get("source_url"):
            got = images.from_url(str(meta["source_url"]))
            if got:
                return images.to_webp(got[0]), str(meta["source_url"])
        if source and source.suffix.lower() == ".pdf":
            got = images.from_pdf(source, pdf_pages(md))
            if got:
                return images.to_webp(got[0]), f"{meta['source_file']}, {got[1]}"
    except Exception as e:
        log.warning("  image lookup failed: %s", e)
    return None


def commit_recipe(meta: dict, md: str, note: str, source: Path | None) -> tuple[str, bool]:
    """Commit one recipe (+ photo if one is found). Returns (path, has_image)."""
    path = unique_path(meta.get("slug") or meta["title"])
    slug = path.stem.split("-", 3)[-1]
    md = re.sub(r"^slug:.*$", f"slug: {slug}", md, count=1, flags=re.M)
    to_add = [str(path.relative_to(REPO)), "INDEX.md"]
    img = find_image(meta, md, source)
    if img:
        name = images.filename(slug, img[0])
        (REPO / "images").mkdir(exist_ok=True)
        (REPO / "images" / name).write_bytes(img[0])
        src = img[1].replace('"', "'")
        md = md.replace("\n---\n", f'\nimage: images/{name}\nimage_source: "{src}"\n---\n', 1)
        to_add.append(f"images/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    build_index()
    git("add", *to_add)
    git("commit", "--quiet", "-m", f"recipe: add {meta['title']}\n\n{note}")
    return path.relative_to(REPO).as_posix(), bool(img)


REPORT_HEADER = ("# Import-Bericht\n\nDateien aus dem OneDrive-Rezeptordner, die der Importer übersprungen hat "
                 "oder nicht verarbeiten konnte. Neueste oben. Erneut versuchen: Datei umbenennen oder neu speichern "
                 "(geänderter Inhalt) – oder den Eintrag in `/data/state.json` löschen.\n\n"
                 "| Datum | Datei | Status | Grund |\n|---|---|---|---|\n")


def report(rel: str, status: str, reason: str) -> None:
    f = REPO / "IMPORT-REPORT.md"
    rows = f.read_text(encoding="utf-8").split("|---|---|---|---|\n", 1)[1] if f.exists() else ""
    clean = lambda s: s.replace("|", "\\|").replace("\n", " ")
    row = f"| {now():%Y-%m-%d %H:%M} | {clean(rel)} | {status} | {clean(reason)} |\n"
    f.write_text(REPORT_HEADER + row + rows, encoding="utf-8")
    git("add", "IMPORT-REPORT.md")
    git("commit", "--quiet", "-m", f"import report: {status} {rel}")


# ---------------------------------------------------------------- onedrive --

def process(client, state: dict, rel: str, path: Path, digest: str):
    """Convert one file. Returns the state update to apply once the repo is
    pushed, or None if nothing was committed (retry later / dry run)."""
    entry = state["files"].setdefault(rel, {"sha": digest, "status": "retry", "attempts": 0})
    entry["sha"] = digest
    log.info("converting %s", rel)
    try:
        result = convert(client, *file_input(path, f"OneDrive Rezepte/{rel}", "aus OneDrive"))
        if result["decision"] == "skip" or not result["recipes"]:
            raise Rejected(result.get("reason") or "kein Rezept erkannt")
        prepared = []
        for r in result["recipes"]:
            try:
                prepared.append(validate(r["markdown"], source_file=f"OneDrive Rezepte/{rel}"))
            except (ValueError, yaml.YAMLError) as e:
                raise Retryable(f"Ausgabe ungültig: {e}") from e
    except Rejected as e:
        log.info("  skipped: %s", e)
        if DRY_RUN:
            return None
        report(rel, "übersprungen", str(e))
        return {"status": "skipped", "reason": str(e)}
    except Exception as e:  # Retryable, or anything unexpected: retry up to MAX_ATTEMPTS
        if not isinstance(e, Retryable):
            log.exception("  unexpected error")
        entry["attempts"] = entry.get("attempts", 0) + 1
        log.warning("  retry later (%d/%d): %s", entry["attempts"], MAX_ATTEMPTS, e)
        if entry["attempts"] < MAX_ATTEMPTS or DRY_RUN:
            return None
        report(rel, "fehlgeschlagen", str(e))
        return {"status": "failed", "reason": str(e)}

    if DRY_RUN:
        for meta, md in prepared:
            print(f"\n===== {meta['title']} =====\n{md}")
        return None
    done = [commit_recipe(meta, md, f"Imported automatically from OneDrive (Rezepte/{rel})", path) for meta, md in prepared]
    files = [f for f, _ in done]
    without = [f for f, has in done if not has]
    if without:  # nothing usable in the source: ask for a photo instead of guessing one
        report(rel, "ohne Foto", "Kein Bild in der Quelle gefunden – bitte von Hand ergänzen: " + ", ".join(without))
    log.info("  committed %d recipe(s): %s", len(files), ", ".join(files))
    return {"status": "imported", "recipes": files}


def scan(client, state: dict) -> None:
    with REPO_LOCK:  # app imports use the same working copy and state
        _scan(client, state)


def _scan(client, state: dict) -> None:
    todo = pending(state)
    if not todo:
        save_state(state)
        return
    sync_repo()
    updates = {}
    for rel, path, digest in todo[:MAX_FILES]:
        update = process(client, state, rel, path, digest)
        if update:
            updates[rel] = update
        save_state(state)  # attempt counters
    if updates:
        push()  # raises on failure -> files stay "retry" and are converted again next scan
        for rel, update in updates.items():
            state["files"][rel].update(update, at=now().isoformat())
        save_state(state)
        log.info("pushed: %s", ", ".join(f"{r} ({u['status']})" for r, u in updates.items()))
    if len(todo) > MAX_FILES:
        log.info("%d more files waiting for the next scan", len(todo) - MAX_FILES)


# ---------------------------------------------------------------- editing --
# Editing existing recipes from the app: the text (by hand, or by Claude after
# an instruction) and the photo (own upload, found on the source page or on
# Wikimedia Commons, or generated by Gemini). Text changes by Claude and photo
# suggestions are only proposals; nothing is committed until the user saves or
# accepts them in the app.

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image")
CANDIDATES = DATA / "candidates"
RECIPE_PATH = re.compile(r"recipes/\d{4}/[a-z0-9-]+\.md")
KEEP_KEYS = ("slug", "source_url", "source_file", "captured_at", "image", "image_source")

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string", "description": "The complete edited recipe file."},
        "summary": {"type": "string", "description": "German, one short sentence: what was changed."},
    },
    "required": ["markdown", "summary"],
    "additionalProperties": False,
}

EDIT_RULES = """You edit one existing recipe file of a recipe vault. The user message contains the
current file and an instruction in German. Return the complete file with the instruction applied.

- Change only what the instruction asks for (plus what follows directly from it, e.g. an added
  ingredient also used in the steps). Keep everything else exactly as it is: wording, order,
  formatting, frontmatter keys and values.
- Never change slug, source_url, source_file, captured_at, image or image_source.
- Keep the recipe's language and the format below. If you estimate values (e.g. nutrition or
  times) because the user asks for it, say so in one line under Notizen/Notes.
- If the instruction is unclear or not about this recipe, return the file unchanged and explain
  why in the summary.

The file format and general rules of the vault (written for importing new recipes; the parts
about importing do not apply here):

"""

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"},
                               "description": "2-3 short English search terms for a photo of this dish"}},
    "required": ["queries"],
    "additionalProperties": False,
}


def norm_md(md: str) -> str:
    return md.replace("\r\n", "\n").strip() + "\n"


def split_front(md: str) -> tuple[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", norm_md(md), re.S)
    if not m:
        raise ValueError("kein Frontmatter")
    return m[1], m[2]


def front_meta(md: str) -> dict:
    meta = yaml.safe_load(split_front(md)[0])
    return meta if isinstance(meta, dict) else {}


def set_front(md: str, key: str, value: str | None) -> str:
    """Set a top-level frontmatter line (value: YAML text as written), or remove it (None)."""
    front, body = split_front(md)
    pat = re.compile(rf"^{re.escape(key)}:.*(?:\n|$)", re.M)
    line = f"{key}: {value}\n" if value is not None else ""
    if pat.search(front):
        front = pat.sub(lambda _: line, front, count=1).rstrip("\n")
    elif line:
        front += "\n" + line.rstrip("\n")
    return f"---\n{front}\n---\n{body}"


def front_line(md: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:\s?(.*)$", split_front(md)[0], re.M)
    return m[1] if m else None


def quoted(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a valid YAML double-quoted string


def recipe_file(rel: str) -> Path:
    if not RECIPE_PATH.fullmatch(rel or ""):
        raise Rejected("Ungültiger Rezeptpfad")
    path = REPO / rel
    if not path.is_file():
        raise Rejected("Rezept nicht gefunden – wurde es umbenannt oder gelöscht?")
    return path


def checked(md: str) -> tuple[dict, str]:
    """validate() for an edited file; keeps whatever provenance the file has."""
    try:
        return validate(norm_md(md), source_file=front_meta(md).get("source_file"))
    except (ValueError, yaml.YAMLError) as e:
        raise Rejected(f"Rezept ungültig: {e}") from e


def ai_edit(client, md: str, instruction: str) -> tuple[str, str]:
    system = EDIT_RULES + (HERE / "prompt.md").read_text(encoding="utf-8").split("# Markdown format", 1)[-1]
    result = claude_json(client, system, [{"type": "text", "text":
        f"<datei>\n{norm_md(md)}</datei>\n\n<anweisung>\n{instruction}\n</anweisung>"}], EDIT_SCHEMA)
    new = norm_md(result["markdown"])
    for key in KEEP_KEYS:  # provenance and photo stay as they are, whatever the model did
        new = set_front(new, key, front_line(md, key))
    checked(new)
    return new, result["summary"]


def commit_paths(paths: list[str], message: str) -> None:
    build_index()
    git("add", "--", *paths, "INDEX.md")
    git("commit", "--quiet", "-m", message)
    push()


def save_recipe(rel: str, base: str, md: str) -> str:
    path = recipe_file(rel)
    current = path.read_text(encoding="utf-8")
    if norm_md(current) != norm_md(base):
        raise Rejected("Das Rezept wurde inzwischen an anderer Stelle geändert. "
                       "Bitte die Seite neu laden und die Änderung noch einmal machen.")
    meta, md = checked(md)
    old_slug = front_meta(current).get("slug")
    if old_slug:  # the slug is the recipe's address in the app; it stays
        md = set_front(md, "slug", str(old_slug))
    if norm_md(md) == norm_md(current):
        raise Rejected("Keine Änderungen")
    path.write_text(md, encoding="utf-8")
    commit_paths([rel], f"recipe: edit {meta['title']}\n\nEdited in the app")
    return md


def commit_image(rel: str, webp: bytes, source: str) -> str:
    """Store webp as the recipe's photo (replacing the old one) and push. Returns the new file."""
    path = recipe_file(rel)
    md = path.read_text(encoding="utf-8")
    meta = front_meta(md)
    slug = str(meta.get("slug") or path.stem.split("-", 3)[-1])
    name = f"images/{images.filename(slug, webp)}"
    (REPO / "images").mkdir(exist_ok=True)
    (REPO / name).write_bytes(webp)
    md = set_front(set_front(md, "image", name), "image_source", quoted(source))
    path.write_text(md, encoding="utf-8")
    paths = [rel, name]
    old = str(meta.get("image") or "")
    if re.fullmatch(r"images/[A-Za-z0-9._-]+", old) and old != name and (REPO / old).exists() \
            and not any(old in f.read_text(encoding="utf-8")
                        for f in (REPO / "recipes").rglob("*.md") if f != path):
        git("rm", "--quiet", "--", old)  # no other recipe uses the old photo
    commit_paths(paths, f"recipe: new photo for {meta.get('title', slug)}\n\nPhoto: {source}")
    return md


def save_candidate(job: dict, webp: bytes, source: str) -> None:
    """Photo suggestion: kept in /data/candidates until the user accepts it (or a day passes)."""
    CANDIDATES.mkdir(exist_ok=True)
    for f in CANDIDATES.iterdir():
        if f.stat().st_mtime < time.time() - 86400:
            f.unlink(missing_ok=True)
    (CANDIDATES / f"{job['id']}.webp").write_bytes(webp)
    (CANDIDATES / f"{job['id']}.json").write_text(json.dumps({"path": job["path"], "source": source}))
    job.update(candidate=job["id"], source=source)


def section(md: str, names: str) -> str:
    m = re.search(rf"^## (?:{names})\s*\n(.*?)(?=^## |\Z)", split_front(md)[1], re.M | re.S)
    return m[1].strip() if m else ""


def find_photo(client, job: dict, md: str) -> None:
    meta, exclude = front_meta(md), set(job.get("exclude", []))
    url = str(meta.get("source_url") or "")
    if url and url not in exclude and url != str(meta.get("image_source") or ""):
        job["step"] = "Foto auf der Rezeptseite suchen …"
        got = images.from_url(url)
        if got:
            return save_candidate(job, images.to_webp(got[0]), url)
    pdf = inbox_source(meta)
    if pdf and pdf.suffix.lower() == ".pdf":
        job["step"] = "Foto im PDF suchen …"
        got = images.from_pdf(pdf, pdf_pages(md))
        label = f"{meta['source_file']}, {got[1]}" if got else ""
        if got and label not in exclude and label != str(meta.get("image_source") or ""):
            return save_candidate(job, images.to_webp(got[0]), label)
    if over_hourly_limit():  # only the Commons search below costs a Claude call
        raise Rejected(f"Zu viele KI-Aufträge (max. {MAX_JOBS_PER_HOUR} pro Stunde) – später noch einmal versuchen.")
    job["step"] = "Foto bei Wikimedia Commons suchen …"
    ingredients = section(md, "Zutaten|Ingredients")[:1500]
    queries = claude_json(client, "You suggest search terms for finding a photo of a dish on Wikimedia Commons.",
                          [{"type": "text", "text": f"Gericht: {meta.get('title')}\n\nZutaten:\n{ingredients}"}],
                          SEARCH_SCHEMA, effort="low", max_tokens=2000)["queries"]
    # photos offered before: Commons sources end with the file page URL (…/wiki/File:Name.jpg)
    seen = {unquote(x.split(" · ")[-1].rsplit("/", 1)[-1]).replace("_", " ") for x in exclude}
    for q in queries[:3]:
        got = images.from_commons(q, exclude=seen)
        if got:
            img, a = got
            return save_candidate(job, images.to_webp(img),
                                  f"Wikimedia Commons: {a['title'].removeprefix('File:')} · {a['author']} · {a['license']} · {a['url']}")
    raise Rejected("Kein passendes freies Foto gefunden – eigenes Foto hochladen oder KI-Bild generieren.")


def generate_photo(job: dict, md: str) -> None:
    if not GEMINI_KEY:
        raise Rejected("Bild-KI ist nicht eingerichtet (GEMINI_API_KEY fehlt).")
    meta = front_meta(md)
    lines = [l[2:].strip() for l in section(md, "Zutaten|Ingredients").splitlines() if l.startswith("- ")]
    lead = next((l.strip() for l in split_front(md)[1].splitlines()
                 if l.strip() and not l.startswith("#")), "")
    prompt = (f'Appetizing, realistic food photograph of the finished dish "{meta.get("title")}". '
              f"{lead} Main ingredients: {', '.join(lines[:15])}. "
              "Plated as it would be served at home, natural daylight, shallow depth of field, "
              "photographed from a 45-degree angle. No text, no labels, no watermark, no people, no hands.")
    if job.get("hint"):
        prompt += f" Additional wishes: {job['hint']}"
    job["step"] = "KI erzeugt das Foto …"
    try:
        data = images.generate(prompt, GEMINI_KEY, IMAGE_MODEL)
    except images.GenerateError as e:
        if e.status in (429, 500, 502, 503, 504):
            raise Retryable(f"Bild-KI {e.status}: {e.message}") from e
        raise Rejected(f"Bild-KI-Fehler: {e.message}") from e
    save_candidate(job, images.to_webp(data), f"KI-generiert ({IMAGE_MODEL})")


def run_edit_job(client, job: dict) -> None:
    kind, rel = job["kind"], job["path"]
    if kind == "ai-edit":
        job.update(status="running", step="Claude überarbeitet das Rezept …")
        job["markdown"], job["message"] = ai_edit(client, job["markdown"], job["instruction"])
    elif kind == "image-find":
        job.update(status="running", step="Foto wird gesucht …")
        find_photo(client, job, job.pop("markdown"))
    elif kind == "image-gen":
        job.update(status="running", step="KI erzeugt das Foto …")
        generate_photo(job, job.pop("markdown"))
    else:  # these change the repo
        job.update(status="running", step="Wird gespeichert …")
        with REPO_LOCK:
            sync_repo()
            if kind == "save":
                job["markdown"] = save_recipe(rel, job.pop("base"), job["markdown"])
            elif kind == "image-upload":
                upload = Path(job.pop("upload"))
                try:
                    webp = images.to_webp(upload.read_bytes())
                except Exception as e:
                    raise Rejected(f"Bild nicht lesbar ({type(e).__name__})") from e
                finally:
                    upload.unlink(missing_ok=True)
                job["markdown"] = commit_image(rel, webp, "eigenes Foto")
            elif kind == "image-accept":
                cand = CANDIDATES / job["candidate"]
                info = json.loads(cand.with_suffix(".json").read_text())
                if info["path"] != rel:
                    raise Rejected("Foto-Vorschlag gehört zu einem anderen Rezept")
                job["markdown"] = commit_image(rel, cand.with_suffix(".webp").read_bytes(), info["source"])
        log.info("app edit (%s): %s", kind, rel)
    if kind in ("save", "image-upload", "image-accept"):
        title = str(front_meta(job["markdown"]).get("title", ""))
        job["recipes"] = [{"title": title, "slug": Path(rel).stem.split("-", 3)[-1], "path": rel}]


# ---------------------------------------------------------------- app API --
# The recipe app (nginx) forwards /api/import/... here. Imports from the app run
# through the same pipeline as OneDrive files, one at a time, and the browser
# polls the job for its result.

REPO_LOCK = threading.Lock()
API_PORT = int(os.environ.get("API_PORT", "8080"))
MAX_JOBS_PER_HOUR = int(os.environ.get("MAX_JOBS_PER_HOUR", "20"))
UPLOAD_TYPES = IMAGE_TYPES | TEXT_TYPES | {".pdf", ".docx", ".xlsx"}
JOBS: dict[str, dict] = {}
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
RECENT: list[float] = []


def over_hourly_limit() -> bool:
    """True if MAX_JOBS_PER_HOUR costly jobs ran in the last hour; otherwise counts this one."""
    t = time.time()
    RECENT[:] = [x for x in RECENT if t - x < 3600]
    if len(RECENT) >= MAX_JOBS_PER_HOUR:
        return True
    RECENT.append(t)
    return False


def known_import(state: dict, key: str | None = None, sha: str | None = None):
    """Earlier import of the same URL key or file content, if any."""
    for k, e in state["files"].items():
        if (key and k == key) or (sha and e.get("sha") == sha):
            if e.get("status") in ("imported", "seeded"):
                return k, e
    return None


def new_job(kind: str, label: str, key: str, **extra) -> dict:
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "label": label, "key": key, "status": "queued",
           "step": "Wartet …", "created": now().isoformat(), "recipes": [], "message": "", **extra}
    JOBS[job["id"]] = job
    JOB_QUEUE.put(job["id"])
    return job


EDIT_KINDS = {"edit/ai": "ai-edit", "edit/save": "save", "image/find": "image-find",
              "image/generate": "image-gen", "image/accept": "image-accept", "image/upload": "image-upload"}
# count against MAX_JOBS_PER_HOUR when requested; image-find only when it needs Claude (find_photo)
COSTLY = {"ai-edit", "image-gen"}


def run_job(client, state: dict, job: dict) -> None:
    if job["kind"] in EDIT_KINDS.values():
        return run_edit_job(client, job)
    job.update(status="running", step="Quelle wird gelesen …")
    with REPO_LOCK:
        sync_repo()
        if job["kind"] == "file":
            path, name = Path(job["path"]), job["label"]
            source_file, source_url, source = f"App-Upload/{name}", None, path
            blocks, info = file_input(path, source_file, "per App-Upload")
            note = f"Imported via app upload ({name})"
        else:
            source_file, source_url, source = None, job["url"], None
            blocks, info = url_input(job["url"])
            note = f"Imported via app from {job['url']}"
        job["step"] = "Claude erstellt das Rezept …"
        log.info("app import (%s): %s", job["kind"], job["label"])
        result = convert(client, blocks, info)
        if result["decision"] == "skip" or not result["recipes"]:
            raise Rejected(result.get("reason") or "Kein Rezept erkannt")
        try:
            prepared = [validate(r["markdown"], source_file=source_file, source_url=source_url) for r in result["recipes"]]
        except (ValueError, yaml.YAMLError) as e:
            raise Retryable(f"Ausgabe ungültig: {e}") from e
        job["step"] = "Foto & Speichern …"
        done = [commit_recipe(meta, md, note, source) for meta, md in prepared]
        push()
        job["recipes"] = [{"title": meta["title"], "slug": Path(f).stem.split("-", 3)[-1], "path": f, "has_image": has}
                          for (meta, _), (f, has) in zip(prepared, done)]
        state["files"][job["key"]] = {"sha": job.get("sha", ""), "status": "imported", "via": "app",
                                     "recipes": [f for f, _ in done], "at": now().isoformat()}
        save_state(state)
    log.info("  app import done: %s", ", ".join(r["path"] for r in job["recipes"]))


def worker(client, state: dict) -> None:
    while True:
        job = JOBS[JOB_QUEUE.get()]
        try:
            run_job(client, state, job)
            job.update(status="done", step="Fertig")
        except Rejected as e:
            job.update(status="skipped", step="Übersprungen", message=str(e))
        except Retryable as e:
            job.update(status="failed", step="Fehler", message=f"Vorübergehender Fehler – bitte später erneut versuchen ({e})")
        except Exception as e:
            log.exception("app import failed")
            job.update(status="failed", step="Fehler", message=f"{type(e).__name__}: {e}")
        if job["status"] != "done":
            log.info("  app import %s: %s", job["status"], job["message"])


class Api(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, fmt, *args):  # keep the importer log readable
        pass

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def public(self, job: dict) -> dict:
        keys = ("id", "kind", "label", "status", "step", "recipes", "message", "candidate", "source")
        out = {k: job[k] for k in keys if k in job}
        if job["status"] == "done" and "markdown" in job:
            out["markdown"] = job["markdown"]
        return out

    def rate_limited(self) -> bool:
        return over_hourly_limit()

    def already(self, hit) -> None:
        k, e = hit
        slugs = [Path(f).stem.split("-", 3)[-1] for f in e.get("recipes", [])]
        self.reply(200, {"id": None, "status": "skipped", "step": "Übersprungen", "label": k,
                         "message": "Wurde bereits importiert.", "recipes": [{"slug": s, "title": s} for s in slugs]})

    def do_GET(self):
        m = re.fullmatch(r"/api/import/jobs/([0-9a-f]{12})", self.path)
        if self.path == "/api/import/health":
            return self.reply(200, {"ok": True, "queue": JOB_QUEUE.qsize(), "generate": bool(GEMINI_KEY)})
        c = re.fullmatch(r"/api/import/candidates/([0-9a-f]{12})\.webp", self.path)
        if c and (CANDIDATES / f"{c[1]}.webp").is_file():
            data = (CANDIDATES / f"{c[1]}.webp").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            return self.wfile.write(data)
        if not m or m[1] not in JOBS:
            return self.reply(404, {"message": "Unbekannter Auftrag"})
        self.reply(200, self.public(JOBS[m[1]]))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if self.path.startswith("/api/import/file"):
            name = unquote(self.headers.get("X-Filename", "")).strip().replace("/", "_") or "upload"
            ext = Path(name).suffix.lower()
            if ext not in UPLOAD_TYPES:
                return self.reply(400, {"message": f"Dateityp {ext or '(ohne Endung)'} wird nicht unterstützt"})
            if not 0 < length <= MAX_BYTES:
                return self.reply(413, {"message": f"Datei zu groß (max. {MAX_BYTES // 1048576} MB)"})
            data = self.rfile.read(length)
            sha = hashlib.sha256(data).hexdigest()
            if hit := known_import(self.state, sha=sha):
                return self.already(hit)
            if self.rate_limited():
                return self.reply(429, {"message": f"Zu viele Importe (max. {MAX_JOBS_PER_HOUR} pro Stunde)"})
            folder = DATA / "uploads"
            folder.mkdir(exist_ok=True)
            path = folder / f"{now():%Y%m%d-%H%M%S}-{slugify(Path(name).stem)}{ext}"
            path.write_bytes(data)
            job = new_job("file", name, f"app:{path.name}", path=str(path), sha=sha)
            return self.reply(202, self.public(job))
        action = self.path.split("?")[0].removeprefix("/api/import/")
        if action in EDIT_KINDS:
            return self.edit_request(action, length)
        if self.path == "/api/import/url":
            try:
                url = str(json.loads(self.rfile.read(min(length, 10_000)) or b"{}").get("url", "")).strip()
            except json.JSONDecodeError:
                url = ""
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                return self.reply(400, {"message": "Bitte einen vollständigen Link (https://…) angeben"})
            key = "url:" + url.split("#")[0]
            if hit := known_import(self.state, key=key):
                return self.already(hit)
            if self.rate_limited():
                return self.reply(429, {"message": f"Zu viele Importe (max. {MAX_JOBS_PER_HOUR} pro Stunde)"})
            job = new_job("url", url, key, url=url)
            return self.reply(202, self.public(job))
        self.reply(404, {"message": "Unbekannter Pfad"})

    def edit_request(self, action: str, length: int):
        kind = EDIT_KINDS[action]
        if kind == "image-upload":
            rel = unquote(self.headers.get("X-Recipe", ""))
            ext = Path(unquote(self.headers.get("X-Filename", ""))).suffix.lower()
            if ext not in IMAGE_TYPES:
                return self.reply(400, {"message": f"Bildtyp {ext or '(ohne Endung)'} wird nicht unterstützt"})
            if not 0 < length <= MAX_BYTES:
                return self.reply(413, {"message": f"Bild zu groß (max. {MAX_BYTES // 1048576} MB)"})
            body = {"path": rel}
        else:
            try:
                body = json.loads(self.rfile.read(min(length, 2_000_000)) or b"{}")
            except json.JSONDecodeError:
                body = None
            if not isinstance(body, dict):
                return self.reply(400, {"message": "Ungültige Anfrage"})
        rel = str(body.get("path", ""))
        if not RECIPE_PATH.fullmatch(rel):
            return self.reply(400, {"message": "Ungültiger Rezeptpfad"})
        text = lambda k, limit: str(body.get(k) or "")[:limit]
        extra = {"path": rel}
        if kind in ("ai-edit", "save", "image-find", "image-gen"):
            extra["markdown"] = text("markdown", 500_000)
            if not extra["markdown"].strip():
                return self.reply(400, {"message": "Rezepttext fehlt"})
        if kind == "ai-edit":
            extra["instruction"] = text("instruction", 2000).strip()
            if not extra["instruction"]:
                return self.reply(400, {"message": "Bitte beschreiben, was geändert werden soll"})
        if kind == "save":
            extra["base"] = text("base", 500_000)
        if kind == "image-find":
            extra["exclude"] = [str(x)[:1000] for x in (body.get("exclude") or [])][:20]
        if kind == "image-gen":
            if not GEMINI_KEY:
                return self.reply(400, {"message": "Bild-KI ist nicht eingerichtet (GEMINI_API_KEY fehlt)"})
            extra["hint"] = text("hint", 500).strip()
        if kind == "image-accept":
            extra["candidate"] = text("candidate", 12)
            if not re.fullmatch(r"[0-9a-f]{12}", extra["candidate"]) \
                    or not (CANDIDATES / f"{extra['candidate']}.webp").is_file():
                return self.reply(400, {"message": "Foto-Vorschlag ist abgelaufen – bitte neu suchen"})
        if kind in COSTLY and self.rate_limited():
            return self.reply(429, {"message": f"Zu viele KI-Aufträge (max. {MAX_JOBS_PER_HOUR} pro Stunde)"})
        if kind == "image-upload":
            folder = DATA / "uploads"
            folder.mkdir(exist_ok=True)
            extra["upload"] = str(folder / f"{now():%Y%m%d-%H%M%S}-photo-{uuid.uuid4().hex[:6]}{ext}")
            Path(extra["upload"]).write_bytes(self.rfile.read(length))
        job = new_job(kind, rel, f"edit:{rel}", **extra)
        return self.reply(202, self.public(job))


def start_api(client, state: dict) -> None:
    Api.state = state
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), Api)
    threading.Thread(target=server.serve_forever, daemon=True, name="api").start()
    threading.Thread(target=worker, args=(client, state), daemon=True, name="worker").start()
    log.info("app import API on port %s (max %s imports/hour)", API_PORT, MAX_JOBS_PER_HOUR)


# ------------------------------------------------------------------- main --

def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    # Synology's Docker uses the "db" log driver, which Portainer can't show -> also log to a file
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  RotatingFileHandler(DATA / "importer.log", maxBytes=1 << 20, backupCount=3)])
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    if not REPO_URL and not (REPO / ".git").exists():
        sys.exit("REPO_URL is not set")
    client = anthropic.Anthropic(api_key=read_secret("ANTHROPIC_API_KEY_FILE", "/run/secrets/anthropic_api_key"),
                                 max_retries=3)
    state = load_state()
    if not INBOX.is_dir():
        sys.exit(f"inbox {INBOX} not found")
    if os.environ.get("API", "1") == "1" and not DRY_RUN:
        start_api(client, state)
    log.info("watching %s every %ss (model %s, effort %s%s)", INBOX, INTERVAL, MODEL, EFFORT,
             ", DRY RUN" if DRY_RUN else "")
    while True:
        try:
            scan(client, state)
        except Exception:
            log.exception("scan failed; trying again next interval")
        if os.environ.get("ONCE") == "1":
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
