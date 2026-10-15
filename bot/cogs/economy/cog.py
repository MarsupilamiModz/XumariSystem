"""Economy: balance, daily, work, crime, rob, pay, leaderboard and admin tools."""

from __future__ import annotations

import random
from typing import Any

import discord
from discord import app_commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import UserError
from ...core.i18n import Translator
from ...core.permissions import require_admin
from ...core.utils import format_number
from ...services.economy import EconomyService
from ...ui.builder import CV2, Bullet

_rng = random.SystemRandom()


class EconomyCog(XCog, name="Economy"):
    feature = "economy"

    eco_group = app_commands.Group(name="eco", description=T("eco"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.economy = EconomyService(bot.db, bot.config.economy.start_balance)

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.economy

    def coins(self, amount: int, locale: str) -> str:
        return f"**{format_number(amount, locale)}** {self.bot.icons.resolve(self.cfg.currency_emoji)}"

    def _result(self, tr: Translator, *, emoji: str, title: str, text: str, balance: int) -> discord.ui.LayoutView:
        b = CV2(self.bot.theme)
        b.banner("economy")
        b.heading(title, emoji=emoji, level=3)
        b.quote(text)
        b.footer(tr("economy.balance_line", balance=self.coins(balance, tr.locale), currency=self.cfg.currency_name))
        return b.build()

    # ------------------------------------------------------------------ user commands
    @app_commands.command(name="balance", description=T("balance"))
    @app_commands.describe(user=T("balance.user"))
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction[Any], user: discord.Member | None = None) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        if target.bot:
            raise UserError("economy.errors.bot")
        tr = self.tr(interaction)
        account = await self.economy.account(interaction.guild_id, target.id)
        rank = await self.economy.rank(interaction.guild_id, target.id)
        b = CV2(self.bot.theme)
        b.banner("economy")
        b.heading(tr("economy.balance.title", user=target.display_name), emoji="bank",
                  subline=tr("economy.balance.rank", rank=rank))
        b.bullets([
            Bullet(tr("economy.balance.current"), self.coins(account.balance, tr.locale), emoji="coin"),
            Bullet(tr("economy.balance.earned"), self.coins(account.total_earned, tr.locale), emoji="add"),
            Bullet(tr("economy.balance.lost"), self.coins(account.total_lost, tr.locale), emoji="remove"),
        ])
        await interaction.response.send_message(view=b.build(), ephemeral=user is None)

    @app_commands.command(name="daily", description=T("daily"))
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        action = self.cfg.daily
        await self.economy.claim_cooldown(interaction.guild_id, interaction.user.id, "daily", action.cooldown_seconds)
        amount = _rng.randint(action.min_amount, action.max_amount)
        balance = await self.economy.change(interaction.guild_id, interaction.user.id, amount, "daily")
        tr = self.tr(interaction)
        await interaction.response.send_message(view=self._result(
            tr, emoji="daily", title=tr("economy.daily.title"),
            text=tr("economy.daily.text", amount=self.coins(amount, tr.locale)), balance=balance))

    @app_commands.command(name="work", description=T("work"))
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        action = self.cfg.work
        await self.economy.claim_cooldown(interaction.guild_id, interaction.user.id, "work", action.cooldown_seconds)
        amount = _rng.randint(action.min_amount, action.max_amount)
        balance = await self.economy.change(interaction.guild_id, interaction.user.id, amount, "work")
        tr = self.tr(interaction)
        line = _rng.choice(tr.list("economy.work.lines"))
        await interaction.response.send_message(view=self._result(
            tr, emoji="work", title=tr("economy.work.title"),
            text=line.format(amount=self.coins(amount, tr.locale)), balance=balance))

    @app_commands.command(name="crime", description=T("crime"))
    @app_commands.guild_only()
    async def crime(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        guild_id, user_id = interaction.guild_id, interaction.user.id
        action = self.cfg.crime
        await self.economy.claim_cooldown(guild_id, user_id, "crime", action.cooldown_seconds)
        tr = self.tr(interaction)
        if _rng.random() < action.success_rate:
            amount = _rng.randint(action.min_amount, action.max_amount)
            balance = await self.economy.change(guild_id, user_id, amount, "crime")
            text = _rng.choice(tr.list("economy.crime.success")).format(amount=self.coins(amount, tr.locale))
            title = tr("economy.crime.title_success")
        else:
            account = await self.economy.account(guild_id, user_id)
            fine = round(account.balance * action.fine_percent)
            balance = await self.economy.change(guild_id, user_id, -fine, "crime_fine") if fine else account.balance
            text = _rng.choice(tr.list("economy.crime.fail")).format(amount=self.coins(fine, tr.locale))
            title = tr("economy.crime.title_fail")
        await interaction.response.send_message(view=self._result(tr, emoji="crime", title=title, text=text,
                                                                  balance=balance))

    @app_commands.command(name="rob", description=T("rob"))
    @app_commands.describe(user=T("rob.user"))
    @app_commands.guild_only()
    async def rob(self, interaction: discord.Interaction[Any], user: discord.Member) -> None:
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        if user.bot or user.id == interaction.user.id:
            raise UserError("economy.errors.rob_target")
        victim = await self.economy.account(guild_id, user.id)
        if victim.balance < self.cfg.rob_min_victim_balance:
            raise UserError("economy.errors.rob_poor", min=self.coins(self.cfg.rob_min_victim_balance,
                                                                      self.bot.locale_for(interaction)))
        action = self.cfg.rob
        await self.economy.claim_cooldown(guild_id, interaction.user.id, "rob", action.cooldown_seconds)
        tr = self.tr(interaction)
        if _rng.random() < action.success_rate:
            amount = _rng.randint(1, max(1, int(victim.balance * self.cfg.rob_max_steal_percent)))
            await self.economy.transfer(guild_id, user.id, interaction.user.id, amount, kind="rob")
            text = tr("economy.rob.success", amount=self.coins(amount, tr.locale), user=user.mention)
            title = tr("economy.rob.title_success")
        else:
            own = await self.economy.account(guild_id, interaction.user.id)
            fine = round(own.balance * action.fine_percent)
            if fine:
                await self.economy.transfer(guild_id, interaction.user.id, user.id, fine, kind="rob_fine")
            text = tr("economy.rob.fail", amount=self.coins(fine, tr.locale), user=user.mention)
            title = tr("economy.rob.title_fail")
        balance = (await self.economy.account(guild_id, interaction.user.id)).balance
        await interaction.response.send_message(
            view=self._result(tr, emoji="rob", title=title, text=text, balance=balance),
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )

    @app_commands.command(name="pay", description=T("pay"))
    @app_commands.describe(user=T("pay.user"), amount=T("pay.amount"))
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction[Any], user: discord.Member,
                  amount: app_commands.Range[int, 1, 10_000_000]) -> None:
        assert interaction.guild_id is not None
        if user.bot or user.id == interaction.user.id:
            raise UserError("economy.errors.rob_target")
        await self.economy.transfer(interaction.guild_id, interaction.user.id, user.id, amount)
        tr = self.tr(interaction)
        balance = (await self.economy.account(interaction.guild_id, interaction.user.id)).balance
        await interaction.response.send_message(view=self._result(
            tr, emoji="send", title=tr("economy.pay.title"),
            text=tr("economy.pay.text", amount=self.coins(amount, tr.locale), user=user.mention), balance=balance))

    # ------------------------------------------------------------------ leaderboard & admin
    @eco_group.command(name="leaderboard", description=T("eco.leaderboard"))
    async def leaderboard(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        accounts = await self.economy.leaderboard(interaction.guild_id, self.cfg.leaderboard_size)
        b = CV2(self.bot.theme)
        b.banner("economy")
        b.heading(tr("economy.leaderboard.title"), emoji="trophy", subline=self.cfg.currency_name)
        if accounts:
            medals = ["🥇", "🥈", "🥉"]
            b.text("\n".join(
                f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{a.user_id}> · {self.coins(a.balance, tr.locale)}"
                for i, a in enumerate(accounts)
            ))
        else:
            b.quote(tr("economy.leaderboard.empty"))
        await interaction.response.send_message(view=b.build(), allowed_mentions=discord.AllowedMentions.none())

    async def _admin_log(self, interaction: discord.Interaction[Any], action: str, user: discord.abc.User | None,
                         amount: int | None) -> None:
        tr = self.gtr(interaction.guild_id)
        await self.bot.audit.log(
            interaction.guild, "economy", f"economy.{action}", title=tr(f"economy.log.{action}"), emoji="coin",
            actor=interaction.user, target_id=user.id if user else None,
            lines=[tr("economy.log.line", user=user.mention if user else tr("economy.log.everyone"),
                      amount=amount if amount is not None else "—")],
        )

    @eco_group.command(name="give", description=T("eco.give"))
    @app_commands.describe(user=T("pay.user"), amount=T("pay.amount"))
    @require_admin()
    async def give(self, interaction: discord.Interaction[Any], user: discord.Member,
                   amount: app_commands.Range[int, 1, 1_000_000_000]) -> None:
        assert interaction.guild_id is not None
        balance = await self.economy.change(interaction.guild_id, user.id, amount, "admin_give",
                                            actor_id=interaction.user.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("economy.admin.done", user=user.mention,
                                                            balance=self.coins(balance, tr.locale)))
        await self._admin_log(interaction, "give", user, amount)

    @eco_group.command(name="take", description=T("eco.take"))
    @app_commands.describe(user=T("pay.user"), amount=T("pay.amount"))
    @require_admin()
    async def take(self, interaction: discord.Interaction[Any], user: discord.Member,
                   amount: app_commands.Range[int, 1, 1_000_000_000]) -> None:
        assert interaction.guild_id is not None
        account = await self.economy.account(interaction.guild_id, user.id)
        amount = min(amount, account.balance)
        balance = await self.economy.change(interaction.guild_id, user.id, -amount, "admin_take",
                                            actor_id=interaction.user.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("economy.admin.done", user=user.mention,
                                                            balance=self.coins(balance, tr.locale)))
        await self._admin_log(interaction, "take", user, amount)

    @eco_group.command(name="set", description=T("eco.set"))
    @app_commands.describe(user=T("pay.user"), amount=T("pay.amount"))
    @require_admin()
    async def set_balance(self, interaction: discord.Interaction[Any], user: discord.Member,
                          amount: app_commands.Range[int, 0, 1_000_000_000]) -> None:
        assert interaction.guild_id is not None
        balance = await self.economy.set_balance(interaction.guild_id, user.id, amount, interaction.user.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("economy.admin.done", user=user.mention,
                                                            balance=self.coins(balance, tr.locale)))
        await self._admin_log(interaction, "set", user, amount)

    @eco_group.command(name="reset", description=T("eco.reset"))
    @app_commands.describe(user=T("eco.reset.user"))
    @require_admin()
    async def reset(self, interaction: discord.Interaction[Any], user: discord.Member | None = None) -> None:
        assert interaction.guild_id is not None
        await self.economy.reset(interaction.guild_id, user.id if user else None)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("economy.admin.reset",
                                                            user=user.mention if user else tr("economy.log.everyone")))
        await self._admin_log(interaction, "reset", user, None)
