"""命令行入口，便于 ``python -m thchaos_backend.server`` 启动。"""

from .config import Settings


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run("thchaos_backend.server.app:app", host=settings.host, port=settings.port,
                reload=False, workers=1, ws_max_size=settings.max_frame_bytes, ws_max_queue=16)


if __name__ == "__main__":
    main()
