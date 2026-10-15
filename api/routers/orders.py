"""Order API (``X-API-Key``): list, read, create and change the status of orders; product catalogue."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator

from bot.core.errors import UserError
from bot.core.events import emit_event
from bot.core.utils import format_money
from bot.services.apikeys import ApiKey
from bot.services.orders import STATUSES
from bot.services.payments import STRIPE_MIN_AMOUNT_CENTS

from ..deps import ServicesDep, guild_of, jsonable, require_scope

router = APIRouter(prefix="/v1", tags=["orders"])

OrderStatus = Literal["draft", "awaiting_payment", "paid", "queued", "in_progress", "pending_answer", "completed",
                      "cancelled", "refunded"]


class OrderItemIn(BaseModel):
    sku: str | None = Field(default=None, description="Product SKU")
    product_id: int | None = None
    bundle_id: int | None = None
    quantity: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _one_reference(self) -> OrderItemIn:
        if sum(x is not None for x in (self.sku, self.product_id, self.bundle_id)) != 1:
            raise ValueError("exactly one of sku, product_id or bundle_id is required")
        return self


class OrderCreate(BaseModel):
    customer_id: int = Field(description="Discord user ID of the customer")
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    delivery: Literal["standard", "express"] = "standard"
    seasonal_key: str | None = None
    coupon_code: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    locale: Literal["de", "en"] = "de"
    create_payment_link: bool = False


class StatusUpdate(BaseModel):
    status: OrderStatus
    note: str | None = Field(default=None, max_length=1000)


def _order_json(order: Any) -> Any:
    return jsonable(jsonable_encoder(order.as_dict()))


@router.get("/orders", summary="List orders")
async def list_orders(
    services: ServicesDep,
    key: Annotated[ApiKey, Depends(require_scope("orders:read"))],
    status: OrderStatus | None = None,
    customer_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    orders = await services.orders.list_orders(guild_of(key), status=status, customer_id=customer_id,
                                               limit=limit, offset=offset)
    return {"orders": [_order_json(o) for o in orders], "limit": limit, "offset": offset}


@router.get("/orders/{order_id}", summary="Get an order")
async def get_order(order_id: int, services: ServicesDep,
                    key: Annotated[ApiKey, Depends(require_scope("orders:read"))]) -> Any:
    order = await services.orders.get(order_id, guild_of(key))
    data = _order_json(order)
    data["queue_position"] = await services.orders.queue_position(order)
    data["history"] = jsonable(jsonable_encoder([dict(r) for r in await services.orders.history(order.id)]))
    return data


@router.post("/orders", status_code=201, summary="Create an order")
async def create_order(body: OrderCreate, services: ServicesDep,
                       key: Annotated[ApiKey, Depends(require_scope("orders:write"))]) -> dict[str, Any]:
    guild_id = guild_of(key)
    shop, orders = services.shop, services.orders
    # resolve every item before touching the database, so invalid requests don't leave orphan drafts
    resolved: list[tuple[str, Any, int]] = []
    for item in body.items:
        if item.bundle_id is not None:
            bundle = await shop.bundle(guild_id, item.bundle_id)
            if not bundle.active:
                raise UserError("shop.errors.bundle_not_found")
            resolved.append(("bundle", bundle, item.quantity))
            continue
        product = (await shop.product_by_sku(guild_id, item.sku) if item.sku
                   else await shop.product(guild_id, item.product_id or 0))
        if not product.active:
            raise UserError("shop.errors.product_not_found")
        resolved.append(("product", product, item.quantity))
    money = lambda cents: format_money(cents, services.state.config.shop.currency, body.locale)  # noqa: E731
    order = await orders.create_draft(guild_id=guild_id, customer_id=body.customer_id, ticket_id=None,
                                      locale=body.locale, source="api")
    try:
        for kind, entity, quantity in resolved:
            if kind == "bundle":
                await orders.add_bundle(order, entity, quantity)
            else:
                await orders.add_product(order, entity, quantity)
        await orders.set_delivery(order, body.delivery)
        await orders.set_seasonal(order, body.seasonal_key)
        if body.notes:
            await orders.set_notes(order, body.notes)
        order = await orders.recalculate(order.id, shop, money=money)
        if body.coupon_code:
            await orders.apply_coupon(order, body.coupon_code, shop, money=money)
            order = await orders.recalculate(order.id, shop, money=money)
        order = await orders.set_status(order, "awaiting_payment", changed_by=None, note="api")
    except Exception:
        await orders.delete_draft(order.id)
        raise
    await emit_event(services.state.db.pool, guild_id, "order.created", {"order_id": order.id})

    payment_url = None
    if body.create_payment_link and services.stripe.available and order.total_cents >= STRIPE_MIN_AMOUNT_CENTS:
        tr = services.i18n.translator(order.locale)
        labels = {
            "express": tr("orders.totals.express"),
            "seasonal": tr("orders.totals.seasonal", name=order.seasonal_key or ""),
            "discount": tr("orders.totals.discount", code=order.coupon_code or ""),
            "order": tr("orders.number", number=order.number),
        }
        if services.state.config.payments.stripe.mode == "payment_link":
            payment_url, external_id = await services.stripe.create_payment_link(order, labels)
            await services.payments.create(order, kind="payment_link", external_id=external_id, url=payment_url,
                                           expires_at=None)
        else:
            payment_url, external_id, expires = await services.stripe.create_checkout(order, labels)
            await services.payments.create(order, kind="checkout", external_id=external_id, url=payment_url,
                                           expires_at=expires)
    return {"order": _order_json(order), "payment_url": payment_url}


@router.patch("/orders/{order_id}/status", summary="Change the status of an order")
async def update_status(order_id: int, body: StatusUpdate, services: ServicesDep,
                        key: Annotated[ApiKey, Depends(require_scope("orders:write"))]) -> Any:
    guild_id = guild_of(key)
    order = await services.orders.get(order_id, guild_id)
    if order.status == body.status:
        return _order_json(order)
    if order.status in ("cancelled", "refunded") and body.status not in ("cancelled", "refunded"):
        raise UserError("orders.errors.closed_order")
    products = {p.id: p for p in await services.shop.products(guild_id, active_only=False)}
    old_status = order.status
    async with services.state.db.transaction() as conn:
        order = await services.orders.set_status(
            order, body.status, changed_by=None, note=body.note,
            delivery_days=services.orders.delivery_days(order, products), conn=conn,
        )
        await emit_event(conn, guild_id, "order.status_changed",
                         {"order_id": order.id, "old_status": old_status, "note": body.note})
    return _order_json(order)


@router.get("/products", tags=["catalogue"], summary="Active products and bundles")
async def products(services: ServicesDep,
                   key: Annotated[ApiKey, Depends(require_scope("products:read"))]) -> dict[str, Any]:
    guild_id = guild_of(key)
    items = await services.shop.products(guild_id)
    bundles = await services.shop.bundles(guild_id)
    return jsonable(jsonable_encoder({
        "currency": services.state.config.shop.currency,
        "products": [asdict(p) for p in items],
        "bundles": [asdict(b) for b in bundles],
        "statuses": list(STATUSES),
    }))
