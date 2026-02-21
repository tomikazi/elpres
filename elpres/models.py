"""Game model definitions for El Presidente."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# Card rank order (low to high): 3, 4, 5, 6, 7, 8, 9, 10, J, Q, K, A, 2
RANK_ORDER = "3456789TJQKA2"
RANK_DISPLAY = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A", "2": "2"}

# Suit order (low to high): C, D, H, S
SUIT_ORDER = "CDHS"
SUIT_DISPLAY = {"C": "C", "D": "D", "H": "H", "S": "S"}


def card_value(rank: str, suit: str) -> int:
    """Return sortable value: rank * 4 + suit."""
    r = RANK_ORDER.index(rank)
    s = SUIT_ORDER.index(suit)
    return r * 4 + s


def parse_card(s: str) -> tuple[str, str]:
    """Parse '4H' or '10S' into (rank, suit)."""
    if len(s) == 3 and s[:2] == "10":
        return ("T", s[2])
    return (s[0], s[1])


def card_display(rank: str, suit: str) -> str:
    """Return display string e.g. '4H', '10S'."""
    r = "10" if rank == "T" else rank
    return f"{r}{suit}"


@dataclass
class Card:
    rank: str
    suit: str

    def __post_init__(self):
        if self.rank == "10":
            self.rank = "T"

    @property
    def value(self) -> int:
        return card_value(self.rank, self.suit)

    def __str__(self) -> str:
        return card_display(self.rank, self.suit)

    def to_dict(self) -> dict:
        return {"rank": self.rank, "suit": self.suit}

    @classmethod
    def from_dict(cls, d: dict) -> "Card":
        return cls(rank=d["rank"], suit=d["suit"])


class Accolade(str, Enum):
    ElPresidente = "ElPresidente"
    VP = "VP"
    Pleb = "Pleb"
    Shithead = "Shithead"


class GamePhase(str, Enum):
    Trading = "Trading"
    Playing = "Playing"


@dataclass
class Play:
    """One or more face-up cards of the same rank discarded onto the pile."""

    cards: list[Card]

    @property
    def rank(self) -> str:
        return self.cards[0].rank if self.cards else ""

    def beats(self, other: "Play") -> bool:
        """This play is stronger than other (higher rank, or same rank then highest suit wins)."""
        if not other or not other.cards:
            return True
        if not self.cards:
            return False
        r1 = self.cards[0].rank
        r2 = other.cards[0].rank
        if RANK_ORDER.index(r1) > RANK_ORDER.index(r2):
            return True
        if RANK_ORDER.index(r1) < RANK_ORDER.index(r2):
            return False
        # Same rank: the play with the higher suit wins (compare max suit in each play)
        self_max_suit = max(SUIT_ORDER.index(c.suit) for c in self.cards)
        other_max_suit = max(SUIT_ORDER.index(c.suit) for c in other.cards)
        return self_max_suit > other_max_suit

    def to_dict(self) -> dict:
        return {"cards": [c.to_dict() for c in self.cards]}

    @classmethod
    def from_dict(cls, d: dict) -> "Play":
        return cls(cards=[Card.from_dict(c) for c in d.get("cards", [])])


@dataclass
class CardDeck:
    """Logical representation of a standard 52 card deck."""

    cards: list[Card] = field(default_factory=list)

    def reset(self):
        self.cards = []
        for r in RANK_ORDER:
            for s in SUIT_ORDER:
                self.cards.append(Card(rank=r, suit=s))

    def shuffle(self, rng=None):
        import random
        if rng is None:
            rng = random
        rng.shuffle(self.cards)

    def deal_one(self) -> Optional[Card]:
        return self.cards.pop(0) if self.cards else None

    def to_dict(self) -> dict:
        return {"cards": [c.to_dict() for c in self.cards]}

    @classmethod
    def from_dict(cls, d: dict) -> "CardDeck":
        return cls(cards=[Card.from_dict(c) for c in d.get("cards", [])])


@dataclass
class CardPile:
    """Logical representation of a stack of plays."""

    plays: list[Play] = field(default_factory=list)

    @property
    def current_play(self) -> Optional[Play]:
        return self.plays[-1] if self.plays else None

    def add_play(self, play: Play):
        self.plays.append(play)

    def clear(self):
        self.plays.clear()

    def to_dict(self) -> dict:
        return {"plays": [p.to_dict() for p in self.plays]}

    @classmethod
    def from_dict(cls, d: dict) -> "CardPile":
        return cls(plays=[Play.from_dict(p) for p in d.get("plays", [])])


@dataclass
class GameRound:
    """A sequence of turns comprising: starting player, card pile."""

    starting_player_idx: int
    pile: CardPile = field(default_factory=CardPile)
    last_play_player_idx: int = -1

    def to_dict(self) -> dict:
        return {
            "starting_player_idx": self.starting_player_idx,
            "pile": self.pile.to_dict(),
            "last_play_player_idx": self.last_play_player_idx,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameRound":
        return cls(
            starting_player_idx=d.get("starting_player_idx", 0),
            pile=CardPile.from_dict(d.get("pile", {})),
            last_play_player_idx=d.get("last_play_player_idx", -1),
        )


@dataclass
class Player:
    """Logical representation of a player."""

    id: str
    name: str
    past_accolade: Accolade = Accolade.Pleb
    accolade: Accolade = Accolade.Pleb
    hand: list[Card] = field(default_factory=list)

    def hand_sorted(self) -> list[Card]:
        return sorted(self.hand, key=lambda c: c.value)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "past_accolade": self.past_accolade.value,
            "accolade": self.accolade.value,
            "hand": [c.to_dict() for c in self.hand],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        return cls(
            id=d["id"],
            name=d["name"],
            past_accolade=Accolade(d.get("past_accolade", Accolade.Pleb.value)),
            accolade=Accolade(d.get("accolade", Accolade.Pleb.value)),
            hand=[Card.from_dict(c) for c in d.get("hand", [])],
        )


@dataclass
class Game:
    """Logical representation of the current game state."""

    dealer_idx: int
    current_player_idx: int
    players: list[Player]
    round: GameRound
    phase: GamePhase = GamePhase.Playing
    results: list[str] = field(default_factory=list)  # player ids in finish order
    passed_this_round: set[int] = field(default_factory=set)  # indices who passed
    rounds_completed: int = 0  # number of completed rounds (0 = first round, 3C required on opening)
    # Explicit trade: cards in center until EP and SH claim them
    trade_high_card: Optional["Card"] = None  # from Shithead, for Presidente
    trade_low_card: Optional["Card"] = None   # from Presidente, for Shithead
    trade_ep_claimed: bool = False
    trade_sh_claimed: bool = False

    def to_dict(self) -> dict:
        return {
            "dealer_idx": self.dealer_idx,
            "current_player_idx": self.current_player_idx,
            "players": [p.to_dict() for p in self.players],
            "round": self.round.to_dict(),
            "phase": self.phase.value,
            "results": self.results,
            "passed_this_round": list(self.passed_this_round),
            "rounds_completed": self.rounds_completed,
            "trade_high_card": self.trade_high_card.to_dict() if self.trade_high_card else None,
            "trade_low_card": self.trade_low_card.to_dict() if self.trade_low_card else None,
            "trade_ep_claimed": self.trade_ep_claimed,
            "trade_sh_claimed": self.trade_sh_claimed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Game":
        th = d.get("trade_high_card")
        tl = d.get("trade_low_card")
        return cls(
            dealer_idx=d.get("dealer_idx", 0),
            current_player_idx=d.get("current_player_idx", 0),
            players=[Player.from_dict(p) for p in d.get("players", [])],
            round=GameRound.from_dict(d.get("round", {})),
            phase=GamePhase(d.get("phase", GamePhase.Playing.value)),
            results=d.get("results", []),
            passed_this_round=set(d.get("passed_this_round", [])),
            rounds_completed=d.get("rounds_completed", 0),
            trade_high_card=Card.from_dict(th) if th else None,
            trade_low_card=Card.from_dict(tl) if tl else None,
            trade_ep_claimed=d.get("trade_ep_claimed", False),
            trade_sh_claimed=d.get("trade_sh_claimed", False),
        )


@dataclass
class GameRoom:
    """Room where players gather; one game at a time."""

    name: str
    current_game: Optional[Game] = None
    players: list[Player] = field(default_factory=list)  # players in room (incl spectators)
    spectator_preferences: dict[str, bool] = field(default_factory=dict)  # player_id -> True=deal me in, False=just watch
    dick_tagged_player_id: Optional[str] = None  # player currently tagged as "dick" (one per room)
    dick_tagged_at: Optional[float] = None  # time.time() when current holder was tagged (for 15s cooldown)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "current_game": self.current_game.to_dict() if self.current_game else None,
            "players": [p.to_dict() for p in self.players],
            "spectator_preferences": dict(self.spectator_preferences),
            "dick_tagged_player_id": self.dick_tagged_player_id,
            "dick_tagged_at": self.dick_tagged_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GameRoom":
        g = d.get("current_game")
        prefs = d.get("spectator_preferences", {})
        dick_id = d.get("dick_tagged_player_id")
        dick_at = d.get("dick_tagged_at")
        return cls(
            name=d["name"],
            current_game=Game.from_dict(g) if g else None,
            players=[Player.from_dict(p) for p in d.get("players", [])],
            spectator_preferences={k: bool(v) for k, v in prefs.items()} if isinstance(prefs, dict) else {},
            dick_tagged_player_id=str(dick_id) if dick_id else None,
            dick_tagged_at=float(dick_at) if dick_at is not None else None,
        )
