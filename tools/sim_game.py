"""本机模拟游戏端。

用法：先启动服务器，再运行此脚本；另一个终端运行 ``sim_bot.py``，在 Bot
终端输入 1/2/3 即可观察完整投票链路。它不会连接真实游戏或 QQ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import websockets

from thchaos_backend.protocol import (
    ClientRole,
    EffectResolvedPayload,
    EffectStatus,
    GamePhase,
    GameStatePayload,
    MessageType,
    StateReason,
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
    parse_message,
)
from thchaos_backend.protocol.types import new_message_id


def make_message(message_type, payload, *, room: str, instance: str, seq: int) -> str:
    return make_envelope(
        message_type,
        room_id=room,
        game_instance_id=instance,
        seq=seq,
        payload=payload,
    ).model_dump_json()


async def run(url: str, room: str, token: str) -> None:
    instance = f"sim-{uuid.uuid4().hex[:12]}"
    seq = 1
    async with websockets.connect(url) as ws:
        from thchaos_backend.protocol import HelloPayload

        await ws.send(
            make_message(
                MessageType.HELLO,
                HelloPayload(
                    role=ClientRole.GAME,
                    client_id="sim-game",
                    client_version="sim-1",
                    token=token,
                    room_id=room,
                    game_instance_id=instance,
                ),
                room=room,
                instance=instance,
                seq=seq,
            )
        )
        print("server:", json.loads(await ws.recv())["type"])
        seq += 1
        state = GameStatePayload(
            phase=GamePhase.WAITING,
            stage=1,
            difficulty=1,
            paused=False,
            live_run=True,
            reason=StateReason.STAGE_ENTERED,
        )
        await ws.send(make_message(MessageType.GAME_STATE_CHANGED, state, room=room, instance=instance, seq=seq))
        seq += 1
        await asyncio.sleep(0.2)
        options = [
            VoteOption(choice=1, event_id=11, event_key="chaos.event_1", name="速度降低"),
            VoteOption(choice=2, event_id=12, event_key="chaos.event_2", name="敌弹加速"),
            VoteOption(choice=3, event_id=13, event_key="chaos.event_3", name="封锁方向"),
        ]
        opened = VoteOpenedPayload(round_id=1, options=options, vote_duration_ms=10_000, remaining_ms=10_000)
        await ws.send(make_message(MessageType.VOTE_OPENED, opened, room=room, instance=instance, seq=seq))
        seq += 1
        print("投票已开放，等待 Bot 投票（Ctrl+C 退出）")
        votes: dict[str, int] = {}
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            # 这里是在读取服务器下发的消息，不能用“游戏端可发送类型”做
            # 方向校验；服务器已经完成了入站方向校验。
            envelope, payload = parse_message(raw)
            if envelope.type == MessageType.VOTE_CAST:
                assert payload is not None
                voter = payload.voter_id
                if voter in votes:
                    ack = VoteAckPayload(
                        cast_id=payload.cast_id,
                        round_id=payload.round_id,
                        choice=payload.choice,
                        status=VoteAckStatus.REJECTED,
                        reason=VoteAckReason.DUPLICATE,
                        counted=False,
                    )
                else:
                    votes[voter] = payload.choice
                    ack = VoteAckPayload(
                        cast_id=payload.cast_id,
                        round_id=payload.round_id,
                        choice=payload.choice,
                        status=VoteAckStatus.ACCEPTED,
                        reason=VoteAckReason.ACCEPTED,
                        counted=True,
                    )
                await ws.send(make_message(MessageType.VOTE_ACK, ack, room=room, instance=instance, seq=seq))
                seq += 1
                totals = [sum(choice == i for choice in votes.values()) for i in (1, 2, 3)]
                snapshot = VoteSnapshotPayload(
                    round_id=1,
                    phase=GamePhase.VOTING,
                    options=[
                        VoteCount(choice=i + 1, event_id=11 + i, event_key=f"chaos.event_{i + 1}", name=options[i].name, votes=totals[i])
                        for i in range(3)
                    ],
                    total_votes=len(votes),
                    unique_voters=len(votes),
                    remaining_ms=5000,
                )
                await ws.send(make_message(MessageType.VOTE_SNAPSHOT, snapshot, room=room, instance=instance, seq=seq))
                seq += 1
                if len(votes) >= 3:
                    break
        totals = [sum(choice == i for choice in votes.values()) for i in (1, 2, 3)]
        best = max(totals) if totals else 0
        if best == 0:
            reason = VoteCloseReason.NO_VOTES_RANDOM
            winner_choices = [1]
            winner_ids = [11]
            winning = 0
        elif totals.count(best) > 1:
            reason = VoteCloseReason.TIE_ALL
            winner_choices = [1, 2, 3]
            winner_ids = [11, 12, 13]
            winning = best
        else:
            reason = VoteCloseReason.WINNER
            winner_choices = [totals.index(best) + 1]
            winner_ids = [10 + winner_choices[0]]
            winning = best
        final = [
            VoteCount(choice=i + 1, event_id=11 + i, event_key=f"chaos.event_{i + 1}", name=options[i].name, votes=totals[i])
            for i in range(3)
        ]
        closed = VoteClosedPayload(
            round_id=1,
            reason=reason,
            winner_event_ids=winner_ids,
            winner_choices=winner_choices,
            winning_votes=winning,
            total_votes=len(votes),
            final_options=final,
        )
        await ws.send(make_message(MessageType.VOTE_CLOSED, closed, room=room, instance=instance, seq=seq))
        seq += 1
        for event_id, choice in zip(winner_ids, winner_choices):
            effect = EffectResolvedPayload(
                round_id=1,
                command_id=str(6_217_451_873_653_358_592 + choice),
                event_id=event_id,
                event_key=f"chaos.event_{choice}",
                status=EffectStatus.APPLIED,
                result_code="applied",
                applied_game_time_ms=1234,
            )
            await ws.send(make_message(MessageType.EFFECT_RESOLVED, effect, room=room, instance=instance, seq=seq))
            seq += 1
        print("投票已关并报告执行结果")
        await asyncio.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws/game")
    parser.add_argument("--room", default="main")
    parser.add_argument("--token", default="dev-game-token")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.room, args.token))


if __name__ == "__main__":
    main()
