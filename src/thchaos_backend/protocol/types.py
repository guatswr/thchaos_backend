"""协议 v1 的基础值类型与公共模型基类。

这里的所有类型都直接对应游戏端已有的约束，不引入游戏端没有的概念：

* ``VoterId`` 的长度和字符集就是 ``vote::ParseVotePacket`` 接受的范围
  （1..63 个可打印、非空白 ASCII 字节），因此一个合法 ``voter_id``
  一定能被游戏端接受。
* ``Choice`` 是 1..3，对应 ``vote::Engine::Submit`` 的 ``choice`` 参数，
  也对应 ``OPTION_COUNT`` 个候选位。
* ``RoundId`` 是游戏端 ``Engine::mRoundId`` 的十进制值，单调递增且不因
  关卡切换回退。
* ``CommandId`` 是游戏端 Chaos 邮箱的去重键，用十进制字符串承载：真实的
  ``commandId`` 形如 ``0x564F544500000000 + round_id * 3 + index``，超过
  JSON 的安全整数范围（2^53-1），用 JSON number 传会被 JS 之类的实现悄悄
  改值，所以线上格式固定为字符串。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# JSON 能精确表示的最大整数（2^53 - 1）。超过它的整数在 JavaScript 一侧会
# 丢精度，协议里所有在线传输的整数字段都必须留在这个范围内。
MAX_JSON_SAFE_INT = 9_007_199_254_740_991

# 游戏端 vote::OPTION_COUNT，协议 v1 固定为 3，不随配置变化。
OPTION_COUNT = 3

# 单条 WSS 文本帧的大小上限。协议消息本身都很小，这个上限只是防御性的。
MAX_MESSAGE_BYTES = 16 * 1024


class ProtocolModel(BaseModel):
    """所有协议模型的基类。

    * ``extra="forbid"``：未知字段一律报错，绝不静默忽略。拼错的字段名会
      在第一次联调时就暴露，而不是变成一个永远为默认值的空字段。
    * ``strict=True``：不做隐式类型转换。``"2"`` 不会被当成 ``2``，
      ``2.0`` 不会被当成 ``2``，``true`` 不是整数 1。
    * ``frozen=True``：模型不可变，解析出来的消息可以安全地在协程之间传递。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _validate_timestamp(value: str) -> str:
    if not _TIMESTAMP_RE.match(value):
        raise ValueError(
            "时间戳必须是 UTC、毫秒精度、带 Z 后缀的 ISO-8601，"
            "形如 2026-09-15T12:00:00.000Z；不接受本地时间、时区偏移或省略毫秒"
        )
    try:
        datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"不是真实存在的时间：{value}") from exc
    return value


def utc_now() -> str:
    """返回协议格式的当前 UTC 时间。"""
    return format_timestamp(datetime.now(timezone.utc))


def format_timestamp(moment: datetime) -> str:
    """把 ``datetime`` 转成协议格式；非 UTC 的输入会先转换到 UTC。"""
    if moment.tzinfo is None:
        raise ValueError("拒绝无时区的 datetime：协议只承载 UTC 时间")
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_timestamp(value: str) -> datetime:
    """把协议格式的时间戳解析成带时区的 ``datetime``。"""
    _validate_timestamp(value)
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


UtcTimestamp = Annotated[str, AfterValidator(_validate_timestamp)]


# ---------------------------------------------------------------------------
# 标识符
# ---------------------------------------------------------------------------

# room_id / game_instance_id 共用的字符集：URL 安全、可以直接出现在主题名、
# 日志和文件路径里，不需要转义。
Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
]

ClientVersion = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=32, pattern=r"^[!-~]+$"),
]


def _validate_uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"必须是 UUID：{value!r}") from exc
    if parsed.version != 4:
        raise ValueError("必须是 v4 UUID（随机生成，不用时间戳或 MAC 派生）")
    return str(parsed)  # 统一规范化为小写带连字符的形式


MessageId = Annotated[str, AfterValidator(_validate_uuid4)]
CastId = Annotated[str, AfterValidator(_validate_uuid4)]


def new_message_id() -> str:
    """生成一个新的 v4 UUID，供 message_id / cast_id 使用。"""
    return str(uuid.uuid4())


def _validate_voter_id(value: str) -> str:
    # 与游戏端 vote::ParseVotePacket 完全一致：1..63 个 0x21..0x7E 字节，
    # 不含空白。超长或不含合法字符的 id 在游戏端会被判成 BadVoter。
    if not value:
        raise ValueError("voter_id 不能为空")
    if len(value) > 63:
        raise ValueError("voter_id 最长 63 字节（游戏端缓冲上限）")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
        raise ValueError("voter_id 只能包含可打印、非空白的 ASCII 字符（0x21..0x7E）")
    return value


VoterId = Annotated[str, AfterValidator(_validate_voter_id)]


def _validate_command_id(value: str) -> str:
    if not value.isascii() or not value.isdigit():
        raise ValueError("command_id 是十进制字符串，例如 \"6217451873653358592\"")
    if len(value) > 1 and value[0] == "0":
        raise ValueError("command_id 不接受前导零")
    number = int(value)
    if number == 0:
        # 游戏端把 commandId == 0 判为 RejectCommandId：0 只留给本地调试入口。
        raise ValueError("command_id 不能为 0（0 是游戏端本地调试入口的保留值）")
    if number > 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError("command_id 超出 uint64 范围")
    return value


CommandId = Annotated[str, AfterValidator(_validate_command_id)]


# ---------------------------------------------------------------------------
# 数值
#
# 全部使用 Annotated[int, Field(...)] 而不是裸 int：越界数字在解析阶段就被
# 拒绝，后端不会带着一个 0 轮次或第 4 个选项继续跑。
# ---------------------------------------------------------------------------

# 游戏端 Engine::mRoundId 从 1 开始单调递增，只在进程重启时归零。
# 上限取 JSON 安全整数，理由见文件顶部说明。
RoundId = Annotated[int, Field(ge=1, le=MAX_JSON_SAFE_INT)]

# 观众的选择：1..3，与游戏端 Submit() 的 choice 参数同义。
# 与候选位下标的关系固定为 choice == ballot_index + 1。
Choice = Annotated[int, Field(ge=1, le=OPTION_COUNT)]

# 候选位下标：0..2，只在需要按下标引用候选时使用。
BallotIndex = Annotated[int, Field(ge=0, le=OPTION_COUNT - 1)]

# 游戏端 ChaosEventId 是 uint16，0 保留为「未注册」。
EventId = Annotated[int, Field(ge=1, le=65535)]

# 每连接、每发送方的单调序号，从 1 开始。
Seq = Annotated[int, Field(ge=1, le=MAX_JSON_SAFE_INT)]

# 毫秒计时。上限一小时，比游戏端 Engine::SetTiming 的钳制上限（3600000）
# 宽松一点，用于拒绝明显是单位搞错（秒当毫秒）的输入。
Millis = Annotated[int, Field(ge=0, le=3_600_000)]

PositiveMillis = Annotated[int, Field(ge=1, le=3_600_000)]

# 游戏端单帧最多推进 250ms，游戏时间总量本身没有上限。
GameTimeMs = Annotated[int, Field(ge=0, le=MAX_JSON_SAFE_INT)]

# 事件池大小：游戏端 vote::MAX_POOL == 64。
PoolSize = Annotated[int, Field(ge=0, le=64)]

# 关卡号 / 难度：协议把它们当不透明整数，只用于展示和审计。
StageNumber = Annotated[int, Field(ge=0, le=999)]
Difficulty = Annotated[int, Field(ge=0, le=7)]

# 事件 key，例如 "resource.bomb.add"。这是跨端唯一的稳定标识，本地化文本
# 永远不能出现在这个位置。
EventKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"),
]

# 短原因键，例如 "stage_entered"、"resumed"。
ReasonKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$"),
]

# 心跳随机串。
Nonce = Annotated[
    str,
    StringConstraints(strict=True, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
]

# 服务器分配的会话 id。
SessionId = Annotated[
    str,
    StringConstraints(strict=True, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
]
