"""Customer data (``X-API-Key``): summary, GDPR export and deletion."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder

from bot.core.events import emit_event
from bot.services.apikeys import ApiKey

from ..deps import ServicesDep, guild_of, jsonable, require_scope

router = APIRouter(prefix="/v1/customers", tags=["customers"])


@router.get("/{user_id}", summary="Customer summary (orders, lifetime value, tickets, reviews)")
async def customer(user_id: int, services: ServicesDep,
                   key: Annotated[ApiKey, Depends(require_scope("customers:read"))]) -> Any:
    return jsonable(jsonable_encoder(await services.customers.summary(guild_of(key), user_id)))


@router.get("/{user_id}/export", summary="GDPR export of all stored personal data")
async def export(user_id: int, services: ServicesDep,
                 key: Annotated[ApiKey, Depends(require_scope("customers:read"))]) -> Any:
    return jsonable(jsonable_encoder(await services.customers.export(guild_of(key), user_id)))


@router.delete("/{user_id}", summary="GDPR deletion (invoices are retained for the statutory period)")
async def delete(user_id: int, services: ServicesDep,
                 key: Annotated[ApiKey, Depends(require_scope("customers:write"))]) -> dict[str, Any]:
    guild_id = guild_of(key)
    result = await services.customers.delete(guild_id, user_id)
    await emit_event(services.state.db.pool, guild_id, "customer.deleted", {"user_id": str(user_id), "result": result})
    return {"deleted": result}
