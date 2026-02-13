"""Game engine - rules and state transitions."""

import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

from .models import (
    Accolade,
    Card,
    CardDeck,
    CardPile,
    Game,
    GamePhase,
    GameRound,
    Play,
    Player,
    RANK_ORDER,
    SUIT_ORDER,
)


def get_highest_card(hand: list[Card]) -> Optional[Card]:
    """Return highest card in hand (by game order)."""
    if not hand:
        return None
    return max(hand, key=lambda c: c.value)


def get_lowest_card(hand: list[Card], exclude_3c: bool = True) -> Optional[Card]:
    """Return lowest card; 3C exempt if exclude_3c."""
    if not hand:
        return None
    candidates = hand
    if exclude_3c:
        candidates = [c for c in hand if not (c.rank == "3" and c.suit == "C")]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.value)


def is_valid_play(play: Play, current: Optional[Play], num_cards_required: Optional[int]) -> bool:
    """Check if play is valid: same count as current (or any if starting), beats current."""
    if not play or not play.cards:
        return False
    rank = play.cards[0].rank
    if not all(c.rank == rank for c in play.cards):
        return False
    if current and current.cards:
        if len(play.cards) != len(current.cards):
            return False
        return play.beats(current)
    if num_cards_required is not None and len(play.cards) != num_cards_required:
        return False
    return True


def must_play_3c(play: Play) -> bool:
    """First play of first round must include 3C."""
    return any(c.rank == "3" and c.suit == "C" for c in play.cards)


def get_valid_plays(hand: list[Card], current: Optional[Play], num_required: Optional[int], must_include_3c: bool) -> list[list[Card]]:
    """Return list of valid card combinations (each is a list of cards)."""
    from collections import defaultdict
    pile_empty = not current or not current.cards
    if not hand:
        return []

    by_rank: dict[str, list[Card]] = defaultdict(list)
    for c in hand:
        by_rank[c.rank].append(c)
    for r, cards in by_rank.items():
        cards.sort(key=lambda c: c.value)

    result: list[list[Card]] = []
    n = len(current.cards) if current and current.cards else None
    if n is None:
        n = num_required

    for rank, cards in by_rank.items():
        if n and len(cards) < n:
            continue
        if must_include_3c and not any(c.rank == "3" and c.suit == "C" for c in cards):
            continue
        # Generate combinations of n cards of this rank
        if n is None:
            for k in range(1, len(cards) + 1):
                for combo in _combos(cards, k):
                    if must_include_3c and not any(c.rank == "3" and c.suit == "C" for c in combo):
                        continue
                    p = Play(cards=combo)
                    if is_valid_play(p, current, num_required):
                        result.append(combo)
        else:
            for combo in _combos(cards, n):
                p = Play(cards=combo)
                if is_valid_play(p, current, num_required):
                    if must_include_3c and not any(c.rank == "3" and c.suit == "C" for c in combo):
                        continue
                    result.append(combo)

    # If pile is empty (opening lead) and we have cards but got nothing, add all same-rank combos
    if pile_empty and hand and not result:
        for rank, cards in by_rank.items():
            for k in range(1, len(cards) + 1):
                for combo in _combos(cards, k):
                    result.append(combo)
    return result


def _combos(arr: list, k: int) -> list[list]:
    """All combinations of k elements from arr."""
    if k == 0:
        return [[]]
    if k > len(arr):
        return []
    result = []
    for i, x in enumerate(arr):
        for c in _combos(arr[i + 1:], k - 1):
            result.append([x] + c)
    return result


class GameEngine:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()

    def start_new_game(
        self,
        room_players: list[Player],
        prev_dealer_idx: Optional[int],
        prev_el_presidente_id: Optional[str],
        prev_shithead_id: Optional[str],
    ) -> Game:
        """Start a new game with given room players. Dealer is next after previous dealer or 0."""
        n = len(room_players)
        if n < 2 or n > 7:
            raise ValueError("Need 2-7 players")

        # Build player list for this game (copy from room, set accolades)
        players: list[Player] = []
        for i, rp in enumerate(room_players):
            p = Player(
                id=rp.id,
                name=rp.name,
                past_accolade=rp.past_accolade,
                accolade=Accolade.Pleb,
                hand=[],
            )
            players.append(p)

        # Dealer: next after previous dealer
        dealer_idx = (prev_dealer_idx + 1) % n if prev_dealer_idx is not None else 0

        # Deal: for 2 players, skip every 3rd card (return to deck / out of play) so 17 cards are out
        deck = CardDeck()
        deck.reset()
        deck.shuffle(self.rng)
        if n == 2:
            card_index = 0
            player_idx = 0
            while deck.cards:
                c = deck.deal_one()
                if not c:
                    break
                skip_slot = card_index % 3 == 2
                is_3c = c.rank == "3" and c.suit == "C"
                if skip_slot and is_3c:
                    players[player_idx % 2].hand.append(c)
                    player_idx += 1
                elif not skip_slot:
                    players[player_idx % 2].hand.append(c)
                    player_idx += 1
                card_index += 1
        else:
            idx = 0
            while deck.cards:
                c = deck.deal_one()
                if c:
                    players[idx % n].hand.append(c)
                    idx += 1

        for p in players:
            p.hand.sort(key=lambda c: c.value)

        # Trading phase if we have prev El Presidente and Shithead
        phase = GamePhase.Playing
        ep_idx = None
        sh_idx = None
        if prev_el_presidente_id and prev_shithead_id:
            for i, p in enumerate(players):
                if p.id == prev_el_presidente_id:
                    ep_idx = i
                if p.id == prev_shithead_id:
                    sh_idx = i
            if ep_idx is not None and sh_idx is not None:
                phase = GamePhase.Trading

        round_ = GameRound(starting_player_idx=0, pile=CardPile())

        # First round: who has 3C starts
        if phase == GamePhase.Playing:
            for i, p in enumerate(players):
                if any(c.rank == "3" and c.suit == "C" for c in p.hand):
                    round_.starting_player_idx = i
                    break
            # Current player is the one who starts
            current_idx = round_.starting_player_idx
        else:
            current_idx = 0  # will advance after trading

        game = Game(
            dealer_idx=dealer_idx,
            current_player_idx=current_idx,
            players=players,
            round=round_,
            phase=phase,
            results=[],
            passed_this_round=set(),
        )

        if phase == GamePhase.Trading:
            # Leave cards in center for explicit trade; EP and SH will claim via UI
            ep = players[ep_idx]
            sh = players[sh_idx]
            high = get_highest_card(sh.hand)
            low = get_lowest_card(ep.hand)
            if high:
                sh.hand.remove(high)
                game.trade_high_card = high
            if low:
                ep.hand.remove(low)
                game.trade_low_card = low
            game.trade_ep_claimed = False
            game.trade_sh_claimed = False

        logger.info("Game started (phase=%s)", phase.value)
        return game

    def apply_play(self, game: Game, player_idx: int, play: Play) -> Optional[str]:
        """Apply a play. Returns error message or None on success."""
        if game.phase != GamePhase.Playing:
            return "Not in playing phase"
        if game.current_player_idx != player_idx:
            return "Not your turn"

        current = game.round.pile.current_play
        num_required = len(current.cards) if current and current.cards else None
        is_first_play = current is None or not current.cards
        must_3c = is_first_play and game.round.starting_player_idx == player_idx and game.rounds_completed == 0

        if not is_valid_play(play, current, num_required):
            return "Invalid play"
        if must_3c and not must_play_3c(play):
            return "Must play 3C in first play"

        player = game.players[player_idx]
        for c in play.cards:
            if c not in player.hand:
                # Check by value (Card might be different instance)
                found = False
                for h in player.hand:
                    if h.rank == c.rank and h.suit == c.suit:
                        player.hand.remove(h)
                        found = True
                        break
                if not found:
                    return "Card not in hand"
            else:
                player.hand.remove(c)

        game.round.pile.add_play(play)
        game.round.last_play_player_idx = player_idx
        game.passed_this_round.clear()  # Pass only skips this trick; after a play, everyone can act again

        if not player.hand:
            game.results.append(player.id)
            # If no other player has cards, round ends immediately (winner need not pass)
            n = len(game.players)
            others_with_cards = [i for i in range(n) if i != player_idx and game.players[i].hand]
            if not others_with_cards:
                winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
                self._start_new_round(game, winner_idx)
                return None

        n = len(game.players)
        next_idx = (player_idx + 1) % n
        while next_idx != player_idx:
            if next_idx not in game.passed_this_round and game.players[next_idx].hand:
                break
            next_idx = (next_idx + 1) % n

        if next_idx == player_idx:
            # No one left with cards (everyone went out) - round over, start next round
            winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
            self._start_new_round(game, winner_idx)
        elif not game.players[next_idx].hand:
            # Next player has no cards (e.g. went out) - end round so pile clears
            winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
            self._start_new_round(game, winner_idx)
        else:
            game.current_player_idx = next_idx
        return None

    def apply_pass(self, game: Game, player_idx: int) -> Optional[str]:
        """Apply pass. Returns error or None."""
        if game.phase != GamePhase.Playing:
            return "Not in playing phase"
        if game.current_player_idx != player_idx:
            return "Not your turn"

        game.passed_this_round.add(player_idx)
        n = len(game.players)
        next_idx = (player_idx + 1) % n

        # Round over only when everyone has passed or has no cards (everyone got a chance to play)
        in_play = [i for i in range(n) if game.players[i].hand and i not in game.passed_this_round]
        if len(in_play) == 0:
            # All passed or out - last player to make a play wins the round
            winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
            self._start_new_round(game, winner_idx)
            return None

        while next_idx != player_idx:
            if next_idx not in game.passed_this_round and game.players[next_idx].hand:
                break
            next_idx = (next_idx + 1) % n

        if next_idx == player_idx:
            # No next player (everyone passed or out) - end round so pile clears
            winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
            self._start_new_round(game, winner_idx)
            return None
        # Never give turn to a player with no cards; end round so pile clears
        if not game.players[next_idx].hand:
            winner_idx = game.round.last_play_player_idx if game.round.last_play_player_idx >= 0 else player_idx
            self._start_new_round(game, winner_idx)
            return None
        game.current_player_idx = next_idx
        return None

    def _start_new_round(self, game: Game, winner_idx: int):
        """Start new round - clear pile, winner starts (or next player if winner is out)."""
        winner_name = game.players[winner_idx].name
        logger.info("Round ended: %s won the round", winner_name)

        game.rounds_completed += 1
        game.round.pile.clear()
        game.round.last_play_player_idx = -1
        game.passed_this_round.clear()
        n = len(game.players)
        start_idx = winner_idx
        if not game.players[winner_idx].hand:
            start_idx = (winner_idx + 1) % n
            while start_idx != winner_idx and not game.players[start_idx].hand:
                start_idx = (start_idx + 1) % n
            # If everyone went out, don't give turn to winner (no cards); next player leads
            if start_idx == winner_idx:
                start_idx = (winner_idx + 1) % n
        game.round.starting_player_idx = start_idx
        game.current_player_idx = start_idx

        logger.info("Round started: %s to lead", game.players[start_idx].name)

    def apply_claim_trade(self, game: Game, player_id: str, role: str) -> Optional[str]:
        """Claim trade card (presidente takes high, shithead takes low). Returns error or None."""
        if game.phase != GamePhase.Trading:
            return "Not in trading phase"
        ep_idx = next((i for i, p in enumerate(game.players) if p.past_accolade == Accolade.ElPresidente), None)
        sh_idx = next((i for i, p in enumerate(game.players) if p.past_accolade == Accolade.Shithead), None)
        if ep_idx is None or sh_idx is None:
            return "No trade in progress"
        player_idx = next((i for i, p in enumerate(game.players) if p.id == player_id), None)
        if player_idx is None:
            return "You are not in this game"

        if role == "presidente":
            if player_idx != ep_idx:
                return "Only El Presidente can claim the high card"
            if game.trade_ep_claimed:
                return "Already claimed"
            if not game.trade_high_card:
                return "No card to claim"
            game.players[ep_idx].hand.append(game.trade_high_card)
            game.players[ep_idx].hand.sort(key=lambda c: c.value)
            game.trade_high_card = None
            game.trade_ep_claimed = True
        elif role == "shithead":
            if player_idx != sh_idx:
                return "Only Shithead can claim the low card"
            if game.trade_sh_claimed:
                return "Already claimed"
            if not game.trade_low_card:
                return "No card to claim"
            game.players[sh_idx].hand.append(game.trade_low_card)
            game.players[sh_idx].hand.sort(key=lambda c: c.value)
            game.trade_low_card = None
            game.trade_sh_claimed = True
        else:
            return "Invalid role"

        if game.trade_ep_claimed and game.trade_sh_claimed:
            game.phase = GamePhase.Playing
            for i, p in enumerate(game.players):
                if any(c.rank == "3" and c.suit == "C" for c in p.hand):
                    game.round.starting_player_idx = i
                    break
            game.current_player_idx = game.round.starting_player_idx
            logger.info("Trade complete; round started: %s to lead", game.players[game.current_player_idx].name)
        return None

    def assign_accolades(self, game: Game):
        """Assign El Presidente, VP, Pleb, Shithead based on results."""
        r = game.results
        n = len(game.players)
        for i, pid in enumerate(r):
            p = next(x for x in game.players if x.id == pid)
            if i == 0:
                p.accolade = Accolade.ElPresidente
            elif i == n - 1:
                p.accolade = Accolade.Shithead
            elif i == 1:
                p.accolade = Accolade.VP
            else:
                p.accolade = Accolade.Pleb
        for p in game.players:
            if p.id not in r:
                p.accolade = Accolade.Shithead

    def remove_player_from_game(self, game: Game, player_idx: int) -> bool:
        """Remove player at index; their hand is discarded (not put on pile). Adjust indices and turn.
        Returns True if game was ended due to too few players."""
        n = len(game.players)
        if player_idx < 0 or player_idx >= n:
            return False
        removed_id = game.players[player_idx].id
        # Drop their hand (out of play, not onto pile)
        game.players.pop(player_idx)
        # Remap indices
        def shift(i: int) -> int:
            if i == player_idx:
                return -1
            return i - 1 if i > player_idx else i

        nn = len(game.players)
        if nn == 0:
            return True
        old_current = game.current_player_idx
        game.current_player_idx = shift(old_current)
        if game.current_player_idx == -1 or game.current_player_idx >= nn:
            # Was removed player's turn: next in order (old idx + 1) becomes new index (next_old - 1 or next_old)
            next_old = (player_idx + 1) % n
            new_idx = next_old - 1 if next_old > player_idx else next_old
            if new_idx >= nn:
                new_idx = 0
            game.current_player_idx = new_idx
        game.dealer_idx = shift(game.dealer_idx)
        if game.dealer_idx < 0:
            game.dealer_idx = 0
        game.round.starting_player_idx = shift(game.round.starting_player_idx)
        if game.round.starting_player_idx < 0:
            game.round.starting_player_idx = 0
        game.results = [pid for pid in game.results if pid != removed_id]
        game.passed_this_round = {shift(i) for i in game.passed_this_round if shift(i) >= 0}
        if game.round.last_play_player_idx == player_idx:
            game.round.last_play_player_idx = -1
        else:
            game.round.last_play_player_idx = shift(game.round.last_play_player_idx)

        if len(game.players) < 2:
            # End game: one or zero players left
            if len(game.players) == 1:
                game.results.append(game.players[0].id)
            self.assign_accolades(game)
            return True
        return False
