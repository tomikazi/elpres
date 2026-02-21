"""HTTP + WebSocket server for El Presidente."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

ROOM_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

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
    """Build client-safe state (hide other players' hands). Lobby list = only currently connected players (no grace period)."""
    g = room.current_game
    if not g or len(g.players) == 0:
        active = _active_player_ids(room.name)
        if player_id:
            active = active | {player_id}  # so the requesting client sees themselves in the list
        lobby_players = [p for p in room.players if p.id in active]
        return {
            "phase": "no_game",
            "room": room.name,
            "players": [{"id": p.id, "name": p.name, "past_accolade": p.past_accolade.value} for p in lobby_players],
            "dick_tagged_player_id": getattr(room, "dick_tagged_player_id", None),
        }

    # Find player index (normalize to str so we never miss due to type mismatch)
    player_idx = None
    if player_id is not None:
        pid_str = str(player_id)
        for i, p in enumerate(g.players):
            if str(p.id) == pid_str:
                player_idx = i
                break

    # Build player views: only the requesting player gets their own hand; never send other players' cards
    players_view = []
    for i, p in enumerate(g.players):
        key = (room.name, p.id)
        t = DISCONNECT_TASKS.get(key)
        disconnected = t is not None and not t.done()
        view = {
            "id": p.id,
            "name": p.name,
            "past_accolade": p.past_accolade.value,
            "accolade": p.accolade.value,
            "card_count": len(p.hand),
            "in_results": p.id in g.results,
            "result_position": g.results.index(p.id) + 1 if p.id in g.results else None,
            "disconnected": disconnected,
        }
        if player_idx is not None and i == player_idx:
            view["hand"] = [c.to_dict() for c in p.hand_sorted()]
        # Do not add "hand" for other players - prevents leaking card details (anti-cheating)
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

    state = {
        "phase": g.phase.value,
        "room": room.name,
        "dealer_idx": g.dealer_idx,
        "current_player_idx": g.current_player_idx,
        "players": players_view,
        "round": {
            "starting_player_idx": g.round.starting_player_idx,
            "last_play_player_idx": g.round.last_play_player_idx,
            "pile": {"plays": pile_plays},
        },
        "rounds_completed": g.rounds_completed,
        "results": g.results,
        "passed_this_round": list(g.passed_this_round),
        "valid_plays": valid_plays,
        "trading": _get_trading_info(g, player_id) if g.phase == GamePhase.Trading else None,
        "dick_tagged_player_id": getattr(room, "dick_tagged_player_id", None),
    }
    # If it's a disconnected player's turn (including when they were already current and then
    # timed out), include waiting countdown so the "waiting for player" flyover shows for others.
    if g.phase == GamePhase.Playing and 0 <= g.current_player_idx < len(g.players):
        current_id = g.players[g.current_player_idx].id
        key = (room.name, current_id)
        if key in DISCONNECT_TASKS and (t := DISCONNECT_TASKS.get(key)) and not t.done():
            start = DISCONNECT_START.get(key)
            if start is not None:
                elapsed = time.monotonic() - start
                secs = max(0, int(DISCONNECT_GRACE_SECONDS - elapsed))
                state["waiting_for_disconnected"] = {
                    "player_name": g.players[g.current_player_idx].name,
                    "seconds_remaining": secs,
                }
    if "waiting_for_disconnected" not in state:
        state["waiting_for_disconnected"] = None
    # Single source of truth: is this client actively in the game or spectating?
    actively_playing = player_idx is not None
    # Failsafe: if we included a hand (only done for in-game player), never mark as spectator
    if any(v.get("hand") is not None for v in state["players"]):
        actively_playing = True
    state["spectator"] = not actively_playing
    if state["spectator"] and player_id:
        state["wants_to_play"] = room.spectator_preferences.get(player_id, True)
    return state


def _get_trading_info(g: Game, player_id: str | None) -> dict | None:
    """Trade cards: face up for EP and SH, face down for others."""
    if not g.trade_high_card and not g.trade_low_card and g.trade_ep_claimed and g.trade_sh_claimed:
        return None
    from .models import Accolade
    is_ep = False
    is_sh = False
    if player_id:
        pid_str = str(player_id)
        for p in g.players:
            if str(p.id) == pid_str:
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
    room_name = (request.query.get("room") or "").strip().lower()
    player_name = (request.query.get("name") or "").strip()
    if not room_name:
        return web.json_response({"error": "Missing room"}, status=400)
    if not ROOM_NAME_RE.match(room_name):
        return web.json_response({"error": "Room name may only contain letters, numbers, hyphens, and underscores"}, status=400)
    if len(room_name) > 20:
        return web.json_response({"error": "Room name must be 20 characters or less"}, status=400)
    if not player_name:
        player_name = "Player"
    elif len(player_name) > 20:
        return web.json_response({"error": "Name must be 20 characters or less"}, status=400)

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

    room_name = (request.query.get("room") or "").strip().lower()
    client_id = (request.query.get("id") or "").strip()
    if not room_name:
        await ws.send_json({"type": "error", "message": "Missing room"})
        await ws.close()
        return ws
    if not ROOM_NAME_RE.match(room_name):
        await ws.send_json({"type": "error", "message": "Room name may only contain letters, numbers, hyphens, and underscores"})
        await ws.close()
        return ws
    if len(room_name) > 20:
        await ws.send_json({"type": "error", "message": "Room name must be 20 characters or less"})
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

    # Key must match game_state_for_client and heartbeat loop: (room.name, player_id)
    key = (room.name, player_id)
    old_task = DISCONNECT_TASKS.pop(key, None)
    DISCONNECT_START.pop(key, None)
    if old_task and not old_task.done():
        old_task.cancel()

    save_room(room)

    # Broadcast state to this client
    state = game_state_for_client(room, player_id)
    await ws.send_json({"type": "state", "state": state, "player_id": player_id})

    register_ws(room_name, player_id, ws)
    LAST_HEARTBEAT[(room_name, player_id)] = time.monotonic()

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
                    LAST_HEARTBEAT[(room_name, player_id)] = time.monotonic()
                    if cmd == "heartbeat":
                        key = (room.name, player_id)
                        if key in DISCONNECT_TASKS:
                            old_task = DISCONNECT_TASKS.pop(key, None)
                            DISCONNECT_START.pop(key, None)
                            if old_task and not old_task.done():
                                old_task.cancel()
                            await broadcast_state(room_name, room_obj=room)
                    elif cmd == "state_request":
                        state = game_state_for_client(room, player_id)
                        await ws.send_json({"type": "state", "state": state, "player_id": player_id})
                    elif cmd == "leave":
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
                    elif cmd == "request_restart_vote":
                        err = await handle_request_restart_vote(room, player_id)
                        if err:
                            await ws.send_json({"type": "error", "message": err})
                    elif cmd == "restart_vote":
                        await handle_restart_vote(room, player_id, data)
                    elif cmd == "spectator_preference":
                        want = data.get("want_to_play")
                        if want is True or want is False:
                            room.spectator_preferences[player_id] = want
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                    elif cmd == "tag_dick":
                        err = await handle_tag_dick(room, player_id, data)
                        if not err:
                            save_room(room)
                            await broadcast_state(room_name, room_obj=room)
                        # On error: silently refuse (no alert)
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
        LAST_HEARTBEAT.pop((room_name, player_id), None)
        logger.info("Player left room: %s (%s)", room_name, name_trimmed)
        if not voluntary_leave:
            room = load_room(room_name)
            if room and room.current_game is None:
                # Lobby: no grace period; remove immediately so list updates for everyone
                room.players = [p for p in room.players if p.id != player_id]
                if getattr(room, "dick_tagged_player_id", None) == player_id:
                    room.dick_tagged_player_id = None
                    room.dick_tagged_at = None
                save_room(room)
                await broadcast_state(room_name, room_obj=room)
            else:
                # Game in progress: 60s reconnect grace before ejecting
                key = (room.name, player_id)
                DISCONNECT_START[key] = time.monotonic()
                task = asyncio.create_task(delayed_remove_after_disconnect(room_name, player_id))
                DISCONNECT_TASKS[key] = task
                # Notify other players so they see this player as disconnected and, if their turn, the flyover
                await broadcast_state(room_name, exclude=player_id, room_obj=room)
                await broadcast_except(room_name, player_id, {"type": "player_disconnected", "player_id": player_id})
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
        _cancel_pending_next_game(room.name)
        PENDING_NEXT_GAME_TASKS[room.name] = asyncio.create_task(start_next_game_after_delay(room))

    return None


async def handle_pass(room: GameRoom, player_id: str) -> str | None:
    g = room.current_game
    if not g:
        return "No game in progress"

    player_idx = next((i for i, p in enumerate(g.players) if p.id == player_id), None)
    if player_idx is None:
        return "You are not in this game"

    return ENGINE.apply_pass(g, player_idx)


DICK_TAG_COOLDOWN_SECONDS = 15


async def handle_tag_dick(room: GameRoom, player_id: str, data: dict) -> str | None:
    """Tag another player as dick. Only one at a time. Cannot tag yourself.
    Can only be granted by the current holder (after 15s cooldown) or by anyone when no one has it."""
    target_id = data.get("target_player_id")
    if not target_id:
        return "No target player specified"
    target_id = str(target_id)
    if target_id == str(player_id):
        return "Cannot tag yourself"
    if not any(p.id == target_id for p in room.players):
        return "Player not in room"
    current = getattr(room, "dick_tagged_player_id", None)
    dick_tagged_at = getattr(room, "dick_tagged_at", None)
    player_str = str(player_id)
    if current == target_id:
        # Toggle off: only holder can remove it from themselves
        if player_str != str(current):
            return "Only the current holder can remove the plant"
        room.dick_tagged_player_id = None
        room.dick_tagged_at = None
    else:
        # Grant/transfer
        if current is None:
            # No one has it: anyone can grant (e.g. at start of game)
            room.dick_tagged_player_id = target_id
            room.dick_tagged_at = time.time()
        else:
            # Someone has it: only holder can transfer, and only after 15 seconds
            if player_str != str(current):
                return "Only the current holder can pass the plant"
            elapsed = time.time() - (dick_tagged_at or 0)
            if elapsed < DICK_TAG_COOLDOWN_SECONDS:
                remaining = int(DICK_TAG_COOLDOWN_SECONDS - elapsed)
                return f"Wait {remaining}s before passing the plant"
            room.dick_tagged_player_id = target_id
            room.dick_tagged_at = time.time()
    return None


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


async def _check_restart_vote_result(room_name: str) -> None:
    """Called after 30s. Not voting = no. 50%+ yes = restart; else reject."""
    await asyncio.sleep(RESTART_VOTE_DURATION_SECONDS)
    vote_state = RESTART_VOTE_STATE.pop(room_name, None)
    if not vote_state:
        return
    room = load_room(room_name)
    if not room or not room.current_game:
        await broadcast(room_name, {"type": "restart_vote_rejected"})
        return
    voters = [p.id for p in room.current_game.players]
    for pid in voters:
        if str(pid) not in vote_state["votes"]:
            vote_state["votes"][str(pid)] = "no"
    await _resolve_restart_vote(room_name, room, vote_state, from_timeout=True)


async def handle_request_restart_vote(room: GameRoom, player_id: str) -> str | None:
    """Start a restart vote. Broadcasts to all clients."""
    if not room.current_game:
        return "No game in progress"
    player_idx = next((i for i, p in enumerate(room.current_game.players) if p.id == player_id), None)
    if player_idx is None:
        return "You are not in this game"
    initiator = room.current_game.players[player_idx]
    _cancel_restart_vote(room.name)
    task = asyncio.create_task(_check_restart_vote_result(room.name))
    vote_state = {
        "initiator_id": player_id,
        "initiator_name": initiator.name,
        "votes": {str(player_id): "yes"},
        "task": task,
    }
    RESTART_VOTE_STATE[room.name] = vote_state
    await broadcast_except(room.name, player_id, {"type": "restart_vote_requested", "initiator_name": initiator.name})
    await _resolve_restart_vote(room.name, room, vote_state)
    return None


async def _resolve_restart_vote(room_name: str, room: GameRoom, vote_state: dict, from_timeout: bool = False) -> bool:
    """Check votes and resolve if we have enough. Returns True if resolved. from_timeout=True when called after 30s (don't cancel ourselves)."""
    voters = [p.id for p in room.current_game.players]
    votes = vote_state.get("votes", {})
    n_voters = len(voters)
    n_yes = sum(1 for pid in voters if votes.get(str(pid)) == "yes")
    n_no = sum(1 for pid in voters if votes.get(str(pid)) == "no")
    votes_needed = n_voters if n_voters == 2 else (n_voters + 1) // 2  # 100% for 2 players, 50%+ otherwise
    if n_yes >= votes_needed:
        if not from_timeout:
            vote_state["task"].cancel()
            RESTART_VOTE_STATE.pop(room_name, None)
        _cancel_pending_next_game(room_name)
        for p in room.players:
            p.past_accolade = Accolade.Pleb
        MAX_PLAYERS = 7
        game_player_ids = {p.id for p in room.current_game.players}
        spectators_who_want_in = [
            p for p in room.players
            if p.id not in game_player_ids and room.spectator_preferences.get(p.id, True)
        ]
        players = list(room.current_game.players)
        for p in players:
            p.past_accolade = Accolade.Pleb
        for sp in spectators_who_want_in:
            if len(players) >= MAX_PLAYERS:
                break
            players.append(sp)
        if len(players) >= 2:
            room.current_game = ENGINE.start_new_game(players, None, None, None)
            save_room(room)
            await broadcast(room_name, {"type": "restart_vote_passed"})
            await broadcast_state(room_name, room_obj=room)
            logger.info("Game restarted via vote: room=%s", room_name)
        else:
            await broadcast(room_name, {"type": "restart_vote_rejected"})
        return True
    if n_no > n_voters - votes_needed:
        if not from_timeout:
            vote_state["task"].cancel()
            RESTART_VOTE_STATE.pop(room_name, None)
        await broadcast(room_name, {"type": "restart_vote_rejected"})
        return True
    return False


async def handle_restart_vote(room: GameRoom, player_id: str, data: dict) -> str | None:
    """Record a player's vote (yes/no). Resolve immediately when outcome is certain."""
    vote_state = RESTART_VOTE_STATE.get(room.name)
    if not vote_state:
        return "No restart vote in progress"
    if not room.current_game:
        return None
    player_idx = next((i for i, p in enumerate(room.current_game.players) if p.id == player_id), None)
    if player_idx is None:
        return None  # Spectators don't vote, ignore
    vote = data.get("vote")
    if vote not in ("yes", "no"):
        return "Invalid vote"
    vote_state["votes"][str(player_id)] = vote
    await _resolve_restart_vote(room.name, room, vote_state)
    return None


async def handle_restart_game(room: GameRoom, player_id: str) -> str | None:
    """Cancel current game, clear accolades, start brand new game. Requires game in progress."""
    if not room.current_game:
        return "No game in progress"
    _cancel_pending_next_game(room.name)
    # Clear accolades for room players and game players
    for p in room.players:
        p.past_accolade = Accolade.Pleb
    MAX_PLAYERS = 7
    game_player_ids = {p.id for p in room.current_game.players}
    spectators_who_want_in = [
        p for p in room.players
        if p.id not in game_player_ids and room.spectator_preferences.get(p.id, True)
    ]
    players = list(room.current_game.players)
    for p in players:
        p.past_accolade = Accolade.Pleb
    for sp in spectators_who_want_in:
        if len(players) >= MAX_PLAYERS:
            break
        players.append(sp)
    if len(players) < 2:
        return "Need at least 2 players to restart"
    room.current_game = ENGINE.start_new_game(players, None, None, None)
    logger.info("Game restarted: room=%s, players=%d", room.name, len(players))
    return None


async def start_next_game_after_delay(room: GameRoom):
    await asyncio.sleep(13)  # Wait for game results window (3s delay + 10s display)
    PENDING_NEXT_GAME_TASKS.pop(room.name, None)  # We're running; no longer pending
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
        # Game players must carry forward past_accolade into the next game (engine copies it)
        for gp in room.current_game.players:
            gp.past_accolade = gp.accolade

    if len(room.players) < 2:
        room.current_game = None
        save_room(room)
        logger.info("Next game skipped (not enough players): %s", room.name)
        await broadcast_state(room.name)
        return

    # Players for next game = game players + spectators who opted "Deal me in next time"
    # Cap at 7 players; spectators beyond the limit stay waiting for the next game
    MAX_PLAYERS = 7
    game_player_ids = {p.id for p in room.current_game.players}
    spectators_who_want_in = [
        p for p in room.players
        if p.id not in game_player_ids and room.spectator_preferences.get(p.id, True)
    ]
    players_for_next = list(room.current_game.players)
    for sp in spectators_who_want_in:
        if len(players_for_next) >= MAX_PLAYERS:
            break
        players_for_next.append(sp)
    if len(players_for_next) < 2:
        room.current_game = None
        save_room(room)
        logger.info("Next game skipped (not enough players after spectator filter): %s", room.name)
        await broadcast_state(room.name)
        return

    room.current_game = ENGINE.start_new_game(players_for_next, prev_dealer, prev_ep, prev_sh)
    save_room(room)
    logger.info("Next game started: room=%s", room.name)
    await broadcast_state(room.name)


# Restart vote state per room: {room_name: {initiator_id, initiator_name, votes: {pid: "yes"|"no"}, task}}
RESTART_VOTE_STATE: dict[str, dict] = {}
RESTART_VOTE_DURATION_SECONDS = 30

# Pending next-game task per room (cancel on restart)
PENDING_NEXT_GAME_TASKS: dict[str, asyncio.Task] = {}


def _cancel_restart_vote(room_name: str) -> None:
    state = RESTART_VOTE_STATE.pop(room_name, None)
    if state and (task := state.get("task")) and not task.done():
        task.cancel()


def _cancel_pending_next_game(room_name: str) -> None:
    task = PENDING_NEXT_GAME_TASKS.pop(room_name, None)
    if task and not task.done():
        task.cancel()


# WebSocket broadcast tracking
WS_CLIENTS: dict[str, dict[str, web.WebSocketResponse]] = {}  # room -> {player_id -> ws}
# Last heartbeat time per connected player: (room_name, player_id) -> time.monotonic()
LAST_HEARTBEAT: dict[tuple[str, str], float] = {}
HEARTBEAT_TIMEOUT_SECONDS = 7
# Disconnect grace period: (room_name, player_id) -> asyncio.Task to remove after 60s
DISCONNECT_TASKS: dict[tuple[str, str], asyncio.Task] = {}
# When each grace period started (for countdown): (room_name, player_id) -> time.monotonic()
DISCONNECT_START: dict[tuple[str, str], float] = {}
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
        except Exception as e:
            logger.warning("Failed to send state to %s in %s: %s", pid, room, e)


async def delayed_remove_after_disconnect(room_name: str, player_id: str) -> None:
    """After DISCONNECT_GRACE_SECONDS, remove player from game/room if they did not reconnect."""
    await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
    room = load_room(room_name)
    # Use same key as heartbeat loop and game_state_for_client: (room.name, player_id)
    key = (room.name, player_id) if room else (room_name, player_id)
    DISCONNECT_TASKS.pop(key, None)
    DISCONNECT_START.pop(key, None)
    LAST_HEARTBEAT.pop((room_name, player_id), None)
    if (room_name, player_id) in WS_CLIENTS.get(room_name, {}):
        return
    if not room:
        return
    await force_remove_player(room_name, room, player_id)
    # Close their WebSocket so client knows they were ejected
    wses = WS_CLIENTS.get(room_name, {})
    if player_id in wses:
        try:
            await wses[player_id].close()
        except Exception:
            pass


async def force_remove_player(room_name: str, room: GameRoom, player_id: str) -> None:
    """Remove player from game and room; broadcast state or game_over. Caller must have loaded room."""
    g = room.current_game
    player_idx = None
    if g:
        player_idx = next((i for i, p in enumerate(g.players) if p.id == player_id), None)
    if player_idx is not None:
        game_ended = ENGINE.remove_player_from_game(g, player_idx)
        if game_ended:
            if len(g.players) == 0:
                room.current_game = None
            save_room(room)
            await broadcast(room_name, {"type": "game_over", "results": g.results})
            _cancel_pending_next_game(room_name)
            PENDING_NEXT_GAME_TASKS[room_name] = asyncio.create_task(start_next_game_after_delay(room))
        else:
            save_room(room)
            await broadcast_state(room_name, room_obj=room)
    room.players = [p for p in room.players if p.id != player_id]
    room.spectator_preferences.pop(player_id, None)
    if getattr(room, "dick_tagged_player_id", None) == player_id:
        room.dick_tagged_player_id = None
        room.dick_tagged_at = None
    if not room.players:
        ROOMS.pop(room_name, None)
        fresh_room = GameRoom(name=room_name)
        save_room(fresh_room)
        logger.info("Room reinitialized (all players left): %s", room_name)
    else:
        save_room(room)
        await broadcast_state(room_name, room_obj=room)
    logger.info("Player force-removed: %s (%s)", room_name, player_id)


async def heartbeat_check_loop():
    """Every few seconds, mark players who missed a heartbeat as disconnected and start grace period."""
    while True:
        await asyncio.sleep(2)
        now = time.monotonic()
        for room_name, clients in list(WS_CLIENTS.items()):
            room = load_room(room_name)
            if not room or not room.current_game:
                continue
            for pid, ws in list(clients.items()):
                if ws.closed:
                    continue
                # Use room.name so the key matches game_state_for_client (room.name, p.id)
                key = (room.name, pid)
                if key in DISCONNECT_TASKS:
                    continue
                last = LAST_HEARTBEAT.get((room_name, pid), 0)
                if now - last > HEARTBEAT_TIMEOUT_SECONDS:
                    DISCONNECT_START[key] = time.monotonic()
                    task = asyncio.create_task(delayed_remove_after_disconnect(room_name, pid))
                    DISCONNECT_TASKS[key] = task
                    other_count = sum(1 for p in WS_CLIENTS[room_name] if p != pid and not WS_CLIENTS[room_name][p].closed)
                    logger.info(
                        "Heartbeat timeout: %s (%s) - grace period started, notifying %s other client(s)",
                        room_name, pid, other_count,
                    )
                    # Push state to other players so they see this player as disconnected and, if
                    # it's their turn, the waiting-for-player flyover.
                    await broadcast_state(room_name, exclude=pid, room_obj=room)
                    # Fallback: notify others so they can request state if the push was missed
                    await broadcast_except(room_name, pid, {"type": "player_disconnected", "player_id": pid})


async def redirect_to_lobby(_request: web.Request) -> web.Response:
    raise web.HTTPFound("/elpres/")


def create_app() -> web.Application:
    app = web.Application()

    async def on_startup(_app):
        asyncio.create_task(heartbeat_check_loop())

    app.on_startup.append(on_startup)
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
