"""Economy ledger: atomic balance changes with transaction log and database-backed cooldowns."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import asyncpg

from ..core.db import Database
from ..core.errors import CooldownActive, UserError
from ..core.utils import ts


@dataclass(slots=True)
class Account:
    user_id: int
    balance: int
    total_earned: int
    total_lost: int


class EconomyService:
    def __init__(self, db: Database, start_balance: int) -> None:
        self.db = db
        self.start_balance = start_balance

    async def _ensure(self, conn: asyncpg.Connection, guild_id: int, user_id: int) -> None:
        await conn.execute(
            "INSERT INTO economy_users (guild_id, user_id, balance) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            guild_id, user_id, self.start_balance,
        )

    async def account(self, guild_id: int, user_id: int) -> Account:
        await self.db.ensure_guild(guild_id)
        async with self.db.acquire() as conn:
            await self._ensure(conn, guild_id, user_id)
            row = await conn.fetchrow(
                "SELECT user_id, balance, total_earned, total_lost FROM economy_users WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
        assert row is not None
        return Account(**dict(row))

    async def change(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        kind: str,
        *,
        reference: str | None = None,
        actor_id: int | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Add (or subtract) ``amount``. Raises ``economy.errors.insufficient`` if the balance would go negative."""
        if conn is None:
            await self.db.ensure_guild(guild_id)
            async with self.db.transaction() as own:
                return await self.change(guild_id, user_id, amount, kind, reference=reference, actor_id=actor_id,
                                         conn=own)
        await self._ensure(conn, guild_id, user_id)
        balance = await conn.fetchval(
            """
            UPDATE economy_users
               SET balance = balance + $3,
                   total_earned = total_earned + GREATEST($3, 0),
                   total_lost = total_lost + GREATEST(-$3, 0)
             WHERE guild_id = $1 AND user_id = $2 AND balance + $3 >= 0
            RETURNING balance
            """,
            guild_id, user_id, amount,
        )
        if balance is None:
            raise UserError("economy.errors.insufficient")
        await conn.execute(
            "INSERT INTO economy_transactions (guild_id, user_id, amount, balance_after, kind, reference, actor_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, user_id, amount, balance, kind, reference, actor_id,
        )
        return int(balance)

    async def transfer(self, guild_id: int, sender: int, receiver: int, amount: int, kind: str = "transfer") -> None:
        if amount <= 0 or sender == receiver:
            raise UserError("errors.invalid_amount")
        await self.db.ensure_guild(guild_id)
        async with self.db.transaction() as conn:
            await self.change(guild_id, sender, -amount, kind, reference=str(receiver), conn=conn)
            await self.change(guild_id, receiver, amount, kind, reference=str(sender), conn=conn)

    async def set_balance(self, guild_id: int, user_id: int, balance: int, actor_id: int) -> int:
        account = await self.account(guild_id, user_id)
        return await self.change(guild_id, user_id, balance - account.balance, "admin_set", actor_id=actor_id)

    async def claim_cooldown(self, guild_id: int, user_id: int, action: str, seconds: int) -> None:
        """Atomically start a cooldown; raises :class:`CooldownActive` while it is running."""
        if seconds <= 0:
            return
        await self.db.ensure_guild(guild_id)
        claimed = await self.db.fetchval(
            """
            INSERT INTO economy_cooldowns (guild_id, user_id, action, available_at)
            VALUES ($1, $2, $3, now() + make_interval(secs => $4))
            ON CONFLICT (guild_id, user_id, action) DO UPDATE SET available_at = EXCLUDED.available_at
             WHERE economy_cooldowns.available_at <= now()
            RETURNING available_at
            """,
            guild_id, user_id, action, float(seconds),
        )
        if claimed is None:
            until: dt.datetime = await self.db.fetchval(
                "SELECT available_at FROM economy_cooldowns WHERE guild_id = $1 AND user_id = $2 AND action = $3",
                guild_id, user_id, action,
            )
            raise CooldownActive("errors.cooldown", until=ts(until, "R"))

    async def reset_cooldown(self, guild_id: int, user_id: int, action: str) -> None:
        await self.db.execute(
            "DELETE FROM economy_cooldowns WHERE guild_id = $1 AND user_id = $2 AND action = $3", guild_id, user_id, action
        )

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list[Account]:
        rows = await self.db.fetch(
            "SELECT user_id, balance, total_earned, total_lost FROM economy_users WHERE guild_id = $1 "
            "ORDER BY balance DESC, user_id LIMIT $2",
            guild_id, limit,
        )
        return [Account(**dict(r)) for r in rows]

    async def rank(self, guild_id: int, user_id: int) -> int:
        return int(await self.db.fetchval(
            "SELECT count(*) + 1 FROM economy_users WHERE guild_id = $1 AND balance > "
            "(SELECT balance FROM economy_users WHERE guild_id = $1 AND user_id = $2)",
            guild_id, user_id,
        ) or 1)

    async def reset(self, guild_id: int, user_id: int | None) -> None:
        if user_id is None:
            await self.db.execute("DELETE FROM economy_users WHERE guild_id = $1", guild_id)
            await self.db.execute("DELETE FROM economy_cooldowns WHERE guild_id = $1", guild_id)
        else:
            await self.db.execute("DELETE FROM economy_users WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)
            await self.db.execute("DELETE FROM economy_cooldowns WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)
