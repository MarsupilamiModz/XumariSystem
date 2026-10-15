"""JSON based localisation (DE/EN). No user facing text is hardcoded in Python code.

Usage::

    tr = bot.i18n.translator("de")
    tr("tickets.panel.title")                 # plain lookup
    tr("orders.number", number=493)           # str.format placeholders
    "{e_ticket}"                              # inside a locale string → emoji from the registry
    "{shop}"                                  # inside a locale string → configured shop name
"""

from __future__ import annotations

import json
import logging
import string
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import discord

_log = logging.getLogger("xumari.i18n")


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            out.update(_flatten(value, full))
        else:
            out[full] = value
    return out


class _FormatDict(dict[str, Any]):
    def __init__(self, values: Mapping[str, Any], emoji_lookup: Callable[[str], str] | None) -> None:
        super().__init__(values)
        self._emoji_lookup = emoji_lookup

    def __missing__(self, key: str) -> str:
        if key.startswith("e_") and self._emoji_lookup is not None:
            return self._emoji_lookup(key[2:])
        return "{" + key + "}"


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError, IndexError):
            return "{" + field_name + "}", field_name


_formatter = _SafeFormatter()


class I18n:
    def __init__(self, locales_dir: Path, default_locale: str = "de") -> None:
        self._dir = locales_dir
        self.default_locale = default_locale
        self._strings: dict[str, dict[str, Any]] = {}
        self.globals: dict[str, Any] = {}
        self.emoji_lookup: Callable[[str], str] | None = None
        self.load()

    def load(self) -> None:
        strings: dict[str, dict[str, Any]] = {}
        for file in sorted(self._dir.glob("*.json")):
            with file.open(encoding="utf-8") as fh:
                strings[file.stem] = _flatten(json.load(fh))
        if self.default_locale not in strings:
            raise RuntimeError(f"Default locale file {self.default_locale}.json not found in {self._dir}")
        self._strings = strings
        _log.info("Loaded locales: %s", ", ".join(sorted(strings)))

    @property
    def locales(self) -> list[str]:
        return sorted(self._strings)

    def has(self, key: str, locale: str | None = None) -> bool:
        return key in self._strings.get(locale or self.default_locale, {})

    def raw(self, key: str, locale: str | None = None) -> Any:
        loc = locale if locale in self._strings else self.default_locale
        if key in self._strings[loc]:
            return self._strings[loc][key]
        if key in self._strings[self.default_locale]:
            return self._strings[self.default_locale][key]
        _log.warning("Missing translation key %r", key)
        return key

    def t(self, key: str, locale: str | None = None, /, **kwargs: Any) -> str:
        value = self.raw(key, locale)
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        text = str(value)
        if "{" not in text:
            return text
        values = _FormatDict({**self.globals, **kwargs}, self.emoji_lookup)
        return _formatter.vformat(text, (), values)

    def translator(self, locale: str | None) -> Translator:
        return Translator(self, self.resolve_locale(locale))

    def resolve_locale(self, locale: str | discord.Locale | None) -> str:
        if locale is None:
            return self.default_locale
        value = locale.value if isinstance(locale, discord.Locale) else str(locale)
        short = value.split("-")[0].lower()
        if short in self._strings:
            return short
        return "en" if "en" in self._strings and short != self.default_locale else self.default_locale

    def missing_keys(self) -> dict[str, list[str]]:
        """Keys present in the default locale but missing in another (used by the self-check)."""
        base = set(self._strings[self.default_locale])
        return {loc: sorted(base - set(keys)) for loc, keys in self._strings.items() if base - set(keys)}


class Translator:
    """Locale-bound callable: ``tr("key", **kwargs)``."""

    __slots__ = ("_i18n", "locale")

    def __init__(self, i18n: I18n, locale: str) -> None:
        self._i18n = i18n
        self.locale = locale

    def __call__(self, key: str, /, **kwargs: Any) -> str:
        return self._i18n.t(key, self.locale, **kwargs)

    def list(self, key: str) -> list[str]:
        value = self._i18n.raw(key, self.locale)
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    def other(self) -> Translator:
        return Translator(self._i18n, "en" if self.locale == "de" else "de")
