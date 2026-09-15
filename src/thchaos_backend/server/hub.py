"""WebSocket 连接、房间路由和游戏权威状态的内存协调器。"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..protocol.envelope import Envelope, MessageType, make_envelope, parse_message
from ..protocol.errors import ErrorCode, ErrorPayload, ProtocolError
from ..protocol.payloads import (
    AuthenticatedPayload,
    ClientRole,
    ConnectionStatusPayload,
    GamePhase,
    GameStatePayload,
    GameSyncPayload,
    HeartbeatPayload,
    HelloPayload,
    ProtocolInfoPayload,
    ServerStatePayload,
    VoteAckPayload,
    VoteCastPayload,
    VoteClosedPayload,
    VoteOpenedPayload,
    VoteSnapshotPayload,
    StateReason,
)
from ..protocol.types import SessionId, new_message_id, utc_now
from .config import Settings
from .storage import AuditStore

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class ClientConnection:
    websocket: WebSocket
    role: ClientRole
    room_id: str
    client_id: str
    game_instance_id: str | None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    inbound_seq: int = 0
    outbound_seq: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cast_timestamps: deque[float] = field(default_factory=deque)

    async def send(self, message_type: MessageType, payload: Any, *, game_instance_id: str | None = None) -> Envelope:
        self.outbound_seq += 1
        envelope = make_envelope(
            message_type,
            room_id=self.room_id,
            game_instance_id=game_instance_id if game_instance_id is not None else self.game_instance_id,
            seq=self.outbound_seq,
            payload=payload,
        )
        async with self.send_lock:
            await self.websocket.send_text(envelope.model_dump_json())
        return envelope


@dataclass
class RoomRuntime:
    room_id: str
    game: ClientConnection | None = None
    bots: set[ClientConnection] = field(default_factory=set)
    state: GameStatePayload = field(
        default_factory=lambda: GameStatePayload(phase=GamePhase.OFFLINE, reason=StateReason.SYNC)
    )
    active_vote: VoteOpenedPayload | None = None
    latest_snapshot: VoteSnapshotPayload | None = None
    seen_voters: set[tuple[str, int, str]] = field(default_factory=set)
    pending_casts: dict[str, ClientConnection] = field(default_factory=dict)


class Hub:
    """一个进程内的房间协调器；通过 SQLite 记录审计，不依赖粘性会话。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = AuditStore(settings.database_path)
        self.rooms: dict[str, RoomRuntime] = {}
        self._lock = asyncio.Lock()

    def room(self, room_id: str) -> RoomRuntime:
        return self.rooms.setdefault(room_id, RoomRuntime(room_id=room_id))

    async def _send_error(
        self,
        connection: ClientConnection | None,
        error: ProtocolError,
        *,
        in_reply_to: str | None = None,
        cast_id: str | None = None,
    ) -> None:
        if connection is None:
            return
        try:
            await connection.send(
                MessageType.ERROR,
                error.to_payload(in_reply_to=in_reply_to, cast_id=cast_id),
            )
        except Exception:
            logger.debug("发送 error 失败", exc_info=True)

    async def _broadcast(self, room: RoomRuntime, message_type: MessageType, payload: Any) -> None:
        targets = list(room.bots)
        for bot in targets:
            try:
                await bot.send(message_type, payload, game_instance_id=room.game.game_instance_id if room.game else None)
            except Exception:
                await self._drop(bot)

    async def _send_server_state(self, bot: ClientConnection, room: RoomRuntime) -> None:
        await bot.send(
            MessageType.PROTOCOL_INFO,
            ProtocolInfoPayload(
                version=1,
                message_types=[item.value for item in MessageType],
            ),
            game_instance_id=room.game.game_instance_id if room.game else None,
        )
        await bot.send(
            MessageType.GAME_SYNC,
            GameSyncPayload(
                state=room.state,
                active_vote=room.active_vote,
                latest_snapshot=room.latest_snapshot,
                last_closed_round=None,
            ),
            game_instance_id=room.game.game_instance_id if room.game else None,
        )

    async def _authenticate(self, websocket: WebSocket, envelope: Envelope, payload: HelloPayload) -> ClientConnection:
        room_from_token = self.settings.token_room(payload.token, payload.role.value)
        if room_from_token is None:
            raise ProtocolError(ErrorCode.AUTH_INVALID_TOKEN, "Token 无效")
        if room_from_token != payload.room_id:
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "Token 不属于该房间")
        if payload.role == ClientRole.GAME and not payload.game_instance_id:
            raise ProtocolError(ErrorCode.AUTH_ROLE_MISMATCH, "游戏端 hello 必须带 game_instance_id")
        return ClientConnection(
            websocket=websocket,
            role=payload.role,
            room_id=payload.room_id,
            client_id=payload.client_id,
            game_instance_id=payload.game_instance_id,
            inbound_seq=envelope.seq,
        )

    async def _register(self, connection: ClientConnection) -> RoomRuntime:
        room = self.room(connection.room_id)
        async with self._lock:
            if connection.role == ClientRole.GAME:
                if room.game is not None:
                    raise ProtocolError(ErrorCode.AUTH_GAME_ALREADY_CONNECTED, "该房间已有游戏端连接")
                room.game = connection
                room.state = GameStatePayload(phase=GamePhase.TITLE, reason=StateReason.SYNC)
            else:
                if len(room.bots) >= self.settings.max_bots_per_room:
                    raise ProtocolError(ErrorCode.SERVER_OVERLOADED, "该房间 Bot 连接数已满")
                room.bots.add(connection)
        return room

    async def _drop(self, connection: ClientConnection) -> None:
        room = self.rooms.get(connection.room_id)
        if room is None:
            return
        pending_for_game: list[tuple[str, ClientConnection]] = []
        offline_status: ConnectionStatusPayload | None = None
        offline_bots: list[ClientConnection] = []
        async with self._lock:
            if connection.role == ClientRole.GAME and room.game is connection:
                room.game = None
                pending_for_game = list(room.pending_casts.items())
                room.pending_casts.clear()
                room.active_vote = None
                room.latest_snapshot = None
                room.seen_voters.clear()
                room.state = GameStatePayload(phase=GamePhase.OFFLINE, reason=StateReason.OFFLINE)
                offline_status = ConnectionStatusPayload(
                    room_id=room.room_id,
                    game_instance_id=connection.game_instance_id,
                    reason="game_disconnected",
                )
                offline_bots = list(room.bots)
            else:
                # A Bot can disappear while its cast is still waiting for the
                # game ACK. Drop that origin now so a later ACK cannot try to
                # write to a closed socket or retain the connection forever.
                for cast_id, origin in list(room.pending_casts.items()):
                    if origin is connection:
                        room.pending_casts.pop(cast_id, None)
            room.bots.discard(connection)

        # Resolve casts that were forwarded but could not receive a game ACK;
        # AstrBot can then clear its per-cast bookkeeping and optionally report
        # the failure to the originating group.
        for cast_id, origin in pending_for_game:
            await self._send_error(
                origin,
                ProtocolError(ErrorCode.ROUND_GAME_OFFLINE, "游戏端已断开", cast_id=cast_id),
            )
        if offline_status is not None:
            for bot in offline_bots:
                try:
                    await bot.send(
                        MessageType.GAME_OFFLINE,
                        offline_status,
                        game_instance_id=connection.game_instance_id,
                    )
                except Exception:
                    await self._drop(bot)

    async def _record(self, connection: ClientConnection, envelope: Envelope) -> None:
        try:
            await self.store.record_event(
                room_id=envelope.room_id,
                game_instance_id=envelope.game_instance_id,
                source_role=connection.role.value,
                message_id=envelope.message_id,
                message_type=envelope.type.value,
                seq=envelope.seq,
                body=envelope.model_dump(mode="json"),
            )
        except Exception:
            logger.warning("审计写入失败", exc_info=True)

    async def _handle_game(self, connection: ClientConnection, envelope: Envelope, payload: Any) -> None:
        room = self.room(connection.room_id)
        if envelope.game_instance_id != connection.game_instance_id:
            raise ProtocolError(ErrorCode.PROTOCOL_ORIGIN_MISMATCH, "game_instance_id 与连接不一致")
        if envelope.type == MessageType.HEARTBEAT_PING:
            await connection.send(MessageType.HEARTBEAT_PONG, payload)
            return
        if envelope.type == MessageType.GAME_SYNC:
            assert isinstance(payload, GameSyncPayload)
            room.state = payload.state
            room.active_vote = payload.active_vote
            room.latest_snapshot = payload.latest_snapshot
            room.seen_voters.clear()
            room.pending_casts.clear()
            await self._broadcast(room, MessageType.GAME_SYNC, payload)
            return
        if envelope.type == MessageType.GAME_STATE_CHANGED:
            assert isinstance(payload, GameStatePayload)
            room.state = payload
            await self._broadcast(room, MessageType.GAME_STATE_CHANGED, payload)
            return
        if envelope.type == MessageType.VOTE_OPENED:
            assert isinstance(payload, VoteOpenedPayload)
            room.active_vote = payload
            room.latest_snapshot = None
            room.seen_voters.clear()
            room.pending_casts.clear()
            await self._broadcast(room, MessageType.VOTE_OPENED, payload)
            return
        if envelope.type == MessageType.VOTE_SNAPSHOT:
            assert isinstance(payload, VoteSnapshotPayload)
            if room.active_vote is None or payload.round_id != room.active_vote.round_id:
                return
            room.latest_snapshot = payload
            await self._broadcast(room, MessageType.VOTE_SNAPSHOT, payload)
            return
        if envelope.type == MessageType.VOTE_ACK:
            assert isinstance(payload, VoteAckPayload)
            await self.store.update_vote_status(
                room_id=room.room_id,
                cast_id=payload.cast_id,
                status="accepted" if payload.counted else f"rejected:{payload.reason.value}",
            )
            origin = room.pending_casts.pop(payload.cast_id, None)
            if origin is not None:
                try:
                    await origin.send(MessageType.VOTE_ACK, payload, game_instance_id=connection.game_instance_id)
                except Exception:
                    await self._drop(origin)
            return
        if envelope.type == MessageType.VOTE_CLOSED:
            assert isinstance(payload, VoteClosedPayload)
            # Any cast that was already reserved for the game but did not get
            # an ACK before close must be resolved for its originating Bot;
            # otherwise AstrBot would wait forever for a result after a close/
            # reconnect race.
            for cast_id, origin in list(room.pending_casts.items()):
                await self._send_error(
                    origin,
                    ProtocolError(ErrorCode.ROUND_CLOSED, "投票已关闭", cast_id=cast_id),
                )
            room.active_vote = None
            room.latest_snapshot = None
            room.seen_voters.clear()
            room.pending_casts.clear()
            await self._broadcast(room, MessageType.VOTE_CLOSED, payload)
            return
        if envelope.type == MessageType.EFFECT_RESOLVED:
            await self._broadcast(room, MessageType.EFFECT_RESOLVED, payload)
            return
        raise ProtocolError(ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED, "游戏端消息类型不能在此处理")

    async def _handle_bot(self, connection: ClientConnection, envelope: Envelope, payload: Any) -> None:
        room = self.room(connection.room_id)
        if envelope.type == MessageType.HEARTBEAT_PING:
            await connection.send(MessageType.HEARTBEAT_PONG, payload)
            return
        if envelope.type != MessageType.VOTE_CAST:
            raise ProtocolError(ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED, "Bot 只能发送 vote.cast")
        assert isinstance(payload, VoteCastPayload)
        now = time.monotonic()
        while connection.cast_timestamps and now - connection.cast_timestamps[0] >= 1.0:
            connection.cast_timestamps.popleft()
        if len(connection.cast_timestamps) >= max(1, self.settings.max_casts_per_second):
            raise ProtocolError(
                ErrorCode.PROTOCOL_RATE_LIMITED,
                "投票提交过于频繁",
                details={"retry_after_ms": "1000"},
                cast_id=payload.cast_id,
            )
        connection.cast_timestamps.append(now)
        game = room.game
        active = room.active_vote
        if game is None:
            raise ProtocolError(ErrorCode.ROUND_GAME_OFFLINE, "游戏端未连接")
        if envelope.game_instance_id != game.game_instance_id:
            raise ProtocolError(ErrorCode.ROUND_GAME_INSTANCE_MISMATCH, "游戏实例已变化")
        if active is None:
            raise ProtocolError(ErrorCode.ROUND_NOT_OPEN, "当前没有开放投票", cast_id=payload.cast_id)
        if payload.round_id != active.round_id:
            raise ProtocolError(ErrorCode.ROUND_STALE, "投票轮次已过期", cast_id=payload.cast_id)
        key = (game.game_instance_id or "", payload.round_id, payload.voter_id)
        if key in room.seen_voters:
            raise ProtocolError(ErrorCode.ROUND_DUPLICATE_VOTE, "该用户本轮已提交过投票", cast_id=payload.cast_id)
        inserted = await self.store.record_vote(
            room_id=room.room_id,
            game_instance_id=game.game_instance_id or "",
            round_id=payload.round_id,
            cast_id=payload.cast_id,
            voter_id=payload.voter_id,
            choice=payload.choice,
        )
        if not inserted:
            raise ProtocolError(ErrorCode.ROUND_DUPLICATE_VOTE, "投票消息已处理过", cast_id=payload.cast_id)
        room.seen_voters.add(key)
        room.pending_casts[payload.cast_id] = connection
        try:
            await game.send(MessageType.VOTE_CAST, payload, game_instance_id=game.game_instance_id)
        except Exception as exc:
            room.pending_casts.pop(payload.cast_id, None)
            raise ProtocolError(ErrorCode.ROUND_GAME_OFFLINE, "游戏端连接已断开") from exc

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection: ClientConnection | None = None
        try:
            first_raw = await websocket.receive_text()
            first_envelope, first_payload = parse_message(first_raw)
            if first_envelope.type != MessageType.HELLO or not isinstance(first_payload, HelloPayload):
                raise ProtocolError(ErrorCode.PROTOCOL_NOT_AUTHENTICATED, "第一条消息必须是 hello")
            if first_envelope.seq != 1:
                raise ProtocolError(ErrorCode.PROTOCOL_SEQ_REGRESSION, "hello 的 seq 必须为 1")
            connection = await self._authenticate(websocket, first_envelope, first_payload)
            room = await self._register(connection)
            auth = AuthenticatedPayload(
                session_id=connection.session_id,
                role=connection.role,
                room_id=connection.room_id,
                game_instance_id=connection.game_instance_id,
                server_time=utc_now(),
                resumed=room.game is not None and connection.role == ClientRole.BOT,
            )
            await connection.send(MessageType.AUTHENTICATED, auth, game_instance_id=room.game.game_instance_id if room.game else None)
            if connection.role == ClientRole.BOT:
                await self._send_server_state(connection, room)
            await self._record(connection, first_envelope)

            while True:
                raw = await websocket.receive_text()
                try:
                    envelope, payload = parse_message(raw, role=connection.role)
                    if envelope.seq != connection.inbound_seq + 1:
                        code = (
                            ErrorCode.PROTOCOL_SEQ_REGRESSION
                            if envelope.seq <= connection.inbound_seq
                            else ErrorCode.PROTOCOL_SEQ_GAP
                        )
                        raise ProtocolError(code, "入站 seq 必须连续")
                    connection.inbound_seq = envelope.seq
                    await self._record(connection, envelope)
                    if connection.role == ClientRole.GAME:
                        await self._handle_game(connection, envelope, payload)
                    else:
                        await self._handle_bot(connection, envelope, payload)
                except ProtocolError as error:
                    await self._send_error(connection, error)
                    # 轮次拒绝、重复投票和限流是单条消息的失败，不应让 Bot
                    # 的长连接一起掉线；只有协议/鉴权等 fatal 错误关闭连接。
                    if error.fatal:
                        try:
                            await websocket.close(code=1008)
                        except Exception:
                            pass
                        return
        except WebSocketDisconnect:
            pass
        except ProtocolError as error:
            await self._send_error(connection, error)
            if error.fatal:
                try:
                    await websocket.close(code=1008)
                except Exception:
                    pass
        except Exception:
            logger.exception("WebSocket 处理异常")
            await self._send_error(connection, ProtocolError(ErrorCode.SERVER_INTERNAL_ERROR, "服务器内部错误"))
        finally:
            if connection is not None:
                await self._drop(connection)
