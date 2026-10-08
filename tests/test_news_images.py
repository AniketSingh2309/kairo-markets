"""News pictures: parsed from the provider feed, proxied through an allowlisted, cached endpoint."""

from __future__ import annotations

import asyncio
import time

import httpx

from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.market_tools import get_announcements
from tools.providers.news_images import NewsImageStore, allowed, proxied
from tools.providers.yahoo_provider import YahooFinanceProvider

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 400
ORIGINAL = "https://media.zenfs.com/en/story/abc.jpg"
THUMB = "https://s.yimg.com/lo/mysterio/api/X/resizefill_w140_h140/https:%2F%2Fmedia.zenfs.com%2Fen%2Fstory%2Fabc.jpg"


def yahoo_news():
    now = int(time.time())
    return {"news": [
        {"uuid": "a1", "title": "With picture", "publisher": "Reuters", "link": "https://example.com/a1",
         "providerPublishTime": now - 600, "relatedTickers": ["AAPL"],
         "thumbnail": {"resolutions": [
             {"url": THUMB, "width": 140, "height": 140, "tag": "140x140"},
             {"url": ORIGINAL, "width": 1400, "height": 700, "tag": "original"}]}},
        {"uuid": "a2", "title": "No picture", "publisher": "Bloomberg", "link": "https://example.com/a2",
         "providerPublishTime": now - 300, "relatedTickers": ["AAPL"]},
        {"uuid": "a3", "title": "Insecure picture", "publisher": "X", "link": "https://example.com/a3",
         "providerPublishTime": now - 100, "relatedTickers": ["AAPL"],
         "thumbnail": {"resolutions": [{"url": "http://media.zenfs.com/x.jpg", "width": 10, "height": 10}]}},
    ]}


def test_yahoo_news_pictures_are_parsed():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=yahoo_news())),
                          base_url="https://query1.finance.yahoo.com")
    items = {i.id: i for i in get_announcements("AAPL", "7d", provider=YahooFinanceProvider(client=client), timeout_s=5).data.items}
    assert items["a1"].image_url == ORIGINAL and items["a1"].thumbnail_url == THUMB   # largest / smallest
    assert items["a2"].image_url is None and items["a2"].thumbnail_url is None
    assert items["a3"].image_url is None                                              # https only


def test_only_provider_image_hosts_are_proxied():
    assert allowed(ORIGINAL) and allowed(THUMB)
    for bad in ["http://media.zenfs.com/a.jpg", "https://evil.com/a.jpg", "https://yimg.com.evil.com/a.jpg",
                "https://127.0.0.1/a.jpg", "file:///etc/passwd", "", None]:
        assert not allowed(bad) and proxied(bad) is None
    assert proxied(ORIGINAL).startswith("/news/image?u=https%3A%2F%2Fmedia.zenfs.com")


def test_store_fetches_once_and_rejects_non_images(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=JPEG if "abc" in str(request.url) else b"<html>blocked</html>")

    async def run():
        store = NewsImageStore(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        a = await store.get(ORIGINAL)
        b = await store.get(ORIGINAL)
        bad = await store.get("https://media.zenfs.com/en/story/notanimage.jpg")
        nope = await store.get("https://evil.com/abc.jpg")
        return a, b, bad, nope

    a, b, bad, nope = asyncio.run(run())
    assert a.content == JPEG and a.content_type == "image/jpeg" and b.content == JPEG
    assert bad is None and nope is None
    assert calls.count(ORIGINAL) == 1 and not any("evil" in c for c in calls)  # cached; never asked off-list hosts


def test_news_api_rewrites_pictures_and_serves_them(env):  # noqa: F811
    from api import deps
    from api.main import app
    from tools.providers.news_images import NewsImage

    class FakeImages:
        async def get(self, url):
            return NewsImage(JPEG, "image/jpeg") if allowed(url) else None

    app.dependency_overrides[deps.get_news_images] = lambda: FakeImages()
    client, _, _ = env
    body = client.get("/news?symbols=AAPL&period=30d").json()
    assert body["items"] and all("image" in i and "thumb" in i for i in body["items"])
    r = client.get("/news/image", params={"u": ORIGINAL})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "max-age" in r.headers["cache-control"]
    assert client.get("/news/image", params={"u": "https://evil.com/a.jpg"}).status_code == 404
