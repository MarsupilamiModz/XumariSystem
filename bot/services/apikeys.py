"""API keys for the order API. Only a SHA-256 hash is stored; the plain key is shown exactly once."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass

from ..core.db import Database

SCOPES: tuple[str, ...] = (
    "orders:read", "orders:write", "customers:read", "customers:write", "products:read", "invoices:read",
)
KEY_PREFIX = "xs_"


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ApiKey:
    id: int
    guild_id: int | None
    name: str
    key_prefix: str
    scopes: list[str]
    created_by: int | None
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


_COLS = "id, guild_id, name, key_prefix, scopes, created_by, last_used_at, revoked_at, created_at"


class ApiKeyService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, guild_id: int, name: str, scopes: list[str], created_by: int | None) -> tuple[str, ApiKey]:
        invalid = [s for s in scopes if s not in SCOPES]
        if invalid:
            raise ValueError(f"unknown scopes: {invalid}")
        await self.db.ensure_guild(guild_id)
        plain = KEY_PREFIX + secrets.token_urlsafe(32)
        row = await self.db.fetchrow(
            f"INSERT INTO api_keys (guild_id, name, key_prefix, key_hash, scopes, created_by) "
            f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {_COLS}",
            guild_id, name, plain[:10], hash_key(plain), scopes, created_by,
        )
        assert row is not None
        return plain, ApiKey(**dict(row))

    async def verify(self, plain: str) -> ApiKey | None:
        if not plain.startswith(KEY_PREFIX):
            return None
        row = await self.db.fetchrow(
            f"UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1 AND revoked_at IS NULL RETURNING {_COLS}",
            hash_key(plain),
        )
        return ApiKey(**dict(row)) if row else None

    async def list_keys(self, guild_id: int) -> list[ApiKey]:
        rows = await self.db.fetch(
            f"SELECT {_COLS} FROM api_keys WHERE guild_id = $1 ORDER BY created_at DESC", guild_id
        )
        return [ApiKey(**dict(r)) for r in rows]

    async def revoke(self, guild_id: int, key_id: int) -> bool:
        status = await self.db.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE guild_id = $1 AND id = $2 AND revoked_at IS NULL",
            guild_id, key_id,
        )
        return status.endswith(" 1")
