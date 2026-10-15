"""Serves locally stored ticket transcripts (``tickets.transcripts.storage: local``)."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..state import ApiState, get_state

router = APIRouter(tags=["transcripts"])
_NAME_RE = re.compile(r"^[A-Z0-9]{8}-[A-Za-z0-9_-]{8,40}\.html$")


@router.get("/transcripts/{file_name}", include_in_schema=False)
async def transcript(file_name: str, state: Annotated[ApiState, Depends(get_state)]) -> FileResponse:
    if not _NAME_RE.match(file_name):
        raise HTTPException(status_code=404)
    directory = state.config.tickets.transcripts.local_dir.resolve()
    path = (directory / file_name).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/html; charset=utf-8", headers={"X-Robots-Tag": "noindex"})
