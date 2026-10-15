"""Public pages: legal documents and Stripe redirect pages (no API key)."""

from __future__ import annotations

import html
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from bot.services.legal import DOCUMENTS, html_page, load_document, markdown_to_html

from ..deps import ServicesDep
from ..state import ApiState, get_state

router = APIRouter(tags=["public"])
Lang = Literal["de", "en"]


@router.get("/legal/{document}", response_class=HTMLResponse, summary="Legal document as HTML page")
async def legal_page(document: str, state: Annotated[ApiState, Depends(get_state)], lang: Lang = "de") -> HTMLResponse:
    text = await load_document(state.config.payments.legal_dir, document, lang)
    if text is None:
        raise HTTPException(status_code=404)
    return HTMLResponse(html_page(f"{state.config.bot.shop_name} – {document}", markdown_to_html(text),
                                  state.config.design.accent_color))


@router.get("/v1/legal/{document}", summary="Legal document as Markdown")
async def legal_json(document: str, state: Annotated[ApiState, Depends(get_state)], lang: Lang = "de") -> dict[str, Any]:
    text = await load_document(state.config.payments.legal_dir, document, lang)
    if text is None:
        raise HTTPException(status_code=404)
    return {"document": document, "locale": lang, "markdown": text, "available": list(DOCUMENTS)}


async def _payment_page(services: ServicesDep, order_id: int | None, success: bool) -> HTMLResponse:
    locale = services.state.config.bot.default_locale
    if order_id is not None:
        try:
            locale = (await services.orders.get(order_id)).locale
        except Exception:  # noqa: BLE001 – unknown order ids simply use the default language
            locale = services.state.config.bot.default_locale
    tr = services.i18n.translator(locale)
    key = "success" if success else "cancel"
    body = (
        f"<h1>{html.escape(tr(f'payments.page.{key}_title'))}</h1>"
        f"<p>{html.escape(tr(f'payments.page.{key}_text', number=order_id or '–'))}</p>"
    )
    return HTMLResponse(html_page(services.state.config.bot.shop_name, body, services.state.config.design.accent_color))


@router.get("/payment/success", response_class=HTMLResponse, include_in_schema=False)
async def payment_success(services: ServicesDep, order: int | None = None) -> HTMLResponse:
    return await _payment_page(services, order, True)


@router.get("/payment/cancel", response_class=HTMLResponse, include_in_schema=False)
async def payment_cancel(services: ServicesDep, order: int | None = None) -> HTMLResponse:
    return await _payment_page(services, order, False)
