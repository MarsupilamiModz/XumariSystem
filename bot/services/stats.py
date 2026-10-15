"""Aggregated statistics for designers, tickets, reviews, sales, customers and downloads."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..core.db import Database

_EXCLUDED = ["cancelled", "refunded"]


@dataclass(slots=True)
class Overview:
    tickets_created: int
    tickets_open: int
    tickets_closed: int
    avg_close_hours: float | None
    orders_paid: int
    revenue_cents: int
    reviews: int
    avg_rating: float | None
    customers: int
    repeat_customers: int
    download_clicks: int | None


@dataclass(slots=True)
class DesignerRow:
    designer_id: int
    completed: int
    active: int
    revenue_cents: int
    avg_rating: float | None
    avg_lead_hours: float | None


@dataclass(slots=True)
class CategoryRow:
    key: str
    name_de: str
    name_en: str
    created: int
    closed: int
    avg_close_hours: float | None


@dataclass(slots=True)
class ReviewStats:
    count: int
    average: float | None
    distribution: dict[int, int]
    latest: list[dict[str, Any]] = field(default_factory=list)


def _hours(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dt.timedelta):
        return value.total_seconds() / 3600
    return float(value) / 3600


class StatsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _exists(self, table: str) -> bool:
        return bool(await self.db.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"))

    async def overview(self, guild_id: int, since: dt.datetime | None) -> Overview:
        tickets = await self.db.fetchrow(
            """
            SELECT count(*) FILTER (WHERE $2::timestamptz IS NULL OR created_at >= $2) AS created,
                   count(*) FILTER (WHERE status = 'open') AS open,
                   count(*) FILTER (WHERE closed_at IS NOT NULL AND ($2::timestamptz IS NULL OR closed_at >= $2)) AS closed,
                   avg(EXTRACT(EPOCH FROM closed_at - created_at))
                       FILTER (WHERE closed_at IS NOT NULL AND ($2::timestamptz IS NULL OR closed_at >= $2)) AS avg_close
              FROM tickets WHERE guild_id = $1
            """,
            guild_id, since,
        )
        orders = await self.db.fetchrow(
            """
            SELECT count(*) AS paid, COALESCE(sum(total_cents), 0) AS revenue, count(DISTINCT customer_id) AS customers
              FROM orders
             WHERE guild_id = $1 AND paid_at IS NOT NULL AND status <> ALL($3::text[])
               AND ($2::timestamptz IS NULL OR paid_at >= $2)
            """,
            guild_id, since, _EXCLUDED,
        )
        repeat = await self.db.fetchval(
            """
            SELECT count(*) FROM (
                SELECT customer_id FROM orders
                 WHERE guild_id = $1 AND paid_at IS NOT NULL AND status <> ALL($2::text[]) AND customer_id <> 0
                 GROUP BY customer_id HAVING count(*) > 1) t
            """,
            guild_id, _EXCLUDED,
        )
        reviews = await self.db.fetchrow(
            "SELECT count(*) AS cnt, avg(rating) AS avg FROM reviews WHERE guild_id = $1 "
            "AND ($2::timestamptz IS NULL OR created_at >= $2)",
            guild_id, since,
        )
        clicks: int | None = None
        if await self._exists("download_clicks"):
            clicks = int(await self.db.fetchval(
                "SELECT count(*) FROM download_clicks WHERE guild_id = $1 AND ($2::timestamptz IS NULL OR created_at >= $2)",
                guild_id, since,
            ))
        assert tickets is not None and orders is not None and reviews is not None
        return Overview(
            tickets_created=int(tickets["created"]),
            tickets_open=int(tickets["open"]),
            tickets_closed=int(tickets["closed"]),
            avg_close_hours=_hours(tickets["avg_close"]),
            orders_paid=int(orders["paid"]),
            revenue_cents=int(orders["revenue"]),
            reviews=int(reviews["cnt"]),
            avg_rating=float(reviews["avg"]) if reviews["avg"] is not None else None,
            customers=int(orders["customers"]),
            repeat_customers=int(repeat or 0),
            download_clicks=clicks,
        )

    async def designers(self, guild_id: int, since: dt.datetime | None) -> list[DesignerRow]:
        rows = await self.db.fetch(
            """
            SELECT o.designer_id,
                   count(*) FILTER (WHERE o.status = 'completed') AS completed,
                   count(*) FILTER (WHERE o.status IN ('in_progress', 'pending_answer')) AS active,
                   COALESCE(sum(o.total_cents) FILTER (WHERE o.paid_at IS NOT NULL AND o.status <> ALL($3::text[])), 0) AS revenue,
                   avg(EXTRACT(EPOCH FROM o.completed_at - o.started_at)) FILTER (WHERE o.completed_at IS NOT NULL) AS lead,
                   (SELECT avg(r.rating) FROM reviews r WHERE r.guild_id = $1 AND r.designer_id = o.designer_id
                      AND ($2::timestamptz IS NULL OR r.created_at >= $2)) AS rating
              FROM orders o
             WHERE o.guild_id = $1 AND o.designer_id IS NOT NULL
               AND ($2::timestamptz IS NULL OR COALESCE(o.completed_at, o.started_at, o.created_at) >= $2)
             GROUP BY o.designer_id
             ORDER BY completed DESC, revenue DESC
             LIMIT 25
            """,
            guild_id, since, _EXCLUDED,
        )
        return [
            DesignerRow(r["designer_id"], int(r["completed"]), int(r["active"]), int(r["revenue"]),
                        float(r["rating"]) if r["rating"] is not None else None, _hours(r["lead"]))
            for r in rows
        ]

    async def ticket_categories(self, guild_id: int, since: dt.datetime | None) -> list[CategoryRow]:
        rows = await self.db.fetch(
            """
            SELECT c.key, c.name_de, c.name_en,
                   count(t.id) AS created,
                   count(t.id) FILTER (WHERE t.closed_at IS NOT NULL) AS closed,
                   avg(EXTRACT(EPOCH FROM t.closed_at - t.created_at)) FILTER (WHERE t.closed_at IS NOT NULL) AS avg_close
              FROM ticket_categories c
              LEFT JOIN tickets t ON t.category_id = c.id AND ($2::timestamptz IS NULL OR t.created_at >= $2)
             WHERE c.guild_id = $1
             GROUP BY c.id ORDER BY created DESC
            """,
            guild_id, since,
        )
        return [CategoryRow(r["key"], r["name_de"], r["name_en"], int(r["created"]), int(r["closed"]),
                            _hours(r["avg_close"])) for r in rows]

    async def top_supporters(self, guild_id: int, since: dt.datetime | None, limit: int = 10) -> list[tuple[int, int]]:
        rows = await self.db.fetch(
            """
            SELECT s.user_id, sum(s.message_count) AS messages
              FROM ticket_messages_stats s JOIN tickets t ON t.id = s.ticket_id
             WHERE t.guild_id = $1 AND s.is_staff AND ($2::timestamptz IS NULL OR s.last_message_at >= $2)
             GROUP BY s.user_id ORDER BY messages DESC LIMIT $3
            """,
            guild_id, since, limit,
        )
        return [(r["user_id"], int(r["messages"])) for r in rows]

    async def reviews(self, guild_id: int, since: dt.datetime | None) -> ReviewStats:
        rows = await self.db.fetch(
            "SELECT rating, count(*) AS cnt FROM reviews WHERE guild_id = $1 "
            "AND ($2::timestamptz IS NULL OR created_at >= $2) GROUP BY rating",
            guild_id, since,
        )
        distribution = {n: 0 for n in range(1, 6)}
        distribution.update({int(r["rating"]): int(r["cnt"]) for r in rows})
        count = sum(distribution.values())
        average = sum(k * v for k, v in distribution.items()) / count if count else None
        latest = await self.db.fetch(
            "SELECT user_id, order_id, rating, feedback, created_at FROM reviews WHERE guild_id = $1 "
            "AND feedback IS NOT NULL ORDER BY created_at DESC LIMIT 5",
            guild_id,
        )
        return ReviewStats(count, average, distribution, [dict(r) for r in latest])

    async def top_products(self, guild_id: int, since: dt.datetime | None, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT i.name, sum(i.quantity) AS qty, sum(i.quantity * i.unit_price_cents) AS revenue
              FROM order_items i JOIN orders o ON o.id = i.order_id
             WHERE o.guild_id = $1 AND o.paid_at IS NOT NULL AND o.status <> ALL($3::text[])
               AND ($2::timestamptz IS NULL OR o.paid_at >= $2)
             GROUP BY i.name ORDER BY revenue DESC LIMIT $4
            """,
            guild_id, since, _EXCLUDED, limit,
        )
        return [dict(r) for r in rows]

    async def revenue_by_day(self, guild_id: int, days: int) -> list[tuple[dt.date, int]]:
        rows = await self.db.fetch(
            """
            SELECT date_trunc('day', paid_at)::date AS day, sum(total_cents) AS revenue
              FROM orders
             WHERE guild_id = $1 AND paid_at >= now() - make_interval(days => $2) AND status <> ALL($3::text[])
             GROUP BY day ORDER BY day
            """,
            guild_id, days, _EXCLUDED,
        )
        return [(r["day"], int(r["revenue"])) for r in rows]

    async def customers(self, guild_id: int, since: dt.datetime | None, limit: int = 10) -> dict[str, Any]:
        new_customers = await self.db.fetchval(
            """
            SELECT count(*) FROM (
                SELECT customer_id, min(paid_at) AS first_paid FROM orders
                 WHERE guild_id = $1 AND paid_at IS NOT NULL AND customer_id <> 0
                 GROUP BY customer_id) t
             WHERE $2::timestamptz IS NULL OR first_paid >= $2
            """,
            guild_id, since,
        )
        top = await self.db.fetch(
            """
            SELECT customer_id, count(*) AS orders, sum(total_cents) AS ltv
              FROM orders
             WHERE guild_id = $1 AND paid_at IS NOT NULL AND status <> ALL($2::text[]) AND customer_id <> 0
             GROUP BY customer_id ORDER BY ltv DESC LIMIT $3
            """,
            guild_id, _EXCLUDED, limit,
        )
        return {"new": int(new_customers or 0), "top": [dict(r) for r in top]}
