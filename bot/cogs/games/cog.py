"""Casino games with Components V2 buttons: Blackjack and Mines (bets via the economy ledger)."""

from __future__ import annotations

import abc
import asyncio
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands, ui

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import CooldownActive, UserError
from ...core.logger import get_logger
from ...core.utils import format_number
from ...services.economy import EconomyService
from ...ui.builder import CV2, Bullet, button
from ...ui.views import InteractiveLayout
from .engine import Blackjack, Mines, hand_value

if TYPE_CHECKING:
    from ...core.bot import XumariBot

_log = get_logger("games")


class GamesCog(XCog, name="Games"):
    feature = "games"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.economy = EconomyService(bot.db, bot.config.economy.start_balance)
        self._active: set[tuple[int, int]] = set()
        self._last_game: dict[tuple[int, int], float] = {}

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.games

    def coins(self, amount: int, locale: str) -> str:
        return f"**{format_number(amount, locale)}** {self.bot.icons.resolve(self.bot.config.economy.currency_emoji)}"

    # ------------------------------------------------------------------ bet handling
    async def start_bet(self, guild_id: int, user_id: int, bet: int) -> None:
        key = (guild_id, user_id)
        if bet < self.cfg.min_bet or bet > self.cfg.max_bet:
            raise UserError("games.errors.bet_limits", min=self.cfg.min_bet, max=self.cfg.max_bet)
        if key in self._active:
            raise UserError("games.errors.already_playing")
        wait = self._last_game.get(key, 0) + self.cfg.cooldown_seconds - time.monotonic()
        if wait > 0:
            raise CooldownActive("games.errors.cooldown", seconds=max(1, round(wait)))
        self._active.add(key)  # reserve before awaiting, so parallel commands can't start a second game
        try:
            await self.economy.change(guild_id, user_id, -bet, "game_bet")
        except Exception:
            self._active.discard(key)
            raise

    async def finish(self, guild_id: int, user_id: int, payout: int, game: str) -> int:
        self._active.discard((guild_id, user_id))
        self._last_game[(guild_id, user_id)] = time.monotonic()
        if payout > 0:
            return await self.economy.change(guild_id, user_id, payout, "game_win", reference=game)
        return (await self.economy.account(guild_id, user_id)).balance

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="blackjack", description=T("blackjack"))
    @app_commands.describe(bet=T("games.bet"))
    @app_commands.guild_only()
    async def blackjack(self, interaction: discord.Interaction[Any], bet: int) -> None:
        assert interaction.guild_id is not None
        await self.start_bet(interaction.guild_id, interaction.user.id, bet)
        view = BlackjackView(self, interaction, bet)
        await view.send(interaction)

    @app_commands.command(name="mines", description=T("mines"))
    @app_commands.describe(bet=T("games.bet"), mines=T("mines.count"))
    @app_commands.guild_only()
    async def mines(self, interaction: discord.Interaction[Any], bet: int, mines: int | None = None) -> None:
        assert interaction.guild_id is not None
        size = max(3, min(5, self.cfg.mines_grid_size))
        count = mines if mines is not None else self.cfg.mines_default_mines
        if not 1 <= count < size * size:
            raise UserError("games.errors.mines_count", max=size * size - 1)
        await self.start_bet(interaction.guild_id, interaction.user.id, bet)
        view = MinesView(self, interaction, bet, size, count)
        await view.send(interaction)


class _GameView(InteractiveLayout, abc.ABC):
    game_key = "game"

    def __init__(self, cog: GamesCog, interaction: discord.Interaction[Any], bet: int) -> None:
        bot: XumariBot = cog.bot
        super().__init__(bot, owner_id=interaction.user.id, timeout=cog.cfg.game_timeout_seconds)
        self.cog = cog
        self.guild_id = interaction.guild_id or 0
        self.user = interaction.user
        self.bet = bet
        self.tr = bot.translator_for(interaction)
        self.balance: int | None = None
        self._settled = False
        self._restarted = False
        # button callbacks run as separate tasks – serialise them so double clicks can't double-charge
        self.lock = asyncio.Lock()

    @abc.abstractmethod
    def render(self) -> None:
        """Rebuild all children from the current game state."""

    async def send(self, interaction: discord.Interaction[Any]) -> None:
        self.render()
        await interaction.response.send_message(view=self)
        self.bind(interaction)
        if self.finished:
            await self.settle()
            self.render()
            await interaction.edit_original_response(view=self)

    @property
    @abc.abstractmethod
    def finished(self) -> bool:
        """Whether the round is over."""

    @abc.abstractmethod
    def payout(self) -> int:
        """Total amount paid back to the player (stake included)."""

    async def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        self.balance = await self.cog.finish(self.guild_id, self.user.id, self.payout(), self.game_key)

    async def update(self, interaction: discord.Interaction[Any]) -> None:
        if self.finished:
            await self.settle()
        self.render()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        async with self.lock:
            if not self._settled and not self._restarted:
                await self.auto_finish()
                await self.settle()
        self.render()
        self.disable_all()
        try:
            if self._interaction is not None:
                await self._interaction.edit_original_response(view=self)
        except discord.HTTPException:
            _log.debug("Could not update timed out game message")

    @abc.abstractmethod
    async def auto_finish(self) -> None:
        """Resolve the round when the player stops interacting (timeout)."""

    def footer(self, b: CV2) -> None:
        if self._settled and self.balance is not None:
            b.footer(self.tr("games.footer_done", balance=self.cog.coins(self.balance, self.tr.locale)))
        else:
            b.footer(self.tr("games.footer_bet", bet=self.cog.coins(self.bet, self.tr.locale)))

    def again_button(self) -> ui.Button[Any]:
        btn = button(self.bot.theme, self.tr("games.again"), emoji="refresh", style=discord.ButtonStyle.success)

        async def again(interaction: discord.Interaction[Any]) -> None:
            async with self.lock:
                if self._restarted:
                    return
                await self.cog.start_bet(self.guild_id, self.user.id, self.bet)
                self._restarted = True
                self.stop()
            view = type(self).restart(self, interaction)
            view.render()
            try:
                await interaction.response.edit_message(view=view)
            except discord.HTTPException:
                # the new round never reached the player → give the stake back and free the slot
                await self.cog.finish(self.guild_id, self.user.id, self.bet, self.game_key)
                raise
            view.bind(interaction)
            if view.finished:
                await view.settle()
                view.render()
                await interaction.edit_original_response(view=view)

        btn.callback = again  # type: ignore[method-assign]
        return btn

    @classmethod
    @abc.abstractmethod
    def restart(cls, old: _GameView, interaction: discord.Interaction[Any]) -> _GameView:
        """Create a fresh round with the same settings ("play again")."""


class BlackjackView(_GameView):
    game_key = "blackjack"

    def __init__(self, cog: GamesCog, interaction: discord.Interaction[Any], bet: int) -> None:
        super().__init__(cog, interaction, bet)
        self.game = Blackjack(bet)

    @classmethod
    def restart(cls, old: _GameView, interaction: discord.Interaction[Any]) -> _GameView:
        return cls(old.cog, interaction, old.bet)

    @property
    def finished(self) -> bool:
        return self.game.finished

    def payout(self) -> int:
        return self.game.payout(self.cog.cfg.blackjack_payout, self.cog.cfg.blackjack_natural_payout)

    async def auto_finish(self) -> None:
        self.game.stand()

    def render(self) -> None:
        self.clear_items()
        tr = self.tr
        g = self.game
        dealer_cards = " ".join(f"`{c}`" for c in g.dealer) if g.finished else f"`{g.dealer[0]}` `??`"
        dealer_value = hand_value(g.dealer)[0] if g.finished else g.dealer[0].value
        player_value = hand_value(g.player)[0]
        b = CV2(self.bot.theme, view=self)
        b.banner("games")
        b.heading(tr("games.blackjack.title"), emoji="cards", subline=self.user.display_name)
        b.bullets([
            Bullet(tr("games.blackjack.dealer", value=dealer_value), dealer_cards, emoji="cards"),
            Bullet(tr("games.blackjack.player", value=player_value), " ".join(f"`{c}`" for c in g.player), emoji="user"),
        ])
        if g.finished:
            payout = self.payout()
            b.quote(tr(f"games.blackjack.result.{g.result}", amount=self.cog.coins(payout, tr.locale)))
        self.footer(b)
        b.separator()
        if g.finished:
            b.actions(self.again_button())
        else:
            hit = button(self.bot.theme, tr("games.blackjack.hit"), emoji="hit")
            stand = button(self.bot.theme, tr("games.blackjack.stand"), emoji="stand", style=discord.ButtonStyle.success)
            double = button(self.bot.theme, tr("games.blackjack.double"), emoji="double", disabled=not g.can_double)
            hit.callback = self._hit  # type: ignore[method-assign]
            stand.callback = self._stand  # type: ignore[method-assign]
            double.callback = self._double  # type: ignore[method-assign]
            b.actions(hit, stand, double)
        b.build()

    async def _hit(self, interaction: discord.Interaction[Any]) -> None:
        async with self.lock:
            self.game.hit()
            await self.update(interaction)

    async def _stand(self, interaction: discord.Interaction[Any]) -> None:
        async with self.lock:
            self.game.stand()
            await self.update(interaction)

    async def _double(self, interaction: discord.Interaction[Any]) -> None:
        async with self.lock:
            if self.game.can_double:
                await self.cog.economy.change(self.guild_id, self.user.id, -self.game.bet, "game_bet",
                                              reference="double")
                self.bet *= 2
                self.game.double()
            await self.update(interaction)


class MinesView(_GameView):
    game_key = "mines"

    def __init__(self, cog: GamesCog, interaction: discord.Interaction[Any], bet: int, size: int, mines: int) -> None:
        super().__init__(cog, interaction, bet)
        self.game = Mines(size=size, mines=mines, bet=bet, house_edge=cog.cfg.mines_house_edge)

    @classmethod
    def restart(cls, old: _GameView, interaction: discord.Interaction[Any]) -> _GameView:
        assert isinstance(old, MinesView)
        return cls(old.cog, interaction, old.bet, old.game.size, old.game.mines)

    @property
    def finished(self) -> bool:
        return self.game.finished

    def payout(self) -> int:
        return self.game.payout()

    async def auto_finish(self) -> None:
        self.game.cash_out()

    def render(self) -> None:
        self.clear_items()
        tr = self.tr
        g = self.game
        b = CV2(self.bot.theme, view=self)
        b.banner("games")
        b.heading(tr("games.mines.title"), emoji="mine",
                  subline=tr("games.mines.subline", mines=g.mines, tiles=g.tiles))
        next_multiplier = g.multiplier(len(g.revealed) + 1) if not g.finished else g.multiplier()
        b.bullets([
            Bullet(tr("games.mines.multiplier"), f"**x{g.multiplier():.2f}**", emoji="gem"),
            Bullet(tr("games.mines.next"), f"x{next_multiplier:.2f}", emoji="next"),
            Bullet(tr("games.mines.potential"), self.cog.coins(g.payout(), tr.locale), emoji="cashout"),
        ])
        if g.finished:
            key = "lost" if g.exploded is not None else "won"
            b.quote(tr(f"games.mines.{key}", amount=self.cog.coins(self.payout(), tr.locale)))
        self.footer(b)
        b.separator()
        for row in range(g.size):
            buttons: list[ui.Item[Any]] = []
            for col in range(g.size):
                index = row * g.size + col
                buttons.append(self._tile(index))
            b.actions(*buttons)
        if g.finished:
            b.actions(self.again_button())
        else:
            cash = button(self.bot.theme, tr("games.mines.cashout"), emoji="cashout",
                          style=discord.ButtonStyle.success, disabled=not g.revealed)
            cash.callback = self._cashout  # type: ignore[method-assign]
            b.actions(cash)
        b.build()

    def _tile(self, index: int) -> ui.Button[Any]:
        g = self.game
        icons = self.bot.icons
        if index in g.revealed:
            btn = ui.Button(emoji=icons.partial("gem"), style=discord.ButtonStyle.success, disabled=True)
        elif g.finished and index in g.positions:
            style = discord.ButtonStyle.danger if index == g.exploded else discord.ButtonStyle.secondary
            btn = ui.Button(emoji=icons.partial("mine"), style=style, disabled=True)
        else:
            btn = ui.Button(emoji=icons.partial("tile"), style=discord.ButtonStyle.secondary, disabled=g.finished)

            async def reveal(interaction: discord.Interaction[Any], i: int = index) -> None:
                async with self.lock:
                    self.game.reveal(i)
                    await self.update(interaction)

            btn.callback = reveal  # type: ignore[method-assign]
        return btn

    async def _cashout(self, interaction: discord.Interaction[Any]) -> None:
        async with self.lock:
            self.game.cash_out()
            await self.update(interaction)
