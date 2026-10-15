"""HTML transcripts in Discord look (dark theme, avatars, role colours, attachments, CV2 content)."""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

from ...services.storage import LocalStorage, R2Storage

if TYPE_CHECKING:
    from ...core.bot import XumariBot
    from .service import Ticket, TicketService

_log = logging.getLogger("xumari.transcripts")
MAX_MESSAGES = 5000


@dataclass(slots=True)
class _Attachment:
    name: str
    url: str
    is_image: bool
    size: int


@dataclass(slots=True)
class _Message:
    author: str
    avatar: str
    color: str
    bot: bool
    timestamp: dt.datetime
    content: str
    attachments: list[_Attachment] = field(default_factory=list)
    embeds: list[tuple[str, str]] = field(default_factory=list)
    edited: bool = False


@dataclass(slots=True)
class TranscriptResult:
    file_name: str
    url: str | None
    data: bytes
    message_count: int

    def as_file(self) -> discord.File:
        return discord.File(io.BytesIO(self.data), filename=self.file_name)


# ---------------------------------------------------------------------------------- markdown
_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDERLINE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])|(?<![_\w])_(?!\s)(.+?)(?<!\s)_(?![_\w])")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_URL = re.compile(r"(?<![\"'=>])(https?://[^\s<]+)")
_CUSTOM_EMOJI = re.compile(r"&lt;(a?):(\w+):(\d+)&gt;")


def _markdown(text: str) -> str:
    """Escape and render the Discord markdown subset to HTML."""
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    escaped = html.escape(text)
    escaped = _CODE_BLOCK.sub(lambda m: stash(f"<pre><code>{m.group(1)}</code></pre>"), escaped)
    escaped = _INLINE_CODE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), escaped)
    escaped = _CUSTOM_EMOJI.sub(
        lambda m: stash(
            f'<img class="emoji" alt=":{m.group(2)}:" src="https://cdn.discordapp.com/emojis/{m.group(3)}.'
            f'{"gif" if m.group(1) else "png"}">'
        ),
        escaped,
    )
    escaped = _LINK.sub(lambda m: stash(f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'), escaped)
    escaped = _URL.sub(lambda m: stash(f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'), escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _UNDERLINE.sub(r"<u>\1</u>", escaped)
    escaped = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
    escaped = _STRIKE.sub(r"<s>\1</s>", escaped)

    lines_out: list[str] = []
    for line in escaped.split("\n"):
        if line.startswith("### "):
            lines_out.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            lines_out.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            lines_out.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("-# "):
            lines_out.append(f"<small>{line[3:]}</small>")
        elif line.startswith("&gt; -# "):
            lines_out.append(f"<blockquote><small>{line[8:]}</small></blockquote>")
        elif line.startswith("&gt; ") or line == "&gt;":
            lines_out.append(f"<blockquote>{line[5:]}</blockquote>")
        else:
            lines_out.append(line + "<br>")
    rendered = "".join(lines_out)
    for index, value in enumerate(placeholders):
        rendered = rendered.replace(f"\x00{index}\x00", value)
    return rendered


def _resolve_mentions(text: str, guild: discord.Guild) -> str:
    def user(m: re.Match[str]) -> str:
        member = guild.get_member(int(m.group(1)))
        return f"@{member.display_name}" if member else "@unknown"

    def channel(m: re.Match[str]) -> str:
        ch = guild.get_channel(int(m.group(1)))
        return f"#{ch.name}" if ch else "#deleted-channel"

    def role(m: re.Match[str]) -> str:
        r = guild.get_role(int(m.group(1)))
        return f"@{r.name}" if r else "@deleted-role"

    text = re.sub(r"<@!?(\d+)>", user, text)
    text = re.sub(r"<#(\d+)>", channel, text)
    return re.sub(r"<@&(\d+)>", role, text)


def _component_text(component: Any) -> list[str]:
    """Recursively collect text from Components V2 (TextDisplay, Section, Container, …)."""
    out: list[str] = []
    content = getattr(component, "content", None)
    if isinstance(content, str) and content:
        out.append(content)
    for attr in ("children", "components"):
        for child in getattr(component, attr, None) or []:
            out.extend(_component_text(child))
    accessory = getattr(component, "accessory", None)
    if accessory is not None and getattr(accessory, "label", None):
        out.append(f"[{accessory.label}]")
    return out


_CSS = """
*{box-sizing:border-box}body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 'gg sans','Segoe UI',Arial,sans-serif}
header{background:#2b2d31;padding:24px 32px;border-bottom:1px solid #1e1f22;display:flex;gap:18px;align-items:center}
header img{width:64px;height:64px;border-radius:50%}header h1{margin:0;font-size:22px;color:#f2f3f5}
header .meta{color:#b5bac1;font-size:13px;margin-top:4px}.meta b{color:#f2f3f5}
main{padding:16px 0}.msg{display:flex;gap:16px;padding:6px 32px;margin-top:12px}.msg:hover{background:#2e3035}
.msg img.avatar{width:40px;height:40px;border-radius:50%;flex:none}.body{min-width:0;flex:1}
.name{font-weight:600}.badge{background:#5865f2;color:#fff;font-size:10px;border-radius:3px;padding:1px 4px;margin-left:4px;vertical-align:middle}
.time{color:#949ba4;font-size:12px;margin-left:8px}.content{white-space:normal;word-wrap:break-word;margin-top:2px}
.content h2,.content h3,.content h4{margin:6px 0;color:#f2f3f5}blockquote{margin:2px 0;padding-left:10px;border-left:4px solid #4e5058}
small{color:#949ba4;font-size:12px}code{background:#2b2d31;padding:1px 4px;border-radius:3px;font-family:Consolas,monospace}
pre{background:#2b2d31;padding:8px;border-radius:4px;overflow:auto}a{color:#00a8fc}img.emoji{width:22px;height:22px;vertical-align:middle}
.att img{max-width:420px;max-height:320px;border-radius:6px;margin-top:6px;display:block}
.att a.file{display:inline-block;margin-top:6px;background:#2b2d31;border:1px solid #1e1f22;padding:10px 14px;border-radius:6px}
.embed{border-left:4px solid {accent};background:#2b2d31;border-radius:4px;padding:8px 12px;margin-top:6px;max-width:520px}
.embed .t{font-weight:600;color:#f2f3f5}footer{color:#949ba4;font-size:12px;padding:24px 32px;border-top:1px solid #1e1f22}
"""


def _render(
    *, guild_name: str, guild_icon: str, channel_name: str, ticket_code: str, category: str, owner: str,
    created: dt.datetime, closed: dt.datetime, messages: list[_Message], accent: str, shop: str,
) -> str:
    parts: list[str] = [
        "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Transcript {html.escape(ticket_code)} – {html.escape(shop)}</title>",
        f"<style>{_CSS.replace('{accent}', accent)}</style></head><body>",
        "<header>",
        f"<img src='{html.escape(guild_icon)}' alt=''>" if guild_icon else "",
        f"<div><h1>#{html.escape(channel_name)}</h1><div class='meta'>{html.escape(guild_name)} · "
        f"Ticket <b>{html.escape(ticket_code)}</b> · <b>{html.escape(category)}</b> · {html.escape(owner)}<br>"
        f"{created:%d.%m.%Y %H:%M} – {closed:%d.%m.%Y %H:%M} UTC · {len(messages)} messages</div></div>",
        "</header><main>",
    ]
    for msg in messages:
        badge = "<span class='badge'>BOT</span>" if msg.bot else ""
        edited = " <small>(edited)</small>" if msg.edited else ""
        parts.append(
            f"<div class='msg'><img class='avatar' src='{html.escape(msg.avatar)}' alt=''><div class='body'>"
            f"<span class='name' style='color:{msg.color}'>{html.escape(msg.author)}</span>{badge}"
            f"<span class='time'>{msg.timestamp:%d.%m.%Y %H:%M}</span>"
            f"<div class='content'>{_markdown(msg.content)}{edited}</div>"
        )
        for title, description in msg.embeds:
            parts.append(f"<div class='embed'><div class='t'>{_markdown(title)}</div>{_markdown(description)}</div>")
        if msg.attachments:
            parts.append("<div class='att'>")
            for att in msg.attachments:
                url = html.escape(att.url)
                if att.is_image:
                    parts.append(f"<a href='{url}' target='_blank'><img src='{url}' alt='{html.escape(att.name)}'></a>")
                else:
                    size_kb = max(1, att.size // 1024)
                    parts.append(f"<a class='file' href='{url}' target='_blank'>📎 {html.escape(att.name)} ({size_kb} KB)</a>")
            parts.append("</div>")
        parts.append("</div></div>")
    parts.append(f"</main><footer>{html.escape(shop)} · Transcript generated {closed:%d.%m.%Y %H:%M} UTC</footer>")
    parts.append("</body></html>")
    return "".join(parts)


class TranscriptService:
    def __init__(self, bot: XumariBot, tickets: TicketService) -> None:
        self.bot = bot
        self.tickets = tickets

    async def _collect(self, channel: discord.TextChannel) -> list[_Message]:
        guild = channel.guild
        messages: list[_Message] = []
        async for message in channel.history(limit=MAX_MESSAGES, oldest_first=True):
            author = message.author
            colour = getattr(author, "colour", discord.Colour.default())
            content = message.content or ""
            if message.components:
                extracted = [t for c in message.components for t in _component_text(c)]
                content = "\n".join(filter(None, [content, *extracted]))
            content = _resolve_mentions(content, guild)
            messages.append(
                _Message(
                    author=getattr(author, "display_name", author.name),
                    avatar=author.display_avatar.replace(size=64).url,
                    color=str(colour) if colour.value else "#f2f3f5",
                    bot=author.bot,
                    timestamp=message.created_at,
                    content=content,
                    attachments=[
                        _Attachment(a.filename, a.url, bool(a.content_type and a.content_type.startswith("image/")), a.size)
                        for a in message.attachments
                    ],
                    embeds=[(e.title or "", e.description or "") for e in message.embeds if e.title or e.description],
                    edited=message.edited_at is not None,
                )
            )
        return messages

    async def create(self, ticket: Ticket, channel: discord.TextChannel, category_name: str) -> TranscriptResult:
        cfg = self.bot.config.tickets.transcripts
        messages = await self._collect(channel)
        owner = channel.guild.get_member(ticket.owner_id)
        guild = channel.guild
        html_text = await asyncio.to_thread(
            _render,
            guild_name=guild.name,
            guild_icon=guild.icon.url if guild.icon else "",
            channel_name=channel.name,
            ticket_code=ticket.code,
            category=category_name,
            owner=f"{owner} ({ticket.owner_id})" if owner else str(ticket.owner_id),
            created=ticket.created_at,
            closed=dt.datetime.now(dt.UTC),
            messages=messages,
            accent=self.bot.config.design.accent_color,
            shop=self.bot.config.bot.shop_name,
        )
        data = html_text.encode("utf-8")
        file_name = f"{ticket.code}-{secrets.token_urlsafe(12)}.html"
        storage = cfg.storage
        url: str | None = None
        if storage == "r2" and not self.bot.secrets.r2_configured:
            _log.warning("Transcript storage 'r2' selected but R2 credentials are missing – using 'local'")
            storage = "local"
        if storage == "r2":
            url = await R2Storage(self.bot.secrets).upload(
                f"transcripts/{ticket.guild_id}/{file_name}", data, "text/html; charset=utf-8"
            )
        elif storage == "local":
            await LocalStorage(cfg.local_dir).write(file_name, data)
            if self.bot.api_mode() != "disabled":
                url = f"{self.bot.config.api.public_url.rstrip('/')}/transcripts/{file_name}"
        await self.tickets.save_transcript(
            ticket.id, storage=storage, url=url, file_name=file_name, size=len(data), messages=len(messages)
        )
        return TranscriptResult(file_name=file_name, url=url, data=data, message_count=len(messages))
