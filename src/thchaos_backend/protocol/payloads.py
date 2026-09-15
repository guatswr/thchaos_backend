"""协议 v1 的消息载荷模型。

载荷模型刻意不包含服务器连接对象或平台对象，C++ 游戏端、Python 后端和
AstrBot 插件都可以依据 ``docs/protocol-v1.md`` 独立实现。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from .types import (
    BallotIndex,
    CastId,
    Choice,
    ClientVersion,
    CommandId,
    Difficulty,
    EventId,
    EventKey,
    GameTimeMs,
    Identifier,
    Millis,
    Nonce,
    PoolSize,
    PositiveMillis,
    RoundId,
    Seq,
    SessionId,
    StageNumber,
    VoterId,
    ProtocolModel,
)


class ClientRole(StrEnum):
    GAME = "game"
    BOT = "bot"


class GamePhase(StrEnum):
    OFFLINE = "offline"
    TITLE = "title"
    WAITING = "waiting"
    VOTING = "voting"
    REPLAY = "replay"


class VoteAckStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class VoteAckReason(StrEnum):
    ACCEPTED = "accepted"
    DISABLED = "disabled"
    NOT_VOTING = "not_voting"
    WRONG_ROUND = "wrong_round"
    BAD_CHOICE = "bad_choice"
    BAD_VOTER = "bad_voter"
    DUPLICATE = "duplicate"
    FULL = "full"


class VoteCloseReason(StrEnum):
    WINNER = "winner"
    TIE_ALL = "tie_all"
    NO_VOTES_RANDOM = "no_votes_random"
    CANCELLED = "cancelled"


class EffectStatus(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


class StateReason(StrEnum):
    STAGE_ENTERED = "stage_entered"
    STAGE_LEFT = "stage_left"
    PAUSED = "paused"
    RESUMED = "resumed"
    OFFLINE = "offline"
    SYNC = "sync"


ShortText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
]
GroupId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=32, pattern=r"^[0-9]+$"),
]
RawMessageId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[!-~]+$"),
]


class HelloPayload(ProtocolModel):
    role: ClientRole
    client_id: Identifier
    client_version: ClientVersion
    token: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    room_id: Identifier
    game_instance_id: Identifier | None = None


class AuthenticatedPayload(ProtocolModel):
    session_id: SessionId
    role: ClientRole
    room_id: Identifier
    game_instance_id: Identifier | None = None
    server_time: str
    resumed: bool = False


class HeartbeatPayload(ProtocolModel):
    nonce: Nonce


class ErrorNoticePayload(ProtocolModel):
    message: ShortText


class GameStatePayload(ProtocolModel):
    phase: GamePhase
    stage: StageNumber | None = None
    difficulty: Difficulty | None = None
    paused: bool = False
    replay: bool = False
    live_run: bool = False
    game_time_ms: GameTimeMs = 0
    remaining_ms: Millis = 0
    reason: StateReason = StateReason.SYNC


class GameSyncPayload(ProtocolModel):
    state: GameStatePayload
    active_vote: "VoteOpenedPayload | None" = None
    latest_snapshot: "VoteSnapshotPayload | None" = None
    last_closed_round: RoundId | None = None


class VoteOption(ProtocolModel):
    choice: Choice
    event_id: EventId
    event_key: EventKey
    name: ShortText


class VoteOpenedPayload(ProtocolModel):
    round_id: RoundId
    options: list[VoteOption] = Field(min_length=3, max_length=3)
    vote_duration_ms: PositiveMillis
    remaining_ms: PositiveMillis
    stage: StageNumber | None = None
    difficulty: Difficulty | None = None
    paused: bool = False

    @model_validator(mode="after")
    def validate_choices(self) -> "VoteOpenedPayload":
        choices = [item.choice for item in self.options]
        if choices != [1, 2, 3]:
            raise ValueError("options 必须按 choice 1、2、3 且每项恰好出现一次")
        return self


class VoteCastPayload(ProtocolModel):
    cast_id: CastId
    round_id: RoundId
    choice: Choice
    voter_id: VoterId
    source_group_id: GroupId
    source_message_id: RawMessageId


class VoteAckPayload(ProtocolModel):
    cast_id: CastId
    round_id: RoundId
    choice: Choice
    status: VoteAckStatus
    reason: VoteAckReason
    counted: bool

    @model_validator(mode="after")
    def validate_status(self) -> "VoteAckPayload":
        if (self.status == VoteAckStatus.ACCEPTED) != self.counted:
            raise ValueError("accepted 必须 counted=true，rejected 必须 counted=false")
        if self.status == VoteAckStatus.ACCEPTED and self.reason != VoteAckReason.ACCEPTED:
            raise ValueError("accepted 的 reason 必须是 accepted")
        if self.status == VoteAckStatus.REJECTED and self.reason == VoteAckReason.ACCEPTED:
            raise ValueError("rejected 不能使用 accepted reason")
        return self


class VoteCount(ProtocolModel):
    choice: Choice
    event_id: EventId
    event_key: EventKey
    name: ShortText
    votes: int = Field(ge=0, le=4_294_967_295)


class VoteSnapshotPayload(ProtocolModel):
    round_id: RoundId
    phase: GamePhase
    options: list[VoteCount] = Field(min_length=3, max_length=3)
    total_votes: int = Field(ge=0, le=4_294_967_295)
    unique_voters: int = Field(ge=0, le=4_294_967_295)
    remaining_ms: Millis
    paused: bool = False

    @model_validator(mode="after")
    def validate_totals(self) -> "VoteSnapshotPayload":
        if [item.choice for item in self.options] != [1, 2, 3]:
            raise ValueError("snapshot options 必须按 choice 1、2、3 排列")
        if sum(item.votes for item in self.options) != self.total_votes:
            raise ValueError("total_votes 必须等于三个选项票数之和")
        if self.unique_voters > self.total_votes:
            raise ValueError("unique_voters 不能大于 total_votes")
        return self


class VoteClosedPayload(ProtocolModel):
    round_id: RoundId
    reason: VoteCloseReason
    winner_event_ids: list[EventId] = Field(min_length=1, max_length=3)
    winner_choices: list[Choice] = Field(min_length=1, max_length=3)
    winning_votes: int = Field(ge=0, le=4_294_967_295)
    total_votes: int = Field(ge=0, le=4_294_967_295)
    final_options: list[VoteCount] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_outcome(self) -> "VoteClosedPayload":
        if len(self.winner_event_ids) != len(self.winner_choices):
            raise ValueError("winner_event_ids 和 winner_choices 长度必须相同")
        if len(set(self.winner_event_ids)) != len(self.winner_event_ids):
            raise ValueError("winner_event_ids 不能重复")
        if [item.choice for item in self.final_options] != [1, 2, 3]:
            raise ValueError("final_options 必须按 choice 1、2、3 排列")
        if sum(item.votes for item in self.final_options) != self.total_votes:
            raise ValueError("final_options 的票数和必须等于 total_votes")
        if self.reason == VoteCloseReason.NO_VOTES_RANDOM:
            if self.total_votes != 0 or len(self.winner_event_ids) != 1 or self.winning_votes != 0:
                raise ValueError("无人投票结果必须是单个随机选项且 winning_votes=0")
        if self.reason == VoteCloseReason.TIE_ALL and len(self.winner_event_ids) != 3:
            raise ValueError("平票结果必须包含全部三个选项")
        return self


class EffectResolvedPayload(ProtocolModel):
    round_id: RoundId
    command_id: CommandId
    event_id: EventId
    event_key: EventKey
    status: EffectStatus
    result_code: ShortText
    applied_game_time_ms: GameTimeMs


class ConnectionStatusPayload(ProtocolModel):
    room_id: Identifier
    game_instance_id: Identifier | None = None
    reason: ShortText


class ServerStatePayload(ProtocolModel):
    state: GameStatePayload
    active_vote: VoteOpenedPayload | None = None
    latest_snapshot: VoteSnapshotPayload | None = None
    game_online: bool


class RateLimitPayload(ProtocolModel):
    retry_after_ms: PositiveMillis


class ProtocolInfoPayload(ProtocolModel):
    version: int = Field(ge=1, le=255)
    message_types: list[ShortText] = Field(min_length=1)


# 解决前向引用，同时让静态检查器看到这些模型的真实类型。
GameSyncPayload.model_rebuild()
