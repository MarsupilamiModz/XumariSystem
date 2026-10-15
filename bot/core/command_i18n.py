"""Localised slash-command descriptions.

Command names are fixed (as specified), but every description, parameter description and choice
name is taken from ``locales/*.json → commands.*`` and delivered to Discord per client language via
an :class:`app_commands.Translator`::

    @app_commands.command(name="close", description=T("close"))
    @app_commands.describe(reason=T("close.reason"))
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
_FALLBACK_LOCALE = "en"

_DISCORD_LOCALES: dict[discord.Locale, str] = {
    discord.Locale.german: "de",
    discord.Locale.american_english: "en",
    discord.Locale.british_english: "en",
}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, full))
        else:
            out[full] = str(value)
    return out


@lru_cache(maxsize=4)
def _strings(locale: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return _flatten(json.load(fh).get("commands", {}))


def _lookup(locale: str, key: str) -> str | None:
    """``key`` or – for nodes that also have children – ``key._``."""
    strings = _strings(locale)
    return strings.get(key) or strings.get(f"{key}._")


def T(key: str) -> app_commands.locale_str:
    """Command text with the English default and a translation key."""
    text = _lookup(_FALLBACK_LOCALE, key) or key
    return app_commands.locale_str(text[:100], key=key)


class CommandTranslator(app_commands.Translator):
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        key = string.extras.get("key")
        target = _DISCORD_LOCALES.get(locale)
        if key is None or target is None:
            return None
        value = _lookup(target, key)
        return value[:100] if value else None
