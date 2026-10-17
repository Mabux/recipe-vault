"""images.from_pdf(): which embedded photo belongs to a recipe page.

Run: pip install -r importer/requirements.txt -r importer/requirements-dev.txt
     python -m pytest importer/tests
"""
import io
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import images  # noqa: E402

PAGE_W, PAGE_H = 420, 595  # A5-ish, like the cookbook PDFs in the inbox


def png(w: int, h: int, color: tuple) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def make_pdf(tmp_path: Path, pages: list[dict]) -> Path:
    """pages: [{"text": str, "images": [(w, h, color)]}] -> PDF file.
    Every image covers the top of the page, like a header strip or a full-page photo."""
    doc = pymupdf.open()
    for spec in pages:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        for w, h, color in spec.get("images", []):
            page.insert_image(pymupdf.Rect(0, 0, PAGE_W, min(PAGE_H, PAGE_H * h / 597)), stream=png(w, h, color))
        if spec.get("text"):
            page.insert_text((20, PAGE_H - 60), spec["text"], fontsize=9)
    path = tmp_path / "book.pdf"
    doc.save(path)
    return path


RECIPE = "1. Place the crisper plate into the air fryer. 2. Cook for 12 minutes."


def test_header_strip_on_recipe_page(tmp_path):
    # compressed cookbook: 421x202 header photo, below the old 250 px minimum
    pdf = make_pdf(tmp_path, [{"text": "1"}, {"text": RECIPE, "images": [(421, 202, (200, 80, 40))]}])
    got = images.from_pdf(pdf, [2])
    assert got is not None and got[1] == "S. 2"


def test_photo_only_page_before_recipe(tmp_path):
    pdf = make_pdf(tmp_path, [
        {"text": "13", "images": [(421, 597, (10, 120, 30))]},   # unrelated photo further back
        {"images": [(421, 597, (220, 180, 40))]},                 # full-page dish photo, no text
        {"text": RECIPE},                                          # the recipe, no photo of its own
    ])
    got = images.from_pdf(pdf, [3])
    assert got is not None and got[1] == "S. 2"


def test_page_number_alone_counts_as_photo_only(tmp_path):
    pdf = make_pdf(tmp_path, [{"text": "14  14", "images": [(421, 597, (220, 180, 40))]}, {"text": RECIPE}])
    got = images.from_pdf(pdf, [2])
    assert got is not None and got[1] == "S. 1"


def test_section_cover_with_text_is_not_used(tmp_path):
    pdf = make_pdf(tmp_path, [{"text": "dessert", "images": [(421, 597, (90, 40, 140))]}, {"text": RECIPE}])
    assert images.from_pdf(pdf, [2]) is None


def test_own_photo_wins_over_previous_page(tmp_path):
    pdf = make_pdf(tmp_path, [
        {"images": [(421, 597, (220, 180, 40))]},
        {"text": RECIPE, "images": [(421, 202, (200, 80, 40))]},
    ])
    assert images.from_pdf(pdf, [2])[1] == "S. 2"


@pytest.mark.parametrize("w,h", [(96, 96), (421, 120), (300, 140)])
def test_small_images_are_ignored(tmp_path, w, h):
    pdf = make_pdf(tmp_path, [{"text": RECIPE, "images": [(w, h, (200, 80, 40))]}])
    assert images.from_pdf(pdf, [1]) is None


def test_logo_on_many_pages_is_ignored(tmp_path):
    logo = (400, 400, (0, 0, 0))
    pdf = make_pdf(tmp_path, [{"text": RECIPE, "images": [logo]} for _ in range(4)])
    assert images.from_pdf(pdf, [2]) is None


def test_result_converts_to_webp(tmp_path):
    pdf = make_pdf(tmp_path, [{"text": RECIPE, "images": [(421, 202, (200, 80, 40))]}])
    data, _ = images.from_pdf(pdf, [1])
    webp = images.to_webp(data)
    assert Image.open(io.BytesIO(webp)).format == "WEBP"
