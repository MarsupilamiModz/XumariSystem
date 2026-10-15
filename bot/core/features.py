"""Feature flags.

* ``config.yaml → features`` decides which cogs are *loaded* at all (disabled cogs are never imported).
* Per-guild runtime overrides (``guild_settings.features``, editable in ``/controlpanel``) can switch a
  loaded feature off/on without a restart. Commands and components of a disabled feature answer with
  a localised notice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config.settings import FeatureFlags

if TYPE_CHECKING:
    from .db import Database

FEATURE_KEYS: tuple[str, ...] = tuple(FeatureFlags.model_fields)


class FeatureService:
    def __init__(self, flags: FeatureFlags, db: Database) -> None:
        self._flags = flags
        self._db = db
        self._overrides: dict[int, dict[str, bool]] = {}

    def loaded(self, key: str) -> bool:
        return bool(getattr(self._flags, key, False))

    async def load_overrides(self) -> None:
        rows = await self._db.fetch("SELECT guild_id, features FROM guild_settings")
        self._overrides = {r["guild_id"]: {k: bool(v) for k, v in (r["features"] or {}).items()} for r in rows}

    def is_enabled(self, guild_id: int | None, key: str | None) -> bool:
        if key is None:
            return True
        if not self.loaded(key):
            return False
        if guild_id is None:
            return True
        return self._overrides.get(guild_id, {}).get(key, True)

    def overrides(self, guild_id: int) -> dict[str, bool]:
        return dict(self._overrides.get(guild_id, {}))

    async def set_enabled(self, guild_id: int, key: str, enabled: bool) -> None:
        if key not in FEATURE_KEYS:
            raise ValueError(f"unknown feature {key!r}")
        await self._db.ensure_guild(guild_id)
        await self._db.execute(
            """
            UPDATE guild_settings
               SET features = features || jsonb_build_object($2::text, $3::boolean)
             WHERE guild_id = $1
            """,
            guild_id,
            key,
            enabled,
        )
        self._overrides.setdefault(guild_id, {})[key] = enabled
