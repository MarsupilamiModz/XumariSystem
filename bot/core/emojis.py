"""Central emoji registry.

Every emoji the bot shows goes through this registry. Buyers replace the Unicode fallbacks with
their own custom emojis in ``config.yaml`` → ``design.emojis`` (e.g. ``ticket: "<:ticket:1234567890>"``).
The full list of keys is documented in ``ASSETS.md``.
"""

from __future__ import annotations

import logging

import discord

_log = logging.getLogger("xumari.emojis")

DEFAULT_EMOJIS: dict[str, str] = {
    # generic
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "dot": "▸",
    "arrow": "➜",
    "language": "🌐",
    "guide": "📖",
    "clock": "🕒",
    "calendar": "📅",
    "user": "👤",
    "users": "👥",
    "message": "💬",
    "file": "📎",
    "link": "🔗",
    "settings": "⚙️",
    "back": "⬅️",
    "next": "➡️",
    "first": "⏮️",
    "last": "⏭️",
    "refresh": "🔄",
    "add": "➕",
    "remove": "➖",
    "edit": "✏️",
    "save": "💾",
    "send": "📨",
    "cancel": "✖️",
    "confirm": "✔️",
    "search": "🔎",
    "stats": "📊",
    "toggle_on": "🟢",
    "toggle_off": "🔴",
    "lock": "🔒",
    "unlock": "🔓",
    "bell": "🔔",
    "shield": "🛡️",
    # tickets
    "ticket": "🎫",
    "close": "🔒",
    "reopen": "🔓",
    "delete": "🗑️",
    "transcript": "📜",
    "support": "🛟",
    "partnership": "🤝",
    "question": "❓",
    "ai": "🤖",
    "holiday": "🏖️",
    "blacklist": "⛔",
    # shop / orders
    "shop": "🏪",
    "order": "🛒",
    "cart": "🛍️",
    "product": "📦",
    "bundle": "🎁",
    "coupon": "🏷️",
    "express": "⚡",
    "standard": "🚚",
    "season": "🎄",
    "money": "💶",
    "payment": "💳",
    "invoice": "🧾",
    "queue": "📋",
    "designer": "🎨",
    "todo": "☑️",
    "todo_open": "⬜",
    "todo_done": "✅",
    "showcase": "🖼️",
    "faq": "💡",
    "legal": "⚖️",
    # reviews
    "star": "⭐",
    "star_empty": "✩",
    "review": "📝",
    "heart": "💙",
    # downloads
    "download": "📥",
    "password": "🔑",
    "preview": "▶️",
    "category": "🏷️",
    "copy": "📋",
    "version": "🔖",
    "size": "💾",
    "script": "📜",
    "vehicle": "🚗",
    "map": "🗺️",
    "graphics": "🖌️",
    # economy / games
    "coin": "🪙",
    "bank": "🏦",
    "daily": "🎁",
    "work": "💼",
    "crime": "🕶️",
    "rob": "🦹",
    "trophy": "🏆",
    "cards": "🃏",
    "hit": "➕",
    "stand": "✋",
    "double": "✖️",
    "mine": "💣",
    "gem": "💎",
    "tile": "⬛",
    "cashout": "💰",
    # levels / invites / community
    "level": "⭐",
    "xp": "✨",
    "boost": "🚀",
    "invite": "📨",
    "giveaway": "🎉",
    "captcha": "🧩",
    "welcome": "👋",
    "role": "🎭",
    "reminder": "⏰",
    "modmail": "📬",
    "ghost": "👻",
    "counter": "🔢",
    "sync": "🔁",
    "cv2": "🧱",
    "text": "📝",
    "image": "🖼️",
    "separator": "➖",
    "button": "🔘",
    "color": "🎨",
    "template": "📐",
    "bar_full": "🟦",
    "bar_empty": "⬜",
}


class EmojiRegistry:
    """Resolves emoji keys to their configured custom emoji or a Unicode fallback."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._emojis: dict[str, str] = {**DEFAULT_EMOJIS, **(overrides or {})}
        self._warned: set[str] = set()

    def __contains__(self, name: object) -> bool:
        return name in self._emojis

    def __getitem__(self, name: str) -> str:
        return self.get(name)

    def __call__(self, name: str) -> str:
        return self.get(name)

    def get(self, name: str, fallback: str | None = None) -> str:
        value = self._emojis.get(name)
        if value is not None:
            return value
        if fallback is not None:
            return fallback
        if name not in self._warned:
            self._warned.add(name)
            _log.warning("Unknown emoji key %r – falling back to %r", name, DEFAULT_EMOJIS["dot"])
        return DEFAULT_EMOJIS["dot"]

    def resolve(self, value: str) -> str:
        """Accept either a registry key or a literal emoji (``"🎫"`` / ``"<:x:123>"``)."""
        if value in self._emojis:
            return self._emojis[value]
        return value

    def partial(self, name_or_emoji: str) -> discord.PartialEmoji:
        """``PartialEmoji`` for buttons/select options (custom ``<:n:id>`` or Unicode)."""
        return discord.PartialEmoji.from_str(self.resolve(name_or_emoji))

    def keys(self) -> list[str]:
        return sorted(self._emojis)
