"""Company logos, fetched from public logo CDNs and cached on disk.

No key is needed. Sources are tried in order until one returns a real image:

  EODHD   eodhd.com/img/logos/<EXCHANGE>/<TICKER>.png     square app-style icons (NSE, BSE, US)
  FMP     financialmodelingprep.com/image-stock/<SYMBOL>.png
  Parqet  assets.parqet.com/logos/{isin|symbol|crypto}/...

Hits are stored as files; misses are remembered for a week so an unknown ticker isn't re-asked on
every page view. Only the ticker (and ISIN, if known) leaves the machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

MISS_TTL_S = 7 * 24 * 3600
MAX_BYTES = 512 * 1024
_MAGIC = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif", b"RIFF": "image/webp"}


def _kind(content: bytes) -> str | None:
    head = content[:12]
    for magic, ctype in _MAGIC.items():
        if head.startswith(magic):
            return ctype
    if b"<svg" in content[:400].lower():
        return "image/svg+xml"
    return None


def candidates(symbol: str, isin: str | None = None) -> list[str]:
    """Logo URLs to try for a Yahoo-style symbol, best first."""
    if symbol.startswith("^") or "=" in symbol:
        return []  # indices, FX and futures have no company logo
    q = lambda s: quote(s, safe="")  # noqa: E731 - M&M must be sent as M%26M
    base, _, suffix = symbol.partition(".")
    if suffix in ("NS", "BO"):
        exch = "NSE" if suffix == "NS" else "BSE"
        out = [f"https://eodhd.com/img/logos/{exch}/{q(base)}.png",
               f"https://financialmodelingprep.com/image-stock/{q(symbol)}.png"]
        if isin:
            out.append(f"https://assets.parqet.com/logos/isin/{q(isin)}?format=png")
        return out
    if suffix:  # other exchanges (.L, .T, ...): FMP knows many of them
        return [f"https://financialmodelingprep.com/image-stock/{q(symbol)}.png"]
    if crypto := re.fullmatch(r"([A-Z0-9]+)-(?:USD|USDT|INR|EUR|GBP)", symbol):  # BTC-USD; class shares are BRK-B
        ticker = re.sub(r"\d+$", "", crypto.group(1)) or crypto.group(1)    # Yahoo's HYPE32196-USD -> HYPE
        return [f"https://assets.parqet.com/logos/crypto/{q(ticker)}?format=png",
                f"https://financialmodelingprep.com/image-stock/{q(ticker)}USD.png"]
    return [f"https://eodhd.com/img/logos/US/{q(symbol)}.png",
            f"https://financialmodelingprep.com/image-stock/{q(symbol)}.png",
            f"https://assets.parqet.com/logos/symbol/{q(symbol)}?format=png"]


@dataclass
class Logo:
    content: bytes
    content_type: str


@dataclass
class LogoStore:
    cache_dir: Path | None
    client: httpx.AsyncClient | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _mem: dict[str, Logo | None] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=8, follow_redirects=True,
                                            headers={"User-Agent": "Mozilla/5.0 (KairoMarkets logo fetcher)"})

    def _paths(self, symbol: str) -> tuple[Path, Path] | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha1(symbol.encode()).hexdigest()[:16]
        return self.cache_dir / f"{key}.img", self.cache_dir / f"{key}.miss"

    def _from_disk(self, symbol: str) -> tuple[bool, Logo | None]:
        paths = self._paths(symbol)
        if not paths:
            return False, None
        hit, miss = paths
        if hit.exists():
            data = hit.read_bytes()
            ctype = _kind(data)
            if ctype:
                return True, Logo(data, ctype)
        if miss.exists() and time.time() - miss.stat().st_mtime < MISS_TTL_S:
            return True, None
        return False, None

    async def _download(self, url: str) -> Logo | None:
        try:
            resp = await self.client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not (200 <= len(resp.content) <= MAX_BYTES):
            return None
        ctype = _kind(resp.content)
        return Logo(resp.content, ctype) if ctype else None

    async def get(self, symbol: str, isin: str | None = None) -> Logo | None:
        if symbol in self._mem:
            return self._mem[symbol]
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            if symbol in self._mem:
                return self._mem[symbol]
            known, logo = self._from_disk(symbol)
            if not known:
                logo = None
                for url in candidates(symbol, isin):
                    if (logo := await self._download(url)) is not None:
                        break
                if (paths := self._paths(symbol)) is not None:
                    hit, miss = paths
                    hit.parent.mkdir(parents=True, exist_ok=True)
                    if logo:
                        hit.write_bytes(logo.content)
                        miss.unlink(missing_ok=True)
                    else:
                        miss.write_bytes(b"")
            if len(self._mem) > 3000:
                self._mem.clear()
            self._mem[symbol] = logo
            return logo
