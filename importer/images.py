"""Find a dish photo for a recipe and store it as a small WebP.

Sources, best first:
  1. the recipe's web page: og:image / JSON-LD image (YouTube: video thumbnail)
  2. the largest photo embedded in the source PDF (on the recipe's pages)
  3. Wikimedia Commons: freely licensed photo found by search term (with attribution)

Used by the importer for new recipes and for photos chosen while editing a recipe.
For editing in the app there is also generate(): a dish photo made by Gemini.
"""
import base64
import hashlib
import html
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
MAX_SIDE = 1200


def _get(url: str, limit: int = 15_000_000, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


# --------------------------------------------------------------- normalize --

def to_webp(data: bytes) -> bytes:
    """Any image -> RGB WebP, longest side <= MAX_SIDE, extreme strips center-cropped to 4:3."""
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener()  # iPhone photos uploaded from the app
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    img = img.convert("RGB")
    w, h = img.size
    if w / h > 2.2:      # wide banner (e.g. cookbook page footers): keep the middle
        nw = int(h * 4 / 3)
        img = img.crop(((w - nw) // 2, 0, (w + nw) // 2, h))
    elif h / w > 2.2:
        nh = int(w * 4 / 3)
        img = img.crop((0, (h - nh) // 2, w, (h + nh) // 2))
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "WEBP", quality=80, method=6)
    return out.getvalue()


def filename(slug: str, webp: bytes) -> str:
    """Content hash in the name -> the app can cache images forever."""
    return f"{slug}-{hashlib.sha256(webp).hexdigest()[:8]}.webp"


# ------------------------------------------------------------------ web ----

def from_url(page_url: str):
    """(image bytes, image url) from a recipe page, or None."""
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", page_url)
    if m:
        for name in ("maxresdefault", "hqdefault"):
            try:
                url = f"https://img.youtube.com/vi/{m[1]}/{name}.jpg"
                return _get(url), url
            except Exception:
                continue
        return None
    if "tiktok.com" in page_url:  # pages need JS; the public oEmbed has the video cover
        try:
            meta = json.loads(_get("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(page_url, safe="")))
            return _get(meta["thumbnail_url"]), meta["thumbnail_url"]
        except Exception:
            return None
    try:
        page = _get(page_url, limit=2_000_000).decode("utf-8", "replace")
    except Exception:
        return None
    patterns = [
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'"image"\s*:\s*\[?\s*"(https?:[^"]+)"',
        r'"image"\s*:\s*\{[^}]*"url"\s*:\s*"(https?:[^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, page)
        if m:
            url = urllib.parse.urljoin(page_url, html.unescape(m[1]).replace("\\/", "/"))
            try:
                return _get(url), url
            except Exception:
                continue
    return None


def page_content(url: str, max_text: int = 60_000) -> dict:
    """What a recipe link gives us without a browser: schema.org Recipe data
    (JSON-LD), title/description, readable page text, and for YouTube/TikTok
    the video caption/description (where creators often put the recipe)."""
    out = {"url": url, "title": "", "description": "", "recipe_jsonld": [], "text": ""}
    if "tiktok.com" in url:
        try:
            meta = json.loads(_get("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe="")))
            out.update(title=meta.get("author_name", ""), description=meta.get("title", ""))
        except Exception:
            pass
        return out
    page = _get(url, limit=4_000_000).decode("utf-8", "replace")
    def meta(prop):
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)', page) or \
            re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{prop}["\']', page)
        return html.unescape(m[1]) if m else ""
    t = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
    out["title"] = meta("og:title") or (html.unescape(t[1]).strip() if t else "")
    out["description"] = meta("og:description") or meta("description")
    if re.search(r"youtube\.com/watch|youtu\.be/", url):
        m = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', page)
        if m:
            out["description"] = json.loads(f'"{m[1]}"')
    def recipes(node):
        if isinstance(node, list):
            for n in node: yield from recipes(n)
        elif isinstance(node, dict):
            types = node.get("@type"); types = types if isinstance(types, list) else [types]
            if "Recipe" in types: yield node
            for k in ("@graph", "mainEntity", "itemListElement"):
                if k in node: yield from recipes(node[k])
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            out["recipe_jsonld"].extend(recipes(json.loads(block.strip())))
        except Exception:
            continue
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", page)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    out["text"] = re.sub(r"\s+", " ", body).strip()[:max_text]
    return out


# ------------------------------------------------------------------ pdf ----

def from_pdf(path, pages=None):
    """(image bytes, 'S. N') for the largest photo on the given 1-based pages, or None.
    Images that repeat on several pages (logos, headers, page decorations) are ignored.
    If the recipe pages have no photo, a photo-only page right before them is used:
    cookbooks often put the dish photo full-page opposite the recipe."""
    import pymupdf
    from collections import Counter
    doc = pymupdf.open(path)
    uses = Counter(im[0] for page in doc for im in page.get_images(full=True))

    def best_on(page_numbers):
        best = None
        for pno in page_numbers:
            if not 1 <= pno <= doc.page_count:
                continue
            for im in doc[pno - 1].get_images(full=True):
                xref, smask, w, h = im[0], im[1], im[2], im[3]
                # compressed books keep header photos at ~420x200, so the short side may be small
                if max(w, h) < 400 or min(w, h) < 150 or not 0.25 <= w / h <= 4.0:
                    continue  # icons, rules, QR codes
                if doc.page_count > 2 and uses[xref] > 2:
                    continue  # same image on many pages: logo/branding
                if best is None or w * h > best[0]:
                    best = (w * h, xref, smask, pno)
        return best

    def photo_only(pno):
        # nothing but a page number: a section cover ("dessert") has words and is skipped
        return 1 <= pno <= doc.page_count and not re.sub(r"[\d\s]", "", doc[pno - 1].get_text())

    best = best_on(pages or range(1, doc.page_count + 1))
    if not best and pages and photo_only(min(pages) - 1):
        best = best_on([min(pages) - 1])
    if not best:
        return None
    _, xref, smask, pno = best
    pix = pymupdf.Pixmap(doc, xref)
    if pix.n - pix.alpha > 3:  # CMYK etc.
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if smask:  # transparency lives in a separate mask; without it corners turn black
        try:
            pix = pymupdf.Pixmap(pymupdf.Pixmap(pix, 0) if pix.alpha else pix, pymupdf.Pixmap(doc, smask))
        except Exception:
            pass
    return pix.tobytes("png"), f"S. {pno}"


# -------------------------------------------------------------- commons ----

def from_commons(query: str, exclude=()):
    """(image bytes, attribution dict) for the first freely licensed Commons photo, or None.
    exclude: Commons file titles to pass over (photos already offered)."""
    api = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": 10,
        "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": MAX_SIDE,
    })
    try:
        data = json.loads(_get(api))
    except Exception:
        return None
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        if p.get("title") in exclude:
            continue
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if not re.search(r"CC0|CC BY|Public domain|PD", lic, re.I) or "NC" in lic or "ND" in lic:
            continue
        if min(info.get("width", 0), info.get("height", 0)) < 400:
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "unbekannt"
        try:
            return _get(info.get("thumburl") or info["url"]), {
                "title": p["title"], "author": artist, "license": lic, "url": info.get("descriptionurl", "")}
        except Exception:
            continue
    return None


# --------------------------------------------------------------- gemini ----

def generate(prompt: str, key: str, model: str) -> bytes:
    """Image bytes from a Gemini image model (REST generateContent). Raises GenerateError."""
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3"}},
        }).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["message"]
        except Exception:
            msg = e.reason
        raise GenerateError(e.code, msg) from e
    cand = (data.get("candidates") or [{}])[0]
    for part in (cand.get("content") or {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    reason = cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "unbekannt"
    raise GenerateError(200, f"kein Bild geliefert ({reason})")


class GenerateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message
