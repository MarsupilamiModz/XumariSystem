"""Opening hours & holidays.

Schedule source (first match wins): ``guild_settings.opening_hours`` (edited in the control panel)
→ ``config.yaml → tickets.opening_hours.schedule``. Holidays: config list + ``holidays`` table.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...config.settings import WEEKDAYS
from ...core.i18n import Translator
from ...core.utils import tz

if TYPE_CHECKING:
    from ...core.bot import XumariBot


@dataclass(slots=True)
class HoursStatus:
    is_open: bool
    next_open: dt.datetime | None
    holiday: str | None


def _parse_span(span: str) -> tuple[dt.time, dt.time]:
    start, end = span.split("-")
    return dt.time.fromisoformat(start), dt.time.fromisoformat(end)


class OpeningHoursService:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.tickets.opening_hours

    @property
    def zone(self) -> dt.tzinfo:
        return tz(self.bot.config.bot.timezone)

    async def schedule(self, guild_id: int) -> dict[str, str | None]:
        override = await self.bot.db.fetchval(
            "SELECT opening_hours FROM guild_settings WHERE guild_id = $1", guild_id
        )
        base = dict(self.cfg.schedule)
        if isinstance(override, dict):
            base.update({k: v for k, v in override.items() if k in WEEKDAYS})
        return {day: base.get(day) for day in WEEKDAYS}

    async def set_day(self, guild_id: int, day: str, span: str | None) -> None:
        if day not in WEEKDAYS:
            raise ValueError(day)
        if span is not None:
            start, end = _parse_span(span)
            if end <= start:
                raise ValueError(span)
        await self.bot.db.ensure_guild(guild_id)
        await self.bot.db.execute(
            """
            UPDATE guild_settings
               SET opening_hours = COALESCE(opening_hours, '{}'::jsonb) || jsonb_build_object($2::text, $3::text)
             WHERE guild_id = $1
            """,
            guild_id, day, span,
        )

    async def reset(self, guild_id: int) -> None:
        await self.bot.db.execute("UPDATE guild_settings SET opening_hours = NULL WHERE guild_id = $1", guild_id)

    async def holidays(self, guild_id: int) -> dict[dt.date, str]:
        result: dict[dt.date, str] = {day: "" for day in self.cfg.holidays}
        rows = await self.bot.db.fetch("SELECT day, name FROM holidays WHERE guild_id = $1", guild_id)
        result.update({r["day"]: r["name"] for r in rows})
        return result

    async def add_holiday(self, guild_id: int, day: dt.date, name: str, created_by: int) -> None:
        await self.bot.db.ensure_guild(guild_id)
        await self.bot.db.execute(
            """
            INSERT INTO holidays (guild_id, day, name, created_by) VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, day) DO UPDATE SET name = EXCLUDED.name
            """,
            guild_id, day, name, created_by,
        )

    async def remove_holiday(self, guild_id: int, day: dt.date) -> bool:
        result = await self.bot.db.execute("DELETE FROM holidays WHERE guild_id = $1 AND day = $2", guild_id, day)
        return result.endswith(" 1")

    async def status(self, guild_id: int, now: dt.datetime | None = None) -> HoursStatus:
        if not self.cfg.enabled:
            return HoursStatus(True, None, None)
        local_now = (now or dt.datetime.now(dt.UTC)).astimezone(self.zone)
        schedule = await self.schedule(guild_id)
        holidays = await self.holidays(guild_id)

        today_holiday = holidays.get(local_now.date())
        if today_holiday is None:
            span = schedule.get(WEEKDAYS[local_now.weekday()])
            if span:
                start, end = _parse_span(span)
                if start <= local_now.time() < end:
                    return HoursStatus(True, None, None)

        for offset in range(0, 15):
            day = local_now.date() + dt.timedelta(days=offset)
            if day in holidays:
                continue
            span = schedule.get(WEEKDAYS[day.weekday()])
            if not span:
                continue
            start, _ = _parse_span(span)
            candidate = dt.datetime.combine(day, start, tzinfo=self.zone)
            if candidate > local_now:
                return HoursStatus(False, candidate, today_holiday or None)
        return HoursStatus(False, None, today_holiday or None)

    async def describe(self, guild_id: int, tr: Translator) -> str:
        """Human readable schedule, e.g. "09:00 bis 18:00 Uhr, montags bis freitags"."""
        schedule = await self.schedule(guild_id)
        groups: list[tuple[str, list[str]]] = []
        for day in WEEKDAYS:
            span = schedule.get(day)
            if not span:
                continue
            if groups and groups[-1][0] == span and WEEKDAYS.index(groups[-1][1][-1]) == WEEKDAYS.index(day) - 1:
                groups[-1][1].append(day)
            else:
                groups.append((span, [day]))
        if not groups:
            return tr("tickets.hours.never")
        parts = []
        for span, days in groups:
            start, end = span.split("-")
            if len(days) == 1:
                day_text = tr(f"tickets.hours.days.{days[0]}")
            else:
                day_text = tr("tickets.hours.day_range", first=tr(f"tickets.hours.days.{days[0]}"),
                              last=tr(f"tickets.hours.days.{days[-1]}"))
            parts.append(tr("tickets.hours.span", start=start, end=end, days=day_text))
        return "; ".join(parts)
