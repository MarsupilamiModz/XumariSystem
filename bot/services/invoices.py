"""Invoices: sequential numbering per guild/year and PDF rendering (fpdf2, executed in a thread)."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import asyncpg

from ..config.settings import AppConfig
from ..core.db import Database
from ..core.i18n import I18n
from ..core.utils import format_money
from .orders import Order

_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
)


@dataclass(slots=True)
class Invoice:
    id: int
    order_id: int
    guild_id: int
    number: str
    issued_at: dt.datetime
    customer_name: str | None
    customer_email: str | None
    customer_address: dict[str, Any] | None
    net_cents: int
    tax_cents: int
    gross_cents: int
    tax_rate: Decimal
    currency: str
    locale: str

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Invoice:
        data = dict(row)
        data.pop("created_at", None)
        return cls(**data)

    @property
    def file_name(self) -> str:
        return f"{self.number}.pdf"


class InvoiceService:
    def __init__(self, db: Database, config: AppConfig, i18n: I18n) -> None:
        self.db = db
        self.config = config
        self.i18n = i18n

    def split_tax(self, gross_cents: int) -> tuple[int, int, Decimal]:
        cfg = self.config.payments.invoice
        if cfg.small_business or cfg.vat_rate_percent <= 0:
            return gross_cents, 0, Decimal(0)
        rate = Decimal(str(cfg.vat_rate_percent))
        net = (Decimal(gross_cents) / (1 + rate / 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return int(net), gross_cents - int(net), rate

    async def get(self, order_id: int) -> Invoice | None:
        row = await self.db.fetchrow("SELECT * FROM invoices WHERE order_id = $1", order_id)
        return Invoice.from_row(row) if row else None

    async def ensure(
        self,
        order: Order,
        *,
        customer_name: str | None,
        customer_email: str | None = None,
        customer_address: dict[str, Any] | None = None,
    ) -> Invoice:
        """Create the invoice for a paid order exactly once (idempotent)."""
        existing = await self.get(order.id)
        if existing is not None:
            return existing
        net, tax, rate = self.split_tax(order.total_cents)
        year = dt.datetime.now(dt.UTC).year
        async with self.db.transaction() as conn:
            # serialise concurrent callers per order *before* touching the counter → gap-free numbers (GoBD)
            await conn.execute("SELECT 1 FROM orders WHERE id = $1 FOR UPDATE", order.id)
            if await conn.fetchval("SELECT 1 FROM invoices WHERE order_id = $1", order.id):
                row = await conn.fetchrow("SELECT * FROM invoices WHERE order_id = $1", order.id)
                assert row is not None
                return Invoice.from_row(row)
            sequence = await conn.fetchval(
                """
                INSERT INTO invoice_counters (guild_id, year, last) VALUES ($1, $2, 1)
                ON CONFLICT (guild_id, year) DO UPDATE SET last = invoice_counters.last + 1
                RETURNING last
                """,
                order.guild_id, year,
            )
            number = f"{self.config.payments.invoice.number_prefix}-{year}-{sequence:05d}"
            row = await conn.fetchrow(
                """
                INSERT INTO invoices (order_id, guild_id, number, customer_name, customer_email, customer_address,
                                      net_cents, tax_cents, gross_cents, tax_rate, currency, locale)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (order_id) DO NOTHING
                RETURNING *
                """,
                order.id, order.guild_id, number, customer_name, customer_email, customer_address, net, tax,
                order.total_cents, rate, order.currency, order.locale,
            )
        if row is None:
            existing = await self.get(order.id)
            assert existing is not None
            return existing
        return Invoice.from_row(row)

    async def render_pdf(self, invoice: Invoice, order: Order) -> bytes:
        return await asyncio.to_thread(self._render, invoice, order)

    # ------------------------------------------------------------------ PDF (sync, runs in a thread)
    def _render(self, invoice: Invoice, order: Order) -> bytes:
        from fpdf import FPDF

        cfg = self.config.payments.invoice
        company = cfg.company
        tr = self.i18n.translator(invoice.locale)
        money = lambda cents: format_money(cents, invoice.currency, invoice.locale)  # noqa: E731

        pdf = FPDF(format="A4")
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()
        font = self._font(pdf)

        def text(value: str) -> str:
            return value if font != "Helvetica" else value.replace("€", "EUR").encode("latin-1", "replace").decode("latin-1")

        accent = self.config.design.accent
        pdf.set_fill_color((accent >> 16) & 255, (accent >> 8) & 255, accent & 255)
        pdf.rect(0, 0, 210, 6, style="F")

        pdf.set_font(font, size=18)
        pdf.set_xy(15, 16)
        pdf.cell(0, 10, text(company.name))
        pdf.set_font(font, size=9)
        pdf.set_xy(15, 26)
        sender = " · ".join([company.name, *company.address_lines])
        pdf.cell(0, 5, text(sender))

        # recipient
        pdf.set_font(font, size=10)
        pdf.set_xy(15, 40)
        recipient = [invoice.customer_name or tr("invoice.customer_fallback")]
        address = invoice.customer_address or {}
        for key in ("line1", "line2"):
            if address.get(key):
                recipient.append(str(address[key]))
        city = " ".join(str(address[k]) for k in ("postal_code", "city") if address.get(k))
        if city:
            recipient.append(city)
        if address.get("country"):
            recipient.append(str(address["country"]))
        if invoice.customer_email:
            recipient.append(invoice.customer_email)
        pdf.multi_cell(90, 5, text("\n".join(recipient)))

        # meta box
        meta = [
            (tr("invoice.number"), invoice.number),
            (tr("invoice.date"), invoice.issued_at.strftime("%d.%m.%Y")),
            (tr("invoice.order"), f"#{order.number}"),
            (tr("invoice.customer_id"), str(order.customer_id)),
        ]
        y = 40
        for label, value in meta:
            pdf.set_xy(120, y)
            pdf.set_font(font, size=9)
            pdf.cell(35, 5, text(label))
            pdf.cell(40, 5, text(value), align="R")
            y += 5

        pdf.set_xy(15, 78)
        pdf.set_font(font, size=15)
        pdf.cell(0, 8, text(tr("invoice.title")))
        pdf.ln(12)

        # items
        pdf.set_font(font, size=9)
        pdf.set_fill_color(240, 243, 248)
        widths = (15, 105, 30, 30)
        headers = (tr("invoice.qty"), tr("invoice.description"), tr("invoice.unit_price"), tr("invoice.amount"))
        pdf.set_x(15)
        for width, header in zip(widths, headers, strict=True):
            pdf.cell(width, 7, text(header), border=0, fill=True, align="R" if header != headers[1] else "L")
        pdf.ln(7)
        rows: list[tuple[str, str, str, str]] = [
            (str(i.quantity), i.name, money(i.unit_price_cents), money(i.total_cents)) for i in order.items
        ]
        if order.express_cents:
            rows.append(("1", tr("invoice.express"), money(order.express_cents), money(order.express_cents)))
        if order.seasonal_cents:
            rows.append(("1", tr("invoice.seasonal"), money(order.seasonal_cents), money(order.seasonal_cents)))
        if order.discount_cents:
            label = tr("invoice.discount", code=order.coupon_code or "")
            rows.append(("", label, "", f"-{money(order.discount_cents)}"))
        for qty, name, unit, total in rows:
            pdf.set_x(15)
            pdf.cell(widths[0], 6, text(qty), align="R")
            pdf.cell(widths[1], 6, text("  " + name[:70]))
            pdf.cell(widths[2], 6, text(unit), align="R")
            pdf.cell(widths[3], 6, text(total), align="R")
            pdf.ln(6)
        pdf.ln(2)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(110, pdf.get_y(), 195, pdf.get_y())
        pdf.ln(2)

        def total_line(label: str, value: str, bold: bool = False) -> None:
            pdf.set_x(110)
            pdf.set_font(font, size=11 if bold else 9)
            pdf.cell(55, 6, text(label))
            pdf.cell(30, 6, text(value), align="R")
            pdf.ln(6)

        if invoice.tax_cents:
            total_line(tr("invoice.net"), money(invoice.net_cents))
            total_line(tr("invoice.vat", rate=f"{invoice.tax_rate:g}"), money(invoice.tax_cents))
        total_line(tr("invoice.total"), money(invoice.gross_cents), bold=True)
        pdf.ln(6)
        pdf.set_font(font, size=9)
        pdf.set_x(15)
        notes = [tr("invoice.paid_note")]
        if cfg.small_business:
            notes.append(tr("invoice.small_business"))
        pdf.multi_cell(180, 5, text("\n".join(notes)))

        # footer
        footer = [company.name, *company.address_lines]
        contact = [v for v in (company.email, company.website) if v]
        tax_ids = []
        if company.tax_number:
            tax_ids.append(f"{tr('invoice.tax_number')}: {company.tax_number}")
        if company.vat_id:
            tax_ids.append(f"{tr('invoice.vat_id')}: {company.vat_id}")
        pdf.set_y(-35)
        pdf.set_font(font, size=7)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 4, text(" · ".join(footer + contact)), align="C")
        if tax_ids or company.bank_lines:
            pdf.multi_cell(0, 4, text(" · ".join(tax_ids + list(company.bank_lines))), align="C")
        return bytes(pdf.output())

    def _font(self, pdf: Any) -> str:
        configured = self.config.payments.invoice.font_path
        candidates = ([configured] if configured else []) + list(_FONT_CANDIDATES)
        for path in candidates:
            if path and Path(path).is_file():
                pdf.add_font("InvoiceFont", "", str(path))
                return "InvoiceFont"
        return "Helvetica"
