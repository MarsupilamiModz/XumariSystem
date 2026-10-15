"""Small, dependency free helpers shared by all modules."""

from __future__ import annotations

import datetime as dt
import re
import secrets
import string
from collections.abc import Iterable, Iterator, Sequence
from typing import TypeVar
from zoneinfo import ZoneInfo

import discord

T = TypeVar("T")

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.IGNORECASE)
_DURATION_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
MAX_MONEY_CENTS = 100_000_000  # 1 000 000.00 – fits the INT columns with plenty of headroom


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def generate_code(length: int = 8, alphabet: str = _CODE_ALPHABET) -> str:
    """Cryptographically random code, e.g. ticket IDs (8 uppercase letters/digits)."""
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_duration(value: str) -> dt.timedelta | None:
    """Parse ``"1w2d3h4m5s"`` / ``"10m"`` / ``"2 d"`` into a timedelta. Returns None if invalid."""
    value = value.strip()
    if not value:
        return None
    matches = _DURATION_RE.findall(value)
    if not matches or _DURATION_RE.sub("", value).strip():
        return None
    seconds = sum(int(amount) * _DURATION_UNITS[unit.lower()] for amount, unit in matches)
    return dt.timedelta(seconds=seconds) if seconds > 0 else None


def parse_date(value: str) -> dt.date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def ts(moment: dt.datetime, style: str = "f") -> str:
    """Discord timestamp markup."""
    return discord.utils.format_dt(moment, style=style)  # type: ignore[arg-type]


def format_money(cents: int, currency: str = "EUR", locale: str = "de") -> str:
    negative = cents < 0
    cents = abs(cents)
    whole, frac = divmod(cents, 100)
    if locale == "de":
        whole_str = f"{whole:,}".replace(",", ".")
        amount = f"{whole_str},{frac:02d}"
    else:
        amount = f"{whole:,}.{frac:02d}"
    symbol = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}.get(currency.upper(), currency.upper())
    sign = "-" if negative else ""
    if locale == "de" or symbol in {"€", "CHF"}:
        return f"{sign}{amount} {symbol}"
    return f"{sign}{symbol}{amount}"


def parse_money(value: str) -> int | None:
    """``"12,50"`` / ``"12.50"`` / ``"12"`` → 1250 cents."""
    cleaned = value.strip().replace("€", "").replace("$", "").replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        amount = round(float(cleaned) * 100)
    except (ValueError, OverflowError):
        return None
    return amount if 0 <= amount <= MAX_MONEY_CENTS else None


def format_number(value: int | float, locale: str = "de") -> str:
    text = f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"
    if locale == "de":
        return text.replace(",", "X").replace(".", ",").replace("X", ".")
    return text


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def first(iterable: Iterable[T]) -> T | None:
    return next(iter(iterable), None)


def slugify(value: str, max_length: int = 90) -> str:
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
    text = value.lower()
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9\-_]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:max_length] or "channel"


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def progress_bar(current: float, total: float, length: int = 10, full: str = "🟦", empty: str = "⬜") -> str:
    ratio = 0.0 if total <= 0 else max(0.0, min(1.0, current / total))
    filled = round(ratio * length)
    return full * filled + empty * (length - filled)


def is_url(value: str | None) -> bool:
    return bool(value) and bool(re.match(r"^https?://\S+$", value or ""))


def escape_md(text: str) -> str:
    return discord.utils.escape_markdown(text)
