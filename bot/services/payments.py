"""Stripe integration (async StripeClient): Checkout Sessions, Payment Links and payment records."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from ..config.settings import AppConfig, Secrets
from ..core.db import Database
from .orders import Order

_log = logging.getLogger("xumari.payments")
STRIPE_MIN_AMOUNT_CENTS = 50


@dataclass(slots=True)
class Payment:
    id: int
    order_id: int
    guild_id: int
    provider: str
    kind: str
    external_id: str | None
    url: str | None
    status: str
    amount_cents: int
    currency: str
    payment_intent: str | None
    customer_email: str | None
    customer_name: str | None
    customer_address: dict[str, Any] | None
    expires_at: dt.datetime | None
    paid_at: dt.datetime | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Payment:
        data = dict(row)
        data.pop("updated_at", None)
        return cls(**data)

    @property
    def usable(self) -> bool:
        if self.status != "open" or not self.url:
            return False
        return self.expires_at is None or self.expires_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)


class PaymentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def latest_for_order(self, order_id: int) -> Payment | None:
        row = await self.db.fetchrow(
            "SELECT * FROM payments WHERE order_id = $1 ORDER BY created_at DESC LIMIT 1", order_id
        )
        return Payment.from_row(row) if row else None

    async def paid_for_order(self, order_id: int) -> Payment | None:
        row = await self.db.fetchrow(
            "SELECT * FROM payments WHERE order_id = $1 AND status = 'paid' ORDER BY paid_at DESC LIMIT 1", order_id
        )
        return Payment.from_row(row) if row else None

    async def create(
        self, order: Order, *, kind: str, external_id: str | None, url: str | None, expires_at: dt.datetime | None
    ) -> Payment:
        row = await self.db.fetchrow(
            """
            INSERT INTO payments (order_id, guild_id, kind, external_id, url, amount_cents, currency, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *
            """,
            order.id, order.guild_id, kind, external_id, url, order.total_cents, order.currency, expires_at,
        )
        assert row is not None
        return Payment.from_row(row)

    async def mark(self, payment_id: int, status: str) -> None:
        await self.db.execute("UPDATE payments SET status = $2 WHERE id = $1", payment_id, status)


class StripeService:
    """Creates checkout sessions / payment links. All amounts in cents, currency from the order."""

    def __init__(self, config: AppConfig, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.secrets.stripe_secret_key)

    @property
    def client(self) -> Any:
        if self._client is None:
            import stripe

            self._client = stripe.StripeClient(self.secrets.stripe_secret_key, http_client=stripe.HTTPXClient())
        return self._client

    def _urls(self, order: Order) -> tuple[str, str]:
        cfg = self.config.payments.stripe
        values = {"api_url": self.config.api.public_url.rstrip("/"), "order_id": order.id}
        return cfg.success_url.format(**values), cfg.cancel_url.format(**values)

    @staticmethod
    def metadata(order: Order) -> dict[str, str]:
        return {"order_id": str(order.id), "guild_id": str(order.guild_id), "user_id": str(order.customer_id)}

    def line_items(self, order: Order, labels: dict[str, str]) -> list[tuple[str, int, int]]:
        """``(name, unit_amount_cents, quantity)`` incl. express / seasonal surcharges."""
        items = [(i.name, i.unit_price_cents, i.quantity) for i in order.items if i.unit_price_cents > 0]
        if order.express_cents:
            items.append((labels["express"], order.express_cents, 1))
        if order.seasonal_cents:
            items.append((labels["seasonal"], order.seasonal_cents, 1))
        return items

    async def create_checkout(self, order: Order, labels: dict[str, str]) -> tuple[str, str, dt.datetime]:
        currency = order.currency.lower()
        success, cancel = self._urls(order)
        minutes = max(30, min(1440, self.config.payments.stripe.session_expires_minutes))
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {"price_data": {"currency": currency, "unit_amount": amount, "product_data": {"name": name[:250]}},
                 "quantity": quantity}
                for name, amount, quantity in self.line_items(order, labels)
            ],
            "metadata": self.metadata(order),
            "payment_intent_data": {"metadata": self.metadata(order),
                                    "description": labels["order"]},
            "client_reference_id": str(order.id),
            "success_url": success,
            "cancel_url": cancel,
            "locale": "de" if order.locale == "de" else "en",
            "expires_at": int(expires_at.timestamp()),
            "billing_address_collection": "required",
        }
        if order.discount_cents:
            coupon = await self.client.v1.coupons.create_async(params={
                "amount_off": order.discount_cents, "currency": currency, "duration": "once",
                "name": labels["discount"][:40], "max_redemptions": 1,
            })
            params["discounts"] = [{"coupon": coupon.id}]
        session = await self.client.v1.checkout.sessions.create_async(params=params)
        return session.url, session.id, expires_at

    async def create_payment_link(self, order: Order, labels: dict[str, str]) -> tuple[str, str]:
        currency = order.currency.lower()
        success, _ = self._urls(order)
        if order.discount_cents:
            # Payment Links don't accept ad-hoc discounts → one line with the final amount
            entries = [(labels["order"], order.total_cents, 1)]
        else:
            entries = self.line_items(order, labels)
        line_items = []
        for name, amount, quantity in entries:
            price = await self.client.v1.prices.create_async(params={
                "currency": currency, "unit_amount": amount, "product_data": {"name": name[:250]},
            })
            line_items.append({"price": price.id, "quantity": quantity})
        link = await self.client.v1.payment_links.create_async(params={
            "line_items": line_items,
            "metadata": self.metadata(order),
            "payment_intent_data": {"metadata": self.metadata(order)},
            "after_completion": {"type": "redirect", "redirect": {"url": success}},
            "billing_address_collection": "required",
            # one payment per link – prevents customers from paying the same order twice
            "restrictions": {"completed_sessions": {"limit": 1}},
        })
        return link.url, link.id

    async def invalidate(self, payment: Payment) -> None:
        if not payment.external_id:
            return
        try:
            if payment.kind == "checkout":
                await self.client.v1.checkout.sessions.expire_async(payment.external_id)
            elif payment.kind == "payment_link":
                await self.client.v1.payment_links.update_async(payment.external_id, params={"active": False})
        except Exception:  # noqa: BLE001 – already expired / paid sessions can't be expired
            _log.info("Could not invalidate Stripe object %s", payment.external_id)

    def construct_event(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        event = self.client.construct_event(payload, signature, self.secrets.stripe_webhook_secret)
        return event.to_dict()  # type: ignore[no-any-return]

    async def close(self) -> None:
        self._client = None
