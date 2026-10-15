"""Shared state for API routes (config, secrets, database, lazily created service clients)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from bot.config.settings import AppConfig, Secrets
from bot.core.db import Database


@dataclass(slots=True)
class ApiState:
    config: AppConfig
    secrets: Secrets
    db: Database
    services: dict[str, Any] = field(default_factory=dict)

    async def close(self) -> None:
        for service in self.services.values():
            closer = getattr(service, "close", None)
            if closer is not None:
                await closer()
        self.services.clear()


def get_state(request: Request) -> ApiState:
    return request.app.state.xs  # type: ignore[no-any-return]
