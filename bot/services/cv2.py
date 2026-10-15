"""Components V2 builder: template schema (validated JSON), storage and rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import asyncpg
import discord
from discord import ui
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ..core.db import Database
from ..core.errors import NotFound, UserError
from ..ui.builder import CV2, Theme, button

_HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")
_URL = re.compile(r"^https?://\S+$")
MAX_BLOCKS = 15
MAX_PREVIEW_COMPONENTS = 24
#: preview and editor share Discord's 4000 character limit per message
MAX_TEMPLATE_TEXT = 3200


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeadingBlock(_Block):
    type: Literal["heading"] = "heading"
    text: str = Field(min_length=1, max_length=200)
    emoji: str | None = None
    subline: str | None = Field(default=None, max_length=200)


class TextBlock(_Block):
    type: Literal["text"] = "text"
    content: str = Field(min_length=1, max_length=3000)
    style: Literal["plain", "quote", "small"] = "plain"


class BannerBlock(_Block):
    type: Literal["banner"] = "banner"
    source: str = Field(min_length=1, max_length=500, description="URL or banner key from design.banners")


class ImageBlock(_Block):
    type: Literal["image"] = "image"
    url: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not _URL.match(value):
            raise ValueError("invalid url")
        return value


class SeparatorBlock(_Block):
    type: Literal["separator"] = "separator"
    large: bool = False
    visible: bool = True


class LinkButton(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=500)
    emoji: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not _URL.match(value):
            raise ValueError("invalid url")
        return value


class ButtonsBlock(_Block):
    type: Literal["buttons"] = "buttons"
    items: list[LinkButton] = Field(min_length=1, max_length=5)


Block = Annotated[HeadingBlock | TextBlock | BannerBlock | ImageBlock | SeparatorBlock | ButtonsBlock,
                  Field(discriminator="type")]
BLOCK_TYPES: tuple[str, ...] = ("heading", "text", "banner", "image", "separator", "buttons")


class TemplateData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accent: str | None = None
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)

    @field_validator("accent")
    @classmethod
    def _accent(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _HEX.match(value):
            raise ValueError("invalid hex colour")
        return value if value.startswith("#") else f"#{value}"


_template_adapter = TypeAdapter(TemplateData)


@dataclass(slots=True)
class Template:
    id: int
    guild_id: int
    name: str
    data: TemplateData
    created_by: int | None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Template:
        return cls(row["id"], row["guild_id"], row["name"], _template_adapter.validate_python(row["data"] or {}),
                   row["created_by"])


def render_template(theme: Theme, data: TemplateData, *, view: ui.LayoutView | None = None) -> ui.LayoutView:
    """Render template blocks into a single container (optionally into an existing view)."""
    accent = int(data.accent.lstrip("#"), 16) if data.accent else None
    b = CV2(theme, accent=accent, view=view)
    for block in data.blocks:
        if isinstance(block, HeadingBlock):
            b.heading(block.text, emoji=block.emoji, subline=block.subline)
        elif isinstance(block, TextBlock):
            if block.style == "quote":
                b.quote(block.content)
            elif block.style == "small":
                b.small(block.content)
            else:
                b.text(block.content)
            b.block_break()
        elif isinstance(block, BannerBlock):
            url = block.source if _URL.match(block.source) else theme.assets.banner(block.source)
            if url:
                b.media(url)
        elif isinstance(block, ImageBlock):
            b.media(block.url, description=block.description)
        elif isinstance(block, SeparatorBlock):
            b.separator(large=block.large, visible=block.visible)
        elif isinstance(block, ButtonsBlock):
            b.actions(*[button(theme, item.label, emoji=item.emoji, url=item.url) for item in block.items])
    return b.build()


def text_length(theme: Theme, data: TemplateData) -> int:
    """Characters Discord counts for the rendered template (quote/small prefixes, resolved emojis included)."""
    view = ui.LayoutView(timeout=None)
    render_template(theme, data, view=view)
    return sum(len(item.content) for item in view.walk_children() if isinstance(item, ui.TextDisplay))


def fits(theme: Theme, data: TemplateData) -> bool:
    return component_count(theme, data) <= MAX_PREVIEW_COMPONENTS and text_length(theme, data) <= MAX_TEMPLATE_TEXT


def component_count(theme: Theme, data: TemplateData) -> int:
    view = ui.LayoutView(timeout=None)
    render_template(theme, data, view=view)
    return view._total_children


def parse_button_lines(text: str) -> list[LinkButton]:
    """``Label | https://url | emoji`` per line."""
    items: list[LinkButton] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            raise UserError("cv2.errors.button_format")
        try:
            items.append(LinkButton(label=parts[0], url=parts[1], emoji=parts[2] if len(parts) > 2 and parts[2] else None))
        except ValueError as exc:
            raise UserError("cv2.errors.button_format") from exc
    if not items or len(items) > 5:
        raise UserError("cv2.errors.button_format")
    return items


def make_block(kind: str, values: dict[str, Any]) -> Any:
    """Build and validate a block from modal values."""
    try:
        if kind == "heading":
            return HeadingBlock(text=values["text"], emoji=values.get("emoji") or None,
                                subline=values.get("subline") or None)
        if kind == "text":
            style = (values.get("style") or ["plain"])[0]
            return TextBlock(content=values["content"], style=style)
        if kind == "banner":
            return BannerBlock(source=values["source"])
        if kind == "image":
            return ImageBlock(url=values["url"], description=values.get("description") or None)
        if kind == "separator":
            large = (values.get("size") or ["small"])[0] == "large"
            visible = (values.get("visible") or ["yes"])[0] == "yes"
            return SeparatorBlock(large=large, visible=visible)
        if kind == "buttons":
            return ButtonsBlock(items=parse_button_lines(values["lines"]))
    except ValueError as exc:
        raise UserError("cv2.errors.invalid_block") from exc
    raise UserError("cv2.errors.invalid_block")


class TemplateService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, template_id: int, guild_id: int | None = None) -> Template:
        row = await self.db.fetchrow(
            "SELECT * FROM cv2_templates WHERE id = $1 AND ($2::bigint IS NULL OR guild_id = $2)", template_id, guild_id
        )
        if row is None:
            raise NotFound("cv2.errors.not_found")
        return Template.from_row(row)

    async def by_name(self, guild_id: int, name: str) -> Template:
        row = await self.db.fetchrow("SELECT * FROM cv2_templates WHERE guild_id = $1 AND lower(name) = lower($2)",
                                     guild_id, name)
        if row is None:
            raise NotFound("cv2.errors.not_found")
        return Template.from_row(row)

    async def list_templates(self, guild_id: int) -> list[asyncpg.Record]:
        return list(await self.db.fetch(
            "SELECT t.id, t.name, t.updated_at, jsonb_array_length(t.data->'blocks') AS blocks, "
            "(SELECT count(*) FROM cv2_messages m WHERE m.template_id = t.id) AS sent "
            "FROM cv2_templates t WHERE t.guild_id = $1 ORDER BY t.updated_at DESC", guild_id))

    async def create(self, guild_id: int, name: str, user_id: int, data: TemplateData | None = None) -> Template:
        await self.db.ensure_guild(guild_id)
        payload = (data or TemplateData()).model_dump(mode="json")
        try:
            row = await self.db.fetchrow(
                "INSERT INTO cv2_templates (guild_id, name, data, created_by, updated_by) VALUES ($1, $2, $3, $4, $4) "
                "RETURNING *",
                guild_id, name.strip(), payload, user_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("cv2.errors.name_taken", name=name) from exc
        assert row is not None
        return Template.from_row(row)

    async def save(self, template: Template, user_id: int) -> Template:
        await self.db.execute("UPDATE cv2_templates SET data = $2, updated_by = $3 WHERE id = $1",
                              template.id, template.data.model_dump(mode="json"), user_id)
        return await self.get(template.id)

    async def rename(self, template: Template, name: str) -> None:
        try:
            await self.db.execute("UPDATE cv2_templates SET name = $2 WHERE id = $1", template.id, name.strip())
        except asyncpg.UniqueViolationError as exc:
            raise UserError("cv2.errors.name_taken", name=name) from exc

    async def delete(self, template: Template) -> None:
        await self.db.execute("DELETE FROM cv2_templates WHERE id = $1", template.id)

    async def record_message(self, template: Template, message: discord.Message, user_id: int) -> None:
        await self.db.execute(
            "INSERT INTO cv2_messages (template_id, guild_id, channel_id, message_id, sent_by) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (message_id) DO UPDATE SET template_id = EXCLUDED.template_id",
            template.id, template.guild_id, message.channel.id, message.id, user_id,
        )

    async def messages(self, template: Template) -> list[asyncpg.Record]:
        return list(await self.db.fetch("SELECT channel_id, message_id FROM cv2_messages WHERE template_id = $1",
                                        template.id))

    async def forget_message(self, message_id: int) -> None:
        await self.db.execute("DELETE FROM cv2_messages WHERE message_id = $1", message_id)
