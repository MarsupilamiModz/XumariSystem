"""Pure game logic (no Discord code) – Blackjack and Mines. Uses ``random.SystemRandom``."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")
RANKS: tuple[str, ...] = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")


@dataclass(frozen=True, slots=True)
class Card:
    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"

    @property
    def value(self) -> int:
        if self.rank == "A":
            return 11
        if self.rank in ("J", "Q", "K"):
            return 10
        return int(self.rank)


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Return ``(total, soft)`` – aces count 11 while that doesn't bust the hand."""
    total = sum(c.value for c in cards)
    aces = sum(1 for c in cards if c.rank == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_value(cards)[0] == 21


class Blackjack:
    """Dealer stands on soft 17. Results: blackjack, win, dealer_bust, push, lose, bust."""

    def __init__(self, bet: int, rng: random.Random | None = None, decks: int = 6) -> None:
        self.rng = rng or random.SystemRandom()
        self.deck = [Card(r, s) for _ in range(decks) for s in SUITS for r in RANKS]
        self.rng.shuffle(self.deck)
        self.bet = bet
        self.doubled = False
        self.result: str | None = None
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        if is_blackjack(self.player) or is_blackjack(self.dealer):
            self._settle_naturals()

    @property
    def finished(self) -> bool:
        return self.result is not None

    @property
    def can_double(self) -> bool:
        return not self.finished and len(self.player) == 2 and not self.doubled

    def _settle_naturals(self) -> None:
        player_bj, dealer_bj = is_blackjack(self.player), is_blackjack(self.dealer)
        if player_bj and dealer_bj:
            self.result = "push"
        elif player_bj:
            self.result = "blackjack"
        else:
            self.result = "lose"

    def hit(self) -> None:
        if self.finished:
            return
        self.player.append(self.deck.pop())
        if hand_value(self.player)[0] > 21:
            self.result = "bust"
        elif hand_value(self.player)[0] == 21:
            self.stand()

    def double(self) -> None:
        if not self.can_double:
            return
        self.doubled = True
        self.bet *= 2
        self.player.append(self.deck.pop())
        if hand_value(self.player)[0] > 21:
            self.result = "bust"
        else:
            self.stand()

    def stand(self) -> None:
        if self.finished:
            return
        while hand_value(self.dealer)[0] < 17:
            self.dealer.append(self.deck.pop())
        player, dealer = hand_value(self.player)[0], hand_value(self.dealer)[0]
        if dealer > 21:
            self.result = "dealer_bust"
        elif player > dealer:
            self.result = "win"
        elif player == dealer:
            self.result = "push"
        else:
            self.result = "lose"

    def payout(self, win_multiplier: float, natural_multiplier: float) -> int:
        """Total amount returned to the player (stake included)."""
        if self.result == "blackjack":
            return math.floor(self.bet * natural_multiplier)
        if self.result in ("win", "dealer_bust"):
            return math.floor(self.bet * win_multiplier)
        if self.result == "push":
            return self.bet
        return 0


@dataclass(slots=True)
class Mines:
    size: int
    mines: int
    bet: int
    house_edge: float = 0.03
    rng: random.Random = field(default_factory=random.SystemRandom)
    positions: set[int] = field(default_factory=set)
    revealed: list[int] = field(default_factory=list)
    exploded: int | None = None
    cashed_out: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.mines < self.tiles:
            raise ValueError("invalid mine count")
        self.positions = set(self.rng.sample(range(self.tiles), self.mines))

    @property
    def tiles(self) -> int:
        return self.size * self.size

    @property
    def finished(self) -> bool:
        return self.exploded is not None or self.cashed_out or len(self.revealed) == self.tiles - self.mines

    def multiplier(self, safe_reveals: int | None = None) -> float:
        k = len(self.revealed) if safe_reveals is None else safe_reveals
        value = 1.0
        for i in range(k):
            value *= (self.tiles - i) / (self.tiles - self.mines - i)
        return value * (1 - self.house_edge) if k else 1.0

    def reveal(self, index: int) -> bool:
        """Reveal a tile. Returns ``True`` if it was safe."""
        if self.finished or index in self.revealed or not 0 <= index < self.tiles:
            return self.exploded is None
        if index in self.positions:
            self.exploded = index
            return False
        self.revealed.append(index)
        return True

    def cash_out(self) -> None:
        if not self.finished and self.revealed:
            self.cashed_out = True

    def payout(self) -> int:
        if self.exploded is not None:
            return 0
        if not self.revealed:
            return self.bet
        return math.floor(self.bet * self.multiplier())
