"""Stripe webhook: signature check, idempotency (``stripe_events``) and order updates.

Everything for one event runs in a single transaction: the event id, the payment row, the order
status (+ history) and the outbox event for the bot are committed together – or not at all, in
which case Stripe retries the delivery.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from bot.core.events import emit_event
from bot.services.orders import Order

from ..deps import Services, ServicesDep

router = APIRouter(tags=["webhooks"])
_log = logging.getLogger("xumari.api.stripe")

PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


@router.post("/stripe/webhook", summary="Stripe webhook endpoint")
async def stripe_webhook(request: Request, services: ServicesDep) -> dict[str, str]:
    if not services.state.secrets.stripe_webhook_secret or not services.stripe.available:
        raise HTTPException(status_code=503, detail="stripe is not configured")
    payload = await request.body()
    try:
        event = services.stripe.construct_event(payload, request.headers.get("stripe-signature"))
    except Exception as exc:  # noqa: BLE001 – ValueError / SignatureVerificationError
        _log.warning("Rejected Stripe webhook: %s", exc)
        raise HTTPException(status_code=400, detail="invalid signature") from exc

    async with services.state.db.transaction() as conn:
        inserted = await conn.fetchval(
            "INSERT INTO stripe_events (id, type) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING RETURNING id",
            event["id"], event["type"],
        )
        if inserted is None:
            return {"status": "duplicate"}
        await _dispatch(services, conn, event)
    return {"status": "ok"}


async def _dispatch(services: Services, conn: asyncpg.Connection, event: dict[str, Any]) -> None:
    kind = event["type"]
    obj = event["data"]["object"]
    if kind in PAID_EVENTS:
        if kind == "checkout.session.completed" and obj.get("payment_status") != "paid":
            await _update_payment(conn, obj, "open")  # async payment methods (e.g. SEPA) confirm later
            return
        await _session_paid(services, conn, obj)
    elif kind == "checkout.session.expired":
        await _update_payment(conn, obj, "expired")
    elif kind == "checkout.session.async_payment_failed":
        await _update_payment(conn, obj, "failed")
    elif kind == "charge.refunded":
        await _charge_refunded(services, conn, obj)
    else:
        _log.debug("Ignoring Stripe event %s", kind)


async def _find_payment(conn: asyncpg.Connection, session: dict[str, Any]) -> asyncpg.Record | None:
    for external in (session.get("id"), session.get("payment_link")):
        if external:
            row = await conn.fetchrow("SELECT * FROM payments WHERE provider = 'stripe' AND external_id = $1 FOR UPDATE",
                                      external)
            if row is not None:
                return row
    return None


async def _update_payment(conn: asyncpg.Connection, session: dict[str, Any], status: str) -> asyncpg.Record | None:
    payment = await _find_payment(conn, session)
    if payment is None or payment["status"] == "paid":
        return payment
    await conn.execute("UPDATE payments SET status = $2 WHERE id = $1", payment["id"], status)
    return payment


async def _session_paid(services: Services, conn: asyncpg.Connection, session: dict[str, Any]) -> None:
    payment = await _find_payment(conn, session)
    order_id = payment["order_id"] if payment else int((session.get("metadata") or {}).get("order_id") or 0)
    if not order_id:
        _log.warning("Paid Stripe session %s without known order", session.get("id"))
        return
    # lock the order first: status decisions below must not race with /order cancel or the API
    locked_status = await conn.fetchval("SELECT status FROM orders WHERE id = $1 FOR UPDATE", order_id)
    if locked_status is None:
        _log.warning("Paid Stripe session %s for deleted order %s", session.get("id"), order_id)
        return
    order: Order = await services.orders.get(order_id, conn=conn)
    details = session.get("customer_details") or {}
    intent = session.get("payment_intent")
    if intent and await conn.fetchval(
        "SELECT 1 FROM payments WHERE order_id = $1 AND payment_intent = $2 AND status = 'paid'", order_id, intent
    ):
        return  # the same money reported again (other event type / redelivery) – already processed
    duplicate =payment is not None and payment["status"] == "paid" and payment["payment_intent"] not in (None, intent)
    if payment is None or duplicate:
        payment = await conn.fetchrow(
            "INSERT INTO payments (order_id, guild_id, kind, external_id, amount_cents, currency) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            order.id, order.guild_id, "payment_link" if session.get("payment_link") else "checkout",
            None if duplicate else session.get("id"), session.get("amount_total") or order.total_cents,
            (session.get("currency") or order.currency).upper(),
        )
    await conn.execute(
        """
        UPDATE payments SET status = 'paid', paid_at = $2, payment_intent = $3, customer_email = $4,
                            customer_name = $5, customer_address = $6
         WHERE id = $1
        """,
        payment["id"], dt.datetime.now(dt.UTC), intent, details.get("email"), details.get("name"),
        details.get("address"),
    )
    amount = session.get("amount_total")
    if amount is not None and amount != order.total_cents:
        _log.warning("Order %s: paid %s but total is %s", order.id, amount, order.total_cents)
    if order.status in ("draft", "awaiting_payment") and not duplicate:
        products = {p.id: p for p in await services.shop.products(order.guild_id, active_only=False)}
        old_status = order.status
        order = await services.orders.set_status(
            order, "paid", changed_by=None, note="stripe", delivery_days=services.orders.delivery_days(order, products),
            conn=conn,
        )
        await emit_event(conn, order.guild_id, "order.status_changed",
                         {"order_id": order.id, "old_status": old_status, "note": None, "source": "stripe"})
        return
    # money arrived for an order that is cancelled / refunded / already paid → the team must act (refund)
    reason = "duplicate" if duplicate or order.status not in ("cancelled", "refunded") else order.status
    _log.error("Unexpected payment for order %s (%s, intent %s)", order.id, reason, intent)
    await emit_event(conn, order.guild_id, "payment.attention",
                     {"order_id": order.id, "reason": reason, "payment_intent": intent, "amount_cents": amount})


async def _charge_refunded(services: Services, conn: asyncpg.Connection, charge: dict[str, Any]) -> None:
    intent = charge.get("payment_intent")
    if not intent:
        return
    payment = await conn.fetchrow("SELECT * FROM payments WHERE payment_intent = $1 FOR UPDATE", intent)
    if payment is None:
        return
    if not charge.get("refunded"):
        _log.info("Partial refund for order %s – status unchanged", payment["order_id"])
        return
    await conn.execute("UPDATE payments SET status = 'refunded' WHERE id = $1", payment["id"])
    await conn.execute("SELECT 1 FROM orders WHERE id = $1 FOR UPDATE", payment["order_id"])
    order = await services.orders.get(payment["order_id"], conn=conn)
    if order.status in ("refunded", "cancelled"):
        return
    still_paid = await conn.fetchval(
        "SELECT count(*) FROM payments WHERE order_id = $1 AND status = 'paid'", order.id
    )
    if still_paid:
        # an additional (duplicate) charge was refunded – the order itself stays paid
        await emit_event(conn, order.guild_id, "payment.attention",
                         {"order_id": order.id, "reason": "extra_refunded", "payment_intent": intent,
                          "amount_cents": charge.get("amount_refunded")})
        return
    old_status = order.status
    order = await services.orders.set_status(order, "refunded", changed_by=None, note="stripe refund", conn=conn)
    await emit_event(conn, order.guild_id, "order.status_changed",
                     {"order_id": order.id, "old_status": old_status, "note": None, "source": "stripe"})
