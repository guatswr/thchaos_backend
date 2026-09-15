"""协议 v1 的错误码、``error`` 消息载荷和解析期异常。

错误码分成四组，前缀就是分组名，便于日志过滤：

* ``protocol.*``：信封层问题（版本、字段、方向、序号、限流）。
* ``auth.*``：握手与鉴权。
* ``round.*``：轮次路由。这一组的拒绝全部发生在服务器，而且**必须**发生在
  转发之前——服务器永远不替游戏端回答「这票算不算」，只回答「这票还能不能
  送到游戏端」。
* ``server.*``：服务器内部问题。

游戏端自己对投票的判定不复用这里的码：那是 ``VoteAckReason``（见
``payloads.py``），取自游戏端 ``vote::VoteResultText`` 的原始字符串，服务器
不得改写。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from .types import MessageId, ProtocolModel, ReasonKey

#: 给人看的自由文本（错误说明、审计备注）。不是标识符，程序不得依赖内容。
HumanText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]


class ErrorCode(StrEnum):
    """协议 v1 的全部错误码。同一大版本内只允许追加，不允许改语义。"""

    # ---- 信封 / 传输 ----
    PROTOCOL_VERSION_UNSUPPORTED = "protocol.version_unsupported"
    PROTOCOL_MALFORMED_MESSAGE = "protocol.malformed_message"
    PROTOCOL_UNKNOWN_MESSAGE_TYPE = "protocol.unknown_message_type"
    PROTOCOL_UNKNOWN_FIELD = "protocol.unknown_field"
    PROTOCOL_NOT_AUTHENTICATED = "protocol.not_authenticated"
    PROTOCOL_ALREADY_AUTHENTICATED = "protocol.already_authenticated"
    PROTOCOL_DIRECTION_NOT_ALLOWED = "protocol.direction_not_allowed"
    PROTOCOL_ORIGIN_MISMATCH = "protocol.origin_mismatch"
    PROTOCOL_SEQ_GAP = "protocol.seq_gap"
    PROTOCOL_SEQ_REGRESSION = "protocol.seq_regression"
    PROTOCOL_RATE_LIMITED = "protocol.rate_limited"
    PROTOCOL_MESSAGE_TOO_LARGE = "protocol.message_too_large"

    # ---- 鉴权 ----
    AUTH_INVALID_TOKEN = "auth.invalid_token"
    AUTH_ROLE_MISMATCH = "auth.role_mismatch"
    AUTH_ROOM_NOT_FOUND = "auth.room_not_found"
    AUTH_GAME_ALREADY_CONNECTED = "auth.game_already_connected"
    AUTH_SESSION_REPLACED = "auth.session_replaced"

    # ---- 轮次路由（服务器侧初步校验）----
    ROUND_UNKNOWN = "round.unknown"
    ROUND_NOT_OPEN = "round.not_open"
    ROUND_CLOSED = "round.closed"
    ROUND_OPTION_MISMATCH = "round.option_mismatch"
    ROUND_DUPLICATE_VOTE = "round.duplicate_vote"
    ROUND_GAME_OFFLINE = "round.game_offline"
    ROUND_STALE = "round.stale"
    ROUND_GAME_INSTANCE_MISMATCH = "round.game_instance_mismatch"

    # ---- 服务器 ----
    SERVER_INTERNAL_ERROR = "server.internal_error"
    SERVER_OVERLOADED = "server.overloaded"


#: 收到后可安全重试的错误码。其余错误码重试只会得到同样的结果。
RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.PROTOCOL_RATE_LIMITED,
        ErrorCode.SERVER_INTERNAL_ERROR,
        ErrorCode.SERVER_OVERLOADED,
    }
)

#: 收到即代表连接会被服务器关闭的错误码。
FATAL_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
        ErrorCode.PROTOCOL_UNKNOWN_MESSAGE_TYPE,
        ErrorCode.PROTOCOL_MALFORMED_MESSAGE,
        ErrorCode.PROTOCOL_UNKNOWN_FIELD,
        ErrorCode.PROTOCOL_ORIGIN_MISMATCH,
        ErrorCode.PROTOCOL_SEQ_REGRESSION,
        ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE,
        ErrorCode.AUTH_INVALID_TOKEN,
        ErrorCode.AUTH_ROLE_MISMATCH,
        ErrorCode.AUTH_ROOM_NOT_FOUND,
        ErrorCode.AUTH_GAME_ALREADY_CONNECTED,
        ErrorCode.AUTH_SESSION_REPLACED,
        ErrorCode.SERVER_OVERLOADED,
    }
)


class ErrorPayload(ProtocolModel):
    """``error`` 消息的载荷。

    ``room_id`` 无法解析时（例如 ``hello`` 本身格式错误），服务器仍然要能回
    一条 error，此时信封里的 ``room_id`` 允许为 null——这是信封层唯一允许
    ``room_id`` 为空的情形，见 ``envelope.py``。
    """

    code: ErrorCode
    #: 给人看的中文说明。不是协议的一部分，程序不得依赖它的内容。
    message: HumanText
    #: 触发这条错误的入站消息的 message_id（能定位到就填）。
    in_reply_to: MessageId | None = None
    #: 触发这条错误的投票尝试的 cast_id（能定位到就填）。插件据此把 error
    #: 对回到它发出、但还没等到 vote.ack 的那一次投票。
    cast_id: str | None = Field(default=None, min_length=1, max_length=64)
    #: 服务器是否会在发出本条消息后关闭连接。
    fatal: bool = False
    #: 重试是否有意义。应与 ``RETRYABLE_ERROR_CODES`` 保持一致。
    retryable: bool = False
    #: 结构化补充信息，例如 {"expected_version": "1", "got_version": "2"}。
    details: dict[ReasonKey, HumanText] = Field(default_factory=dict, max_length=8)


class ProtocolError(ValueError):
    """协议层异常。``code`` 可以直接放进 ``error`` 消息。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        fatal: bool | None = None,
        cast_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, str] = {str(k): str(v) for k, v in (details or {}).items()}
        self.fatal = code in FATAL_ERROR_CODES if fatal is None else fatal
        self.cast_id = cast_id

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_ERROR_CODES

    def to_payload(
        self, *, in_reply_to: str | None = None, cast_id: str | None = None
    ) -> ErrorPayload:
        """把异常转换成可以直接下发的 ``error`` 载荷。"""
        return ErrorPayload(
            code=self.code,
            message=self.message[:200] or self.code.value,
            in_reply_to=in_reply_to,
            cast_id=cast_id or self.cast_id,
            fatal=self.fatal,
            retryable=self.retryable,
            details=dict(list(self.details.items())[:8]),
        )


class MessageValidationError(ProtocolError):
    """消息结构不合法（字段缺失、类型不对、出现未知字段等）。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.PROTOCOL_MALFORMED_MESSAGE, message, details=details)


class UnknownMessageTypeError(ProtocolError):
    """``type`` 不在 v1 的类型表里。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.PROTOCOL_UNKNOWN_MESSAGE_TYPE, message, details=details)


class DirectionError(ProtocolError):
    """消息类型与发送方角色不匹配（例如 bot 试图发 ``vote.opened``）。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED, message, details=details)


class MessageTooLargeError(ProtocolError):
    """单帧超过 ``MAX_MESSAGE_BYTES``。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE, message, details=details)
