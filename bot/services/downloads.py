"""Download records, click tracking and statistics."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..core.db import Database
from ..core.errors import NotFound

_FIELDS = {
    "title", "description_de", "description_en", "download_url", "password", "youtube_url", "youtube_id",
    "preview_image_url", "category", "tags", "version", "file_size", "channel_id", "required_role_id", "message_id",
    "embed_message_id", "published_at",
}


@dataclass(slots=True)
class Download:
    id: int
    guild_id: int
    title: str
    description_de: str | None
    description_en: str | None
    download_url: str
    password: str | None
    youtube_url: str | None
    youtube_id: str | None
    preview_image_url: str | None
    category: str | None
    tags: list[str]
    version: str | None
    file_size: str | None
    channel_id: int | None
    required_role_id: int | None
    message_id: int | None
    embed_message_id: int | None
    click_count: int
    published_at: dt.datetime | None
    created_by: int | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Download:
        data = dict(row)
        data["tags"] = list(data["tags"] or [])
        return cls(**data)

    def description(self, locale: str) -> str | None:
        primary, secondary = (self.description_de, self.description_en) if locale == "de" else \
            (self.description_en, self.description_de)
        return primary or secondary

    @property
    def is_posted(self) -> bool:
        return self.message_id is not None


class DownloadService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, download_id: int, guild_id: int | None = None) -> Download:
        row = await self.db.fetchrow(
            "SELECT * FROM downloads WHERE id = $1 AND ($2::bigint IS NULL OR guild_id = $2)", download_id, guild_id
        )
        if row is None:
            raise NotFound("downloads.errors.not_found")
        return Download.from_row(row)

    async def list_downloads(self, guild_id: int, *, limit: int = 25, offset: int = 0) -> tuple[list[Download], int]:
        rows = await self.db.fetch(
            "SELECT * FROM downloads WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            guild_id, limit, offset,
        )
        total = await self.db.fetchval("SELECT count(*) FROM downloads WHERE guild_id = $1", guild_id)
        return [Download.from_row(r) for r in rows], int(total)

    async def search(self, guild_id: int, text: str, limit: int = 25) -> list[Download]:
        rows = await self.db.fetch(
            "SELECT * FROM downloads WHERE guild_id = $1 AND title ILIKE '%' || $2 || '%' ORDER BY created_at DESC "
            "LIMIT $3",
            guild_id, text, limit,
        )
        return [Download.from_row(r) for r in rows]

    async def create(self, guild_id: int, created_by: int, **values: Any) -> Download:
        await self.db.ensure_guild(guild_id)
        data = {k: v for k, v in values.items() if k in _FIELDS}
        columns = ["guild_id", "created_by", *data]
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        row = await self.db.fetchrow(
            f"INSERT INTO downloads ({', '.join(columns)}) VALUES ({placeholders}) RETURNING *",
            guild_id, created_by, *data.values(),
        )
        assert row is not None
        return Download.from_row(row)

    async def update(self, download_id: int, **values: Any) -> Download:
        data = {k: v for k, v in values.items() if k in _FIELDS}
        if data:
            sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(data, start=2))
            await self.db.execute(f"UPDATE downloads SET {sets} WHERE id = $1", download_id, *data.values())
        return await self.get(download_id)

    async def delete(self, download_id: int) -> None:
        await self.db.execute("DELETE FROM downloads WHERE id = $1", download_id)

    async def by_message(self, message_id: int) -> Download | None:
        row = await self.db.fetchrow("SELECT * FROM downloads WHERE message_id = $1", message_id)
        return Download.from_row(row) if row else None

    async def register_click(self, download: Download, user_id: int) -> int:
        async with self.db.transaction() as conn:
            await conn.execute("INSERT INTO download_clicks (download_id, guild_id, user_id) VALUES ($1, $2, $3)",
                               download.id, download.guild_id, user_id)
            return int(await conn.fetchval(
                "UPDATE downloads SET click_count = click_count + 1 WHERE id = $1 RETURNING click_count", download.id))

    async def stats(self, guild_id: int) -> dict[str, Any]:
        totals = await self.db.fetchrow(
            """
            SELECT count(*) AS downloads,
                   count(*) FILTER (WHERE message_id IS NOT NULL) AS posted,
                   COALESCE(sum(click_count), 0) AS clicks
              FROM downloads WHERE guild_id = $1
            """,
            guild_id,
        )
        periods = await self.db.fetchrow(
            """
            SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 day') AS day,
                   count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS week,
                   count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS month,
                   count(DISTINCT user_id) FILTER (WHERE created_at >= now() - interval '30 days') AS users_month
              FROM download_clicks WHERE guild_id = $1
            """,
            guild_id,
        )
        top = await self.db.fetch(
            "SELECT id, title, click_count FROM downloads WHERE guild_id = $1 ORDER BY click_count DESC, id LIMIT 10",
            guild_id,
        )
        daily = await self.db.fetch(
            """
            SELECT date_trunc('day', created_at)::date AS day, count(*) AS clicks
              FROM download_clicks WHERE guild_id = $1 AND created_at >= now() - interval '14 days'
             GROUP BY day ORDER BY day
            """,
            guild_id,
        )
        assert totals is not None and periods is not None
        return {
            "downloads": int(totals["downloads"]), "posted": int(totals["posted"]), "clicks": int(totals["clicks"]),
            "day": int(periods["day"]), "week": int(periods["week"]), "month": int(periods["month"]),
            "users_month": int(periods["users_month"]),
            "top": [dict(r) for r in top], "daily": [(r["day"], int(r["clicks"])) for r in daily],
        }
