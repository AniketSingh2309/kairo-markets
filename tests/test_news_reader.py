"""In-app article page: link previews and BSE filing PDFs, both restricted to an allowlist."""

from __future__ import annotations

import asyncio

import httpx

from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.providers.news_reader import NewsReader, article_allowed, filing_allowed, parse_preview

ARTICLE = "https://finance.yahoo.com/markets/stocks/articles/some-story-123.html"
FILING = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc-123.pdf"
PAGE = """<html><head><title>Fallback title</title>
<meta property="og:title" content="Broadcom&#39;s AI Revenue Is Growing">
<meta name="description" content="Plain description">
<meta property="og:description" content="There&#39;s an   overlooked reason.">
<meta property="og:image" content="https://s.yimg.com/lo/x.jpg">
<meta property="og:site_name" content="Yahoo Finance">
</head><body><p>Full copyrighted article text that must not be returned.</p></body></html>"""
PDF = b"%PDF-1.7\n" + b"0" * 500


def test_preview_parsing_reads_meta_tags_only():
    p = parse_preview(PAGE)
    assert p["title"] == "Broadcom's AI Revenue Is Growing" and p["description"] == "There's an overlooked reason."
    assert p["image"] == "https://s.yimg.com/lo/x.jpg" and p["site"] == "Yahoo Finance"
    assert "copyrighted" not in str(p)                                     # never the article body
    assert parse_preview("<title>Only title</title>")["title"] == "Only title"
    generic = '<meta property="og:image" content="https://s.yimg.com/cv/apiv2/social/images/yahoo-finance-default-logo.png">'
    assert parse_preview(generic)["image"] is None                         # a site's fallback logo isn't a cover picture


def test_allowlists():
    assert article_allowed(ARTICLE) and article_allowed("https://uk.finance.yahoo.com/news/x.html")
    for bad in ["http://finance.yahoo.com/x", "https://yahoo.com.evil.com/x", "https://evil.com/?u=yahoo.com", None]:
        assert not article_allowed(bad)
    assert filing_allowed(FILING)
    for bad in ["https://www.bseindia.com/xml-data/corpfiling/../secret.pdf", "https://www.bseindia.com/other/x.pdf",
                "https://www.bseindia.com/xml-data/corpfiling/AttachLive/x.html", "https://evil.com/xml-data/corpfiling/x.pdf"]:
        assert not filing_allowed(bad)


def test_reader_fetches_caches_and_refuses_redirects_off_the_list(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "redirect-out" in str(request.url):
            return httpx.Response(302, headers={"location": "https://evil.com/steal"})
        if request.url.host == "evil.com":
            return httpx.Response(200, text=PAGE)
        if str(request.url).endswith(".pdf"):
            return httpx.Response(200, content=PDF)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    async def run():
        r = NewsReader(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        a = await r.preview(ARTICLE)
        await r.preview(ARTICLE)
        out = await r.preview("https://finance.yahoo.com/redirect-out")
        pdf1 = await r.filing_pdf(FILING)
        again = NewsReader(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        pdf2 = await again.filing_pdf(FILING)
        return a, out, pdf1, pdf2

    a, out, pdf1, pdf2 = asyncio.run(run())
    assert a["title"].startswith("Broadcom") and out is None and pdf1 == pdf2 == PDF
    assert calls.count(ARTICLE) == 1 and calls.count(FILING) == 1        # preview cached; PDF cached on disk
    assert not any("evil.com" in c for c in calls)                       # the redirect was never followed


def test_reader_endpoints(env):  # noqa: F811
    from api import deps
    from api.main import app

    class FakeReader:
        async def preview(self, url):
            return parse_preview(PAGE)

        async def filing_pdf(self, url):
            return PDF if filing_allowed(url) else None

    app.dependency_overrides[deps.get_news_reader] = lambda: FakeReader()
    client, _, _ = env
    body = client.get("/news/preview", params={"u": ARTICLE}).json()
    assert body["kind"] == "news" and body["title"].startswith("Broadcom")
    assert body["image"].startswith("/news/image?u=")                    # pictures come through our own server
    assert client.get("/news/preview", params={"u": FILING}).json()["pdf"].startswith("/news/filing?u=")
    assert client.get("/news/preview", params={"u": "https://evil.com/x"}).status_code == 404
    r = client.get("/news/filing", params={"u": FILING})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.headers["content-disposition"] == "inline"
    assert client.get("/news/filing", params={"u": "https://evil.com/x.pdf"}).status_code == 404
