"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, WebSocket

from .config import Settings
from .hub import Hub


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = await asyncio.to_thread(Hub, configured)
        app.state.hub = hub
        hub.start()
        try:
            yield
        finally:
            await hub.close()

    app = FastAPI(title="THChaos Backend", version="0.1.0", lifespan=lifespan)
    app.state.settings = configured

    def authorize(token: str | None) -> None:
        if not configured.admin_token or not secrets.compare_digest(
                (token or "").encode(), configured.admin_token.encode()):
            raise HTTPException(status_code=404, detail="not found")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        hub: Hub = app.state.hub
        return {"ok": True, "rooms": len(hub.rooms), "service": "thchaos-backend"}

    @app.get("/rooms/{room_id}/state")
    async def room_state(room_id: str, x_admin_token: str | None = Header(default=None)) -> dict[str, object]:
        # 状态接口默认关闭；需要排障时显式设置管理员 Token，并由反代继续
        # 限制来源。投票 Token 永远不能访问这个接口。
        authorize(x_admin_token)
        hub: Hub = app.state.hub
        room = hub.rooms.get(room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "room_id": room.room_id,
            "game_online": room.game is not None,
            "game_ready": room.ready,
            "game_instance_id": room.game.game_instance_id if room.game else None,
            "state": room.state.model_dump(mode="json"),
            "active_vote": room.active_vote.model_dump(mode="json") if room.active_vote else None,
            "latest_snapshot": room.latest_snapshot.model_dump(mode="json") if room.latest_snapshot else None,
        }

    @app.get("/metrics")
    async def metrics(x_admin_token: str | None = Header(default=None)) -> dict[str, object]:
        authorize(x_admin_token)
        return app.state.hub.metrics()

    @app.websocket("/ws/game")
    async def game_socket(websocket: WebSocket) -> None:
        await app.state.hub.handle(websocket)

    @app.websocket("/ws/bot")
    async def bot_socket(websocket: WebSocket) -> None:
        await app.state.hub.handle(websocket)

    return app


app = create_app()
