"""HTTP API.

    uvicorn api.main:app --reload

POST /analyze  {"symbol": "AAPL", "question": "...", "date": null, "period": "30d"}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from agents.orchestrator import Orchestrator
from api.live import router as live_router
from api.live import watch_router
from api.explore import router as explore_router
from api.funds import router as funds_router
from api.insights import router as insights_router
from api.market import router as market_router
from api.options import router as options_router
from api.trading import router as trading_router
from api.research import router as research_router
from api.portfolio import router as portfolio_router
from core.config import get_settings
from core.schemas import AnalyzeRequest, AnalyzeResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Kairo Markets API",
    version="0.1.0",
    description="Live quotes, technical outlooks and cited multi-agent research (Agno + Groq) "
                "on a strict, timestamped tool contract.",
)

app.include_router(live_router)
app.include_router(watch_router)
app.include_router(market_router)
app.include_router(portfolio_router)
app.include_router(insights_router)
app.include_router(explore_router)
app.include_router(funds_router)
app.include_router(options_router)
app.include_router(trading_router)
app.include_router(research_router)


@app.on_event("startup")
async def _start_warm_up() -> None:
    from api import warmup

    if warmup.enabled():
        asyncio.get_running_loop().create_task(warmup.warm_up())


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    return Orchestrator(get_settings())


STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Browsers must re-check app files on load (cheap 304s), so updates show up immediately."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


def _asset_versions() -> dict[str, str]:
    """Content-derived version per app asset (size + mtime), for cache-busting URLs."""
    out = {}
    for path in sorted([*STATIC_DIR.glob("js/*.js"), *STATIC_DIR.glob("css/*.css"), *STATIC_DIR.glob("vendor/*.mjs")]):
        st = path.stat()
        out[f"/static/{path.relative_to(STATIC_DIR).as_posix()}"] = hashlib.sha1(
            f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:10]
    return out


@app.get("/", include_in_schema=False)
def frontend() -> HTMLResponse:
    """index.html with versioned asset URLs. An import map versions the ES modules too, so a browser
    can never pair a new page with a stale cached module after an update."""
    versions = _asset_versions()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for url in ("/static/css/app.css", "/static/js/app.js"):
        html = html.replace(f'"{url}"', f'"{url}?v={versions.get(url, "0")}"')
    import_map = json.dumps({"imports": {u: f"{u}?v={v}" for u, v in versions.items() if u.endswith((".js", ".mjs"))}})
    html = html.replace('<script type="module"', f'<script type="importmap">{import_map}</script>\n<script type="module"', 1)
    return HTMLResponse(html)


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "model": settings.groq_model if settings.llm_provider == "groq" else "stub",
        "llm_configured": settings.llm_provider == "stub" or settings.groq_api_key is not None,
        "data_provider": settings.data_provider,
        "data_agent_mode": settings.data_agent_mode,
    }


# Sync endpoint: FastAPI runs it in a worker thread, so blocking tool/LLM calls
# don't stall the event loop.
@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(
    request: AnalyzeRequest,
    include_trace: bool = Query(True, description="Include stage trace and tool-call ledger"),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> JSONResponse:
    response = orchestrator.run(request)
    if not include_trace:
        response.trace = []
        response.tool_calls = []
    codes = {f.code for f in response.flags}
    status_code = 200
    if response.status == "error":
        status_code = 404 if "UNKNOWN_SYMBOL" in codes else 422 if "INVALID_INPUT" in codes else 500
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


@app.post("/analyze/report", response_class=PlainTextResponse)
def analyze_report(
    request: AnalyzeRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> str:
    """Same pipeline, returns only the rendered Markdown report."""
    return orchestrator.run(request).report_markdown
