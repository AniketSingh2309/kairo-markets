"""In-app reading: article previews and BSE filing PDFs, served from our own origin.

Publishers (Yahoo, BSE) forbid framing their pages, and their article text is copyrighted, so the app
does what link previews do: it shows the headline, picture and the publisher's own short description
(``og:`` meta tags), with a link to the full story. BSE filings are public regulatory documents, so the
filed PDF itself is relayed and shown inside the app.

Both fetchers only touch an allowlist of hosts over HTTPS, so these endpoints can't be used to fetch
arbitrary URLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tools.providers.listings import BROWSER_HEADERS, BSE_HEADERS

ARTICLE_HOSTS = ("finance.yahoo.com", "yahoo.com")                 # where the news feed links to
FILING_PREFIX = "https://www.bseindia.com/xml-data/corpfiling/"   # BSE filing attachments
MAX_HTML = 4 * 1024 * 1024
MAX_PDF = 25 * 1024 * 1024
PREVIEW_TTL_S = 6 * 3600


def article_allowed(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ARTICLE_HOSTS)


def filing_allowed(url: str | None) -> bool:
    return bool(url) and url.startswith(FILING_PREFIX) and url.lower().endswith(".pdf") and ".." not in url


_META = re.compile(r"<meta\b[^>]*>", re.I)
# Sites fall back to their own logo when an article has no picture; that isn't the story's picture.
_GENERIC_IMAGE = re.compile(r"default[-_]?logo|/social/images/|placeholder", re.I)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def parse_preview(page: str) -> dict[str, str | None]:
    """og:/twitter:/article: meta tags -> {title, description, image, site, published, author}."""
    tags: dict[str, str] = {}
    for tag in _META.findall(page[:400_000]):  # meta tags live in <head>
        attrs = {m.group(1).lower(): html.unescape(m.group(3) if m.group(3) is not None else m.group(4) or "")
                 for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in tags:
            tags[key] = re.sub(r"\s+", " ", attrs["content"]).strip()
    title_tag = re.search(r"<title[^>]*>(.*?)</title>", page[:200_000], re.I | re.S)
    image = tags.get("og:image") or tags.get("twitter:image")
    return {
        "title": tags.get("og:title") or tags.get("twitter:title") or (html.unescape(title_tag.group(1)).strip() if title_tag else None),
        "description": tags.get("og:description") or tags.get("description") or tags.get("twitter:description"),
        "image": image if image and image.startswith("https://") and not _GENERIC_IMAGE.search(image) else None,
        "site": tags.get("og:site_name"),
        "published": tags.get("article:published_time"),
        "author": tags.get("author") or tags.get("article:author"),
    }


@dataclass
class NewsReader:
    cache_dir: Path | None = None
    client: httpx.AsyncClient | None = None
    _previews: dict[str, tuple[float, dict]] = field(default_factory=dict, init=False)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10, follow_redirects=False, headers=BROWSER_HEADERS)

    async def _get(self, url: str, limit: int, headers: dict | None = None) -> httpx.Response | None:
        """GET without following redirects off the allowlist."""
        for _ in range(3):
            try:
                resp = await self.client.get(url, headers=headers)
            except httpx.HTTPError:
                return None
            if resp.is_redirect and (loc := resp.headers.get("location")):
                nxt = str(resp.url.join(loc))
                if not (article_allowed(nxt) or filing_allowed(nxt)):
                    return None
                url = nxt
                continue
            return resp if resp.status_code == 200 and len(resp.content) <= limit else None
        return None

    async def preview(self, url: str) -> dict | None:
        if not article_allowed(url):
            return None
        hit = self._previews.get(url)
        if hit and time.monotonic() - hit[0] < PREVIEW_TTL_S:
            return hit[1]
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            hit = self._previews.get(url)
            if hit and time.monotonic() - hit[0] < PREVIEW_TTL_S:
                return hit[1]
            resp = await self._get(url, MAX_HTML)
            self._locks.pop(url, None)
            if resp is None or "html" not in resp.headers.get("content-type", "html"):
                return None
            data = parse_preview(resp.text)
            if len(self._previews) > 2000:
                self._previews.clear()
            self._previews[url] = (time.monotonic(), data)
            return data

    def _pdf_path(self, url: str) -> Path | None:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".pdf") if self.cache_dir else None

    async def filing_pdf(self, url: str) -> bytes | None:
        """The filed PDF (immutable once filed, so cached on disk for good)."""
        if not filing_allowed(url):
            return None
        path = self._pdf_path(url)
        if path and path.exists():
            data = path.read_bytes()
            if data.startswith(b"%PDF"):
                return data
        resp = await self._get(url, MAX_PDF, headers={**BROWSER_HEADERS, **BSE_HEADERS, "Accept": "application/pdf,*/*"})
        if resp is None or not resp.content.startswith(b"%PDF"):
            return None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(resp.content)
            os.replace(tmp, path)
        return resp.content
