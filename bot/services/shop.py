"""Shop catalogue: product categories, products, bundles and coupons (shared by bot and API)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from ..core.db import Database
from ..core.errors import NotFound, UserError


@dataclass(slots=True)
class ProductCategory:
    id: int
    key: str
    name_de: str
    name_en: str
    emoji: str | None
    sort_order: int
    active: bool

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en


@dataclass(slots=True)
class Product:
    id: int
    guild_id: int
    category_id: int | None
    sku: str
    name_de: str
    name_en: str
    description_de: str
    description_en: str
    emoji: str | None
    price_cents: int
    delivery_days: int | None
    image_url: str | None
    active: bool
    sort_order: int

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en

    def description(self, locale: str) -> str:
        return self.description_de if locale == "de" else self.description_en


@dataclass(slots=True)
class BundleItem:
    product_id: int
    name_de: str
    name_en: str
    quantity: int


@dataclass(slots=True)
class Bundle:
    id: int
    guild_id: int
    sku: str
    name_de: str
    name_en: str
    description_de: str
    description_en: str
    emoji: str | None
    price_cents: int
    active: bool
    sort_order: int
    items: list[BundleItem] = field(default_factory=list)

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en

    def description(self, locale: str) -> str:
        return self.description_de if locale == "de" else self.description_en


@dataclass(slots=True)
class Coupon:
    id: int
    guild_id: int
    code: str
    kind: str
    value: int
    min_order_cents: int
    max_uses: int | None
    uses: int
    expires_at: dt.datetime | None
    active: bool
    product_ids: list[int] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.max_uses is not None and self.uses >= self.max_uses

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= dt.datetime.now(dt.UTC)

    def discount_for(self, eligible_cents: int) -> int:
        if eligible_cents <= 0:
            return 0
        if self.kind == "percent":
            return min(eligible_cents, round(eligible_cents * self.value / 100))
        return min(eligible_cents, self.value)


_PRODUCT_COLS = (
    "id, guild_id, category_id, sku, name_de, name_en, description_de, description_en, emoji, price_cents, "
    "delivery_days, image_url, active, sort_order"
)
_BUNDLE_COLS = "id, guild_id, sku, name_de, name_en, description_de, description_en, emoji, price_cents, active, sort_order"
_PRODUCT_FIELDS = {
    "category_id", "sku", "name_de", "name_en", "description_de", "description_en", "emoji", "price_cents",
    "delivery_days", "image_url", "active", "sort_order",
}
_BUNDLE_FIELDS = {"sku", "name_de", "name_en", "description_de", "description_en", "emoji", "price_cents", "active",
                  "sort_order"}


class ShopService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ categories
    async def categories(self, guild_id: int, *, active_only: bool = True) -> list[ProductCategory]:
        rows = await self.db.fetch(
            "SELECT id, key, name_de, name_en, emoji, sort_order, active FROM product_categories "
            "WHERE guild_id = $1 AND ($2 = FALSE OR active) ORDER BY sort_order, id",
            guild_id, active_only,
        )
        return [ProductCategory(**dict(r)) for r in rows]

    async def upsert_category(
        self, guild_id: int, key: str, name_de: str, name_en: str, emoji: str | None, sort_order: int = 0
    ) -> ProductCategory:
        await self.db.ensure_guild(guild_id)
        row = await self.db.fetchrow(
            """
            INSERT INTO product_categories (guild_id, key, name_de, name_en, emoji, sort_order)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (guild_id, key) DO UPDATE
               SET name_de = EXCLUDED.name_de, name_en = EXCLUDED.name_en, emoji = EXCLUDED.emoji,
                   sort_order = EXCLUDED.sort_order, active = TRUE
            RETURNING id, key, name_de, name_en, emoji, sort_order, active
            """,
            guild_id, key, name_de, name_en, emoji, sort_order,
        )
        assert row is not None
        return ProductCategory(**dict(row))

    async def delete_category(self, guild_id: int, key: str) -> bool:
        result = await self.db.execute(
            "DELETE FROM product_categories WHERE guild_id = $1 AND key = $2", guild_id, key
        )
        return result.endswith(" 1")

    # ------------------------------------------------------------------ products
    async def products(self, guild_id: int, *, active_only: bool = True) -> list[Product]:
        rows = await self.db.fetch(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE guild_id = $1 AND ($2 = FALSE OR active) "
            "ORDER BY sort_order, name_de",
            guild_id, active_only,
        )
        return [Product(**dict(r)) for r in rows]

    async def product(self, guild_id: int, product_id: int) -> Product:
        row = await self.db.fetchrow(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE guild_id = $1 AND id = $2", guild_id, product_id
        )
        if row is None:
            raise NotFound("shop.errors.product_not_found")
        return Product(**dict(row))

    async def product_by_sku(self, guild_id: int, sku: str) -> Product:
        row = await self.db.fetchrow(
            f"SELECT {_PRODUCT_COLS} FROM products WHERE guild_id = $1 AND sku = $2", guild_id, sku
        )
        if row is None:
            raise NotFound("shop.errors.product_not_found")
        return Product(**dict(row))

    async def create_product(self, guild_id: int, **values: Any) -> Product:
        await self.db.ensure_guild(guild_id)
        data = {k: v for k, v in values.items() if k in _PRODUCT_FIELDS}
        columns = ["guild_id", *data]
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        try:
            row = await self.db.fetchrow(
                f"INSERT INTO products ({', '.join(columns)}) VALUES ({placeholders}) RETURNING {_PRODUCT_COLS}",
                guild_id, *data.values(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("shop.errors.sku_exists", sku=data.get("sku", "")) from exc
        assert row is not None
        return Product(**dict(row))

    async def update_product(self, guild_id: int, product_id: int, **values: Any) -> Product:
        data = {k: v for k, v in values.items() if k in _PRODUCT_FIELDS}
        if not data:
            return await self.product(guild_id, product_id)
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(data, start=3))
        try:
            row = await self.db.fetchrow(
                f"UPDATE products SET {sets} WHERE guild_id = $1 AND id = $2 RETURNING {_PRODUCT_COLS}",
                guild_id, product_id, *data.values(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("shop.errors.sku_exists", sku=data.get("sku", "")) from exc
        if row is None:
            raise NotFound("shop.errors.product_not_found")
        return Product(**dict(row))

    async def delete_product(self, guild_id: int, product_id: int) -> None:
        result = await self.db.execute("DELETE FROM products WHERE guild_id = $1 AND id = $2", guild_id, product_id)
        if not result.endswith(" 1"):
            raise NotFound("shop.errors.product_not_found")

    # ------------------------------------------------------------------ bundles
    async def _bundle_items(self, bundle_ids: list[int]) -> dict[int, list[BundleItem]]:
        if not bundle_ids:
            return {}
        rows = await self.db.fetch(
            """
            SELECT bi.bundle_id, bi.product_id, bi.quantity, p.name_de, p.name_en
              FROM bundle_items bi JOIN products p ON p.id = bi.product_id
             WHERE bi.bundle_id = ANY($1::bigint[])
             ORDER BY p.sort_order, p.name_de
            """,
            bundle_ids,
        )
        out: dict[int, list[BundleItem]] = {}
        for r in rows:
            out.setdefault(r["bundle_id"], []).append(BundleItem(r["product_id"], r["name_de"], r["name_en"], r["quantity"]))
        return out

    async def bundles(self, guild_id: int, *, active_only: bool = True) -> list[Bundle]:
        rows = await self.db.fetch(
            f"SELECT {_BUNDLE_COLS} FROM bundles WHERE guild_id = $1 AND ($2 = FALSE OR active) "
            "ORDER BY sort_order, name_de",
            guild_id, active_only,
        )
        bundles = [Bundle(**dict(r)) for r in rows]
        items = await self._bundle_items([b.id for b in bundles])
        for bundle in bundles:
            bundle.items = items.get(bundle.id, [])
        return bundles

    async def bundle(self, guild_id: int, bundle_id: int) -> Bundle:
        row = await self.db.fetchrow(f"SELECT {_BUNDLE_COLS} FROM bundles WHERE guild_id = $1 AND id = $2",
                                     guild_id, bundle_id)
        if row is None:
            raise NotFound("shop.errors.bundle_not_found")
        bundle = Bundle(**dict(row))
        bundle.items = (await self._bundle_items([bundle.id])).get(bundle.id, [])
        return bundle

    async def create_bundle(self, guild_id: int, **values: Any) -> Bundle:
        await self.db.ensure_guild(guild_id)
        data = {k: v for k, v in values.items() if k in _BUNDLE_FIELDS}
        columns = ["guild_id", *data]
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        try:
            row = await self.db.fetchrow(
                f"INSERT INTO bundles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING {_BUNDLE_COLS}",
                guild_id, *data.values(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("shop.errors.sku_exists", sku=data.get("sku", "")) from exc
        assert row is not None
        return Bundle(**dict(row))

    async def update_bundle(self, guild_id: int, bundle_id: int, **values: Any) -> Bundle:
        data = {k: v for k, v in values.items() if k in _BUNDLE_FIELDS}
        if data:
            sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(data, start=3))
            result = await self.db.execute(
                f"UPDATE bundles SET {sets} WHERE guild_id = $1 AND id = $2", guild_id, bundle_id, *data.values()
            )
            if not result.endswith(" 1"):
                raise NotFound("shop.errors.bundle_not_found")
        return await self.bundle(guild_id, bundle_id)

    async def set_bundle_item(self, guild_id: int, bundle_id: int, product_id: int, quantity: int) -> None:
        await self.bundle(guild_id, bundle_id)
        await self.product(guild_id, product_id)
        if quantity <= 0:
            await self.db.execute("DELETE FROM bundle_items WHERE bundle_id = $1 AND product_id = $2",
                                  bundle_id, product_id)
            return
        await self.db.execute(
            """
            INSERT INTO bundle_items (bundle_id, product_id, quantity) VALUES ($1, $2, $3)
            ON CONFLICT (bundle_id, product_id) DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            bundle_id, product_id, quantity,
        )

    async def delete_bundle(self, guild_id: int, bundle_id: int) -> None:
        result = await self.db.execute("DELETE FROM bundles WHERE guild_id = $1 AND id = $2", guild_id, bundle_id)
        if not result.endswith(" 1"):
            raise NotFound("shop.errors.bundle_not_found")

    # ------------------------------------------------------------------ coupons
    async def _coupon_from_row(self, row: asyncpg.Record) -> Coupon:
        product_rows = await self.db.fetch("SELECT product_id FROM coupon_products WHERE coupon_id = $1", row["id"])
        return Coupon(
            id=row["id"], guild_id=row["guild_id"], code=row["code"], kind=row["kind"], value=row["value"],
            min_order_cents=row["min_order_cents"], max_uses=row["max_uses"], uses=row["uses"],
            expires_at=row["expires_at"], active=row["active"], product_ids=[r["product_id"] for r in product_rows],
        )

    async def coupons(self, guild_id: int) -> list[Coupon]:
        rows = await self.db.fetch("SELECT * FROM coupons WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)
        return [await self._coupon_from_row(r) for r in rows]

    async def coupon(self, guild_id: int, coupon_id: int) -> Coupon:
        row = await self.db.fetchrow("SELECT * FROM coupons WHERE guild_id = $1 AND id = $2", guild_id, coupon_id)
        if row is None:
            raise NotFound("shop.errors.coupon_not_found")
        return await self._coupon_from_row(row)

    async def coupon_by_code(self, guild_id: int, code: str) -> Coupon:
        row = await self.db.fetchrow(
            "SELECT * FROM coupons WHERE guild_id = $1 AND upper(code) = upper($2)", guild_id, code.strip()
        )
        if row is None:
            raise UserError("shop.errors.coupon_invalid")
        return await self._coupon_from_row(row)

    async def create_coupon(
        self,
        guild_id: int,
        *,
        code: str,
        kind: str,
        value: int,
        min_order_cents: int = 0,
        max_uses: int | None = None,
        expires_at: dt.datetime | None = None,
        product_ids: list[int] | None = None,
        created_by: int | None = None,
    ) -> Coupon:
        if kind not in {"percent", "fixed"} or value <= 0 or (kind == "percent" and value > 100):
            raise UserError("shop.errors.coupon_value")
        await self.db.ensure_guild(guild_id)
        try:
            async with self.db.transaction() as conn:
                coupon_id = await conn.fetchval(
                    """
                    INSERT INTO coupons (guild_id, code, kind, value, min_order_cents, max_uses, expires_at, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
                    """,
                    guild_id, code.strip().upper(), kind, value, min_order_cents, max_uses, expires_at, created_by,
                )
                for pid in product_ids or []:
                    await conn.execute(
                        "INSERT INTO coupon_products (coupon_id, product_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        coupon_id, pid,
                    )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("shop.errors.coupon_exists", code=code.upper()) from exc
        return await self.coupon(guild_id, coupon_id)

    async def set_coupon_active(self, guild_id: int, coupon_id: int, active: bool) -> None:
        await self.db.execute("UPDATE coupons SET active = $3 WHERE guild_id = $1 AND id = $2", guild_id, coupon_id, active)

    async def delete_coupon(self, guild_id: int, coupon_id: int) -> None:
        result = await self.db.execute("DELETE FROM coupons WHERE guild_id = $1 AND id = $2", guild_id, coupon_id)
        if not result.endswith(" 1"):
            raise NotFound("shop.errors.coupon_not_found")

    @staticmethod
    def check_coupon(
        coupon: Coupon, *, subtotal_cents: int, eligible_cents: int, money: Callable[[int], str] = str
    ) -> int:
        """Validate a coupon against an order and return the discount in cents."""
        if not coupon.active or coupon.expired:
            raise UserError("shop.errors.coupon_expired")
        if coupon.exhausted:
            raise UserError("shop.errors.coupon_exhausted")
        if subtotal_cents < coupon.min_order_cents:
            raise UserError("shop.errors.coupon_min_order", min=money(coupon.min_order_cents))
        if coupon.product_ids and eligible_cents <= 0:
            raise UserError("shop.errors.coupon_products")
        return coupon.discount_for(eligible_cents)
