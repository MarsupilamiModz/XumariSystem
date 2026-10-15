"""asyncpg connection pool wrapper (no ORM)."""

from __future__ import annotations

import datetime as dt
import decimal
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


class Database:
    """Thin async facade over an ``asyncpg.Pool`` with JSON codecs pre-registered."""

    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._known_guilds: set[int] = set()

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await conn.set_type_codec(type_name, encoder=dumps, decoder=json.loads, schema="pg_catalog")

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            init=self._init_connection,
            command_timeout=30,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool

    @property
    def dsn(self) -> str:
        return self._dsn

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self.pool.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        return await self.pool.execute(query, *args)

    async def executemany(self, query: str, args: Sequence[Sequence[Any]]) -> None:
        await self.pool.executemany(query, args)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def ping(self) -> bool:
        try:
            return await self.fetchval("SELECT 1") == 1
        except (asyncpg.PostgresError, OSError, RuntimeError):
            return False

    async def ensure_guild(self, guild_id: int, name: str | None = None) -> None:
        """Make sure a ``guilds`` + ``guild_settings`` row exists (cached per process)."""
        if guild_id in self._known_guilds:
            return
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO guilds (guild_id, name) VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET name = COALESCE(EXCLUDED.name, guilds.name), left_at = NULL
                """,
                guild_id,
                name,
            )
            await conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", guild_id
            )
        self._known_guilds.add(guild_id)
