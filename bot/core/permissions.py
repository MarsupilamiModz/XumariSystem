"""Role based permission helpers and app-command checks."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar

import discord
from discord import app_commands

from .errors import PermissionDenied

if TYPE_CHECKING:
    from .bot import XumariBot

T = TypeVar("T")


def _role_ids(member: discord.abc.User | discord.Member) -> set[int]:
    return {r.id for r in getattr(member, "roles", [])}


def _has_any(member: discord.abc.User | discord.Member, ids: Iterable[int]) -> bool:
    wanted = set(ids)
    return bool(wanted) and bool(_role_ids(member) & wanted)


def is_owner(bot: XumariBot, user: discord.abc.User) -> bool:
    return user.id in bot.config.bot.owner_ids


def is_admin(bot: XumariBot, member: discord.abc.User | discord.Member) -> bool:
    if is_owner(bot, member):
        return True
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        return _has_any(member, bot.config.roles.admin)
    return False


def is_staff(bot: XumariBot, member: discord.abc.User | discord.Member) -> bool:
    if is_admin(bot, member):
        return True
    roles = bot.config.roles
    return _has_any(member, [*roles.staff, *(roles.designer if roles.designers_are_staff else [])])


def is_designer(bot: XumariBot, member: discord.abc.User | discord.Member) -> bool:
    return _has_any(member, bot.config.roles.designer) or is_admin(bot, member)


def has_any_role(member: discord.abc.User | discord.Member, role_ids: Iterable[int]) -> bool:
    return _has_any(member, role_ids)


def ensure_staff(bot: XumariBot, user: discord.abc.User | discord.Member) -> None:
    if not is_staff(bot, user):
        raise PermissionDenied()


def ensure_admin(bot: XumariBot, user: discord.abc.User | discord.Member) -> None:
    if not is_admin(bot, user):
        raise PermissionDenied()


def require_staff() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction[Any]) -> bool:
        ensure_staff(interaction.client, interaction.user)
        return True

    return app_commands.check(predicate)


def require_admin() -> Callable[[T], T]:
    async def predicate(interaction: discord.Interaction[Any]) -> bool:
        ensure_admin(interaction.client, interaction.user)
        return True

    return app_commands.check(predicate)
