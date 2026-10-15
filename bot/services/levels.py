"""XP and levels. ``levels.formula`` (safe expression of ``level``) = XP needed from level L to L+1."""

from __future__ import annotations

from dataclasses import dataclass

from ..config.settings import LevelsSection
from ..core.db import Database
from ..core.safe_eval import SafeExpression

MAX_LEVEL = 1000


@dataclass(slots=True)
class LevelEntry:
    user_id: int
    xp: int
    level: int
    messages: int


@dataclass(slots=True)
class LevelUp:
    old_level: int
    new_level: int
    xp: int


class LevelService:
    def __init__(self, db: Database, cfg: LevelsSection) -> None:
        self.db = db
        self.cfg = cfg
        self.formula = SafeExpression(cfg.formula, ("level",))
        self._thresholds: list[int] = [0]

    def xp_for_next(self, level: int) -> int:
        return max(1, int(self.formula(level=level)))

    def total_for(self, level: int) -> int:
        """Total XP required to reach ``level``."""
        level = min(level, MAX_LEVEL)
        while len(self._thresholds) <= level:
            current = len(self._thresholds) - 1
            self._thresholds.append(self._thresholds[-1] + self.xp_for_next(current))
        return self._thresholds[level]

    def level_for(self, xp: int) -> int:
        level = 0
        while level < MAX_LEVEL and self.total_for(level + 1) <= xp:
            level += 1
        return level

    def progress(self, xp: int) -> tuple[int, int, int]:
        """``(level, xp_in_level, xp_needed_for_next)``"""
        level = self.level_for(xp)
        return level, xp - self.total_for(level), self.xp_for_next(level)

    async def award(self, guild_id: int, user_id: int, amount: int) -> LevelUp | None:
        """Give XP if the per-user cooldown has passed (enforced in SQL). Returns a LevelUp on level change."""
        await self.db.ensure_guild(guild_id)
        row = await self.db.fetchrow(
            """
            INSERT INTO levels (guild_id, user_id, xp, messages, last_xp_at) VALUES ($1, $2, $3, 1, now())
            ON CONFLICT (guild_id, user_id) DO UPDATE
               SET xp = levels.xp + EXCLUDED.xp, messages = levels.messages + 1, last_xp_at = now()
             WHERE levels.last_xp_at < now() - make_interval(secs => $4)
            RETURNING xp, level
            """,
            guild_id, user_id, amount, float(self.cfg.cooldown_seconds),
        )
        if row is None:
            await self.db.execute(
                "UPDATE levels SET messages = messages + 1 WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
            )
            return None
        new_level = self.level_for(int(row["xp"]))
        if new_level == row["level"]:
            return None
        await self.db.execute("UPDATE levels SET level = $3 WHERE guild_id = $1 AND user_id = $2",
                              guild_id, user_id, new_level)
        return LevelUp(int(row["level"]), new_level, int(row["xp"]))

    async def get(self, guild_id: int, user_id: int) -> LevelEntry:
        row = await self.db.fetchrow(
            "SELECT user_id, xp, level, messages FROM levels WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
        )
        return LevelEntry(**dict(row)) if row else LevelEntry(user_id, 0, 0, 0)

    async def rank(self, guild_id: int, user_id: int) -> int:
        return int(await self.db.fetchval(
            "SELECT count(*) + 1 FROM levels WHERE guild_id = $1 AND xp > "
            "COALESCE((SELECT xp FROM levels WHERE guild_id = $1 AND user_id = $2), -1)",
            guild_id, user_id,
        ) or 1)

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list[LevelEntry]:
        rows = await self.db.fetch(
            "SELECT user_id, xp, level, messages FROM levels WHERE guild_id = $1 ORDER BY xp DESC, user_id LIMIT $2",
            guild_id, limit,
        )
        return [LevelEntry(**dict(r)) for r in rows]

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> LevelEntry:
        await self.db.ensure_guild(guild_id)
        level = self.level_for(xp)
        await self.db.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp, level) VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = EXCLUDED.xp, level = EXCLUDED.level
            """,
            guild_id, user_id, xp, level,
        )
        return await self.get(guild_id, user_id)

    async def reset(self, guild_id: int, user_id: int | None) -> None:
        if user_id is None:
            await self.db.execute("DELETE FROM levels WHERE guild_id = $1", guild_id)
        else:
            await self.db.execute("DELETE FROM levels WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)

    async def rewards(self, guild_id: int) -> dict[int, list[int]]:
        """Configured (config.yaml) + DB managed rewards: ``{level: [role_id, …]}``."""
        result: dict[int, list[int]] = {}
        for level, role_id in self.cfg.rewards.items():
            result.setdefault(int(level), []).append(int(role_id))
        for row in await self.db.fetch("SELECT level, role_id FROM level_rewards WHERE guild_id = $1", guild_id):
            result.setdefault(row["level"], [])
            if row["role_id"] not in result[row["level"]]:
                result[row["level"]].append(row["role_id"])
        return dict(sorted(result.items()))

    async def add_reward(self, guild_id: int, level: int, role_id: int) -> None:
        await self.db.ensure_guild(guild_id)
        await self.db.execute(
            "INSERT INTO level_rewards (guild_id, level, role_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            guild_id, level, role_id,
        )

    async def remove_reward(self, guild_id: int, level: int, role_id: int) -> bool:
        status = await self.db.execute(
            "DELETE FROM level_rewards WHERE guild_id = $1 AND level = $2 AND role_id = $3", guild_id, level, role_id
        )
        return status.endswith(" 1")
