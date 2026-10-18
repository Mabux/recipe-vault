#!/usr/bin/env python3
"""Import Chefkoch recipes from import/chefkoch-links.json, with cookbooks as tags (stdlib only).

The links file (data repo) lists every recipe saved on chefkoch.de:
  [{"url": "https://www.chefkoch.de/rezepte/...", "title": "...", "cookbooks": ["Kuchen", ...]}]

For every link, in file order:
  - recipe already in the vault (same Chefkoch recipe id in a `source_url`) -> only add the tags
  - otherwise -> import it like "+ Rezept → Link" in the app (POST /api/import/url), wait for
    the job, then add the tags to the new file (POST /api/import/edit/save, one commit)

Tags: each cookbook name goes lowercase into `keywords` (the app's tag filter), and all names
go unchanged into an extra frontmatter line `cookbooks: [...]`. Recipes without a cookbook
are imported without tags.

  python3 scripts/chefkoch_import.py --api https://recipe-vault.example --limit 20
  python3 scripts/chefkoch_import.py --api ... --cookbook Kuchen          only this cookbook
  python3 scripts/chefkoch_import.py --api ... --dry-run                  show the plan only

--api is the app (its nginx forwards /api/import/ and serves /gitea/...). --limit counts new
imports only (each one is a Claude call); tagging existing recipes is free and not counted.
The importer allows MAX_JOBS_PER_HOUR imports per hour; on HTTP 429 the script waits and retries.
Safe to run again: imported links are found in the vault (or the importer answers "already
imported") and are only tagged. A log of every link is kept in --state (JSON).
"""
import argparse
import base64
import io
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DONE = ("done", "skipped", "failed")
ID = re.compile(r"chefkoch\.de/rezepte/(\d+)")
LINKS_PATH = "import/chefkoch-links.json"


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def raw(self, path: str) -> bytes:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=120) as r:
            return r.read()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(f"{self.base}/api/import/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            data = e.read()
            if e.code >= 500 or not data.startswith(b"{"):
                raise RuntimeError(f"{method} {path}: HTTP {e.code}") from e
            return e.code, json.loads(data)

    def tree(self) -> dict[str, str]:
        return {e["path"]: e["sha"] for e in json.loads(self.raw("/gitea/tree"))["tree"] if e["type"] == "blob"}

    def file(self, path: str, tree: dict[str, str] | None = None) -> str:
        sha = (tree or self.tree())[path]
        return base64.b64decode(json.loads(self.raw(f"/gitea/blob/{sha}"))["content"]).decode("utf-8")

    def recipes(self) -> dict[str, str]:
        """All recipe files of the branch: path -> text (one archive download)."""
        out = {}
        with tarfile.open(fileobj=io.BytesIO(self.raw("/gitea/archive")), mode="r:gz") as tar:
            for m in tar.getmembers():
                p = m.name.split("/", 1)[-1]  # strip the "<repo>/" prefix
                if m.isfile() and re.fullmatch(r"recipes/\d{4}/[a-z0-9-]+\.md", p):
                    out[p] = tar.extractfile(m).read().decode("utf-8")
        return out

    def wait(self, job: dict) -> dict:
        while job.get("id") and job["status"] not in DONE:
            time.sleep(5)
            job = self.call("GET", f"jobs/{job['id']}")[1]
        return job


# ---------------------------------------------------------------- frontmatter --

def split_front(md: str) -> tuple[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", md, re.S)
    if not m:
        raise ValueError("kein Frontmatter")
    return m[1], m[2]


def front_value(front: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.*)$", front, re.M)
    return m[1].strip() if m else None


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def keywords(front: str) -> list[str]:
    m = re.search(r"^keywords:[ \t]*(.*)\n?((?:[ \t]+-.*\n?)*)", front, re.M)
    if not m:
        return []
    if m[1].startswith("["):
        inner = m[1].strip()[1:-1]
        return [unquote(x) for x in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', inner) if x.strip()]
    if m[1]:
        return [unquote(m[1])]
    return [unquote(x.split("-", 1)[1]) for x in m[2].splitlines() if x.strip()]


def flow(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in items) + "]"


def with_tags(md: str, cookbooks: list[str]) -> str:
    """md with each cookbook (lowercase) in keywords and a `cookbooks:` line; unchanged if nothing new."""
    front, body = split_front(md)
    kw = keywords(front)
    have = {k.casefold() for k in kw}
    new_kw = kw + [c.lower() for c in cookbooks if c.casefold() not in have]
    old_cb = front_value(front, "cookbooks")
    new_cb = flow(cookbooks)
    if new_kw == kw and old_cb == new_cb:
        return md
    lines = f"keywords: {flow(new_kw)}\ncookbooks: {new_cb}\n"
    front = re.sub(r"^cookbooks:.*\n?", "", front + "\n", flags=re.M)
    pat = re.compile(r"^keywords:.*\n(?:[ \t]+-.*\n)*", re.M)
    front = pat.sub(lambda _: lines, front, count=1) if pat.search(front) else front + lines
    return f"---\n{front.rstrip(chr(10))}\n---\n{body}"


def tag(api: Api, path: str, cookbooks: list[str], dry: bool) -> str:
    if not cookbooks:
        return "keine Kochbücher"
    current = api.file(path)
    new = with_tags(current, cookbooks)
    if new == current:
        return "Tags schon vorhanden"
    if dry:
        return "würde getaggt"
    code, job = api.call("POST", "edit/save", {"path": path, "markdown": new, "base": current})
    if code != 202:
        return f"Taggen fehlgeschlagen: {job.get('message', code)}"
    job = api.wait(job)
    if job["status"] == "done" or job.get("message") == "Keine Änderungen":
        return "getaggt"
    return f"Taggen fehlgeschlagen: {job.get('message')}"


# ----------------------------------------------------------------------- run --

def submit(api: Api, url: str, deadline: float | None) -> dict:
    """POST the link; on the hourly limit, wait and try again."""
    while True:
        code, job = api.call("POST", "url", {"url": url})
        if code != 429:
            return job
        if deadline and time.time() + 600 > deadline:
            raise TimeoutError("Zeitlimit erreicht")
        print(f"    Stundenlimit erreicht – warte 10 Minuten ({job.get('message', '')})", flush=True)
        time.sleep(600)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True, help="app URL, e.g. https://recipe-vault.example")
    ap.add_argument("--links", help=f"local links file (default: {LINKS_PATH} from the data repo via the app)")
    ap.add_argument("--limit", type=int, default=20, help="max. new imports in this run (default 20)")
    ap.add_argument("--cookbook", action="append", default=[], help="only links in this cookbook (repeatable)")
    ap.add_argument("--state", default="chefkoch-import-state.json", help="log file (JSON)")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="start no new import after this many minutes (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    a = ap.parse_args()
    deadline = time.time() + a.max_minutes * 60 if a.max_minutes else None

    api = Api(a.api)
    links = json.loads(Path(a.links).read_text(encoding="utf-8") if a.links else api.file(LINKS_PATH))
    if a.cookbook:
        want = {c.casefold() for c in a.cookbook}
        links = [l for l in links if want & {c.casefold() for c in l["cookbooks"]}]
    state_file = Path(a.state)
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}

    def existing() -> dict[str, str]:
        out = {}
        for path, md in api.recipes().items():
            m = ID.search(front_value(split_front(md)[0], "source_url") or "")
            if m:
                out[m[1]] = path
        return out

    known = existing()
    imported = 0
    counts: dict[str, int] = {}
    for link in links:
        url, books = link["url"], link["cookbooks"]
        rid = ID.search(url)[1]
        entry = state.get(url, {})
        if entry.get("status") in ("imported", "existing") and entry.get("tags") in ("getaggt", "Tags schon vorhanden", "keine Kochbücher"):
            continue
        if entry.get("status") == "skipped":
            continue
        if rid in known:
            res = tag(api, known[rid], books, a.dry_run)
            print(f"= {link['title']}  [{known[rid]}] – {res}", flush=True)
            entry = {"status": "existing", "paths": [known[rid]], "tags": res}
        else:
            if imported >= a.limit:
                break
            if deadline and time.time() > deadline:
                print("Zeitlimit erreicht – beim nächsten Lauf geht es hier weiter.")
                break
            imported += 1
            print(f"+ {link['title']}  ({url})", flush=True)
            if a.dry_run:
                print(f"    würde importiert, Tags: {', '.join(books) or '–'}")
                continue
            try:
                job = api.wait(submit(api, url, deadline))
            except TimeoutError:
                print("    Zeitlimit erreicht – beim nächsten Lauf geht es hier weiter.", flush=True)
                break
            paths = [r["path"] for r in job.get("recipes", []) if r.get("path")]
            if job["status"] == "done" and paths:
                res = "; ".join(tag(api, p, books, False) for p in paths)
                photo = all(r.get("has_image") for r in job["recipes"])
                print(f"    importiert: {', '.join(paths)}{'' if photo else ' (ohne Foto)'} – {res}", flush=True)
                entry = {"status": "imported", "paths": paths, "tags": res, "photo": photo}
                for p in paths:
                    known[rid] = p
            elif job["status"] == "skipped" and job.get("message") == "Wurde bereits importiert.":
                known = existing()  # imported earlier under this exact URL
                if rid in known:
                    res = tag(api, known[rid], books, False)
                    print(f"    war schon importiert: {known[rid]} – {res}", flush=True)
                    entry = {"status": "existing", "paths": [known[rid]], "tags": res}
                else:
                    print("    war schon importiert, Datei nicht gefunden", flush=True)
                    entry = {"status": "skipped", "message": job.get("message")}
            elif "API-Fehler" in job.get("message", "") or job["status"] == "failed":
                # Anthropic API error (e.g. workspace usage limit): not the link's fault -> retry later
                print(f"    {job['status']}: {job.get('message', '')}", flush=True)
                entry = {"status": "failed", "message": job.get("message", "")}
                if "usage limits" in job.get("message", ""):
                    print("API-Nutzungslimit des Anthropic-Workspace erreicht – Abbruch.", flush=True)
                    state[url] = {**entry, "title": link["title"], "cookbooks": books}
                    if not a.dry_run:
                        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                    break
            else:
                print(f"    {job['status']}: {job.get('message', '')}", flush=True)
                entry = {"status": job["status"], "message": job.get("message", "")}
        entry.update(title=link["title"], cookbooks=books, at=time.strftime("%Y-%m-%d %H:%M:%S"))
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        if not a.dry_run:
            state[url] = entry
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    left = sum(1 for l in links if l["url"] not in state or state[l["url"]].get("status") == "failed")
    print(f"\nFertig: {', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'nichts zu tun'}"
          f" – noch offen: {left} von {len(links)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
