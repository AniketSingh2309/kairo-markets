"""News pictures, fetched by the server and cached on disk.

The app never loads article images straight from third-party hosts: the news API rewrites each image
URL to ``/news/image?u=...``, and this store fetches it, but only from the news providers' own image
hosts (an allowlist, so the endpoint can't be used to fetch arbitrary URLs), only over HTTPS, and only
if the response really is an image of a sane size.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

ALLOWED_HOSTS = ("yimg.com", "zenfs.com")  # Yahoo Finance's image CDNs (and their subdomains)
MAX_BYTES = 3 * 1024 * 1024
MAX_FILES = 1500
_MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif"}


def allowed(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def proxied(url: str | None) -> str | None:
    """The app-local URL for a provider image, or None if it isn't one we serve."""
    return f"/news/image?u={quote(url, safe='')}" if allowed(url) else None


def _kind(content: bytes) -> str | None:
    for magic, ctype in _MAGIC.items():
        if content.startswith(magic):
            return ctype
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


@dataclass
class NewsImage:
    content: bytes
    content_type: str


@dataclass
class NewsImageStore:
    cache_dir: Path | None
    client: httpx.AsyncClient | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=6, follow_redirects=False,
                                            headers={"User-Agent": "Mozilla/5.0 (KairoMarkets news images)"})

    def _path(self, url: str) -> Path | None:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".img") if self.cache_dir else None

    def _trim(self) -> None:
        files = sorted(self.cache_dir.glob("*.img"), key=lambda p: p.stat().st_mtime)  # type: ignore[union-attr]
        for old in files[: max(0, len(files) - MAX_FILES)]:
            old.unlink(missing_ok=True)

    async def get(self, url: str) -> NewsImage | None:
        if not allowed(url):
            return None
        path = self._path(url)
        if path and path.exists() and (ctype := _kind(data := path.read_bytes())):
            return NewsImage(data, ctype)
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            if path and path.exists() and (ctype := _kind(data := path.read_bytes())):
                return NewsImage(data, ctype)
            try:
                resp = await self.client.get(url)
            except httpx.HTTPError:
                return None
            finally:
                self._locks.pop(url, None)
            if resp.status_code != 200 or not (100 <= len(resp.content) <= MAX_BYTES):
                return None
            ctype = _kind(resp.content)
            if ctype is None:
                return None
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(resp.content)
                os.replace(tmp, path)
                if len(self._locks) == 0:
                    self._trim()
            return NewsImage(resp.content, ctype)
