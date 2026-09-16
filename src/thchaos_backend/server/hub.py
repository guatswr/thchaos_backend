"""单进程房间协调器。状态变更与发送入队在事件循环中不挂起。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..protocol.envelope import Envelope, MessageType, make_envelope, parse_message
from ..protocol.errors import ErrorCode, ProtocolError
from ..protocol.payloads import (
    AuthenticatedPayload, ClientRole, ConnectionStatusPayload, GamePhase,
    GameStatePayload, GameSyncPayload, HelloPayload, ProtocolInfoPayload,
    StateReason, VoteAckPayload, VoteCastPayload, VoteClosedPayload,
    VoteOpenedPayload, VoteSnapshotPayload,
)
from ..protocol.types import utc_now
from .config import Settings
from .connection import ClientConnection
from .storage import AuditStore

logger = logging.getLogger(__name__)
HEARTBEATS = {MessageType.HEARTBEAT_PING, MessageType.HEARTBEAT_PONG}


@dataclass
class PendingCast:
    origin: ClientConnection | None
    instance: str
    round_id: int
    choice: int
    generation: int
    created_at: float
    deadline: float
    message_id: str
    attempted: bool = False


@dataclass
class RoomRuntime:
    room_id: str
    game: ClientConnection | None = None
    bots: set[ClientConnection] = field(default_factory=set)
    state: GameStatePayload = field(default_factory=lambda: GameStatePayload(
        phase=GamePhase.OFFLINE, reason=StateReason.SYNC))
    active_vote: VoteOpenedPayload | None = None
    latest_snapshot: VoteSnapshotPayload | None = None
    last_closed_round: int | None = None
    ready: bool = False
    has_connected: bool = False
    generation: int = 0
    pending_casts: dict[str, PendingCast] = field(default_factory=dict)
    # Bounded process-local replay window; vote uniqueness is durable in SQLite.
    messages: OrderedDict[tuple[str, str, str], str] = field(default_factory=OrderedDict)


class Hub:
    """必须单 worker 运行；SQLite 不能共享各进程的 WebSocket 或房间状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = AuditStore(settings.database_path, settings.audit_queue_size)
        self.rooms: dict[str, RoomRuntime] = {}
        self.connections: set[ClientConnection] = set()
        self._sweeper: asyncio.Task | None = None
        self.forward_ms: deque[float] = deque(maxlen=1024)
        self.ack_ms: deque[float] = deque(maxlen=1024)
        self.ack_timeouts = 0

    def start(self) -> None:
        self.store.start()
        self._sweeper = asyncio.create_task(self._sweep(), name="ack-timeouts")

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        connections = list(self.connections)
        for connection in connections:
            await self._drop(connection)
        await asyncio.gather(*(connection.close() for connection in connections))
        await self.store.aclose()

    def room(self, room_id: str) -> RoomRuntime:
        if room_id not in self.rooms:
            self.rooms[room_id] = RoomRuntime(room_id)
        return self.rooms[room_id]

    def metrics(self) -> dict[str, Any]:
        def percentiles(values: deque[float]) -> dict[str, float | int]:
            ordered = sorted(values)
            return {"samples": len(ordered), **{
                f"p{p}_ms": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))], 3)
                if ordered else 0.0 for p in (95, 99)}}
        return {"forward": percentiles(self.forward_ms), "ack": percentiles(self.ack_ms),
                "ack_timeouts": self.ack_timeouts, "audit_dropped": self.store.dropped,
                "audit_failures": self.store.failures,
                "pending_casts": sum(len(room.pending_casts) for room in self.rooms.values())}

    async def _send_error(self, connection: ClientConnection | None, error: ProtocolError, *,
                          in_reply_to: str | None = None, cast_id: str | None = None) -> None:
        if connection is not None:
            try:
                await connection.send(MessageType.ERROR,
                                      error.to_payload(in_reply_to=in_reply_to, cast_id=cast_id))
            except ConnectionError:
                await self._drop(connection)

    async def _broadcast(self, room: RoomRuntime, kind: MessageType, payload: Any) -> None:
        instance = room.game.game_instance_id if room.game else None
        for bot in list(room.bots):
            try:
                await bot.send(kind, payload, game_instance_id=instance)
            except ConnectionError:
                await self._drop(bot)

    async def _send_server_state(self, bot: ClientConnection, room: RoomRuntime) -> None:
        instance = room.game.game_instance_id if room.game else None
        await bot.send(MessageType.PROTOCOL_INFO, ProtocolInfoPayload(
            version=1, message_types=[item.value for item in MessageType]), game_instance_id=instance)
        await bot.send(MessageType.GAME_SYNC, GameSyncPayload(
            state=room.state, active_vote=room.active_vote, latest_snapshot=room.latest_snapshot,
            last_closed_round=room.last_closed_round), game_instance_id=instance)

    async def _authenticate(self, websocket: WebSocket, envelope: Envelope,
                            payload: HelloPayload) -> ClientConnection:
        if self.settings.token_room(payload.token, payload.role.value) is None:
            raise ProtocolError(ErrorCode.AUTH_INVALID_TOKEN, "Token 无效")
        if (self.settings.token_room(payload.token, payload.role.value) != payload.room_id
                or envelope.room_id != payload.room_id
                or envelope.game_instance_id != payload.game_instance_id):
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "握手身份不一致")
        expected_path = f"/ws/{payload.role.value}"
        if websocket.url.path != expected_path:
            raise ProtocolError(ErrorCode.AUTH_ROLE_MISMATCH, "连接路径与角色不一致")
        if payload.role == ClientRole.GAME and not payload.game_instance_id:
            raise ProtocolError(ErrorCode.AUTH_ROLE_MISMATCH, "游戏端必须带 game_instance_id")
        return ClientConnection(websocket, payload.role, payload.room_id, payload.client_id,
                                payload.game_instance_id, inbound_seq=envelope.seq,
                                queue_limit=self.settings.outbound_queue_size,
                                send_timeout=self.settings.send_timeout)

    async def _register(self, connection: ClientConnection) -> RoomRuntime:
        room = self.room(connection.room_id)
        if connection.role == ClientRole.GAME:
            if room.game is not None:
                raise ProtocolError(ErrorCode.AUTH_GAME_ALREADY_CONNECTED, "该房间已有游戏端连接")
            room.game = connection
            # Preserve initial-connection compatibility; every reconnect needs sync.
            room.ready = not room.has_connected
            room.has_connected = True
            room.state = GameStatePayload(phase=GamePhase.TITLE, reason=StateReason.SYNC)
            room.generation += 1
        else:
            if len(room.bots) >= self.settings.max_bots_per_room:
                raise ProtocolError(ErrorCode.SERVER_OVERLOADED, "该房间 Bot 连接数已满")
            room.bots.add(connection)
        return room

    def _status(self, room: RoomRuntime, cast_id: str, pending: PendingCast, status: str) -> None:
        self.store.queue_vote_status(room_id=room.room_id, game_instance_id=pending.instance,
                                     round_id=pending.round_id, cast_id=cast_id, status=status)

    async def _resolve(self, room: RoomRuntime, cast_id: str, reason: str,
                       code: ErrorCode = ErrorCode.ROUND_RESULT_UNKNOWN) -> None:
        pending = room.pending_casts.pop(cast_id, None)
        if pending is None:
            return
        self._status(room, cast_id, pending, f"{'unknown' if pending.attempted else 'not_sent'}:{reason}")
        await self._send_error(pending.origin, ProtocolError(
            code if not pending.attempted else ErrorCode.ROUND_RESULT_UNKNOWN,
            "投票结果未知，请以游戏快照为准" if pending.attempted else "投票未发送，轮次或连接已变化",
            cast_id=cast_id), in_reply_to=pending.message_id)

    async def _resolve_all(self, room: RoomRuntime, reason: str,
                           code: ErrorCode = ErrorCode.ROUND_RESULT_UNKNOWN) -> None:
        for cast_id in list(room.pending_casts):
            await self._resolve(room, cast_id, reason, code)

    async def _drop(self, connection: ClientConnection) -> None:
        self.connections.discard(connection)
        room = self.rooms.get(connection.room_id)
        if room is None:
            return
        if room.game is connection:
            room.game = None
            room.ready = False
            room.generation += 1
            room.active_vote = None
            room.latest_snapshot = None
            room.state = GameStatePayload(phase=GamePhase.OFFLINE, reason=StateReason.OFFLINE)
            await self._resolve_all(room, "game_offline", ErrorCode.ROUND_GAME_OFFLINE)
            status = ConnectionStatusPayload(room_id=room.room_id,
                game_instance_id=connection.game_instance_id, reason="game_disconnected")
            for bot in list(room.bots):
                try:
                    await bot.send(MessageType.GAME_OFFLINE, status,
                                   game_instance_id=connection.game_instance_id)
                except ConnectionError:
                    await self._drop(bot)
        else:
            room.bots.discard(connection)
            # Keep metadata until ACK/timeout, but don't retain a disconnected socket.
            for pending in room.pending_casts.values():
                if pending.origin is connection:
                    pending.origin = None

    async def _record(self, connection: ClientConnection, envelope: Envelope) -> None:
        if envelope.type in HEARTBEATS:
            return
        await self.store.record_event(room_id=connection.room_id,
            game_instance_id=envelope.game_instance_id, source_role=connection.role.value,
            message_id=envelope.message_id, message_type=envelope.type.value,
            seq=envelope.seq, body=envelope.model_dump(mode="json"))

    async def _handle_game(self, connection: ClientConnection, envelope: Envelope, payload: Any) -> None:
        room = self.room(connection.room_id)
        if room.game is not connection or envelope.game_instance_id != connection.game_instance_id:
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "game_instance_id 与连接不一致")
        if envelope.type in HEARTBEATS:
            if envelope.type == MessageType.HEARTBEAT_PING:
                await connection.send(MessageType.HEARTBEAT_PONG, payload)
            return
        if not room.ready and envelope.type != MessageType.GAME_SYNC:
            raise ProtocolError(ErrorCode.PROTOCOL_NOT_AUTHENTICATED, "游戏端必须先发送 game.sync")
        if envelope.type == MessageType.GAME_SYNC:
            assert isinstance(payload, GameSyncPayload)
            room.generation += 1
            await self._resolve_all(room, "sync")
            room.state, room.active_vote = payload.state, payload.active_vote
            room.latest_snapshot, room.last_closed_round = payload.latest_snapshot, payload.last_closed_round
            room.ready = True
        elif envelope.type == MessageType.GAME_STATE_CHANGED:
            room.state = payload
        elif envelope.type == MessageType.VOTE_OPENED:
            assert isinstance(payload, VoteOpenedPayload)
            if room.last_closed_round is not None and payload.round_id <= room.last_closed_round:
                raise ProtocolError(ErrorCode.ROUND_STALE, "不能重新开启已关闭的轮次")
            if room.active_vote is not None and payload.round_id <= room.active_vote.round_id:
                raise ProtocolError(ErrorCode.ROUND_STALE, "新轮次必须大于当前轮次，同步请使用 game.sync")
            room.generation += 1
            await self._resolve_all(room, "round_changed", ErrorCode.ROUND_STALE)
            room.active_vote, room.latest_snapshot = payload, None
        elif envelope.type == MessageType.VOTE_SNAPSHOT:
            if room.active_vote is None or payload.round_id != room.active_vote.round_id:
                return
            room.latest_snapshot = payload
        elif envelope.type == MessageType.VOTE_ACK:
            assert isinstance(payload, VoteAckPayload)
            pending = room.pending_casts.get(payload.cast_id)
            if pending is not None:
                if (pending.instance != connection.game_instance_id or pending.round_id != payload.round_id
                        or pending.choice != payload.choice or not pending.attempted):
                    raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "ACK 与投票不一致")
                room.pending_casts.pop(payload.cast_id)
                self.ack_ms.append((time.monotonic() - pending.created_at) * 1000)
                if pending.origin is not None:
                    try:
                        await pending.origin.send(MessageType.VOTE_ACK, payload,
                                                  game_instance_id=connection.game_instance_id)
                    except ConnectionError:
                        await self._drop(pending.origin)
            # Late ACKs still reconcile audit, scoped to the exact instance/round.
            self.store.queue_vote_status(room_id=room.room_id,
                game_instance_id=connection.game_instance_id or "", round_id=payload.round_id,
                cast_id=payload.cast_id, status="accepted" if payload.counted else f"rejected:{payload.reason.value}")
            return
        elif envelope.type == MessageType.VOTE_CLOSED:
            assert isinstance(payload, VoteClosedPayload)
            if room.active_vote is None or payload.round_id != room.active_vote.round_id:
                raise ProtocolError(ErrorCode.ROUND_STALE, "关闭消息不属于当前轮次")
            room.generation += 1
            room.active_vote, room.latest_snapshot = None, None
            room.last_closed_round = payload.round_id
            await self._resolve_all(room, "round_closed", ErrorCode.ROUND_CLOSED)
        elif envelope.type != MessageType.EFFECT_RESOLVED:
            raise ProtocolError(ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED, "游戏端消息方向不允许")
        await self._broadcast(room, envelope.type, payload)

    def _rate_limit(self, connection: ClientConnection, payload: VoteCastPayload) -> None:
        now = time.monotonic()
        while connection.cast_timestamps and now - connection.cast_timestamps[0] >= 1:
            connection.cast_timestamps.popleft()
        if len(connection.cast_timestamps) >= self.settings.max_casts_per_second:
            raise ProtocolError(ErrorCode.PROTOCOL_RATE_LIMITED, "投票提交过于频繁",
                                details={"retry_after_ms": "1000"}, cast_id=payload.cast_id)
        connection.cast_timestamps.append(now)

    async def _handle_bot(self, connection: ClientConnection, envelope: Envelope, payload: Any) -> None:
        room = self.room(connection.room_id)
        if envelope.type in HEARTBEATS:
            if envelope.type == MessageType.HEARTBEAT_PING:
                await connection.send(MessageType.HEARTBEAT_PONG, payload)
            return
        if envelope.type != MessageType.VOTE_CAST:
            raise ProtocolError(ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED, "Bot 只能发送 vote.cast")
        assert isinstance(payload, VoteCastPayload)
        game, active, generation = room.game, room.active_vote, room.generation
        if game is None or game.closed:
            raise ProtocolError(ErrorCode.ROUND_GAME_OFFLINE, "游戏端未连接", cast_id=payload.cast_id)
        if envelope.game_instance_id != game.game_instance_id:
            raise ProtocolError(ErrorCode.ROUND_GAME_INSTANCE_MISMATCH, "游戏实例已变化", cast_id=payload.cast_id)
        if not room.ready or active is None:
            raise ProtocolError(ErrorCode.ROUND_NOT_OPEN, "当前没有开放投票", cast_id=payload.cast_id)
        if payload.round_id != active.round_id:
            raise ProtocolError(ErrorCode.ROUND_STALE, "投票轮次已过期", cast_id=payload.cast_id)
        started = time.monotonic()
        inserted = await self.store.record_vote(room_id=room.room_id,
            game_instance_id=game.game_instance_id or "", round_id=payload.round_id,
            cast_id=payload.cast_id, voter_id=payload.voter_id, choice=payload.choice)
        if not inserted:
            raise ProtocolError(ErrorCode.ROUND_DUPLICATE_VOTE, "投票消息已处理过", cast_id=payload.cast_id)
        pending = PendingCast(connection, game.game_instance_id or "", payload.round_id,
                              payload.choice, generation, started, started + self.settings.ack_timeout,
                              envelope.message_id)
        if (room.game is not game or game.closed or connection.closed or room.generation != generation
                or time.monotonic() >= pending.deadline):
            self._status(room, payload.cast_id, pending, "not_sent:state_changed")
            raise ProtocolError(ErrorCode.ROUND_STALE, "投票保存期间连接或轮次已变化", cast_id=payload.cast_id)
        room.pending_casts[payload.cast_id] = pending

        def valid() -> bool:
            return (room.game is game and room.generation == generation
                    and room.pending_casts.get(payload.cast_id) is pending
                    and time.monotonic() < pending.deadline)

        def on_start() -> None:
            pending.attempted = True

        def on_sent() -> None:
            self.forward_ms.append((time.monotonic() - started) * 1000)
            self._status(room, payload.cast_id, pending, "forwarded")

        try:
            await game.send(MessageType.VOTE_CAST, payload, game_instance_id=game.game_instance_id,
                            valid=valid, on_start=on_start, on_sent=on_sent)
        except ConnectionError:
            await self._resolve(room, payload.cast_id, "send_failed", ErrorCode.ROUND_GAME_OFFLINE)
            await self._drop(game)

    async def _expire_pending(self) -> None:
        now = time.monotonic()
        for room in list(self.rooms.values()):
            for cast_id, pending in list(room.pending_casts.items()):
                if pending.deadline <= now:
                    self.ack_timeouts += 1
                    await self._resolve(room, cast_id, "ack_timeout")

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(min(1.0, self.settings.ack_timeout))
            await self._expire_pending()

    def _message_key(self, connection: ClientConnection, envelope: Envelope) -> tuple[str, str, str]:
        return connection.role.value, envelope.game_instance_id or "", envelope.message_id

    def _fingerprint(self, envelope: Envelope) -> str:
        return hashlib.sha256(json.dumps({"type": envelope.type.value, "payload": envelope.payload},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def _dispatch(self, connection: ClientConnection, envelope: Envelope, payload: Any) -> None:
        if envelope.room_id != connection.room_id:
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "room_id 与连接不一致")
        if envelope.seq != connection.inbound_seq + 1:
            code = ErrorCode.PROTOCOL_SEQ_REGRESSION if envelope.seq <= connection.inbound_seq else ErrorCode.PROTOCOL_SEQ_GAP
            raise ProtocolError(code, "入站 seq 必须连续")
        connection.inbound_seq = envelope.seq
        if envelope.type == MessageType.HELLO:
            raise ProtocolError(ErrorCode.PROTOCOL_ALREADY_AUTHENTICATED, "连接已认证")
        if connection.role == ClientRole.GAME and envelope.game_instance_id != connection.game_instance_id:
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "game_instance_id 与连接不一致")
        if connection.role == ClientRole.BOT and isinstance(payload, VoteCastPayload):
            self._rate_limit(connection, payload)
        room = self.room(connection.room_id)
        key, digest = self._message_key(connection, envelope), self._fingerprint(envelope)
        if envelope.type not in HEARTBEATS and key in room.messages:
            if room.messages[key] != digest:
                raise ProtocolError(ErrorCode.PROTOCOL_MALFORMED_MESSAGE, "message_id 不能复用于不同消息")
            if envelope.type == MessageType.VOTE_CAST:
                raise ProtocolError(ErrorCode.ROUND_DUPLICATE_VOTE, "投票消息已处理过", cast_id=payload.cast_id)
            # A reconnect always needs a fresh authoritative sync, even with a reused ID.
            if envelope.type != MessageType.GAME_SYNC or room.ready:
                return
        if connection.role == ClientRole.GAME:
            await self._handle_game(connection, envelope, payload)
        else:
            await self._handle_bot(connection, envelope, payload)
        await self._record(connection, envelope)
        if envelope.type not in HEARTBEATS:
            room.messages[key] = digest
            room.messages.move_to_end(key)
            if len(room.messages) > 4096:
                room.messages.popitem(last=False)

    async def _receive(self, websocket: WebSocket) -> str:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("text") is None:
            raise ProtocolError(ErrorCode.PROTOCOL_MALFORMED_MESSAGE, "只接受文本帧")
        return message["text"]

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection: ClientConnection | None = None
        first_envelope: Envelope | None = None
        try:
            try:
                raw = await asyncio.wait_for(self._receive(websocket), self.settings.handshake_timeout)
            except TimeoutError as exc:
                raise ProtocolError(ErrorCode.PROTOCOL_NOT_AUTHENTICATED, "等待 hello 超时") from exc
            first_envelope, payload = parse_message(raw, max_frame_bytes=self.settings.max_frame_bytes)
            if first_envelope.type != MessageType.HELLO or not isinstance(payload, HelloPayload):
                raise ProtocolError(ErrorCode.PROTOCOL_NOT_AUTHENTICATED, "第一条消息必须是 hello")
            if first_envelope.seq != 1:
                raise ProtocolError(ErrorCode.PROTOCOL_SEQ_REGRESSION, "hello 的 seq 必须为 1")
            connection = await self._authenticate(websocket, first_envelope, payload)
            connection.start(self._drop)
            self.connections.add(connection)
            room = await self._register(connection)
            await connection.send(MessageType.AUTHENTICATED, AuthenticatedPayload(
                session_id=connection.session_id, role=connection.role, room_id=connection.room_id,
                game_instance_id=connection.game_instance_id, server_time=utc_now(),
                resumed=room.ready and connection.role == ClientRole.BOT),
                game_instance_id=room.game.game_instance_id if room.game else None)
            if connection.role == ClientRole.BOT:
                await self._send_server_state(connection, room)
            await self._record(connection, first_envelope)
            while not connection.closed:
                envelope = None
                payload = None
                try:
                    raw = await self._receive(websocket)
                    # Direction is checked after seq is consumed, so nonfatal direction
                    # errors don't strand a client on an impossible next sequence.
                    envelope, payload = parse_message(raw, max_frame_bytes=self.settings.max_frame_bytes)
                    await self._dispatch(connection, envelope, payload)
                except ProtocolError as error:
                    await self._send_error(connection, error,
                        in_reply_to=envelope.message_id if envelope else None,
                        cast_id=payload.cast_id if isinstance(payload, VoteCastPayload) else None)
                    if error.fatal:
                        await connection.flush()
                        return
        except WebSocketDisconnect:
            pass
        except ProtocolError as error:
            logger.info("握手拒绝: %s", error.code.value)
            if connection is not None:
                await self._send_error(connection, error)
                await connection.flush()
            else:
                # Reserved valid room identifier when malformed input has no trustworthy room.
                response = make_envelope(MessageType.ERROR,
                    room_id=first_envelope.room_id if first_envelope else "unauthenticated",
                    seq=1, payload=error.to_payload(
                        in_reply_to=first_envelope.message_id if first_envelope else None))
                await asyncio.wait_for(websocket.send_text(response.model_dump_json()), self.settings.send_timeout)
        except Exception:
            logger.exception("WebSocket 处理异常")
            if connection is not None:
                await self._send_error(connection, ProtocolError(
                    ErrorCode.SERVER_INTERNAL_ERROR, "服务器内部错误", fatal=True))
                try:
                    await connection.flush()
                except (TimeoutError, ConnectionError):
                    pass
        finally:
            if connection is not None:
                await self._drop(connection)
                await connection.close()
            try:
                await asyncio.wait_for(websocket.close(code=1008), self.settings.send_timeout)
            except Exception:
                pass
