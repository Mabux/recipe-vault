"""scripts/backfill_photos.py against a fake importer API (same endpoints and job states)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import backfill_photos as bf  # noqa: E402

WITH = '---\ntitle: "Has Photo"\nimage: images/x.webp\n---\n# Has Photo\n'
WITHOUT = '---\ntitle: "{t}"\nslug: {s}\n---\n# {t}\n'


class FakeImporter(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200 if ctype != "application/json" or "id" in body else 202)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        action = self.path.removeprefix("/api/import/")
        FakeImporter.calls.append((action, body["path"]))
        if action == "image/accept":
            return self.reply({"id": "a" * 12, "status": "done", "step": "Fertig"})
        if "none" in body["path"]:
            return self.reply({"id": "b" * 12, "status": "skipped", "message": "Kein passendes freies Foto gefunden"})
        source = "KI-generiert (test)" if action == "image/generate" else "book.pdf, S. 2"
        self.reply({"id": "c" * 12, "status": "queued", "_source": source})
        FakeImporter.pending = source

    def do_GET(self):
        if self.path.endswith(".webp"):
            return self.reply(b"RIFFfakeWEBP", "image/webp")
        self.reply({"id": "c" * 12, "status": "done", "candidate": "d" * 12, "source": FakeImporter.pending})


@pytest.fixture
def api():
    FakeImporter.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImporter)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def data_repo(tmp_path):
    rec = tmp_path / "data" / "recipes" / "2026"
    rec.mkdir(parents=True)
    (rec / "2026-01-01-has-photo.md").write_text(WITH, encoding="utf-8")
    (rec / "2026-01-01-onion-rings.md").write_text(WITHOUT.format(t="Onion Rings", s="onion-rings"), encoding="utf-8")
    (rec / "2026-01-01-salmon.md").write_text(WITHOUT.format(t="Salmon", s="salmon"), encoding="utf-8")
    (rec / "2026-01-01-none-found.md").write_text(WITHOUT.format(t="Nothing", s="none-found"), encoding="utf-8")
    return tmp_path / "data"


def test_without_photo(data_repo):
    assert [p for p, _ in bf.without_photo(data_repo)] == [
        "recipes/2026/2026-01-01-none-found.md", "recipes/2026/2026-01-01-onion-rings.md",
        "recipes/2026/2026-01-01-salmon.md"]


def test_preview_then_accept(api, data_repo, tmp_path):
    out = tmp_path / "preview"
    bf.preview(api, data_repo, out, generate={"recipes/2026/2026-01-01-salmon.md"})
    manifest = {e["title"]: e for e in json.loads((out / "manifest.json").read_text(encoding="utf-8"))}
    assert manifest["Onion Rings"]["source"] == "book.pdf, S. 2"
    assert manifest["Salmon"]["source"] == "KI-generiert (test)"
    assert "candidate" not in manifest["Nothing"]
    assert (out / "2026-01-01-onion-rings.webp").read_bytes() == b"RIFFfakeWEBP"
    assert "Onion Rings" in (out / "index.html").read_text(encoding="utf-8")
    assert ("image/generate", "recipes/2026/2026-01-01-salmon.md") in FakeImporter.calls
    assert not any(a == "image/accept" for a, _ in FakeImporter.calls)  # preview never saves

    FakeImporter.calls = []
    bf.accept(api, out, skip={"salmon"})
    assert FakeImporter.calls == [("image/accept", "recipes/2026/2026-01-01-onion-rings.md")]
