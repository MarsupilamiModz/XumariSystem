"""Database event bridge (transactional outbox).

The FastAPI service (embedded or separate container) cannot talk to Discord directly when it runs
in its own process. Instead it inserts rows into ``bot_events`` and sends ``pg_notify``. The bot
listens on the channel, claims pending events with ``FOR UPDATE SKIP LOCKED`` and dispatches them to
the handlers registered by cogs. A periodic poll guarantees delivery even if a notification is lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from .bot import XumariBot

_log = logging.getLogger("xumari.events")

CHANNEL = "xumari_events"
MAX_ATTEMPTS = 5
POLL_SECONDS = 30

EventHandler = Callable[[int | None, dict[str, Any]], Awaitable[None]]


async def emit_event(conn: asyncpg.Connection | asyncpg.Pool, guild_id: int | None, event_type: str,
                     payload: dict[str, Any]) -> int:
    """Insert an event and notify listeners. Usable from the bot *and* the API."""
    event_id: int = await conn.fetchval(
        "INSERT INTO bot_events (guild_id, event_type, payload) VALUES ($1, $2, $3) RETURNING id",
        guild_id,
        event_type,
        payload,
    )
    await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, str(event_id))
    return event_id


class EventBridge:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot
        self._handlers: dict[str, list[tuple[str, EventHandler]]] = {}
        self._listen_conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def register(self, event_type: str, handler: EventHandler, *, owner: str) -> None:
        self._handlers.setdefault(event_type, []).append((owner, handler))

    def unregister_owner(self, owner: str) -> None:
        for key in list(self._handlers):
            self._handlers[key] = [(o, h) for o, h in self._handlers[key] if o != owner]

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="xumari-event-bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_listener()

    async def _close_listener(self) -> None:
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
            self._listen_conn = None

    def _on_notify(self, *_: Any) -> None:
        self._wake.set()

    async def _ensure_listener(self) -> None:
        if self._listen_conn is not None and not self._listen_conn.is_closed():
            return
        try:
            self._listen_conn = await asyncpg.connect(self.bot.db.dsn)
            await self._listen_conn.add_listener(CHANNEL, self._on_notify)
        except (OSError, asyncpg.PostgresError):
            _log.warning("Event listener connection failed – falling back to polling", exc_info=True)
            self._listen_conn = None

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await self._ensure_listener()
            try:
                await self.process_pending()
            except Exception:  # noqa: BLE001
                _log.exception("Event processing failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)

    async def process_pending(self) -> int:
        rows = await self.bot.db.fetch(
            """
            UPDATE bot_events SET attempts = attempts + 1, claimed_at = now()
             WHERE id IN (
                SELECT id FROM bot_events
                 WHERE processed_at IS NULL AND attempts < $1
                   AND (claimed_at IS NULL OR claimed_at < now() - interval '5 minutes')
                 ORDER BY id
                 LIMIT 25
                 FOR UPDATE SKIP LOCKED)
            RETURNING id, guild_id, event_type, payload
            """,
            MAX_ATTEMPTS,
        )
        for row in rows:
            handlers = self._handlers.get(row["event_type"], [])
            try:
                for _, handler in handlers:
                    await handler(row["guild_id"], dict(row["payload"] or {}))
            except Exception as exc:  # noqa: BLE001
                _log.exception("Handler for event %s (%s) failed", row["id"], row["event_type"])
                await self.bot.db.execute(
                    "UPDATE bot_events SET last_error = $2, claimed_at = NULL WHERE id = $1",
                    row["id"],
                    repr(exc)[:2000],
                )
                continue
            await self.bot.db.execute("UPDATE bot_events SET processed_at = now() WHERE id = $1", row["id"])
        return len(rows)
