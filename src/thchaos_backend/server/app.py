"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, WebSocket

from .config import Settings
from .hub import Hub


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="THChaos Backend", version="0.1.0")
    app.state.settings = settings or Settings.from_env()
    app.state.hub = Hub(app.state.settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        hub: Hub = app.state.hub
        return {"ok": True, "rooms": len(hub.rooms), "service": "thchaos-backend"}

    @app.get("/rooms/{room_id}/state")
    async def room_state(room_id: str, x_admin_token: str | None = Header(default=None)) -> dict[str, object]:
        # 状态接口默认关闭；需要排障时显式设置管理员 Token，并由反代继续
        # 限制来源。投票 Token 永远不能访问这个接口。
        if not app.state.settings.admin_token or x_admin_token != app.state.settings.admin_token:
            raise HTTPException(status_code=404, detail="not found")
        hub: Hub = app.state.hub
        room = hub.room(room_id)
        return {
            "room_id": room.room_id,
            "game_online": room.game is not None,
            "game_instance_id": room.game.game_instance_id if room.game else None,
            "state": room.state.model_dump(mode="json"),
            "active_vote": room.active_vote.model_dump(mode="json") if room.active_vote else None,
            "latest_snapshot": room.latest_snapshot.model_dump(mode="json") if room.latest_snapshot else None,
        }

    @app.websocket("/ws/game")
    async def game_socket(websocket: WebSocket) -> None:
        await app.state.hub.handle(websocket)

    @app.websocket("/ws/bot")
    async def bot_socket(websocket: WebSocket) -> None:
        await app.state.hub.handle(websocket)

    return app


app = create_app()
