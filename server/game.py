"""
Kaadi3 room + round state machine. Pure logic, no networking here.
"""
import random
import string
import time
import uuid

import cards as C

PHASE_LOBBY = "lobby"
PHASE_BIDDING = "bidding"
PHASE_POWER_COLOR = "power_color"
PHASE_PARTNER_SELECT = "partner_select"
PHASE_PLAYING = "playing"
PHASE_ROUND_END = "round_end"


def team_composition(num_players: int, bid_amount: int) -> int:
    """Returns caller's team size (including caller) for a given bid."""
    if num_players == 5:
        return 3 if bid_amount >= 200 else 2
    base = {6: 3, 7: 3, 8: 4}[num_players]
    extra = bid_amount >= 450
    return base + 1 if extra else base


def bid_bounds(num_players: int):
    if num_players == 5:
        return 150, 250, 5
    return 280, 500, 5


class Player:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
        self.connected = True
        self.seat = None  # assigned at game start
        self.session_score = 0
        self.ws = None


class PartnerSlot:
    def __init__(self, suit, rank, occurrence):
        self.suit = suit
        self.rank = rank
        self.occurrence = occurrence
        self.owner_seat = None  # resolved silently at selection time
        self.revealed = False


class Round:
    def __init__(self, room, dealer_seat, rng):
        self.room = room
        self.dealer_seat = dealer_seat
        self.rng = rng
        n = room.num_players
        deck = C.build_playing_deck(n, rng)
        self.deck_snapshot = deck  # for validating partner-card choices
        start = (dealer_seat + 1) % n
        hands = C.deal_round_robin(deck, n)
        # align hands[0] (first dealt) with seat `start`
        self.hands = {}
        for i, h in enumerate(hands):
            seat = (start + i) % n
            self.hands[seat] = sorted(h, key=lambda c: (c["suit"], C.RANK_INDEX[c["rank"]]))

        self.phase = PHASE_BIDDING
        lo, hi, step = bid_bounds(n)
        self.bid_lo, self.bid_hi, self.bid_step = lo, hi, step
        self.active_seats = set(range(n))
        self.turn_seat = start
        self.highest_bid = None  # amount
        self.highest_bidder = None  # seat
        self.bid_history = []  # [{seat, action, amount}]

        self.caller_seat = None
        self.bid_amount = None
        self.power_color = None
        self.partners_needed = 0
        self.partner_slots = []  # PartnerSlot
        self.team_of = {}  # seat -> "caller" | "opposition" (secret to opposition until reveal, tracked server-side regardless)

        self.trick_cards = []  # [{seat, card}]
        self.lead_suit = None
        self.trick_number = 0
        self.cards_played_count = 0
        self.points_won = {s: 0 for s in range(n)}  # points captured by trick-winning seat
        self.trick_winner_history = []
        self.last_trick = None  # for brief UI display after each trick
        self.round_result = None  # filled at round end

    # ---------------- bidding ----------------
    def can_pass(self, seat):
        return len(self.active_seats) > 1

    def place_bid(self, seat, amount):
        if self.phase != PHASE_BIDDING or seat != self.turn_seat:
            raise ValueError("not your turn")
        if amount % self.bid_step != 0 or amount < self.bid_lo or amount > self.bid_hi:
            raise ValueError("invalid bid amount")
        if self.highest_bid is not None and amount <= self.highest_bid:
            raise ValueError("bid must exceed current highest bid")
        self.highest_bid = amount
        self.highest_bidder = seat
        self.bid_history.append({"seat": seat, "action": "bid", "amount": amount})
        self._advance_bidding()

    def pass_bid(self, seat):
        if self.phase != PHASE_BIDDING or seat != self.turn_seat:
            raise ValueError("not your turn")
        if not self.can_pass(seat):
            raise ValueError("you must bid, you are the last active bidder")
        self.active_seats.discard(seat)
        self.bid_history.append({"seat": seat, "action": "pass", "amount": None})
        self._advance_bidding()

    def _advance_bidding(self):
        if len(self.active_seats) == 1:
            last = next(iter(self.active_seats))
            if self.highest_bidder == last:
                self._finish_bidding(last, self.highest_bid)
                return
            # last remaining player hasn't bid yet -- forced to bid next
            self.turn_seat = last
            return
        n = self.room.num_players
        nxt = (self.turn_seat + 1) % n
        while nxt not in self.active_seats:
            nxt = (nxt + 1) % n
        self.turn_seat = nxt

    def _finish_bidding(self, caller_seat, bid_amount):
        self.caller_seat = caller_seat
        self.bid_amount = bid_amount
        self.phase = PHASE_POWER_COLOR
        team_size = team_composition(self.room.num_players, bid_amount)
        self.partners_needed = team_size - 1
        self.team_of = {s: "opposition" for s in range(self.room.num_players)}
        self.team_of[caller_seat] = "caller"

    # ---------------- power color ----------------
    def select_power_color(self, seat, suit):
        if self.phase != PHASE_POWER_COLOR or seat != self.caller_seat:
            raise ValueError("not allowed")
        if suit not in C.SUITS:
            raise ValueError("invalid suit")
        self.power_color = suit
        if self.partners_needed == 0:
            self._start_play()
        else:
            self.phase = PHASE_PARTNER_SELECT

    # ---------------- partner selection ----------------
    def available_partner_options(self):
        """suit -> rank -> list of occurrences present in this round's deck."""
        opts = {}
        for c in self.deck_snapshot:
            opts.setdefault(c["suit"], {}).setdefault(c["rank"], set()).add(c["occurrence"])
        return {
            suit: {rank: sorted(list(occs)) for rank, occs in ranks.items()}
            for suit, ranks in opts.items()
        }

    def _find_owner_seat(self, suit, rank, occurrence):
        for seat, hand in self.hands.items():
            for c in hand:
                if c["suit"] == suit and c["rank"] == rank and c["occurrence"] == occurrence:
                    return seat
        return None

    def select_partner(self, seat, suit, rank, occurrence):
        if self.phase != PHASE_PARTNER_SELECT or seat != self.caller_seat:
            raise ValueError("not allowed")
        if len(self.partner_slots) >= self.partners_needed:
            raise ValueError("all partner slots already filled")
        owner = self._find_owner_seat(suit, rank, occurrence)
        if owner is None:
            raise ValueError("that card is not in play this round")
        if any(p.suit == suit and p.rank == rank and p.occurrence == occurrence for p in self.partner_slots):
            raise ValueError("that card was already requested")
        slot = PartnerSlot(suit, rank, occurrence)
        slot.owner_seat = owner
        if owner != self.caller_seat:
            self.team_of[owner] = "caller"
        # if owner == caller_seat: caller accidentally picked own card -> self-partner, no-op on team
        self.partner_slots.append(slot)
        if len(self.partner_slots) >= self.partners_needed:
            self._start_play()

    # ---------------- play ----------------
    def _start_play(self):
        self.phase = PHASE_PLAYING
        self.turn_seat = self.caller_seat
        self.trick_number = 1

    def legal_cards(self, seat):
        hand = self.hands[seat]
        if not self.trick_cards:
            return [c["id"] for c in hand]
        lead = self.lead_suit
        follow = [c for c in hand if c["suit"] == lead]
        if follow:
            return [c["id"] for c in follow]
        return [c["id"] for c in hand]

    def play_card(self, seat, card_id):
        if self.phase != PHASE_PLAYING or seat != self.turn_seat:
            raise ValueError("not your turn")
        hand = self.hands[seat]
        card = next((c for c in hand if c["id"] == card_id), None)
        if card is None:
            raise ValueError("card not in hand")
        legal = self.legal_cards(seat)
        if card_id not in legal:
            raise ValueError("must follow suit")
        hand.remove(card)
        if not self.trick_cards:
            self.lead_suit = card["suit"]
        self.trick_cards.append({"seat": seat, "card": card})
        self.cards_played_count += 1

        # partner reveal check
        for slot in self.partner_slots:
            if not slot.revealed and slot.suit == card["suit"] and slot.rank == card["rank"] and slot.occurrence == card["occurrence"]:
                slot.revealed = True

        n = self.room.num_players
        if len(self.trick_cards) == n:
            self._resolve_trick()
        else:
            nxt = (seat + 1) % n
            self.turn_seat = nxt

    def _resolve_trick(self):
        lead = self.lead_suit
        power = self.power_color
        played = self.trick_cards

        def strength(entry):
            c = entry["card"]
            if c["suit"] == power:
                bucket = 2 if power != lead else 1
            elif c["suit"] == lead:
                bucket = 1
            else:
                bucket = 0
            return (bucket, C.RANK_INDEX[c["rank"]])

        winner_entry = max(played, key=strength)
        winner_seat = winner_entry["seat"]
        pts = sum(e["card"]["points"] for e in played)
        self.points_won[winner_seat] += pts
        self.last_trick = {
            "cards": [{"seat": e["seat"], "card": e["card"]} for e in played],
            "winner_seat": winner_seat,
            "points": pts,
        }
        self.trick_winner_history.append(winner_seat)

        self.trick_cards = []
        self.lead_suit = None
        self.trick_number += 1

        if self.cards_played_count >= self.room.num_players * C.CARDS_PER_PLAYER[self.room.num_players]:
            self._end_round()
        else:
            self.turn_seat = winner_seat

    def _end_round(self):
        self.phase = PHASE_ROUND_END
        n = self.room.num_players
        caller_pts = sum(self.points_won[s] for s in range(n) if self.team_of[s] == "caller")
        opp_pts = sum(self.points_won[s] for s in range(n) if self.team_of[s] == "opposition")
        won = caller_pts >= self.bid_amount
        deltas = {s: 0 for s in range(n)}
        for s in range(n):
            if self.team_of[s] == "caller":
                if s == self.caller_seat:
                    deltas[s] = self.bid_amount * 2 if won else -self.bid_amount
                else:
                    deltas[s] = self.bid_amount if won else 0
            else:
                deltas[s] = 0 if won else self.bid_amount
        self.round_result = {
            "caller_seat": self.caller_seat,
            "bid_amount": self.bid_amount,
            "caller_points": caller_pts,
            "opposition_points": opp_pts,
            "won": won,
            "deltas": deltas,
            "team_of": dict(self.team_of),
        }
        for seat, delta in deltas.items():
            pid = self.room.seat_player[seat]
            self.room.players[pid].session_score += delta


class Room:
    def __init__(self, code):
        self.code = code
        self.host_id = None
        self.players = {}  # id -> Player, insertion order = join order
        self.num_players = None
        self.num_decks = None
        self.phase = PHASE_LOBBY
        self.seat_player = {}  # seat -> player id
        self.player_seat = {}  # player id -> seat
        self.dealer_seat = None
        self.round = None
        self.created_at = time.time()

    def name_taken(self, name):
        return any(p.name.lower() == name.lower() for p in self.players.values())

    def add_player(self, name):
        if self.name_taken(name):
            raise ValueError("name_taken")
        if self.num_players is not None and len(self.players) >= self.num_players and self.phase == PHASE_LOBBY:
            raise ValueError("room_full")
        if self.phase != PHASE_LOBBY:
            raise ValueError("game_in_progress")
        pid = uuid.uuid4().hex[:12]
        p = Player(pid, name)
        self.players[pid] = p
        if self.host_id is None:
            self.host_id = pid
        return p

    def set_num_players(self, n):
        if n < 5 or n > 8:
            raise ValueError("num_players must be 5-8")
        self.num_players = n
        self.num_decks = C.num_decks_for(n)

    def ready_to_start(self):
        return self.num_players is not None and len(self.players) == self.num_players

    def start_game(self, rng=None):
        rng = rng or random.Random()
        if not self.ready_to_start():
            raise ValueError("room not full")
        ids = list(self.players.keys())
        rng.shuffle(ids)
        for seat, pid in enumerate(ids):
            self.seat_player[seat] = pid
            self.player_seat[pid] = seat
            self.players[pid].seat = seat
        self.dealer_seat = rng.randrange(self.num_players)
        self.phase = PHASE_BIDDING
        self.round = Round(self, self.dealer_seat, rng)

    def start_next_round(self, rng=None):
        rng = rng or random.Random()
        self.dealer_seat = (self.dealer_seat + 1) % self.num_players
        self.phase = PHASE_BIDDING
        self.round = Round(self, self.dealer_seat, rng)

    def remove_player_reset_session(self, pid):
        """A player exits: remove them, reset session scores, back to lobby."""
        if pid in self.players:
            del self.players[pid]
        for p in self.players.values():
            p.session_score = 0
            p.seat = None
        self.seat_player = {}
        self.player_seat = {}
        self.dealer_seat = None
        self.round = None
        self.phase = PHASE_LOBBY
        if self.host_id == pid:
            self.host_id = next(iter(self.players), None)


def gen_room_code(existing):
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    while True:
        code = "".join(random.choice(alphabet) for _ in range(5))
        if code not in existing:
            return code
