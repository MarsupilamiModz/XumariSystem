"""Ticket data access (asyncpg, no ORM)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from ...core.utils import generate_code

if TYPE_CHECKING:
    from ...config.settings import TicketCategory
    from ...core.bot import XumariBot

_SELECT = """
SELECT t.*, c.key AS category_key
  FROM tickets t
  JOIN ticket_categories c ON c.id = t.category_id
"""


@dataclass(slots=True)
class Ticket:
    id: int
    code: str
    guild_id: int
    category_id: int
    category_key: str
    channel_id: int | None
    owner_id: int
    status: str
    locale: str
    answers: list[dict[str, str]]
    welcome_message_id: int | None
    claimed_by: int | None
    last_activity_at: dt.datetime
    reminder_sent_at: dt.datetime | None
    closed_at: dt.datetime | None
    closed_by: int | None
    close_reason: str | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Ticket:
        return cls(
            id=row["id"],
            code=row["code"].strip(),
            guild_id=row["guild_id"],
            category_id=row["category_id"],
            category_key=row["category_key"],
            channel_id=row["channel_id"],
            owner_id=row["owner_id"],
            status=row["status"],
            locale=row["locale"],
            answers=list(row["answers"] or []),
            welcome_message_id=row["welcome_message_id"],
            claimed_by=row["claimed_by"],
            last_activity_at=row["last_activity_at"],
            reminder_sent_at=row["reminder_sent_at"],
            closed_at=row["closed_at"],
            closed_by=row["closed_by"],
            close_reason=row["close_reason"],
            created_at=row["created_at"],
        )

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(slots=True)
class TicketStats:
    messages: int
    participants: int
    attachments: int


@dataclass(slots=True)
class BlacklistEntry:
    id: int
    user_id: int
    reason: str
    created_by: int | None
    expires_at: dt.datetime | None
    created_at: dt.datetime


class TicketService:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot
        self._category_ids: dict[tuple[int, str], int] = {}
        self._synced_guilds: set[int] = set()

    # ------------------------------------------------------------------ categories
    async def sync_categories(self, guild_id: int) -> None:
        """Mirror the configured categories into ``ticket_categories`` (FK target + statistics)."""
        await self.bot.db.ensure_guild(guild_id)
        categories: list[TicketCategory] = self.bot.config.tickets.categories
        async with self.bot.db.transaction() as conn:
            for index, cat in enumerate(categories):
                row_id = await conn.fetchval(
                    """
                    INSERT INTO ticket_categories (guild_id, key, name_de, name_en, emoji, discord_category_id,
                                                   is_order, active, sort_order)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8)
                    ON CONFLICT (guild_id, key) DO UPDATE
                       SET name_de = EXCLUDED.name_de, name_en = EXCLUDED.name_en, emoji = EXCLUDED.emoji,
                           discord_category_id = EXCLUDED.discord_category_id, is_order = EXCLUDED.is_order,
                           active = TRUE, sort_order = EXCLUDED.sort_order
                    RETURNING id
                    """,
                    guild_id, cat.key, cat.name_de, cat.name_en, cat.emoji, cat.discord_category_id,
                    cat.is_order, index,
                )
                self._category_ids[(guild_id, cat.key)] = row_id
            await conn.execute(
                "UPDATE ticket_categories SET active = FALSE WHERE guild_id = $1 AND NOT (key = ANY($2::text[]))",
                guild_id,
                [c.key for c in categories],
            )
        self._synced_guilds.add(guild_id)

    async def category_id(self, guild_id: int, key: str) -> int:
        if guild_id not in self._synced_guilds:
            await self.sync_categories(guild_id)
        try:
            return self._category_ids[(guild_id, key)]
        except KeyError:
            raise LookupError(f"unknown ticket category {key!r}") from None

    # ------------------------------------------------------------------ tickets
    async def create(
        self, *, guild_id: int, category_key: str, owner_id: int, locale: str, answers: list[dict[str, str]]
    ) -> Ticket:
        category_id = await self.category_id(guild_id, category_key)
        for _ in range(10):
            code = generate_code(8)
            try:
                row = await self.bot.db.fetchrow(
                    """
                    INSERT INTO tickets (code, guild_id, category_id, owner_id, locale, answers)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    code, guild_id, category_id, owner_id, locale, answers,
                )
            except asyncpg.UniqueViolationError:
                continue
            assert row is not None
            ticket = await self.by_id(row["id"])
            assert ticket is not None
            return ticket
        raise RuntimeError("could not generate a unique ticket code")

    async def _one(self, where: str, *args: Any) -> Ticket | None:
        row = await self.bot.db.fetchrow(f"{_SELECT} WHERE {where}", *args)
        return Ticket.from_row(row) if row else None

    async def by_id(self, ticket_id: int) -> Ticket | None:
        return await self._one("t.id = $1", ticket_id)

    async def by_code(self, code: str) -> Ticket | None:
        return await self._one("t.code = $1", code.upper())

    async def by_channel(self, channel_id: int) -> Ticket | None:
        return await self._one("t.channel_id = $1", channel_id)

    async def set_channel(self, ticket_id: int, channel_id: int) -> None:
        await self.bot.db.execute("UPDATE tickets SET channel_id = $2 WHERE id = $1", ticket_id, channel_id)

    async def set_welcome_message(self, ticket_id: int, message_id: int) -> None:
        await self.bot.db.execute("UPDATE tickets SET welcome_message_id = $2 WHERE id = $1", ticket_id, message_id)

    async def discard(self, ticket_id: int) -> None:
        """Remove a ticket whose channel could not be created."""
        await self.bot.db.execute("DELETE FROM tickets WHERE id = $1", ticket_id)

    async def open_count(self, guild_id: int, owner_id: int) -> int:
        return int(
            await self.bot.db.fetchval(
                "SELECT count(*) FROM tickets WHERE guild_id = $1 AND owner_id = $2 AND status = 'open'",
                guild_id, owner_id,
            )
        )

    async def mark_closed(self, ticket_id: int, closed_by: int | None, reason: str | None) -> None:
        await self.bot.db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = now(), closed_by = $2, close_reason = $3 WHERE id = $1",
            ticket_id, closed_by, reason,
        )

    async def mark_open(self, ticket_id: int) -> None:
        await self.bot.db.execute(
            """
            UPDATE tickets SET status = 'open', closed_at = NULL, closed_by = NULL, close_reason = NULL,
                               last_activity_at = now(), reminder_sent_at = NULL
             WHERE id = $1
            """,
            ticket_id,
        )

    async def mark_deleted(self, ticket_id: int) -> None:
        await self.bot.db.execute(
            "UPDATE tickets SET status = 'deleted', deleted_at = now(), closed_at = COALESCE(closed_at, now()) "
            "WHERE id = $1",
            ticket_id,
        )

    async def set_category(self, ticket_id: int, guild_id: int, category_key: str) -> None:
        category_id = await self.category_id(guild_id, category_key)
        await self.bot.db.execute("UPDATE tickets SET category_id = $2 WHERE id = $1", ticket_id, category_id)

    async def touch(self, ticket_id: int) -> None:
        await self.bot.db.execute(
            "UPDATE tickets SET last_activity_at = now(), reminder_sent_at = NULL WHERE id = $1", ticket_id
        )

    async def mark_reminded(self, ticket_id: int) -> None:
        await self.bot.db.execute("UPDATE tickets SET reminder_sent_at = now() WHERE id = $1", ticket_id)

    async def active_channels(self) -> dict[int, int]:
        rows = await self.bot.db.fetch(
            "SELECT channel_id, id FROM tickets WHERE status IN ('open', 'closed') AND channel_id IS NOT NULL"
        )
        return {r["channel_id"]: r["id"] for r in rows}

    async def due_for_reminder(self, reminder_at: dt.datetime) -> list[Ticket]:
        rows = await self.bot.db.fetch(
            f"{_SELECT} WHERE t.status = 'open' AND t.reminder_sent_at IS NULL AND t.last_activity_at < $1",
            reminder_at,
        )
        return [Ticket.from_row(r) for r in rows]

    async def due_for_close(self, close_before: dt.datetime) -> list[Ticket]:
        rows = await self.bot.db.fetch(
            f"{_SELECT} WHERE t.status = 'open' AND t.last_activity_at < $1", close_before
        )
        return [Ticket.from_row(r) for r in rows]

    async def due_for_delete(self, closed_before: dt.datetime) -> list[Ticket]:
        rows = await self.bot.db.fetch(f"{_SELECT} WHERE t.status = 'closed' AND t.closed_at < $1", closed_before)
        return [Ticket.from_row(r) for r in rows]

    # ------------------------------------------------------------------ members & stats
    async def add_member(self, ticket_id: int, user_id: int, added_by: int) -> None:
        await self.bot.db.execute(
            "INSERT INTO ticket_members (ticket_id, user_id, added_by) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            ticket_id, user_id, added_by,
        )

    async def remove_member(self, ticket_id: int, user_id: int) -> None:
        await self.bot.db.execute("DELETE FROM ticket_members WHERE ticket_id = $1 AND user_id = $2", ticket_id, user_id)

    async def member_ids(self, ticket_id: int) -> list[int]:
        rows = await self.bot.db.fetch("SELECT user_id FROM ticket_members WHERE ticket_id = $1", ticket_id)
        return [r["user_id"] for r in rows]

    async def record_message(self, ticket_id: int, user_id: int, *, is_staff: bool, attachments: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO ticket_messages_stats (ticket_id, user_id, is_staff, message_count, attachment_count)
            VALUES ($1, $2, $3, 1, $4)
            ON CONFLICT (ticket_id, user_id) DO UPDATE
               SET message_count = ticket_messages_stats.message_count + 1,
                   attachment_count = ticket_messages_stats.attachment_count + EXCLUDED.attachment_count,
                   is_staff = ticket_messages_stats.is_staff OR EXCLUDED.is_staff,
                   last_message_at = now()
            """,
            ticket_id, user_id, is_staff, attachments,
        )

    async def stats(self, ticket_id: int) -> TicketStats:
        row = await self.bot.db.fetchrow(
            """
            SELECT COALESCE(sum(message_count), 0) AS messages,
                   count(*) AS participants,
                   COALESCE(sum(attachment_count), 0) AS attachments
              FROM ticket_messages_stats WHERE ticket_id = $1
            """,
            ticket_id,
        )
        assert row is not None
        return TicketStats(int(row["messages"]), int(row["participants"]), int(row["attachments"]))

    async def save_transcript(
        self, ticket_id: int, *, storage: str, url: str | None, file_name: str, size: int, messages: int
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO transcripts (ticket_id, storage, url, file_name, size_bytes, message_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            ticket_id, storage, url, file_name, size, messages,
        )

    async def has_transcript(self, ticket_id: int) -> bool:
        return bool(await self.bot.db.fetchval("SELECT 1 FROM transcripts WHERE ticket_id = $1 LIMIT 1", ticket_id))

    # ------------------------------------------------------------------ blacklist
    async def blacklist_entry(self, guild_id: int, user_id: int) -> BlacklistEntry | None:
        row = await self.bot.db.fetchrow(
            """
            SELECT id, user_id, reason, created_by, expires_at, created_at FROM blacklist
             WHERE guild_id = $1 AND user_id = $2 AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > now())
            """,
            guild_id, user_id,
        )
        return BlacklistEntry(**dict(row)) if row else None

    async def blacklist_add(
        self, guild_id: int, user_id: int, reason: str, created_by: int, expires_at: dt.datetime | None
    ) -> None:
        await self.bot.db.ensure_guild(guild_id)
        async with self.bot.db.transaction() as conn:
            # expired entries still occupy the unique slot → revoke them first
            await conn.execute(
                "UPDATE blacklist SET revoked_at = now() WHERE guild_id = $1 AND user_id = $2 AND revoked_at IS NULL",
                guild_id, user_id,
            )
            await conn.execute(
                "INSERT INTO blacklist (guild_id, user_id, reason, created_by, expires_at) VALUES ($1, $2, $3, $4, $5)",
                guild_id, user_id, reason, created_by, expires_at,
            )

    async def blacklist_remove(self, guild_id: int, user_id: int, revoked_by: int) -> bool:
        result = await self.bot.db.execute(
            "UPDATE blacklist SET revoked_at = now(), revoked_by = $3 "
            "WHERE guild_id = $1 AND user_id = $2 AND revoked_at IS NULL",
            guild_id, user_id, revoked_by,
        )
        return result.endswith(" 1")

    async def blacklist_list(self, guild_id: int, *, limit: int = 25, offset: int = 0) -> tuple[list[BlacklistEntry], int]:
        rows = await self.bot.db.fetch(
            """
            SELECT id, user_id, reason, created_by, expires_at, created_at FROM blacklist
             WHERE guild_id = $1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
             ORDER BY created_at DESC LIMIT $2 OFFSET $3
            """,
            guild_id, limit, offset,
        )
        total = await self.bot.db.fetchval(
            "SELECT count(*) FROM blacklist WHERE guild_id = $1 AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())",
            guild_id,
        )
        return [BlacklistEntry(**dict(r)) for r in rows], int(total)
