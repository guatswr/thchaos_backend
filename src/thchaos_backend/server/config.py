"""服务器配置。

生产环境通过 ``THCHAOS_GAME_TOKENS`` 和 ``THCHAOS_BOT_TOKENS`` 注入 JSON
对象，例如 ``{"token":"main-room"}``。默认 dev token 只为本地模拟端服务，
部署时设置 ``THCHAOS_ALLOW_DEV_TOKENS=0``。
"""

from __future__ import annotations

import json
import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


def _token_map(raw: str | None, fallback: dict[str, str]) -> dict[str, str]:
    if not raw:
        return dict(fallback)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Token 配置必须是 JSON 对象，例如 {\"token\":\"room\"}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("Token 配置必须是非空 JSON 对象")
    result: dict[str, str] = {}
    for token, room in data.items():
        if not isinstance(token, str) or not token or len(token) > 256:
            raise ValueError("Token 必须是 1..256 字符字符串")
        if not isinstance(room, str) or not room:
            raise ValueError("Token 的值必须是 room_id")
        result[token] = room
    return result


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    database_path: Path = Path("data/thchaos.sqlite3")
    game_tokens: dict[str, str] = field(default_factory=lambda: {"dev-game-token": "main"})
    bot_tokens: dict[str, str] = field(default_factory=lambda: {"dev-bot-token": "main"})
    allow_dev_tokens: bool = True
    max_frame_bytes: int = 16 * 1024
    max_bots_per_room: int = 8
    admin_token: str = ""
    max_casts_per_second: int = 30
    handshake_timeout: float = 5.0
    send_timeout: float = 2.0
    ack_timeout: float = 15.0
    outbound_queue_size: int = 128
    audit_queue_size: int = 4096

    def __post_init__(self) -> None:
        for name in ("max_frame_bytes", "max_bots_per_room", "max_casts_per_second",
                     "outbound_queue_size", "audit_queue_size"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        for name in ("handshake_timeout", "send_timeout", "ack_timeout"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if not 1 <= self.port <= 65535:
            raise ValueError("port 必须在 1..65535 范围内")
        if self.game_tokens.keys() & self.bot_tokens.keys():
            raise ValueError("游戏和 Bot 不能共用 Token")
        for mapping in (self.game_tokens, self.bot_tokens):
            for token, room in mapping.items():
                if not isinstance(token, str) or not 1 <= len(token) <= 256:
                    raise ValueError("Token 必须是 1..256 字符字符串")
                if not isinstance(room, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room) is None:
                    raise ValueError("room_id 必须符合协议 Identifier 格式")

    @classmethod
    def from_env(cls) -> "Settings":
        allow_dev = os.getenv("THCHAOS_ALLOW_DEV_TOKENS", "0").lower() in {"1", "true", "yes"}
        game_fallback = {"dev-game-token": "main"} if allow_dev else {}
        bot_fallback = {"dev-bot-token": "main"} if allow_dev else {}
        return cls(
            host=os.getenv("THCHAOS_HOST", "127.0.0.1"),
            port=int(os.getenv("THCHAOS_PORT", "8765")),
            database_path=Path(os.getenv("THCHAOS_DATABASE", "data/thchaos.sqlite3")),
            game_tokens=_token_map(os.getenv("THCHAOS_GAME_TOKENS"), game_fallback),
            bot_tokens=_token_map(os.getenv("THCHAOS_BOT_TOKENS"), bot_fallback),
            allow_dev_tokens=allow_dev,
            max_frame_bytes=int(os.getenv("THCHAOS_MAX_FRAME_BYTES", str(16 * 1024))),
            admin_token=os.getenv("THCHAOS_ADMIN_TOKEN", ""),
            max_casts_per_second=int(os.getenv("THCHAOS_MAX_CASTS_PER_SECOND", "30")),
            handshake_timeout=float(os.getenv("THCHAOS_HANDSHAKE_TIMEOUT", "5")),
            send_timeout=float(os.getenv("THCHAOS_SEND_TIMEOUT", "2")),
            ack_timeout=float(os.getenv("THCHAOS_ACK_TIMEOUT", "15")),
            outbound_queue_size=int(os.getenv("THCHAOS_OUTBOUND_QUEUE_SIZE", "128")),
            audit_queue_size=int(os.getenv("THCHAOS_AUDIT_QUEUE_SIZE", "4096")),
        )

    def token_room(self, token: str, role: str) -> str | None:
        mapping = self.game_tokens if role == "game" else self.bot_tokens
        room = mapping.get(token)
        if room is None:
            return None
        if not self.allow_dev_tokens and token in {"dev-game-token", "dev-bot-token"}:
            return None
        return room
