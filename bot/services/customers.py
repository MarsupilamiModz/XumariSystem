"""GDPR: export and deletion of all personal data of a customer.

Retained on deletion (Art. 17 (3) GDPR):

* invoices and the orders they belong to – statutory retention (§147 AO / §257 HGB: 10 years),
* ticket blacklist entries and audit-log entries about actions of the user – legitimate interest
  (abuse prevention, accountability); audit entries *about* the user are pseudonymised,
* open tickets – they are removed once closed, so the running conversation isn't destroyed.
"""

from __future__ import annotations

from typing import Any

from ..core.db import Database

#: (table, user column) – exported and deleted if the table exists (whitelisted identifiers only)
DELETE_TABLES: tuple[tuple[str, str], ...] = (
    ("reviews", "user_id"),
    ("economy_users", "user_id"),
    ("economy_transactions", "user_id"),
    ("levels", "user_id"),
    ("invites", "invitee_id"),
    ("invites", "inviter_id"),
    ("giveaway_entries", "user_id"),
    ("reminders", "user_id"),
    ("download_clicks", "user_id"),
    ("modmail_threads", "user_id"),
)
#: exported (Art. 15) but retained on deletion
RETAINED_TABLES: tuple[tuple[str, str], ...] = (("blacklist", "user_id"),)
EXPORT_TABLES: tuple[tuple[str, str], ...] = DELETE_TABLES + RETAINED_TABLES


def _affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


class CustomerDataService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _table_exists(self, table: str) -> bool:
        return bool(await self.db.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"))

    async def summary(self, guild_id: int, user_id: int) -> dict[str, Any]:
        orders = await self.db.fetch(
            "SELECT id, status, total_cents, currency, created_at, paid_at FROM orders "
            "WHERE guild_id = $1 AND customer_id = $2 AND status <> 'draft' ORDER BY id DESC",
            guild_id, user_id,
        )
        tickets = await self.db.fetchval(
            "SELECT count(*) FROM tickets WHERE guild_id = $1 AND owner_id = $2", guild_id, user_id
        )
        revenue = sum(r["total_cents"] for r in orders if r["paid_at"] and r["status"] not in ("cancelled", "refunded"))
        reviews: list[dict[str, Any]] = []
        if await self._table_exists("reviews"):
            reviews = [dict(r) for r in await self.db.fetch(
                "SELECT order_id, rating, created_at FROM reviews WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id)]
        return {
            "user_id": str(user_id),
            "orders": [dict(r) for r in orders],
            "order_count": len(orders),
            "lifetime_value_cents": revenue,
            "ticket_count": int(tickets),
            "reviews": reviews,
        }

    async def export(self, guild_id: int, user_id: int) -> dict[str, Any]:
        data: dict[str, Any] = {"user_id": str(user_id), "guild_id": str(guild_id)}
        data["orders"] = [dict(r) for r in await self.db.fetch(
            "SELECT * FROM orders WHERE guild_id = $1 AND customer_id = $2 ORDER BY id", guild_id, user_id)]
        data["order_items"] = [dict(r) for r in await self.db.fetch(
            "SELECT i.* FROM order_items i JOIN orders o ON o.id = i.order_id "
            "WHERE o.guild_id = $1 AND o.customer_id = $2 ORDER BY i.id", guild_id, user_id)]
        data["payments"] = [dict(r) for r in await self.db.fetch(
            "SELECT p.* FROM payments p JOIN orders o ON o.id = p.order_id "
            "WHERE o.guild_id = $1 AND o.customer_id = $2 ORDER BY p.id", guild_id, user_id)]
        data["invoices"] = [dict(r) for r in await self.db.fetch(
            "SELECT v.* FROM invoices v JOIN orders o ON o.id = v.order_id "
            "WHERE o.guild_id = $1 AND o.customer_id = $2 ORDER BY v.id", guild_id, user_id)]
        data["tickets"] = [dict(r) for r in await self.db.fetch(
            "SELECT code, status, locale, answers, created_at, closed_at, close_reason FROM tickets "
            "WHERE guild_id = $1 AND owner_id = $2 ORDER BY id", guild_id, user_id)]
        for table, column in EXPORT_TABLES:
            if await self._table_exists(table):
                rows = await self.db.fetch(
                    f"SELECT * FROM {table} WHERE guild_id = $1 AND {column} = $2", guild_id, user_id
                )
                data.setdefault(table, []).extend(dict(r) for r in rows)
        data["logs"] = [dict(r) for r in await self.db.fetch(
            "SELECT category, action, created_at, details FROM logs "
            "WHERE guild_id = $1 AND (actor_id = $2 OR target_id = $2) ORDER BY id", guild_id, user_id)]
        return data

    async def delete(self, guild_id: int, user_id: int) -> dict[str, int]:
        """Delete / anonymise personal data. Returns affected rows per area."""
        result: dict[str, int] = {}
        async with self.db.transaction() as conn:
            for table, column in DELETE_TABLES:
                if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"):
                    status = await conn.execute(
                        f"DELETE FROM {table} WHERE guild_id = $1 AND {column} = $2", guild_id, user_id
                    )
                    result[f"{table}.{column}"] = _affected(status)
            result["ticket_messages_stats"] = _affected(await conn.execute(
                "DELETE FROM ticket_messages_stats s USING tickets t "
                "WHERE s.ticket_id = t.id AND t.guild_id = $1 AND s.user_id = $2", guild_id, user_id))
            result["tickets"] = _affected(await conn.execute(
                "DELETE FROM tickets WHERE guild_id = $1 AND owner_id = $2 AND status <> 'open'", guild_id, user_id))
            result["tickets_open_retained"] = int(await conn.fetchval(
                "SELECT count(*) FROM tickets WHERE guild_id = $1 AND owner_id = $2 AND status = 'open'",
                guild_id, user_id))
            result["orders_anonymised"] = _affected(await conn.execute(
                """
                UPDATE orders SET customer_id = 0, notes = NULL
                 WHERE guild_id = $1 AND customer_id = $2
                   AND status IN ('draft', 'completed', 'cancelled', 'refunded')
                   AND NOT EXISTS (SELECT 1 FROM invoices v WHERE v.order_id = orders.id)
                """,
                guild_id, user_id))
            await conn.execute(
                """
                UPDATE payments p SET customer_email = NULL, customer_name = NULL, customer_address = NULL
                  FROM orders o
                 WHERE o.id = p.order_id AND o.guild_id = $1 AND o.customer_id = 0
                """,
                guild_id)
            await conn.execute("UPDATE orders SET notes = NULL WHERE guild_id = $1 AND customer_id = $2",
                               guild_id, user_id)
            result["orders_retained"] = int(await conn.fetchval(
                "SELECT count(*) FROM orders WHERE guild_id = $1 AND customer_id = $2", guild_id, user_id))
            result["logs_pseudonymised"] = _affected(await conn.execute(
                "UPDATE logs SET target_id = NULL, details = '{}'::jsonb "
                "WHERE guild_id = $1 AND target_id = $2 AND (actor_id IS NULL OR actor_id <> $2)",
                guild_id, user_id))
        return result
