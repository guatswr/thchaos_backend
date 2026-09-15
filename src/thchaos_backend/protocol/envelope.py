"""协议 v1 信封、消息解析与发送方向。"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import Field, ValidationError, model_validator

from .errors import DirectionError, MessageValidationError, UnknownMessageTypeError
from .payloads import (
    AuthenticatedPayload,
    ClientRole,
    ConnectionStatusPayload,
    EffectResolvedPayload,
    GameStatePayload,
    GameSyncPayload,
    HeartbeatPayload,
    HelloPayload,
    ProtocolInfoPayload,
    RateLimitPayload,
    ServerStatePayload,
    VoteAckPayload,
    VoteCastPayload,
    VoteClosedPayload,
    VoteOpenedPayload,
    VoteSnapshotPayload,
)
from .types import Identifier, MessageId, ProtocolModel, Seq, UtcTimestamp, utc_now

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024


class MessageType(StrEnum):
    HELLO = "hello"
    AUTHENTICATED = "authenticated"
    ERROR = "error"
    HEARTBEAT_PING = "heartbeat.ping"
    HEARTBEAT_PONG = "heartbeat.pong"
    PROTOCOL_INFO = "protocol.info"
    GAME_SYNC = "game.sync"
    GAME_STATE_CHANGED = "game.state_changed"
    GAME_OFFLINE = "game.offline"
    VOTE_OPENED = "vote.opened"
    VOTE_CAST = "vote.cast"
    VOTE_ACK = "vote.ack"
    VOTE_SNAPSHOT = "vote.snapshot"
    VOTE_CLOSED = "vote.closed"
    EFFECT_RESOLVED = "effect.resolved"


class Envelope(ProtocolModel):
    version: int = Field(ge=1, le=255)
    type: MessageType
    message_id: MessageId
    room_id: Identifier
    game_instance_id: Identifier | None = None
    seq: Seq
    sent_at: UtcTimestamp
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_version(self) -> "Envelope":
        if self.version != PROTOCOL_VERSION:
            raise ValueError(f"只支持 protocol v{PROTOCOL_VERSION}")
        return self


PAYLOAD_MODELS: dict[MessageType, type[ProtocolModel]] = {
    MessageType.HELLO: HelloPayload,
    MessageType.AUTHENTICATED: AuthenticatedPayload,
    MessageType.HEARTBEAT_PING: HeartbeatPayload,
    MessageType.HEARTBEAT_PONG: HeartbeatPayload,
    MessageType.PROTOCOL_INFO: ProtocolInfoPayload,
    MessageType.GAME_SYNC: GameSyncPayload,
    MessageType.GAME_STATE_CHANGED: GameStatePayload,
    MessageType.GAME_OFFLINE: ConnectionStatusPayload,
    MessageType.VOTE_OPENED: VoteOpenedPayload,
    MessageType.VOTE_CAST: VoteCastPayload,
    MessageType.VOTE_ACK: VoteAckPayload,
    MessageType.VOTE_SNAPSHOT: VoteSnapshotPayload,
    MessageType.VOTE_CLOSED: VoteClosedPayload,
    MessageType.EFFECT_RESOLVED: EffectResolvedPayload,
}

# error payload 在 errors.py 中定义，避免这里产生循环导入；服务器单独处理 error。
ALLOWED_BY_ROLE: dict[ClientRole, frozenset[MessageType]] = {
    ClientRole.GAME: frozenset(
        {
            MessageType.HELLO,
            MessageType.HEARTBEAT_PING,
            MessageType.HEARTBEAT_PONG,
            MessageType.GAME_SYNC,
            MessageType.GAME_STATE_CHANGED,
            MessageType.VOTE_OPENED,
            MessageType.VOTE_ACK,
            MessageType.VOTE_SNAPSHOT,
            MessageType.VOTE_CLOSED,
            MessageType.EFFECT_RESOLVED,
        }
    ),
    ClientRole.BOT: frozenset(
        {
            MessageType.HELLO,
            MessageType.HEARTBEAT_PING,
            MessageType.HEARTBEAT_PONG,
            MessageType.VOTE_CAST,
        }
    ),
}


def allowed_message_types(role: ClientRole) -> frozenset[MessageType]:
    return ALLOWED_BY_ROLE[role]


def make_envelope(
    message_type: MessageType,
    *,
    room_id: str,
    seq: int,
    payload: ProtocolModel,
    game_instance_id: str | None = None,
    message_id: str | None = None,
    sent_at: str | None = None,
) -> Envelope:
    """构造并校验一条出站消息。"""

    from .types import new_message_id

    expected = PAYLOAD_MODELS.get(message_type)
    if expected is not None and not isinstance(payload, expected):
        raise TypeError(f"{message_type.value} 需要 {expected.__name__}")
    return Envelope(
        version=PROTOCOL_VERSION,
        type=message_type,
        message_id=message_id or new_message_id(),
        room_id=room_id,
        game_instance_id=game_instance_id,
        seq=seq,
        sent_at=sent_at or utc_now(),
        payload=payload.model_dump(mode="json"),
    )


def parse_message(raw: str | bytes, *, role: ClientRole | None = None) -> tuple[Envelope, ProtocolModel | None]:
    """解析一帧 JSON，并按消息类型验证载荷和发送方向。"""

    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise MessageValidationError("消息超过 16 KiB 限制")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MessageValidationError("消息必须是 UTF-8 文本") from exc
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise MessageValidationError("消息超过 16 KiB 限制")
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MessageValidationError("消息不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise MessageValidationError("消息顶层必须是 JSON 对象")
    try:
        # JSON mode is intentional: Pydantic accepts the wire representation of
        # enums (strings) while the strict model still rejects string numbers and
        # string booleans. ``model_validate(data)`` would reject every enum value
        # because it is already a Python string rather than an enum instance.
        envelope = Envelope.model_validate_json(raw)
    except ValidationError as exc:
        raise MessageValidationError("信封字段不合法", details={"validation": str(exc)[:180]}) from exc
    if envelope.type == MessageType.ERROR:
        return envelope, None
    model = PAYLOAD_MODELS.get(envelope.type)
    if model is None:
        raise UnknownMessageTypeError(f"未知消息类型：{envelope.type}")
    try:
        payload = model.model_validate_json(json.dumps(envelope.payload, ensure_ascii=False))
    except ValidationError as exc:
        raise MessageValidationError(
            f"{envelope.type.value} payload 字段不合法",
            details={"validation": str(exc)[:180]},
        ) from exc
    if role is not None and envelope.type not in allowed_message_types(role):
        raise DirectionError(f"角色 {role.value} 不允许发送 {envelope.type.value}")
    return envelope, payload
