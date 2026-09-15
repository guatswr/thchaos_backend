"""服务器配置。

生产环境通过 ``THCHAOS_GAME_TOKENS`` 和 ``THCHAOS_BOT_TOKENS`` 注入 JSON
对象，例如 ``{"token":"main-room"}``。默认 dev token 只为本地模拟端服务，
部署时设置 ``THCHAOS_ALLOW_DEV_TOKENS=0``。
"""

from __future__ import annotations

import json
import os
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
        )

    def token_room(self, token: str, role: str) -> str | None:
        mapping = self.game_tokens if role == "game" else self.bot_tokens
        room = mapping.get(token)
        if room is None:
            return None
        if not self.allow_dev_tokens and token in {"dev-game-token", "dev-bot-token"}:
            return None
        return room
