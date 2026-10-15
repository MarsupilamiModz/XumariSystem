"""Components V2 message builder implementing the XumariSystem design system.

Every message follows the same structure::

    [optional plain TextDisplay above the container – e.g. a user ping]
    Container (accent colour → blue bar on the left)
      MediaGallery   banner
      Separator
      Section/Text   "## <emoji> Title"            (+ accessory button on the right)
      Text           "> subtitle"                  (muted quote)
      Text           content, **bold** keywords, emoji bullet lists, "> " sub points
      Text           "> -# footer"                 (small print in a quote)
      Separator
      ActionRow(s)   buttons / selects

Consecutive text blocks are merged into a single ``TextDisplay`` to stay well below Discord's
40 component limit; the 4000 character text budget per message is enforced by truncation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import ui

from ..core.assets import AssetRegistry
from ..core.emojis import EmojiRegistry

TEXT_BUDGET = 4000
_TRUNCATION_MARK = "…"


@dataclass(slots=True)
class Theme:
    accent: int
    emojis: EmojiRegistry
    assets: AssetRegistry
    shop_name: str


@dataclass(slots=True)
class Bullet:
    """``{emoji} **{label}** {value}`` followed by quoted sub points."""

    label: str
    value: str | None = None
    emoji: str | None = None
    sub: Sequence[str] = field(default_factory=tuple)
    separator: str = ":"


class StaticLayoutView(ui.LayoutView):
    """A layout that is *sent and forgotten*.

    Interactive children use routed ``xs:`` custom IDs and are handled by the global
    :class:`~bot.core.router.ComponentRouter`, so the view itself does not need to be stored in the
    view store (prevents unbounded memory growth for thousands of persistent messages).
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    def is_dispatchable(self) -> bool:
        return False


class CV2:
    """Fluent builder. ``CV2(theme).banner("tickets").heading(...).quote(...).actions(...).build()``"""

    def __init__(self, theme: Theme, *, accent: int | None = None, view: ui.LayoutView | None = None) -> None:
        self.theme = theme
        self._accent = theme.accent if accent is None else accent
        self._view = view
        self._above: list[ui.Item[Any]] = []
        self._below: list[ui.Item[Any]] = []
        self._items: list[ui.Item[Any]] = []
        self._text: list[str] = []
        self._budget = TEXT_BUDGET

    # ------------------------------------------------------------------ text helpers
    def _consume(self, text: str) -> str:
        if self._budget <= 0:
            return ""
        if len(text) > self._budget:
            text = text[: max(0, self._budget - len(_TRUNCATION_MARK))] + _TRUNCATION_MARK
        self._budget -= len(text)
        return text

    def _flush(self) -> None:
        if self._text:
            content = self._consume("\n".join(self._text))
            if content:
                self._items.append(ui.TextDisplay(content))
            self._text = []

    def _push(self, item: ui.Item[Any]) -> CV2:
        self._flush()
        self._items.append(item)
        return self

    @staticmethod
    def quote_lines(text: str) -> str:
        return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines() or [""])

    # ------------------------------------------------------------------ outside of the container
    def mention(self, text: str) -> CV2:
        """Plain text above the container (e.g. ``<@user>`` ping)."""
        self._above.append(ui.TextDisplay(self._consume(text)))
        return self

    def below(self, item: ui.Item[Any]) -> CV2:
        self._below.append(item)
        return self

    # ------------------------------------------------------------------ media
    def banner(self, key: str | None, *, separator: bool = True) -> CV2:
        url = self.theme.assets.banner(key)
        if url:
            self._push(ui.MediaGallery(discord.MediaGalleryItem(url)))
            if separator:
                self.separator()
        return self

    def media(self, *urls: str, description: str | None = None) -> CV2:
        items = [discord.MediaGalleryItem(u, description=description) for u in urls if u][:10]
        if items:
            self._push(ui.MediaGallery(*items))
        return self

    def file(self, filename: str) -> CV2:
        """Show an uploaded attachment (CV2 messages only display referenced attachments)."""
        return self._push(ui.File(f"attachment://{filename}"))

    def separator(self, *, large: bool = False, visible: bool = True) -> CV2:
        spacing = discord.SeparatorSpacing.large if large else discord.SeparatorSpacing.small
        return self._push(ui.Separator(visible=visible, spacing=spacing))

    # ------------------------------------------------------------------ headings / text
    def heading(
        self,
        title: str,
        *,
        emoji: str | None = None,
        level: int = 2,
        subline: str | None = None,
        accessory: ui.Item[Any] | None = None,
    ) -> CV2:
        prefix = "#" * max(1, min(3, level))
        icon = f"{self.theme.emojis.resolve(emoji)} " if emoji else ""
        text = f"{prefix} {icon}{title}"
        if subline:
            text += f"\n-# {subline}"
        if accessory is not None:
            return self.section(text, accessory=accessory)
        self._text.append(text)
        return self

    def section(self, *texts: str, accessory: ui.Item[Any]) -> CV2:
        """A Section with up to three text blocks and an accessory (button or thumbnail)."""
        self._flush()
        displays = [ui.TextDisplay(self._consume(t)) for t in texts[:3] if t]
        self._items.append(ui.Section(*displays, accessory=accessory))
        return self

    def thumbnail_section(self, *texts: str, url: str) -> CV2:
        return self.section(*texts, accessory=ui.Thumbnail(url))

    def text(self, content: str | None) -> CV2:
        if content:
            self._text.append(content)
        return self

    def quote(self, content: str | None) -> CV2:
        if content:
            self._text.append(self.quote_lines(content))
        return self

    def small(self, content: str | None) -> CV2:
        if content:
            self._text.append("\n".join(f"-# {line}" for line in content.splitlines() if line.strip()))
        return self

    def footer(self, content: str | None) -> CV2:
        """Small print inside a quote block: ``> -# …``."""
        if content:
            self._text.append("\n".join(f"> -# {line}" for line in content.splitlines() if line.strip()))
        return self

    def bullets(self, items: Iterable[Bullet | str], *, default_emoji: str = "dot") -> CV2:
        lines: list[str] = []
        for entry in items:
            if isinstance(entry, str):
                lines.append(f"{self.theme.emojis.resolve(default_emoji)} {entry}")
                continue
            icon = self.theme.emojis.resolve(entry.emoji or default_emoji)
            line = f"{icon} **{entry.label}**"
            if entry.value is not None:
                line += f"{entry.separator} {entry.value}"
            lines.append(line)
            lines.extend(f"> {s}" for s in entry.sub if s)
        if lines:
            self._text.append("\n".join(lines))
        return self

    def block_break(self) -> CV2:
        """Force the next text into a new TextDisplay (visual spacing without a separator)."""
        self._flush()
        return self

    # ------------------------------------------------------------------ interactions
    def actions(self, *items: ui.Item[Any]) -> CV2:
        """Add one ActionRow (max. 5 buttons or exactly 1 select)."""
        if items:
            self._push(ui.ActionRow(*items))
        return self

    def action_rows(self, rows: Iterable[Sequence[ui.Item[Any]]]) -> CV2:
        for row in rows:
            self.actions(*row)
        return self

    def raw(self, item: ui.Item[Any]) -> CV2:
        return self._push(item)

    # ------------------------------------------------------------------ build
    def build(self) -> ui.LayoutView:
        self._flush()
        view = self._view if self._view is not None else StaticLayoutView()
        for item in self._above:
            view.add_item(item)
        if self._items:
            view.add_item(ui.Container(*self._items, accent_colour=discord.Colour(self._accent)))
        for item in self._below:
            view.add_item(item)
        return view


# ---------------------------------------------------------------------- component factories
def button(
    theme: Theme,
    label: str,
    *,
    emoji: str | None = None,
    custom_id: str | None = None,
    url: str | None = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> ui.Button[Any]:
    """Button with custom emoji + text (design rule: buttons always carry an emoji)."""
    partial = theme.emojis.partial(emoji) if emoji else None
    if url:
        return ui.Button(label=label, emoji=partial, url=url, style=discord.ButtonStyle.link, disabled=disabled)
    return ui.Button(label=label, emoji=partial, custom_id=custom_id, style=style, disabled=disabled)


def select_option(
    theme: Theme,
    label: str,
    value: str,
    *,
    description: str | None = None,
    emoji: str | None = None,
    default: bool = False,
) -> discord.SelectOption:
    return discord.SelectOption(
        label=label[:100],
        value=value[:100],
        description=(description or None) and description[:100],
        emoji=theme.emojis.partial(emoji) if emoji else None,
        default=default,
    )


def string_select(
    custom_id: str,
    options: Sequence[discord.SelectOption],
    *,
    placeholder: str | None = None,
    min_values: int = 1,
    max_values: int = 1,
    disabled: bool = False,
) -> ui.Select[Any]:
    opts = list(options)[:25]
    disabled = disabled or not opts
    if not opts:
        opts = [discord.SelectOption(label="—", value="__none__")]
    return ui.Select(
        custom_id=custom_id,
        options=opts,
        placeholder=placeholder,
        min_values=min(min_values, len(opts)),
        max_values=min(max_values, len(opts)),
        disabled=disabled,
    )
