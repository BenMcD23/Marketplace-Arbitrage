"""FastAPI application.

    uv run arb serve            # or: uvicorn api.main:app --reload

Serves the dashboard's data and nothing else — no HTML, no templates. The React
app is a separate build that talks to this over JSON, which keeps the two
deployable independently and means the API is equally usable from a script.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import close_db, get_app_settings, get_db
from api.routers import deals, insights, runs, watchlist
from arb.factory import seed_queries
from arb.logging_conf import configure_logging, get_logger

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_app_settings()
    configure_logging(env=settings.env, level=settings.log_level)
    db = get_db()
    seed_queries(settings, db)
    log.info("api_start", db=settings.db_path, marketplace=settings.ebay_marketplace)
    yield
    close_db()
    log.info("api_stop")


app = FastAPI(
    title="Marketplace Arbitrage API",
    version="0.2.0",
    summary="Finds underpriced electronics on eBay and values them against real resale data.",
    lifespan=lifespan,
)

_settings = get_app_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(deals.router)
app.include_router(runs.router)
app.include_router(watchlist.router)
app.include_router(insights.router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": "Marketplace Arbitrage API",
        "version": app.version,
        "docs": "/docs",
    }
