"""find_photo() ("Foto suchen" in the app): the source PDF is searched before Wikimedia Commons,
and only the Commons search (a Claude call) counts against MAX_JOBS_PER_HOUR."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_images import RECIPE, make_pdf  # noqa: E402

MD = """---
title: "Onion Rings"
slug: onion-rings
source_file: "OneDrive Rezepte/book.pdf"
category: side
---

# Onion Rings

## Ingredients

- 2 onions

## Instructions

1. Fry.

## Quelle

COSORI · gespeichert am 2026-10-14 · importiert am 2026-10-14 aus OneDrive (PDF, S. 2)
"""


@pytest.fixture
def importer(tmp_path, monkeypatch):
    inbox, data = tmp_path / "inbox", tmp_path / "data"
    inbox.mkdir(), data.mkdir()
    monkeypatch.setenv("INBOX", str(inbox))
    monkeypatch.setenv("DATA", str(data))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("importer", None)
    mod = importlib.import_module("importer")
    mod.RECENT.clear()
    return mod


def with_book(importer, tmp_path, pages):
    pdf = make_pdf(tmp_path, pages)
    pdf.replace(importer.INBOX / "book.pdf")


def test_pdf_pages():
    import importer as mod
    assert mod.pdf_pages(MD) == [2]
    assert mod.pdf_pages(MD.replace("S. 2", "S. 12–13")) == [12, 13]
    assert mod.pdf_pages(MD.replace("(PDF, S. 2)", "(PDF)")) is None


def test_inbox_source(importer, tmp_path):
    meta = importer.front_meta(MD)
    assert importer.inbox_source(meta) is None  # not in the inbox (anymore)
    with_book(importer, tmp_path, [{"text": RECIPE}])
    assert importer.inbox_source(meta) == importer.INBOX / "book.pdf"
    assert importer.inbox_source({"source_file": "App-Upload/x.pdf"}) is None


def test_photo_from_pdf_without_claude(importer, tmp_path, monkeypatch):
    with_book(importer, tmp_path, [{"text": "1"}, {"text": RECIPE, "images": [(421, 202, (200, 80, 40))]}])
    saved = {}
    monkeypatch.setattr(importer, "save_candidate", lambda job, webp, source: saved.update(source=source))
    monkeypatch.setattr(importer, "claude_json", lambda *a, **k: pytest.fail("Claude must not be called"))
    importer.find_photo(None, {"exclude": []}, MD)
    assert saved["source"] == "OneDrive Rezepte/book.pdf, S. 2"
    assert importer.RECENT == []  # free: not counted against the hourly limit


def test_rejected_pdf_photo_falls_back_to_commons(importer, tmp_path, monkeypatch):
    with_book(importer, tmp_path, [{"text": "1"}, {"text": RECIPE, "images": [(421, 202, (200, 80, 40))]}])
    monkeypatch.setattr(importer, "claude_json", lambda *a, **k: {"queries": ["onion rings"]})
    monkeypatch.setattr(importer.images, "from_commons", lambda q, exclude=(): None)
    with pytest.raises(importer.Rejected):
        importer.find_photo(None, {"exclude": ["OneDrive Rezepte/book.pdf, S. 2"]}, MD)
    assert len(importer.RECENT) == 1  # the Commons search was charged


def test_commons_search_respects_hourly_limit(importer, monkeypatch):
    monkeypatch.setattr(importer, "MAX_JOBS_PER_HOUR", 1)
    importer.RECENT.append(__import__("time").time())
    monkeypatch.setattr(importer, "claude_json", lambda *a, **k: pytest.fail("over the limit: no Claude call"))
    with pytest.raises(importer.Rejected, match="Zu viele"):
        importer.find_photo(None, {"exclude": []}, MD)
