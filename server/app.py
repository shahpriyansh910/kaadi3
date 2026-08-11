import os
import random

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import cards as C
from game import Room, gen_room_code, PHASE_LOBBY, PHASE_PLAYING

app = FastAPI()

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
    for pid, ws in conns.items():
        try:
            await ws.send_json(serialize_room(room, pid))
        except Exception:
            dead.append(pid)
    for pid in dead:
        conns.pop(pid, None)


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
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")

            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if mtype == "create_room":
                name = (msg.get("name") or "").strip()[:20]
                num_players = msg.get("numPlayers")
                if not name:
                    await send_error(websocket, "Enter a name")
                    continue
                if num_players not in (5, 6, 7, 8):
                    await send_error(websocket, "Pick 5-8 players")
                    continue
                new_code = gen_room_code(rooms.keys())
                room = Room(new_code)
                room.set_num_players(num_players)
                try:
                    player = room.add_player(name)
                except ValueError:
                    await send_error(websocket, "Could not create room")
                    continue
                rooms[new_code] = room
                await register_conn(new_code, player.id, websocket)
                code, pid = new_code, player.id
                await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
                await broadcast(room)

            elif mtype == "join_room":
                jcode = (msg.get("code") or "").strip().upper()
                name = (msg.get("name") or "").strip()[:20]
                room = rooms.get(jcode)
                if not room:
                    await send_error(websocket, "Room not found")
                    continue
                if not name:
                    await send_error(websocket, "Enter a name")
                    continue
                if room.name_taken(name):
                    await websocket.send_json({"type": "name_taken"})
                    continue
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
                    continue
                code, pid = jcode, player.id
                await register_conn(code, pid, websocket)
                await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
                await broadcast(room)

            elif mtype == "rejoin":
                jcode = (msg.get("code") or "").strip().upper()
                jpid = msg.get("playerId")
                room = rooms.get(jcode)
                if not room or jpid not in room.players:
                    await send_error(websocket, "Session not found")
                    continue
                code, pid = jcode, jpid
                room.players[pid].connected = True
                await register_conn(code, pid, websocket)
                await websocket.send_json({"type": "joined", "code": code, "playerId": pid})
                await broadcast(room)

            else:
                room = rooms.get(code) if code else None
                if not room or not pid:
                    await send_error(websocket, "Not in a room")
                    continue

                try:
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
                        await broadcast(room)
                        code = pid = None
                        continue
                    else:
                        raise ValueError(f"unknown message type {mtype}")
                except ValueError as e:
                    await send_error(websocket, str(e))
                    continue

                await broadcast(room)

    except WebSocketDisconnect:
        if code and pid and code in rooms:
            room = rooms[code]
            if pid in room.players:
                room.players[pid].connected = False
            room_conns.get(code, {}).pop(pid, None)
            await broadcast(room)


app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="static")
