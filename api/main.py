"""FastAPI application factory.

* Embedded mode: the bot calls :func:`create_app` with its own DB pool and runs uvicorn in its loop.
* External mode: ``uvicorn api.main:build --factory`` – the app creates its own pool on startup.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bot import __version__
from bot.config.settings import AppConfig, Secrets, load_config
from bot.core.db import Database
from bot.core.errors import NotFound, XumariError
from migrations.runner import run_migrations

from .state import ApiState

_log = logging.getLogger("xumari.api")


def create_app(*, config: AppConfig | None = None, secrets: Secrets | None = None, db: Database | None = None) -> FastAPI:
    secrets = secrets or Secrets()
    config = config or load_config(secrets.config_path)
    owns_db = db is None
    database = db or Database(
        secrets.database_url, min_size=config.database.min_pool_size, max_size=config.database.max_pool_size
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_db:
            await database.connect()
            if config.database.auto_migrate:
                await run_migrations(database.pool)
        try:
            yield
        finally:
            await app.state.xs.close()
            if owns_db:
                await database.close()

    app = FastAPI(title=f"{config.bot.name} API", version=__version__, lifespan=lifespan)
    app.state.xs = ApiState(config=config, secrets=secrets, db=database)

    if config.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(XumariError)
    async def _domain_error(request: Request, exc: XumariError) -> JSONResponse:
        from .deps import Services

        message = Services(app.state.xs).i18n.t(exc.key, "en", **exc.kwargs)
        return JSONResponse(status_code=404 if isinstance(exc, NotFound) else 400,
                            content={"error": exc.key, "message": message})

    from .routers import register_routers

    register_routers(app)
    return app


def build() -> FastAPI:
    """Factory for standalone mode: ``uvicorn api.main:build --factory --host 0.0.0.0 --port 8080``."""
    from bot.core.logger import setup_logging

    secrets = Secrets()
    setup_logging(secrets.log_level, secrets.log_dir)
    return create_app(secrets=secrets)
