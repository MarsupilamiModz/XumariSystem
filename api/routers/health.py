from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from bot import __version__

from ..state import ApiState, get_state

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(state: Annotated[ApiState, Depends(get_state)]) -> dict[str, Any]:
    return {"status": "ok" if await state.db.ping() else "degraded", "version": __version__}
