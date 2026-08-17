import hmac
import logging
import os
import random

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaadi3")

import cards as C
from game import Room, gen_room_code, PHASE_LOBBY, PHASE_PLAYING, PHASE_ROUND_END
import game_logger

app = FastAPI()


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Force the client to always revalidate index.html/app.js/style.css
    with the server instead of serving a stale cached copy after a deploy.
    'no-cache' still allows a fast 304 when the file is unchanged -- it just
    guarantees a real deploy is never masked by the browser's own cache."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.add_middleware(NoCacheMiddleware)

rooms: dict[str, Room] = {}
room_conns: dict[str, dict[str, WebSocket]] = {}

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")


def serialize_room(room: Room, viewer_id: str):
    players_public = []
    for pid, p in room.players.items():
        players_public.append({
            "id": pid,
            "name": p.name,
            "seat": p.seat,
            "connected": p.connected,
            "isHost": pid == room.host_id,
            "isYou": pid == viewer_id,
            "sessionScore": p.session_score,
        })
    state = {
        "type": "state",
        "code": room.code,
        "hostId": room.host_id,
        "youAreHost": viewer_id == room.host_id,
        "numPlayers": room.num_players,
        "numDecks": room.num_decks,
        "phase": room.phase,
        "players": players_public,
        "dealerSeat": room.dealer_seat,
        "you": {"id": viewer_id, "seat": room.player_seat.get(viewer_id)},
    }
    r = room.round
    if r:
        vs = room.player_seat.get(viewer_id)
        state["round"] = {
            "phase": r.phase,
            "bidLo": r.bid_lo,
            "bidHi": r.bid_hi,
            "bidStep": r.bid_step,
            "highestBid": r.highest_bid,
            "highestBidder": r.highest_bidder,
            "activeSeats": sorted(r.active_seats),
            "turnSeat": r.turn_seat,
            "bidHistory": r.bid_history,
            "callerSeat": r.caller_seat,
            "bidAmount": r.bid_amount,
            "powerColor": r.power_color,
            "partnersNeeded": r.partners_needed,
            "partnerRequests": [
                {
                    "suit": s.suit,
                    "rank": s.rank,
                    "occurrence": s.occurrence,
                    "revealed": s.revealed,
                    "ownerSeat": s.owner_seat if (s.revealed or s.owner_seat == vs) else None,
                }
                for s in r.partner_slots
            ],
            "teamOfYou": r.team_of.get(vs) if r.team_of else None,
            "trickCards": [{"seat": e["seat"], "card": e["card"]} for e in r.trick_cards],
            "leadSuit": r.lead_suit,
            "trickNumber": r.trick_number,
            "lastTrick": r.last_trick,
            "pointsWon": r.points_won,
            "hand": r.hands.get(vs, []) if vs is not None else [],
            "legalCardIds": r.legal_cards(vs) if (r.phase == PHASE_PLAYING and vs == r.turn_seat) else [],
            "roundResult": r.round_result,
        }
        if r.phase == "partner_select" and vs == r.caller_seat:
            state["round"]["partnerOptions"] = r.available_partner_options()
    return state


async def broadcast(room: Room):
    conns = room_conns.get(room.code, {})
    dead = []
    for pid, ws in list(conns.items()):
        try:
            await ws.send_json(serialize_room(room, pid))
        except Exception:
            dead.append((pid, ws))
    for pid, ws in dead:
        # Same identity check as the disconnect handler: the `await` above
        # yields control, so a reconnect could have already replaced this
        # pid's entry with a fresh working socket by the time we get here.
        if conns.get(pid) is ws:
            conns.pop(pid, None)


async def advance_and_broadcast(room: Room):
    """Log the round to game_logger the moment it ends, then broadcast --
    replaces every plain `await broadcast(room)` call site so a completed
    round is never missed no matter which action ended it."""
    if room.round and room.round.phase == PHASE_ROUND_END:
        game_logger.log_completed_round(room, room.round)
    await broadcast(room)


async def send_error(ws: WebSocket, message: str):
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass


async def register_conn(code: str, pid: str, ws: WebSocket):
    """Bind pid -> ws, closing any previous stale socket for the same pid
    (e.g. the same session opened in a second tab) so it doesn't sit
    silently orphaned."""
    conns = room_conns.setdefault(code, {})
    old = conns.get(pid)
    if old is not None and old is not ws:
        try:
            await old.close()
        except Exception:
            pass
    conns[pid] = ws


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    code = None
    pid = None

    async def handle(msg: dict):
        nonlocal code, pid
        mtype = msg.get("type")

        if mtype == "ping":
            await websocket.send_json({"type": "pong"})
            return

        if mtype == "create_room":
            name = (msg.get("name") or "").strip()[:20]
            num_players = msg.get("numPlayers")
            if not name:
                await send_error(websocket, "Enter a name")
                return
            if num_players not in (5, 6, 7, 8):
                await send_error(websocket, "Pick 5-8 players")
                return
            new_code = gen_room_code(rooms.keys())
            room = Room(new_code)
            room.set_num_players(num_players)
            try:
                player = room.add_player(name)
            except ValueError:
                await send_error(websocket, "Could not create room")
                return
            rooms[new_code] = room
            await register_conn(new_code, player.id, websocket)
            code, pid = new_code, player.id
            await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
            await advance_and_broadcast(room)

        elif mtype == "join_room":
            jcode = (msg.get("code") or "").strip().upper()
            name = (msg.get("name") or "").strip()[:20]
            room = rooms.get(jcode)
            if not room:
                await send_error(websocket, "Room not found")
                return
            if not name:
                await send_error(websocket, "Enter a name")
                return
            if room.name_taken(name):
                await websocket.send_json({"type": "name_taken"})
                return
            try:
                player = room.add_player(name)
            except ValueError as e:
                reason = str(e)
                if reason == "room_full":
                    await send_error(websocket, "Room is full")
                elif reason == "game_in_progress":
                    await send_error(websocket, "Game already in progress")
                else:
                    await send_error(websocket, "Could not join room")
                return
            code, pid = jcode, player.id
            await register_conn(code, pid, websocket)
            await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
            await advance_and_broadcast(room)

        elif mtype == "rejoin":
            jcode = (msg.get("code") or "").strip().upper()
            jpid = msg.get("playerId")
            room = rooms.get(jcode)
            if not room or jpid not in room.players:
                # Room no longer exists (e.g. the server process restarted --
                # a free-tier host can be replaced on deploys/maintenance,
                # which wipes all in-memory game state). Tell the client
                # explicitly so it can drop the stale session instead of
                # sitting frozen on the last screen it rendered.
                await websocket.send_json({"type": "session_gone"})
                return
            code, pid = jcode, jpid
            room.players[pid].connected = True
            await register_conn(code, pid, websocket)
            await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
            await advance_and_broadcast(room)

        else:
            room = rooms.get(code) if code else None
            if not room or not pid:
                await send_error(websocket, "Not in a room")
                return

            if mtype == "set_num_players":
                if pid != room.host_id or room.phase != PHASE_LOBBY:
                    raise ValueError("not allowed")
                room.set_num_players(msg.get("numPlayers"))
            elif mtype == "start_game":
                if pid != room.host_id:
                    raise ValueError("only host can start")
                room.start_game(random.Random())
            elif mtype == "bid":
                seat = room.player_seat.get(pid)
                room.round.place_bid(seat, int(msg.get("amount")))
            elif mtype == "pass":
                seat = room.player_seat.get(pid)
                room.round.pass_bid(seat)
            elif mtype == "select_power_color":
                seat = room.player_seat.get(pid)
                room.round.select_power_color(seat, msg.get("suit"))
            elif mtype == "select_partner":
                seat = room.player_seat.get(pid)
                room.round.select_partner(seat, msg.get("suit"), msg.get("rank"), int(msg.get("occurrence", 1)))
            elif mtype == "play_card":
                seat = room.player_seat.get(pid)
                room.round.play_card(seat, msg.get("cardId"))
            elif mtype == "next_round":
                if pid != room.host_id:
                    raise ValueError("only host can advance")
                room.start_next_round(random.Random())
            elif mtype == "leave_room":
                room.remove_player_reset_session(pid)
                room_conns.get(code, {}).pop(pid, None)
                if not room.players:
                    rooms.pop(code, None)
                    room_conns.pop(code, None)
                await advance_and_broadcast(room)
                code = pid = None
                return
            else:
                raise ValueError(f"unknown message type {mtype}")

            await advance_and_broadcast(room)

    try:
        while True:
            msg = await websocket.receive_json()
            try:
                await handle(msg)
            except ValueError as e:
                await send_error(websocket, str(e))
            except Exception:
                # A bug in handling one message must never take the whole
                # connection (or, worse, other players' connections) down --
                # log it, tell this client something went wrong, and keep
                # the socket alive so their session survives.
                logger.exception("unhandled error handling message: %r", msg)
                await send_error(websocket, "Something went wrong -- please try again")

    except WebSocketDisconnect:
        pass
    finally:
        if code and pid and code in rooms:
            room = rooms[code]
            conns = room_conns.get(code, {})
            # Only clean up if THIS socket is still the one on record for pid.
            # A refresh opens a new connection that calls register_conn(),
            # which re-registers pid under the new socket and closes this
            # old one -- that close then lands us here too. Without this
            # identity check we would blindly pop the *new* connection out
            # of room_conns (since pop only cares about the key, not which
            # socket it points to), silently dropping the just-reconnected
            # player from every future broadcast until they refreshed again.
            if conns.get(pid) is websocket:
                if pid in room.players:
                    room.players[pid].connected = False
                conns.pop(pid, None)
                try:
                    await advance_and_broadcast(room)
                except Exception:
                    logger.exception("error broadcasting after disconnect")


@app.get("/admin/export-games")
async def export_games(request: Request):
    """Pull-before-deploy escape hatch for game_logger's local (ephemeral
    on Render's free tier) log file -- returns its raw JSONL content so it
    can be merged into the repo's data/ directory before the next deploy
    wipes it. Gated on ADMIN_TOKEN: unset means the endpoint is disabled
    entirely (fails closed), not "open to anyone"."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        return Response(status_code=404)
    provided = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(provided, admin_token):
        return Response(status_code=404)
    if not os.path.exists(game_logger.LOG_PATH):
        return Response(content="", media_type="text/plain")
    with open(game_logger.LOG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/plain")


app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="static")
