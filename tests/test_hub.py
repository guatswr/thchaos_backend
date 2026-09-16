import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
import websockets

from thchaos_backend.protocol import (
    ClientRole,
    EffectResolvedPayload,
    EffectStatus,
    GamePhase,
    MessageType,
    VoteAckPayload,
    VoteAckReason,
    VoteAckStatus,
    VoteCloseReason,
    VoteClosedPayload,
    VoteCount,
    VoteOpenedPayload,
    VoteOption,
    VoteSnapshotPayload,
    make_envelope,
)
from thchaos_backend.protocol.types import new_message_id
from thchaos_backend.server.app import create_app
from thchaos_backend.server.config import Settings

ROOM = "main"
INSTANCE = "game-a"


def envelope(message_type, payload, *, seq: int, instance: str | None = INSTANCE):
    return make_envelope(
        message_type,
        room_id=ROOM,
        game_instance_id=instance,
        seq=seq,
        payload=payload,
    ).model_dump_json()


def hello(role: ClientRole, token: str, client_id: str, *, instance: str | None = INSTANCE):
    from thchaos_backend.protocol import HelloPayload

    return envelope(
        MessageType.HELLO,
        HelloPayload(
            role=role,
            client_id=client_id,
            client_version="test-1",
            token=token,
            room_id=ROOM,
            game_instance_id=instance,
        ),
        seq=1,
        instance=instance,
    )


def opened(round_id: int = 1):
    return VoteOpenedPayload(
        round_id=round_id,
        options=[
            VoteOption(choice=1, event_id=11, event_key="chaos.event_1", name="事件 1",
                       description="冻结5秒，再以150%速度运动2秒。"),
            VoteOption(choice=2, event_id=12, event_key="chaos.event_2", name="事件 2"),
            VoteOption(choice=3, event_id=13, event_key="chaos.event_3", name="事件 3"),
        ],
        vote_duration_ms=10_000,
        remaining_ms=10_000,
    )


async def recv_type(ws, expected: str) -> dict:
    value = await asyncio.wait_for(ws.recv(), timeout=2)
    data = json.loads(value)
    assert data["type"] == expected, data
    return data


@pytest.mark.asyncio
async def test_game_bot_round_flow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    settings = Settings(
        host="127.0.0.1",
        port=listener.getsockname()[1],
        database_path=tmp_path / "audit.sqlite3",
        game_tokens={"game-token": ROOM},
        bot_tokens={"bot-token": ROOM},
        allow_dev_tokens=False,
    )
    config = uvicorn.Config(create_app(settings), host=settings.host, port=settings.port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with websockets.connect(f"ws://127.0.0.1:{settings.port}/ws/game") as game, websockets.connect(
            f"ws://127.0.0.1:{settings.port}/ws/bot"
        ) as bot:
            await game.send(hello(ClientRole.GAME, "game-token", "game-client"))
            await recv_type(game, "authenticated")
            await bot.send(hello(ClientRole.BOT, "bot-token", "astrbot", instance=None))
            await recv_type(bot, "authenticated")
            await recv_type(bot, "protocol.info")
            await recv_type(bot, "game.sync")

            await game.send(envelope(MessageType.VOTE_OPENED, opened(), seq=2))
            announced = await recv_type(bot, "vote.opened")
            assert announced["payload"]["options"][0]["description"] == opened().options[0].description
            # A newly connected bot gets descriptions from cached sync, too.
            async with websockets.connect(f"ws://127.0.0.1:{settings.port}/ws/bot") as resumed_bot:
                await resumed_bot.send(hello(ClientRole.BOT, "bot-token", "astrbot-resumed", instance=None))
                await recv_type(resumed_bot, "authenticated")
                await recv_type(resumed_bot, "protocol.info")
                synced = await recv_type(resumed_bot, "game.sync")
                assert synced["payload"]["active_vote"]["options"][0]["description"] == opened().options[0].description

            cast_id = new_message_id()
            from thchaos_backend.protocol import VoteCastPayload

            cast = VoteCastPayload(
                cast_id=cast_id,
                round_id=1,
                choice=2,
                voter_id="qq-hmac-a",
                source_group_id="123456",
                source_message_id="9001",
            )
            await bot.send(envelope(MessageType.VOTE_CAST, cast, seq=2))
            forwarded = await recv_type(game, "vote.cast")
            assert forwarded["payload"]["cast_id"] == cast_id

            ack = VoteAckPayload(
                cast_id=cast_id,
                round_id=1,
                choice=2,
                status=VoteAckStatus.ACCEPTED,
                reason=VoteAckReason.ACCEPTED,
                counted=True,
            )
            await game.send(envelope(MessageType.VOTE_ACK, ack, seq=3))
            await recv_type(bot, "vote.ack")

            # Backend-side duplicate protection is a non-fatal per-message
            # rejection: the Bot connection remains usable for later rounds.
            duplicate = VoteCastPayload(
                cast_id=new_message_id(),
                round_id=1,
                choice=1,
                voter_id="qq-hmac-a",
                source_group_id="123456",
                source_message_id="9002",
            )
            await bot.send(envelope(MessageType.VOTE_CAST, duplicate, seq=3))
            duplicate_error = await recv_type(bot, "error")
            assert duplicate_error["payload"]["code"] == "round.duplicate_vote"

            snapshot = VoteSnapshotPayload(
                round_id=1,
                phase=GamePhase.VOTING,
                options=[
                    VoteCount(choice=1, event_id=11, event_key="chaos.event_1", name="事件 1", votes=0),
                    VoteCount(choice=2, event_id=12, event_key="chaos.event_2", name="事件 2", votes=1),
                    VoteCount(choice=3, event_id=13, event_key="chaos.event_3", name="事件 3", votes=0),
                ],
                total_votes=1,
                unique_voters=1,
                remaining_ms=9000,
            )
            await game.send(envelope(MessageType.VOTE_SNAPSHOT, snapshot, seq=4))
            await recv_type(bot, "vote.snapshot")

            closed = VoteClosedPayload(
                round_id=1,
                reason=VoteCloseReason.WINNER,
                winner_event_ids=[12],
                winner_choices=[2],
                winning_votes=1,
                total_votes=1,
                final_options=snapshot.options,
            )
            await game.send(envelope(MessageType.VOTE_CLOSED, closed, seq=5))
            await recv_type(bot, "vote.closed")

            effect = EffectResolvedPayload(
                round_id=1,
                command_id="6217451873653358592",
                event_id=12,
                event_key="chaos.event_2",
                status=EffectStatus.APPLIED,
                result_code="applied",
                applied_game_time_ms=5000,
            )
            await game.send(envelope(MessageType.EFFECT_RESOLVED, effect, seq=6))
            await recv_type(bot, "effect.resolved")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=3)
        listener.close()
