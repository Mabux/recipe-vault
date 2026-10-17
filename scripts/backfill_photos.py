#!/usr/bin/env python3
"""Add photos to recipes that have none, through the importer's photo API (stdlib only).

Same path as "Bearbeiten → Foto" in the app: the importer looks for a photo (recipe page,
source PDF in the inbox, Wikimedia Commons) or generates one with Gemini, and commits it
only when it is accepted. Two steps, so every photo can be checked before it is saved:

  1. preview: python3 scripts/backfill_photos.py ../recipe-vault-data --api http://HOST:8096
     -> for every recipe without `image:` a suggestion in ./photo-preview/ (+ index.html)
     --generate recipes/2026/x.md   use Gemini for that recipe instead of searching
  2. accept:  python3 scripts/backfill_photos.py ../recipe-vault-data --api http://HOST:8096 --accept
     -> saves the previewed suggestions (one commit per recipe); --skip SLUG leaves one out

--api is the app (its nginx forwards /api/import/) or the importer itself (port 8099).
Suggestions expire after a day on the importer; run the preview again if needed.
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DONE = ("done", "skipped", "failed")


def call(api: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"{api}/api/import/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        data = e.read()
        if e.code >= 500 or not data.startswith(b"{"):
            raise RuntimeError(f"{method} {path}: HTTP {e.code}") from e
    return data if path.endswith(".webp") else json.loads(data)


def run(api: str, path: str, body: dict) -> dict:
    """Start an importer job and wait until it has finished."""
    job = call(api, "POST", path, body)
    while job.get("id") and job.get("status") not in DONE:
        time.sleep(2)
        job = call(api, "GET", f"jobs/{job['id']}")
    return job


def front(md: str) -> str:
    return md.split("\n---", 1)[0]


def without_photo(data_repo: Path) -> list[tuple[str, str]]:
    """(repo-relative path, markdown) of every recipe without an image."""
    out = []
    for f in sorted((data_repo / "recipes").rglob("*.md")):
        md = f.read_text(encoding="utf-8")
        if not re.search(r"^image:", front(md), re.M):
            out.append((f.relative_to(data_repo).as_posix(), md))
    return out


def title_of(md: str) -> str:
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', front(md), re.M)
    return m[1] if m else "?"


def preview(api: str, data_repo: Path, out: Path, generate: set[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    todo = without_photo(data_repo)
    print(f"{len(todo)} recipes without a photo")
    manifest = []
    for rel, md in todo:
        kind = "image/generate" if rel in generate else "image/find"
        body = {"path": rel, "markdown": md} | ({} if rel in generate else {"exclude": []})
        job = run(api, kind, body)
        entry = {"path": rel, "title": title_of(md), "status": job.get("status"),
                 "source": job.get("source", ""), "message": job.get("message", "")}
        if job.get("candidate"):
            entry["candidate"] = job["candidate"]
            entry["file"] = f"{Path(rel).stem}.webp"
            (out / entry["file"]).write_bytes(call(api, "GET", f"candidates/{job['candidate']}.webp"))
        print(f"  {'ok ' if 'candidate' in entry else '-- '} {entry['title']}: {entry['source'] or entry['message']}")
        manifest.append(entry)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "index.html").write_text(contact_sheet(manifest), encoding="utf-8")
    print(f"preview: {out / 'index.html'}  ({sum('candidate' in e for e in manifest)} suggestions)")


def contact_sheet(manifest: list[dict]) -> str:
    cards = "".join(
        f'<figure><img src="{html.escape(e["file"])}" alt=""><figcaption><b>{html.escape(e["title"])}</b>'
        f'<br>{html.escape(e["source"])}</figcaption></figure>' if "file" in e else
        f'<figure class="none"><figcaption><b>{html.escape(e["title"])}</b><br>{html.escape(e["message"])}</figcaption></figure>'
        for e in manifest)
    return ("<!doctype html><meta charset=utf-8><title>Photo preview</title><style>"
            "body{font:14px system-ui;margin:16px;background:#fff;color:#222}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}"
            "figure{margin:0;border:1px solid #ddd;border-radius:8px;overflow:hidden}"
            "img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}"
            "figcaption{padding:6px 8px;font-size:12px;color:#555}figcaption b{color:#222}"
            ".none{background:#fff4f0}</style>"
            f"<h1>Photo preview ({len(manifest)})</h1><main>{cards}</main>")


def accept(api: str, out: Path, skip: set[str]) -> None:
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for e in manifest:
        slug = Path(e["path"]).stem.split("-", 3)[-1]
        if "candidate" not in e or slug in skip:
            continue
        job = run(api, "image/accept", {"path": e["path"], "candidate": e["candidate"]})
        print(f"  {job.get('status'):8} {e['title']}{': ' + job['message'] if job.get('message') else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_repo", type=Path, help="local clone of the recipe data repo (pulled)")
    ap.add_argument("--api", required=True, help="app or importer base URL, e.g. http://192.168.1.10:8095")
    ap.add_argument("--out", type=Path, default=Path("photo-preview"), help="preview folder")
    ap.add_argument("--generate", action="append", default=[], metavar="PATH", help="use Gemini for this recipe")
    ap.add_argument("--accept", action="store_true", help="save the previewed suggestions")
    ap.add_argument("--skip", action="append", default=[], metavar="SLUG", help="with --accept: leave this one out")
    a = ap.parse_args()
    if not (a.data_repo / "recipes").is_dir():
        sys.exit(f"no recipes/ folder in {a.data_repo}")
    api = a.api.rstrip("/")
    if a.accept:
        accept(api, a.out, set(a.skip))
    else:
        preview(api, a.data_repo, a.out, set(a.generate))


if __name__ == "__main__":
    main()
