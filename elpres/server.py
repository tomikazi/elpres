"""HTTP + WebSocket server for El Presidente."""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

from aiohttp import web
from aiohttp import WSMsgType

from .engine import GameEngine
from .models import Accolade, Card, Game, GamePhase, GameRoom, Play, Player


# Base path for static files and data
DATA_DIR = Path(os.environ.get("ELPRES_DATA", "/elpres"))
ROOMS: dict[str, GameRoom] = {}
ENGINE = GameEngine()


def room_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return DATA_DIR / f"{safe}.json"


def load_room(name: str) -> GameRoom | None:
    p = room_path(name)
    if not p.exists():
        return None
    raw = p.read_text().strip()
    if not raw:
        logger.info("Room detected: %s (empty file, new room)", name)
        return GameRoom(name=name)  # Empty file = new room
    try:
        data = json.loads(raw)
        if not data:
            logger.info("Room detected: %s (minimal file, new room)", name)
            return GameRoom(name=name)
        room = GameRoom.from_dict(data)
        logger.info("Room loaded: %s (%d players, game=%s)", name, len(room.players), room.current_game is not None)
        return room
    except (json.JSONDecodeError, KeyError):
        logger.info("Room detected: %s (invalid file, new room)", name)
        return GameRoom(name=name)  # Invalid/minimal JSON = new room


def save_room(room: GameRoom):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = room_path(room.name)
    p.write_text(json.dumps(room.to_dict(), indent=2))


def _active_player_ids(room_name: str) -> set[str]:
    """Player ids that currently have an open WebSocket in this room."""
    out = set()
    for pid, ws in WS_CLIENTS.get(room_name, {}).items():
        if not ws.closed:
            out.add(pid)
    return out


def game_state_for_client(room: GameRoom, player_id: str | None) -> dict:
    """Build client-safe state (hide other players' hands)."""
    g = room.current_game
    if not g:
        active = _active_player_ids(room.name)
        lobby_players = [p for p in room.players if p.id in active]
        return {
            "phase": "no_game",
            "room": room.name,
            "players": [{"id": p.id, "name": p.name, "past_accolade": p.past_accolade.value} for p in lobby_players],
        }

    # Find player index
    player_idx = None
    for i, p in enumerate(g.players):
        if p.id == player_id:
            player_idx = i
            break

    # Build player views (hide hands for others)
    players_view = []
    for i, p in enumerate(g.players):
        view = {
            "id": p.id,
            "name": p.name,
            "past_accolade": p.past_accolade.value,
            "accolade": p.accolade.value,
            "card_count": len(p.hand),
            "in_results": p.id in g.results,
            "result_position": g.results.index(p.id) + 1 if p.id in g.results else None,
        }
        if player_idx is not None and i == player_idx:
            view["hand"] = [c.to_dict() for c in p.hand_sorted()]
        players_view.append(view)

    # Pile
    pile_plays = []
    for play in g.round.pile.plays:
        pile_plays.append({"cards": [c.to_dict() for c in play.cards]})

    # Valid plays for current player
    valid_plays = []
    if player_idx is not None and g.current_player_idx == player_idx and g.phase == GamePhase.Playing:
        from .engine import get_valid_plays
        current = g.round.pile.current_play
        num_req = len(current.cards) if current and current.cards else None
        must_3c = (
            not current or not current.cards
        ) and g.round.starting_player_idx == player_idx and (g.rounds_completed or 0) == 0
        combos = get_valid_plays(
            g.players[player_idx].hand,
            current,
            num_req,
            must_3c,
        )
        for combo in combos:
            valid_plays.append([c.to_dict() for c in combo])

    return {
        "phase": g.phase.value,
        "room": room.name,
        "dealer_idx": g.dealer_idx,
        "current_player_idx": g.current_player_idx,
        "players": players_view,
        "round": {
            "starting_player_idx": g.round.starting_player_idx,
            "pile": {"plays": pile_plays},
        },
        "results": g.results,
        "passed_this_round": list(g.passed_this_round),
        "valid_plays": valid_plays,
        "trading": _get_trading_info(g, player_id) if g.phase == GamePhase.Trading else None,
    }


def _get_trading_info(g: Game, player_id: str | None) -> dict | None:
    """Trade cards: face up for EP and SH, face down for others."""
    if not g.trade_high_card and not g.trade_low_card and g.trade_ep_claimed and g.trade_sh_claimed:
        return None
    from .models import Accolade
    is_ep = False
    is_sh = False
    if player_id:
        for p in g.players:
            if p.id == player_id:
                is_ep = p.past_accolade == Accolade.ElPresidente
                is_sh = p.past_accolade == Accolade.Shithead
                break
    face_up = is_ep or is_sh
    return {
        "high_card": g.trade_high_card.to_dict() if face_up and g.trade_high_card else None,
        "low_card": g.trade_low_card.to_dict() if face_up and g.trade_low_card else None,
        "ep_claimed": g.trade_ep_claimed,
        "sh_claimed": g.trade_sh_claimed,
        "face_down": not face_up,
        "trade_count": (1 if g.trade_high_card else 0) + (1 if g.trade_low_card else 0),
    }


async def handle_static(request: web.Request) -> web.Response:
    """Serve static files from static/ under /elpres/."""
    path = request.match_info.get("path", "index.html")
    if not path or path == "/":
        path = "index.html"
    # Prevent directory traversal
    if ".." in path:
        raise web.HTTPForbidden()
    static_dir = Path(__file__).parent.parent / "static"
    file_path = static_dir / path
    if not file_path.exists() or not file_path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(file_path)


async def handle_room(request: web.Request) -> web.Response:
    """Serve game.html for room entry - path /elpres/room/ROOMNAME."""
    room_name = request.match_info.get("name", "")
    static_dir = Path(__file__).parent.parent / "static"
    return web.FileResponse(static_dir / "game.html")


async def handle_join(request: web.Request) -> web.Response:
    """Resolve player name to id for a room (from persisted players). Returns JSON { id } or error."""
    room_name = (request.query.get("room") or "").strip()
    player_name = (request.query.get("name") or "").strip()[:50] or "Player"
    if not room_name:
        return web.json_response({"error": "Missing room"}, status=400)

    room = load_room(room_name)
    if not room:
        room = GameRoom(name=room_name)
        ROOMS[room_name] = room

    existing = next((p for p in room.players if p.name == player_name), None)
    if existing:
        return web.json_response(
            {"id": existing.id},
            headers={"Cache-Control": "no-store"},
        )

    player_id = str(uuid.uuid4())
    player = Player(id=player_id, name=player_name)
    room.players.append(player)
    save_room(room)
    logger.info("Player joined via /join: %s (%s)", room_name, player_name)
    return web.json_response(
        {"id": player_id},
        headers={"Cache-Control": "no-store"},
    )


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    room_name = (request.query.get("room") or "").strip()
    client_id = (request.query.get("id") or "").strip()
    if not room_name:
        await ws.send_json({"type": "error", "message": "Missing room"})
        await ws.close()
        return ws
    if not client_id:
        await ws.send_json({"type": "error", "message": "Missing id"})
        await ws.close()
        return ws

    room = load_room(room_name)
    if not room:
        await ws.send_json({"type": "error", "message": "Room not found"})
        await ws.close()
        return ws

    # After load from disk, ensure room.players includes everyone in the current game
    if room.current_game:
        for gp in room.current_game.players:
            if not any(p.id == gp.id for p in room.players):
                room.players.append(gp)
                save_room(room)
                break

    # Player must already exist (joined via /elpres/join); find by id
    existing = next((p for p in room.players if p.id == client_id), None)
    if not existing:
        await ws.send_json({"type": "error", "message": "Unknown player; join from lobby first"})
        await ws.close()
        return ws

    has_connection = any(
        pid == existing.id and not w.closed
        for pid, w in WS_CLIENTS.get(room_name, {}).items()
    )
    if has_connection:
        await ws.send_json({"type": "error", "message": "Id already in use"})
        await ws.close()
        return ws

    player_id = existing.id
    name_trimmed = existing.name
    is_reconnect = False
    logger.info("Player connected: %s (%s)", room_name, name_trimmed)

    key = (room_name, player_id)
    old_task = DISCONNECT_TASKS.pop(key, None)
    if old_task and not old_task.done():
        old_task.cancel()

    save_room(room)

    # Broadcast state to this client
    state = game_state_for_client(room, player_id)
    await ws.send_json({"type": "state", "state": state, "player_id": player_id})

    register_ws(room_name, player_id, ws)

    # Notify others (skip player_joined for reconnect - they're not new)
    if not is_reconnect:
        await broadcast_except(room_name, player_id, {"type": "player_joined", "player": {"id": player_id, "name": name_trimmed}})
    await broadcast_state(room_name, exclude=player_id)

    voluntary_leave = False
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    room = load_room(room_name)
                    if not room:
                        await ws.send_json({"type": "error", "message": "Room no longer exists"})
                        break
                    data = json.loads(msg.data)
                    cmd = data.get("type")
                    if cmd == "leave":
                        voluntary_leave = True
                        await force_remove_player(room_name, room, player_id)
                        try:
                            await ws.send_json({"type": "you_left"})
                        except Exception:
                            pass
                        break
                    if cmd == "play":
                        err = await handle_play(room, player_id, data)
                        if err:
                            await ws.send_json({"type": "error", "message": err})
                        else:
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                    elif cmd == "pass":
                        err = await handle_pass(room, player_id)
                        if err:
                            await ws.send_json({"type": "error", "message": err})
                        else:
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                    elif cmd == "start_game":
                        err = await handle_start_game(room, player_id)
                        if err:
                            await ws.send_json({"type": "error", "message": err})
                        else:
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                    elif cmd == "claim_trade":
                        err = await handle_claim_trade(room, player_id, data)
                        if err:
                            await ws.send_json({"type": "error", "message": err})
                        else:
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                except json.JSONDecodeError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        unregister_ws(room_name, player_id)
        logger.info("Player left room: %s (%s)", room_name, name_trimmed)
        if not voluntary_leave:
            task = asyncio.create_task(delayed_remove_after_disconnect(room_name, player_id))
            DISCONNECT_TASKS[(room_name, player_id)] = task
        await ws.close()

    return ws


async def handle_play(room: GameRoom, player_id: str, data: dict) -> str | None:
    g = room.current_game
    if not g:
        return "No game in progress"

    player_idx = next((i for i, p in enumerate(g.players) if p.id == player_id), None)
    if player_idx is None:
        return "You are not in this game"

    cards_data = data.get("cards", [])
    if not cards_data:
        return "No cards specified"

    cards = [Card.from_dict(c) for c in cards_data]
    play = Play(cards=cards)

    err = ENGINE.apply_play(g, player_idx, play)
    if err:
        return err

    # Check game over
    in_hand = [p for p in g.players if p.hand]
    if len(in_hand) <= 1:
        if in_hand:
            g.results.append(in_hand[0].id)
        ENGINE.assign_accolades(g)
        results_names = [next(p.name for p in g.players if p.id == r) for r in g.results]
        logger.info("Game stopped: room=%s, results=%s", room.name, results_names)
        await broadcast(room.name, {"type": "game_over", "results": g.results})
        # Start next game after short delay
        asyncio.create_task(start_next_game_after_delay(room))

    return None


async def handle_pass(room: GameRoom, player_id: str) -> str | None:
    g = room.current_game
    if not g:
        return "No game in progress"

    player_idx = next((i for i, p in enumerate(g.players) if p.id == player_id), None)
    if player_idx is None:
        return "You are not in this game"

    return ENGINE.apply_pass(g, player_idx)


async def handle_claim_trade(room: GameRoom, player_id: str, data: dict) -> str | None:
    g = room.current_game
    if not g:
        return "No game in progress"
    role = data.get("role")
    if role not in ("presidente", "shithead"):
        return "Invalid role"
    return ENGINE.apply_claim_trade(g, player_id, role)


async def handle_start_game(room: GameRoom, player_id: str) -> str | None:
    if room.current_game:
        return "Game already in progress"

    # Only players in room (not spectators) - anyone can start
    n = len(room.players)
    if n < 2:
        return "Need at least 2 players"

    prev_ep = prev_sh = None
    # Use past accolades from room players
    for p in room.players:
        if p.past_accolade == Accolade.ElPresidente:
            prev_ep = p.id
        if p.past_accolade == Accolade.Shithead:
            prev_sh = p.id

    room.current_game = ENGINE.start_new_game(room.players, None, prev_ep, prev_sh)
    logger.info("Game started: room=%s, players=%d", room.name, len(room.players))
    return None


async def start_next_game_after_delay(room: GameRoom):
    await asyncio.sleep(3)  # Brief score screen
    room = load_room(room.name)
    if not room:
        return
    prev_dealer = None
    prev_ep = prev_sh = None
    if room.current_game:
        prev_dealer = room.current_game.dealer_idx
        for p in room.current_game.players:
            if p.accolade == Accolade.ElPresidente:
                prev_ep = p.id
            if p.accolade == Accolade.Shithead:
                prev_sh = p.id
        # Update room players with new accolades
        for rp in room.players:
            for gp in room.current_game.players:
                if gp.id == rp.id:
                    rp.past_accolade = gp.accolade
                    break

    if len(room.players) < 2:
        room.current_game = None
        save_room(room)
        logger.info("Next game skipped (not enough players): %s", room.name)
        await broadcast_state(room.name)
        return

    # Players for next game = those in room
    room.current_game = ENGINE.start_new_game(room.players, prev_dealer, prev_ep, prev_sh)
    save_room(room)
    logger.info("Next game started: room=%s", room.name)
    await broadcast_state(room.name)


# WebSocket broadcast tracking
WS_CLIENTS: dict[str, dict[str, web.WebSocketResponse]] = {}  # room -> {player_id -> ws}
# Disconnect grace period: (room_name, player_id) -> asyncio.Task to remove after 60s
DISCONNECT_TASKS: dict[tuple[str, str], asyncio.Task] = {}
DISCONNECT_GRACE_SECONDS = 60


def register_ws(room: str, player_id: str, ws: web.WebSocketResponse):
    if room not in WS_CLIENTS:
        WS_CLIENTS[room] = {}
    WS_CLIENTS[room][player_id] = ws


def unregister_ws(room: str, player_id: str):
    if room in WS_CLIENTS:
        WS_CLIENTS[room].pop(player_id, None)


async def broadcast(room: str, msg: dict):
    if room not in WS_CLIENTS:
        return
    for pid, ws in list(WS_CLIENTS[room].items()):
        try:
            if not ws.closed:
                await ws.send_json(msg)
        except Exception:
            pass


async def broadcast_except(room: str, exclude_id: str, msg: dict):
    if room not in WS_CLIENTS:
        return
    for pid, ws in list(WS_CLIENTS[room].items()):
        if pid == exclude_id:
            continue
        try:
            if not ws.closed:
                await ws.send_json(msg)
        except Exception:
            pass


async def broadcast_state(room: str, exclude: str | None = None, room_obj: GameRoom | None = None):
    if room_obj is None:
        room_obj = load_room(room)
    if not room_obj or room not in WS_CLIENTS:
        return
    for pid, ws in list(WS_CLIENTS[room].items()):
        if pid == exclude:
            continue
        try:
            if not ws.closed:
                state = game_state_for_client(room_obj, pid)
                await ws.send_json({"type": "state", "state": state, "player_id": pid})
        except Exception:
            pass


async def delayed_remove_after_disconnect(room_name: str, player_id: str) -> None:
    """After DISCONNECT_GRACE_SECONDS, remove player from game/room if they did not reconnect."""
    await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
    key = (room_name, player_id)
    DISCONNECT_TASKS.pop(key, None)
    if (room_name, player_id) in WS_CLIENTS.get(room_name, {}):
        return
    room = load_room(room_name)
    if not room:
        return
    await force_remove_player(room_name, room, player_id)


async def force_remove_player(room_name: str, room: GameRoom, player_id: str) -> None:
    """Remove player from game and room; broadcast state or game_over. Caller must have loaded room."""
    g = room.current_game
    player_idx = None
    if g:
        player_idx = next((i for i, p in enumerate(g.players) if p.id == player_id), None)
    if player_idx is not None:
        game_ended = ENGINE.remove_player_from_game(g, player_idx)
        if game_ended:
            save_room(room)
            await broadcast(room_name, {"type": "game_over", "results": g.results})
            asyncio.create_task(start_next_game_after_delay(room))
        else:
            save_room(room)
            await broadcast_state(room_name, room_obj=room)
    room.players = [p for p in room.players if p.id != player_id]
    if not room.players:
        ROOMS.pop(room_name, None)
        fresh_room = GameRoom(name=room_name)
        save_room(fresh_room)
        logger.info("Room reinitialized (all players left): %s", room_name)
    else:
        save_room(room)
        await broadcast_state(room_name, room_obj=room)
    logger.info("Player force-removed: %s (%s)", room_name, player_id)


async def redirect_to_lobby(_request: web.Request) -> web.Response:
    raise web.HTTPFound("/elpres/")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", redirect_to_lobby)
    app.router.add_get("/elpres", redirect_to_lobby)
    app.router.add_get("/elpres/", handle_static)
    app.router.add_get("/elpres/join", handle_join)
    app.router.add_get("/elpres/room/{name}", handle_room)
    app.router.add_get("/elpres/ws", websocket_handler)
    app.router.add_get("/elpres/{path:.*}", handle_static)
    return app


def run():
    app = create_app()
    logger.info("Server started on port 8765")
    web.run_app(app, port=8765)
