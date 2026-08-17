"""
Append-only game-history logger. Each completed round (any player count,
human or bot) gets one JSON line appended to LOG_PATH, containing exactly
enough to deterministically replay the round later through the real
game.py engine: the shuffled/trimmed deck it was dealt from, and the
ordered list of actions taken. No player names/ids are recorded -- only
seat numbers and whether each seat was bot-controlled -- since nothing in
this game's logic or the training encoding needs identity, only what
happened.

Render's free tier wipes local disk on every redeploy, so this file is
NOT a durable archive by itself -- see the /admin/export-games endpoint
in app.py, which is meant to be pulled and committed into the repo's
data/ directory before any future deploy.
"""
import json
import logging
import os
import time
import uuid

logger = logging.getLogger("kaadi3")

LOG_PATH = os.environ.get(
    "GAME_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "games.jsonl"),
)


def replay_round(entry):
    """Reconstruct a logged round by replaying its actions through the real
    game.py engine (deterministic given the same deck_snapshot). Returns
    the finished Round. This is both the correctness check for the log
    format and the mechanism any future training script would use to turn
    a logged entry back into full per-step state for whatever observation
    encoding it needs -- import-time only, no network/DB required.
    """
    import game as G

    room = G.Room("REPLAY")
    room.set_num_players(entry["num_players"])
    for i in range(entry["num_players"]):
        room.add_player(f"replay{i}")
    ids = list(room.players.keys())
    for seat, pid in enumerate(ids):
        room.seat_player[seat] = pid
        room.player_seat[pid] = seat
        room.players[pid].seat = seat
    room.phase = G.PHASE_BIDDING
    round_ = G.Round(room, entry["dealer_seat"], rng=None, deck_snapshot=entry["deck_snapshot"])
    room.round = round_

    dispatch = {
        "bid": lambda a: round_.place_bid(a["seat"], a["amount"]),
        "pass": lambda a: round_.pass_bid(a["seat"]),
        "power_color": lambda a: round_.select_power_color(a["seat"], a["suit"]),
        "partner": lambda a: round_.select_partner(a["seat"], a["suit"], a["rank"], a["occurrence"]),
        "play": lambda a: round_.play_card(a["seat"], a["cardId"]),
    }
    for action in entry["actions"]:
        dispatch[action["type"]](action)
    return round_


def log_completed_round(room, round_):
    """Persist one completed round. Safe to call multiple times for the
    same round (e.g. from more than one broadcast tick) -- only the first
    call actually writes, via round_.logged."""
    if round_.logged:
        return
    try:
        entry = {
            "round_id": uuid.uuid4().hex,
            "logged_at": time.time(),
            "num_players": room.num_players,
            "num_decks": room.num_decks,
            "dealer_seat": round_.dealer_seat,
            "deck_snapshot": round_.deck_snapshot,
            # A round only exists after start_game() assigns every seat
            # 0..num_players-1, so this is always fully aligned by position.
            "is_bot": [room.players[room.seat_player[s]].is_bot for s in range(room.num_players)],
            "actions": round_.action_log,
            "round_result": round_.round_result,
        }
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        round_.logged = True
    except Exception:
        # Logging games is a nice-to-have, never worth taking a real game
        # down over -- log the failure and move on.
        logger.exception("failed to log completed round")
