"""Invoices (``X-API-Key``): metadata and PDF download."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

from bot.services.apikeys import ApiKey
from bot.services.invoices import Invoice

from ..deps import Services, ServicesDep, guild_of, jsonable, require_scope

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])


async def _invoice(services: Services, guild_id: int, order_id: int) -> tuple[Invoice, Any]:
    order = await services.orders.get(order_id, guild_id)
    if order.paid_at is None:
        raise HTTPException(status_code=409, detail="order is not paid")
    paid = await services.payments.paid_for_order(order.id)
    invoice = await services.invoices.ensure(
        order,
        customer_name=paid.customer_name if paid else None,
        customer_email=paid.customer_email if paid else None,
        customer_address=paid.customer_address if paid else None,
    )
    return invoice, order


@router.get("/{order_id}", summary="Invoice metadata (created on demand for paid orders)")
async def invoice(order_id: int, services: ServicesDep,
                  key: Annotated[ApiKey, Depends(require_scope("invoices:read"))]) -> Any:
    item, _ = await _invoice(services, guild_of(key), order_id)
    data = {s: getattr(item, s) for s in item.__slots__}
    data["pdf_url"] = f"/v1/invoices/{order_id}/pdf"
    return jsonable(jsonable_encoder(data))


@router.get("/{order_id}/pdf", summary="Invoice as PDF", response_class=Response)
async def invoice_pdf(order_id: int, services: ServicesDep,
                      key: Annotated[ApiKey, Depends(require_scope("invoices:read"))]) -> Response:
    item, order = await _invoice(services, guild_of(key), order_id)
    data = await services.invoices.render_pdf(item, order)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{item.file_name}"'})
