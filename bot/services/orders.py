"""Orders: cart, totals, status workflow, queue, todos and statistics (shared by bot and API)."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from ..config.settings import AppConfig
from ..core.db import Database
from ..core.errors import NotFound, UserError
from .shop import Bundle, Coupon, Product, ShopService

_log = logging.getLogger("xumari.orders")

STATUSES: tuple[str, ...] = (
    "draft", "awaiting_payment", "paid", "queued", "in_progress", "pending_answer", "completed", "cancelled", "refunded",
)
QUEUE_STATUSES: tuple[str, ...] = ("paid", "queued")
ACTIVE_STATUSES: tuple[str, ...] = ("paid", "queued", "in_progress", "pending_answer")
REVENUE_EXCLUDED: tuple[str, ...] = ("cancelled", "refunded")


@dataclass(slots=True)
class OrderItem:
    id: int
    product_id: int | None
    bundle_id: int | None
    name: str
    quantity: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(slots=True)
class Order:
    id: int
    guild_id: int
    ticket_id: int | None
    customer_id: int
    status: str
    source: str
    locale: str
    currency: str
    delivery: str
    seasonal_key: str | None
    coupon_id: int | None
    coupon_code: str | None
    subtotal_cents: int
    express_cents: int
    seasonal_cents: int
    discount_cents: int
    total_cents: int
    designer_id: int | None
    notes: str | None
    due_at: dt.datetime | None
    wizard_channel_id: int | None
    wizard_message_id: int | None
    todo_message_id: int | None
    submitted_at: dt.datetime | None
    paid_at: dt.datetime | None
    started_at: dt.datetime | None
    completed_at: dt.datetime | None
    cancelled_at: dt.datetime | None
    created_at: dt.datetime
    items: list[OrderItem] = field(default_factory=list)

    @property
    def number(self) -> int:
        return self.id

    @property
    def item_count(self) -> int:
        return sum(i.quantity for i in self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "number": self.number, "guild_id": str(self.guild_id),
            "ticket_id": self.ticket_id, "customer_id": str(self.customer_id), "status": self.status,
            "source": self.source, "locale": self.locale, "currency": self.currency, "delivery": self.delivery,
            "seasonal_key": self.seasonal_key, "coupon_code": self.coupon_code,
            "subtotal_cents": self.subtotal_cents, "express_cents": self.express_cents,
            "seasonal_cents": self.seasonal_cents, "discount_cents": self.discount_cents,
            "total_cents": self.total_cents, "designer_id": str(self.designer_id) if self.designer_id else None,
            "notes": self.notes, "due_at": self.due_at, "submitted_at": self.submitted_at, "paid_at": self.paid_at,
            "started_at": self.started_at, "completed_at": self.completed_at, "cancelled_at": self.cancelled_at,
            "created_at": self.created_at,
            "items": [
                {"id": i.id, "product_id": i.product_id, "bundle_id": i.bundle_id, "name": i.name,
                 "quantity": i.quantity, "unit_price_cents": i.unit_price_cents, "total_cents": i.total_cents}
                for i in self.items
            ],
        }


@dataclass(slots=True)
class Todo:
    id: int
    position: int
    text: str
    done: bool
    done_by: int | None
    done_at: dt.datetime | None


@dataclass(slots=True)
class DesignerStats:
    designer_id: int
    orders: int
    revenue_cents: int


@dataclass(slots=True)
class OrderStats:
    count: int
    revenue_cents: int
    average_cents: int
    by_status: dict[str, int]
    designers: list[DesignerStats]
    express_share: float


_ORDER_SELECT = """
SELECT o.*, c.code AS coupon_code
  FROM orders o
  LEFT JOIN coupons c ON c.id = o.coupon_id
"""


class OrderService:
    def __init__(self, db: Database, config: AppConfig) -> None:
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ loading
    def _executor(self, conn: asyncpg.Connection | None) -> Any:
        return conn if conn is not None else self.db

    async def _items(self, order_ids: list[int], conn: asyncpg.Connection | None = None) -> dict[int, list[OrderItem]]:
        if not order_ids:
            return {}
        rows = await self._executor(conn).fetch(
            "SELECT id, order_id, product_id, bundle_id, name, quantity, unit_price_cents FROM order_items "
            "WHERE order_id = ANY($1::bigint[]) ORDER BY id",
            order_ids,
        )
        out: dict[int, list[OrderItem]] = {}
        for r in rows:
            out.setdefault(r["order_id"], []).append(
                OrderItem(r["id"], r["product_id"], r["bundle_id"], r["name"], r["quantity"], r["unit_price_cents"])
            )
        return out

    @staticmethod
    def _from_row(row: asyncpg.Record) -> Order:
        data = dict(row)
        data.pop("updated_at", None)
        return Order(**data)

    async def _many(
        self, where: str, *args: Any, order_by: str = "o.id", limit: int | None = None, offset: int = 0,
        conn: asyncpg.Connection | None = None,
    ) -> list[Order]:
        paging = f" LIMIT {int(limit)} OFFSET {int(offset)}" if limit is not None else ""
        rows = await self._executor(conn).fetch(f"{_ORDER_SELECT} WHERE {where} ORDER BY {order_by}{paging}", *args)
        orders = [self._from_row(r) for r in rows]
        items = await self._items([o.id for o in orders], conn)
        for order in orders:
            order.items = items.get(order.id, [])
        return orders

    async def get(self, order_id: int, guild_id: int | None = None, *, conn: asyncpg.Connection | None = None) -> Order:
        orders = await self._many("o.id = $1 AND ($2::bigint IS NULL OR o.guild_id = $2)", order_id, guild_id,
                                  conn=conn)
        if not orders:
            raise NotFound("orders.errors.not_found")
        return orders[0]

    async def draft_for_ticket(self, ticket_id: int) -> Order | None:
        orders = await self._many("o.ticket_id = $1 AND o.status = 'draft'", ticket_id, order_by="o.id DESC")
        return orders[0] if orders else None

    async def latest_for_ticket(self, ticket_id: int, *, include_draft: bool = False) -> Order | None:
        orders = await self._many(
            "o.ticket_id = $1 AND ($2 OR o.status <> 'draft') AND o.status NOT IN ('cancelled', 'refunded')",
            ticket_id, include_draft, order_by="o.id DESC",
        )
        return orders[0] if orders else None

    async def for_customer(self, guild_id: int | None, customer_id: int) -> list[Order]:
        return await self._many(
            "o.customer_id = $1 AND ($2::bigint IS NULL OR o.guild_id = $2) AND o.status <> 'draft'",
            customer_id, guild_id, order_by="o.id DESC",
        )

    async def list_orders(
        self, guild_id: int | None, *, status: str | None = None, customer_id: int | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[Order]:
        return await self._many(
            "($1::bigint IS NULL OR o.guild_id = $1) AND ($2::text IS NULL OR o.status = $2) "
            "AND ($3::bigint IS NULL OR o.customer_id = $3) AND o.status <> 'draft'",
            guild_id, status, customer_id, order_by="o.id DESC", limit=min(limit, 200), offset=max(offset, 0),
        )

    # ------------------------------------------------------------------ cart
    async def create_draft(
        self, *, guild_id: int, customer_id: int, ticket_id: int | None, locale: str, source: str = "discord"
    ) -> Order:
        await self.db.ensure_guild(guild_id)
        order_id = await self.db.fetchval(
            "INSERT INTO orders (guild_id, ticket_id, customer_id, locale, currency, source) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            guild_id, ticket_id, customer_id, locale, self.config.shop.currency, source,
        )
        return await self.get(order_id)

    # Every cart mutation only applies while the order is still a draft. The order row is share-locked, so a
    # concurrent submit (which holds FOR UPDATE) serialises with it and the item can't slip past the total.
    _DRAFT_GUARD = "SELECT 1 FROM orders WHERE id = $1 AND status = 'draft' FOR SHARE"

    async def _mutate_cart(self, order: Order, query: str, *args: Any) -> None:
        async with self.db.transaction() as conn:
            if not await conn.fetchval(self._DRAFT_GUARD, order.id):
                raise UserError("orders.errors.locked")
            await conn.execute(query, order.id, *args)

    async def add_product(self, order: Order, product: Product, quantity: int = 1) -> None:
        await self._mutate_cart(
            order,
            """
            INSERT INTO order_items (order_id, product_id, name, quantity, unit_price_cents)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id, product_id) WHERE product_id IS NOT NULL
            DO UPDATE SET quantity = order_items.quantity + EXCLUDED.quantity
            """,
            product.id, product.name(order.locale), quantity, product.price_cents,
        )

    async def add_bundle(self, order: Order, bundle: Bundle, quantity: int = 1) -> None:
        await self._mutate_cart(
            order,
            """
            INSERT INTO order_items (order_id, bundle_id, name, quantity, unit_price_cents)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id, bundle_id) WHERE bundle_id IS NOT NULL
            DO UPDATE SET quantity = order_items.quantity + EXCLUDED.quantity
            """,
            bundle.id, bundle.name(order.locale), quantity, bundle.price_cents,
        )

    async def add_custom_item(self, order: Order, name: str, unit_price_cents: int, quantity: int = 1) -> None:
        await self._mutate_cart(
            order,
            "INSERT INTO order_items (order_id, name, quantity, unit_price_cents) VALUES ($1, $2, $3, $4)",
            name, quantity, unit_price_cents,
        )

    async def change_quantity(self, order: Order, item_id: int, delta: int) -> None:
        async with self.db.transaction() as conn:
            if not await conn.fetchval(self._DRAFT_GUARD, order.id):
                raise UserError("orders.errors.locked")
            quantity = await conn.fetchval(
                "SELECT quantity FROM order_items WHERE order_id = $1 AND id = $2 FOR UPDATE", order.id, item_id
            )
            if quantity is None:
                return
            if quantity + delta <= 0:
                await conn.execute("DELETE FROM order_items WHERE order_id = $1 AND id = $2", order.id, item_id)
            else:
                await conn.execute(
                    "UPDATE order_items SET quantity = $3 WHERE order_id = $1 AND id = $2",
                    order.id, item_id, quantity + delta,
                )

    async def remove_item(self, order: Order, item_id: int) -> None:
        await self._mutate_cart(order, "DELETE FROM order_items WHERE order_id = $1 AND id = $2", item_id)

    async def clear(self, order: Order) -> None:
        await self._mutate_cart(order, "DELETE FROM order_items WHERE order_id = $1")

    async def delete_draft(self, order_id: int) -> None:
        """Remove an unfinished draft (e.g. when API validation fails after creation)."""
        await self.db.execute("DELETE FROM orders WHERE id = $1 AND status = 'draft'", order_id)

    async def set_delivery(self, order: Order, delivery: str) -> None:
        if delivery not in {"standard", "express"}:
            raise UserError("errors.invalid_input")
        if delivery == "express" and not self.config.shop.express.enabled:
            raise UserError("orders.errors.express_disabled")
        await self._mutate_cart(order, "UPDATE orders SET delivery = $2 WHERE id = $1", delivery)

    async def set_seasonal(self, order: Order, key: str | None) -> None:
        if key is not None and not any(s.key == key for s in self.config.shop.seasonal_designs):
            raise UserError("errors.invalid_input")
        await self._mutate_cart(order, "UPDATE orders SET seasonal_key = $2 WHERE id = $1", key)

    async def set_coupon(self, order: Order, coupon_id: int | None) -> None:
        await self._mutate_cart(order, "UPDATE orders SET coupon_id = $2 WHERE id = $1", coupon_id)

    async def set_notes(self, order: Order, notes: str | None) -> None:
        await self.db.execute("UPDATE orders SET notes = $2 WHERE id = $1", order.id, notes)

    async def set_wizard_message(self, order: Order, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE orders SET wizard_channel_id = $2, wizard_message_id = $3 WHERE id = $1",
            order.id, channel_id, message_id,
        )

    async def set_todo_message(self, order: Order, message_id: int | None) -> None:
        await self.db.execute("UPDATE orders SET todo_message_id = $2 WHERE id = $1", order.id, message_id)

    async def set_designer(self, order: Order, designer_id: int | None) -> None:
        await self.db.execute("UPDATE orders SET designer_id = $2 WHERE id = $1", order.id, designer_id)

    # ------------------------------------------------------------------ totals
    def seasonal_price(self, key: str | None) -> int:
        if key is None:
            return 0
        design = next((s for s in self.config.shop.seasonal_designs if s.key == key), None)
        return design.price_cents if design else 0

    def express_surcharge(self, subtotal_cents: int) -> int:
        cfg = self.config.shop.express
        return round(subtotal_cents * cfg.surcharge_percent / 100) + cfg.surcharge_fixed_cents

    async def recalculate(self, order_id: int, shop: ShopService, *, money: Callable[[int], str] = str) -> Order:
        order = await self.get(order_id)
        subtotal = sum(i.total_cents for i in order.items)
        express = self.express_surcharge(subtotal) if order.delivery == "express" and subtotal > 0 else 0
        seasonal = self.seasonal_price(order.seasonal_key)
        discount = 0
        coupon_id = order.coupon_id
        if coupon_id is not None:
            try:
                coupon = await shop.coupon(order.guild_id, coupon_id)
                discount = shop.check_coupon(
                    coupon, subtotal_cents=subtotal, eligible_cents=self._eligible(order, coupon), money=money
                )
            except UserError:
                coupon_id = None
        total = max(0, subtotal + express + seasonal - discount)
        await self.db.execute(
            """
            UPDATE orders SET subtotal_cents = $2, express_cents = $3, seasonal_cents = $4, discount_cents = $5,
                              total_cents = $6, coupon_id = $7
             WHERE id = $1
            """,
            order.id, subtotal, express, seasonal, discount, total, coupon_id,
        )
        return await self.get(order.id)

    @staticmethod
    def _eligible(order: Order, coupon: Coupon) -> int:
        if not coupon.product_ids:
            return sum(i.total_cents for i in order.items)
        ids = set(coupon.product_ids)
        return sum(i.total_cents for i in order.items if i.product_id in ids)

    async def apply_coupon(
        self, order: Order, code: str, shop: ShopService, *, money: Callable[[int], str] = str
    ) -> Coupon:
        coupon = await shop.coupon_by_code(order.guild_id, code)
        subtotal = sum(i.total_cents for i in order.items)
        shop.check_coupon(coupon, subtotal_cents=subtotal, eligible_cents=self._eligible(order, coupon), money=money)
        await self.set_coupon(order, coupon.id)
        return coupon

    def delivery_days(self, order: Order, products: dict[int, Product] | None = None) -> int:
        if order.delivery == "express":
            return self.config.shop.express.delivery_days
        days = self.config.shop.standard_delivery_days
        if products:
            for item in order.items:
                product = products.get(item.product_id or -1)
                if product and product.delivery_days:
                    days = max(days, product.delivery_days)
        return days

    # ------------------------------------------------------------------ status workflow
    async def set_status(
        self,
        order: Order,
        status: str,
        *,
        changed_by: int | None,
        note: str | None = None,
        designer_id: int | None = None,
        delivery_days: int | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> Order:
        """Change the status (with history). Pass ``conn`` to run inside an outer transaction."""
        if status not in STATUSES:
            raise UserError("errors.invalid_input")
        if conn is not None:
            async with conn.transaction():
                await self._set_status(conn, order, status, changed_by, note, designer_id, delivery_days)
            return await self.get(order.id, conn=conn)
        async with self.db.transaction() as own:
            await self._set_status(own, order, status, changed_by, note, designer_id, delivery_days)
        return await self.get(order.id)

    async def _set_status(
        self, conn: asyncpg.Connection, order: Order, status: str, changed_by: int | None, note: str | None,
        designer_id: int | None, delivery_days: int | None,
    ) -> None:
        current = await conn.fetchrow(
            "SELECT status, paid_at, coupon_id, subtotal_cents, delivery, express_cents, seasonal_key, seasonal_cents, "
            "discount_cents FROM orders WHERE id = $1 FOR UPDATE",
            order.id,
        )
        if current is None:
            raise NotFound("orders.errors.not_found")
        old_status = current["status"]
        # optimistic check under the row lock: the caller decided based on ``order.status`` – if someone else
        # (customer, staff, Stripe webhook, API) changed it meanwhile, the transition must be re-evaluated
        if old_status != order.status:
            raise UserError("orders.errors.status_changed")
        if old_status == "draft" and status != "cancelled":
            item_total = await conn.fetchval(
                "SELECT COALESCE(sum(quantity * unit_price_cents), 0) FROM order_items WHERE order_id = $1", order.id
            )
            subtotal = int(item_total)
            expected_express = self.express_surcharge(subtotal) if current["delivery"] == "express" and subtotal else 0
            if (
                subtotal != current["subtotal_cents"]
                or current["express_cents"] != expected_express
                or current["seasonal_cents"] != self.seasonal_price(current["seasonal_key"])
                or (current["coupon_id"] is None and current["discount_cents"] > 0)
            ):
                # something changed after the last recalculation → the stored total is not trustworthy
                raise UserError("orders.errors.cart_changed")
        sets = ["status = $2"]
        args: list[Any] = [order.id, status]

        def add(expr: str, value: Any) -> None:
            args.append(value)
            sets.append(expr.format(n=len(args)))

        if status == "awaiting_payment":
            sets.append("submitted_at = COALESCE(submitted_at, now())")
        if status in ACTIVE_STATUSES + ("completed",) and current["paid_at"] is None:
            sets.append("paid_at = now()")
            if delivery_days is not None:
                add("due_at = now() + make_interval(days => ${n})", delivery_days)
            if current["coupon_id"] is not None:
                counted = await conn.fetchval(
                    "UPDATE coupons SET uses = uses + 1 WHERE id = $1 AND (max_uses IS NULL OR uses < max_uses) "
                    "RETURNING id",
                    current["coupon_id"],
                )
                if counted is None:
                    # payment already happened – keep the order, but make the overuse visible
                    _log.warning("Coupon %s exceeded max_uses with order %s", current["coupon_id"], order.id)
                    note = f"{note + ' · ' if note else ''}coupon limit exceeded"
        if status == "in_progress":
            sets.append("started_at = COALESCE(started_at, now())")
        if status == "completed":
            sets.append("completed_at = now()")
        if status in {"cancelled", "refunded"}:
            sets.append("cancelled_at = now()")
        if designer_id is not None:
            add("designer_id = ${n}", designer_id)
        await conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = $1", *args)
        await conn.execute(
            "INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, note) "
            "VALUES ($1, $2, $3, $4, $5)",
            order.id, old_status, status, changed_by, note,
        )

    async def history(self, order_id: int) -> list[asyncpg.Record]:
        return await self.db.fetch(
            "SELECT old_status, new_status, changed_by, note, created_at FROM order_status_history "
            "WHERE order_id = $1 ORDER BY created_at",
            order_id,
        )

    # ------------------------------------------------------------------ queue
    async def queue(self, guild_id: int) -> list[Order]:
        return await self._many(
            "o.guild_id = $1 AND o.status = ANY($2::text[])",
            guild_id, list(QUEUE_STATUSES),
            order_by="(o.delivery = 'express') DESC, o.paid_at NULLS LAST, o.id",
        )

    async def in_progress(self, guild_id: int) -> list[Order]:
        return await self._many("o.guild_id = $1 AND o.status IN ('in_progress', 'pending_answer')", guild_id,
                                order_by="o.started_at NULLS LAST, o.id")

    async def queue_position(self, order: Order) -> int | None:
        if order.status not in QUEUE_STATUSES:
            return None
        queue = await self.queue(order.guild_id)
        return next((index + 1 for index, o in enumerate(queue) if o.id == order.id), None)

    # ------------------------------------------------------------------ todos
    async def todos(self, order_id: int) -> list[Todo]:
        rows = await self.db.fetch(
            "SELECT id, position, text, done, done_by, done_at FROM order_todos WHERE order_id = $1 "
            "ORDER BY position, id",
            order_id,
        )
        return [Todo(**dict(r)) for r in rows]

    async def add_todo(self, order_id: int, text: str) -> None:
        await self.db.execute(
            "INSERT INTO order_todos (order_id, position, text) "
            "VALUES ($1, COALESCE((SELECT max(position) + 1 FROM order_todos WHERE order_id = $1), 0), $2)",
            order_id, text,
        )

    async def ensure_default_todos(self, order: Order) -> None:
        if await self.db.fetchval("SELECT 1 FROM order_todos WHERE order_id = $1 LIMIT 1", order.id):
            return
        texts = self.config.shop.default_todos_de if order.locale == "de" else self.config.shop.default_todos_en
        for text in texts:
            await self.add_todo(order.id, text)

    async def toggle_todo(self, order_id: int, todo_id: int, user_id: int) -> Todo:
        row = await self.db.fetchrow(
            """
            UPDATE order_todos
               SET done = NOT done,
                   done_by = CASE WHEN done THEN NULL ELSE $3::bigint END,
                   done_at = CASE WHEN done THEN NULL ELSE now() END
             WHERE order_id = $1 AND id = $2
            RETURNING id, position, text, done, done_by, done_at
            """,
            order_id, todo_id, user_id,
        )
        if row is None:
            raise NotFound()
        return Todo(**dict(row))

    async def remove_todo(self, order_id: int, todo_id: int) -> None:
        await self.db.execute("DELETE FROM order_todos WHERE order_id = $1 AND id = $2", order_id, todo_id)

    # ------------------------------------------------------------------ statistics
    async def stats(self, guild_id: int, since: dt.datetime | None = None) -> OrderStats:
        row = await self.db.fetchrow(
            """
            SELECT count(*) AS cnt, COALESCE(sum(total_cents), 0) AS revenue,
                   COALESCE(avg(total_cents), 0) AS average,
                   COALESCE(avg((delivery = 'express')::int), 0) AS express_share
              FROM orders
             WHERE guild_id = $1 AND paid_at IS NOT NULL AND status <> ALL($3::text[])
               AND ($2::timestamptz IS NULL OR paid_at >= $2)
            """,
            guild_id, since, list(REVENUE_EXCLUDED),
        )
        status_rows = await self.db.fetch(
            "SELECT status, count(*) AS cnt FROM orders WHERE guild_id = $1 AND status <> 'draft' "
            "AND ($2::timestamptz IS NULL OR created_at >= $2) GROUP BY status",
            guild_id, since,
        )
        designer_rows = await self.db.fetch(
            """
            SELECT designer_id, count(*) AS cnt, COALESCE(sum(total_cents), 0) AS revenue
              FROM orders
             WHERE guild_id = $1 AND designer_id IS NOT NULL AND paid_at IS NOT NULL
               AND status <> ALL($3::text[]) AND ($2::timestamptz IS NULL OR paid_at >= $2)
             GROUP BY designer_id ORDER BY revenue DESC LIMIT 15
            """,
            guild_id, since, list(REVENUE_EXCLUDED),
        )
        assert row is not None
        return OrderStats(
            count=int(row["cnt"]),
            revenue_cents=int(row["revenue"]),
            average_cents=int(round(float(row["average"]))),
            by_status={r["status"]: int(r["cnt"]) for r in status_rows},
            designers=[DesignerStats(r["designer_id"], int(r["cnt"]), int(r["revenue"])) for r in designer_rows],
            express_share=float(row["express_share"]),
        )
