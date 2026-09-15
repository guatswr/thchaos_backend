"""AstrBot 插件的纯函数，脱离 AstrBot 运行时即可测试。"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any


def parse_vote_choice(text: str) -> int | None:
    """只接受完整、去空白后的单字符 1/2/3。"""

    value = text.strip()
    return int(value) if re.fullmatch(r"[123]", value) else None


def pseudonymous_voter_id(secret: str, sender_id: str) -> str:
    """生成符合游戏端 ASCII/63 字节约束的稳定伪名。"""

    digest = hmac.new(secret.encode("utf-8"), f"qq:{sender_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    # 游戏端 voter 缓冲最多接受 63 个 ASCII 字节；1 个前缀字符 + 62 个
    # 十六进制字符刚好保留 248 bit 的碰撞裕量。
    return f"q{digest[:62]}"


def default_umo(group_id: str) -> str:
    return f"aiocqhttp:GroupMessage:{group_id}"


def format_vote_opened(payload: dict[str, Any]) -> str:
    lines = [f"【观众投票 #{payload['round_id']}】"]
    for item in payload["options"]:
        lines.append(f"{item['choice']}. {item['name']}")
    seconds = payload.get("remaining_ms", 0) / 1000
    lines.append(f"回复 1/2/3 投票（剩余 {seconds:.1f} 秒）")
    return "\n".join(lines)


def format_snapshot(payload: dict[str, Any]) -> str:
    parts = [f"{item['choice']}:{item['votes']}票" for item in payload["options"]]
    suffix = "（游戏暂停，计时冻结）" if payload.get("paused") else ""
    return f"【票况 #{payload['round_id']}】" + "  ".join(parts) + suffix


def format_vote_closed(payload: dict[str, Any]) -> str:
    labels = {
        "winner": "最高票",
        "tie_all": "平票，三个全开",
        "no_votes_random": "无人投票，随机抽取",
        "cancelled": "已取消",
    }
    winners = ", ".join(str(item) for item in payload["winner_choices"])
    return (
        f"【投票结果 #{payload['round_id']}】{labels.get(payload['reason'], payload['reason'])}\n"
        f"选项：{winners}；最高票 {payload['winning_votes']}，总票 {payload['total_votes']}"
    )


def format_effect(payload: dict[str, Any]) -> str:
    status = "已生效" if payload["status"] == "applied" else "被游戏拒绝"
    return f"【Chaos 执行】{payload['name'] if 'name' in payload else payload['event_key']}：{status}（{payload['result_code']}）"
