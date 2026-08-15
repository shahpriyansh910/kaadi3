"""
Kaadi3 card/deck engine.

Point values:
  - 5's                     -> 5 points
  - 10, J, Q, K, A           -> 10 points
  - 3 of Spades              -> 30 points
  - everything else          -> 0 points

Per-deck total is always 250 regardless of which non-point cards get
removed for even distribution, since only 0-point cards are ever removed:
  4x5 (5pts) = 20
  5 ranks (10,J,Q,K,A) x 4 suits x 10pts = 200
  3 of Spades = 30
  20 + 200 + 30 = 250   (500 for a 2-deck game)
"""
import random

SUITS = ["S", "D", "H", "C"]  # Spade, Diamond, Heart, Club
SUIT_NAMES = {"S": "Spades", "D": "Diamonds", "H": "Hearts", "C": "Clubs"}
SUIT_SYMBOLS = {"S": "♠", "D": "♦", "H": "♥", "C": "♣"}

# Ascending rank order. Index = heaviness (higher index beats lower index).
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_INDEX = {r: i for i, r in enumerate(RANKS)}

POINT_RANKS_10 = {"10", "J", "Q", "K", "A"}

# Order in which non-point ranks get thinned out (lowest first) when a
# deck needs to be trimmed to divide evenly among players. 5 is skipped
# (it's a point card); 3 of Spades is a point card and is protected
# implicitly because point_value() > 0 excludes it from the removal pool.
REMOVAL_RANK_ORDER = ["2", "3", "4", "6", "7", "8", "9"]

CARDS_PER_PLAYER = {5: 10, 6: 14, 7: 13, 8: 12}


def num_decks_for(num_players: int) -> int:
    return 1 if num_players == 5 else 2


def point_value(suit: str, rank: str) -> int:
    if rank == "3" and suit == "S":
        return 30
    if rank == "5":
        return 5
    if rank in POINT_RANKS_10:
        return 10
    return 0


def deck_total_points(num_decks: int) -> int:
    return 250 * num_decks


def _fresh_deck(num_decks: int):
    deck = []
    for _ in range(num_decks):
        for suit in SUITS:
            for rank in RANKS:
                deck.append({"suit": suit, "rank": rank})
    return deck


def build_playing_deck(num_players: int, rng: random.Random = None):
    """Build the shuffled, trimmed deck used to deal a round.

    Returns a list of card dicts: {suit, rank, points, occurrence, id}
    'occurrence' is 1 or 2 -- for 2-deck games, which copy this card is,
    an arbitrary tag (by position in the shuffled deck) used only to give
    each physical copy a distinct card id in a hand. For 1-deck games it
    is always 1.

    NOTE: this tag is NOT what "1st"/"2nd" means when calling a partner --
    that's resolved dynamically by play order instead (see game.py's
    select_partner/play_card), since no player can observe deal-order at
    the table. Don't reuse this field for that.
    """
    rng = rng or random
    num_decks = num_decks_for(num_players)
    deck = _fresh_deck(num_decks)

    cards_per_player = CARDS_PER_PLAYER[num_players]
    target = cards_per_player * num_players
    remove_count = len(deck) - target

    def is_point(c):
        return point_value(c["suit"], c["rank"]) > 0

    non_point = [c for c in deck if not is_point(c)]
    removed_ids = set()
    remaining = remove_count
    for rank in REMOVAL_RANK_ORDER:
        if remaining <= 0:
            break
        tier = [c for c in non_point if c["rank"] == rank and id(c) not in removed_ids]
        rng.shuffle(tier)
        take = tier[: min(len(tier), remaining)]
        for c in take:
            removed_ids.add(id(c))
        remaining -= len(take)

    final_deck = [c for c in deck if id(c) not in removed_ids]
    rng.shuffle(final_deck)

    seen = {}
    for c in final_deck:
        key = (c["suit"], c["rank"])
        seen[key] = seen.get(key, 0) + 1
        c["occurrence"] = seen[key]
        c["points"] = point_value(c["suit"], c["rank"])
        c["id"] = f"{c['suit']}{c['rank']}#{c['occurrence']}"

    assert len(final_deck) == target, (len(final_deck), target)
    return final_deck


def deal_round_robin(shuffled_deck, num_players):
    """Deal one card at a time to each seat, in shuffled_deck order.
    Index 0 of the returned list = first player to receive a card
    (caller should align this with the seat left of the dealer).
    """
    hands = [[] for _ in range(num_players)]
    for i, card in enumerate(shuffled_deck):
        hands[i % num_players].append(card)
    return hands


def card_label(card) -> str:
    return f"{card['rank']}{SUIT_SYMBOLS[card['suit']]}"


def rank_beats(rank_a: str, rank_b: str) -> bool:
    """True if rank_a is heavier than rank_b (hierarchy, not points)."""
    return RANK_INDEX[rank_a] > RANK_INDEX[rank_b]
