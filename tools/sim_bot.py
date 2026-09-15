"""本机模拟 AstrBot：从标准输入读取 1/2/3，并显示后端广播。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import websockets

from thchaos_backend.protocol import ClientRole, MessageType, VoteCastPayload, make_envelope, parse_message


async def run(url: str, room: str, token: str) -> None:
    seq = 1
    active_instance: str | None = None
    active_round: int | None = None
    async with websockets.connect(url) as ws:
        from thchaos_backend.protocol import HelloPayload

        await ws.send(
            make_envelope(
                MessageType.HELLO,
                room_id=room,
                seq=seq,
                game_instance_id=None,
                payload=HelloPayload(
                    role=ClientRole.BOT,
                    client_id="sim-bot",
                    client_version="sim-1",
                    token=token,
                    room_id=room,
                ),
            ).model_dump_json()
        )
        print("server:", json.loads(await ws.recv())["type"])
        print("server:", json.loads(await ws.recv())["type"])
        sync = json.loads(await ws.recv())
        print("server:", sync["type"])

        async def receive() -> None:
            nonlocal active_instance, active_round
            while True:
                raw = await ws.recv()
                envelope, payload = parse_message(raw)
                print("<-", envelope.type.value, json.dumps(envelope.payload, ensure_ascii=False))
                if envelope.type == MessageType.VOTE_OPENED:
                    active_instance = envelope.game_instance_id
                    active_round = payload.round_id  # type: ignore[union-attr]
                elif envelope.type == MessageType.VOTE_CLOSED:
                    active_round = None

        receiver = asyncio.create_task(receive())
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                choice = line.strip()
                if choice not in {"1", "2", "3"}:
                    print("只接受 1 / 2 / 3")
                    continue
                if active_instance is None or active_round is None:
                    print("当前没有活动投票")
                    continue
                payload = VoteCastPayload(
                    cast_id=str(uuid.uuid4()),
                    round_id=active_round,
                    choice=int(choice),
                    voter_id="sim-qq-hmac",
                    source_group_id="123456",
                    source_message_id=str(uuid.uuid4()),
                )
                seq += 1
                await ws.send(
                    make_envelope(
                        MessageType.VOTE_CAST,
                        room_id=room,
                        game_instance_id=active_instance,
                        seq=seq,
                        payload=payload,
                    ).model_dump_json()
                )
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws/bot")
    parser.add_argument("--room", default="main")
    parser.add_argument("--token", default="dev-bot-token")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.room, args.token))


if __name__ == "__main__":
    main()
