from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError
from thchaos_backend.protocol.errors import ErrorCode, ErrorPayload, ProtocolError

from thchaos_backend.protocol import (
    ClientRole,
    EffectResolvedPayload,
    EffectStatus,
    Envelope,
    GamePhase,
    GameStatePayload,
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
    parse_message,
)


def option(choice: int, event_id: int | None = None) -> VoteOption:
    return VoteOption(
        choice=choice,
        event_id=event_id or choice + 10,
        event_key=f"chaos.event_{choice}",
        name=f"事件 {choice}",
    )


def counts(values=(2, 1, 0)) -> list[VoteCount]:
    return [
        VoteCount(
            choice=i + 1,
            event_id=i + 11,
            event_key=f"chaos.event_{i + 1}",
            name=f"事件 {i + 1}",
            votes=value,
        )
        for i, value in enumerate(values)
    ]


def test_opened_requires_three_ordered_choices():
    payload = VoteOpenedPayload(
        round_id=1,
        options=[option(1), option(2), option(3)],
        vote_duration_ms=10_000,
        remaining_ms=10_000,
    )
    assert [item.choice for item in payload.options] == [1, 2, 3]
    with pytest.raises(ValidationError):
        VoteOpenedPayload(
            round_id=1,
            options=[option(1), option(2), option(2)],
            vote_duration_ms=10_000,
            remaining_ms=10_000,
        )


def test_envelope_rejects_unknown_fields_and_wrong_version():
    payload = VoteOpenedPayload(
        round_id=1,
        options=[option(1), option(2), option(3)],
        vote_duration_ms=10_000,
        remaining_ms=10_000,
    )
    message = make_envelope(
        MessageType.VOTE_OPENED,
        room_id="main",
        game_instance_id="instance-a",
        seq=1,
        payload=payload,
    ).model_dump_json()
    envelope, parsed = parse_message(message, role=None)
    assert envelope.type == MessageType.VOTE_OPENED
    assert isinstance(parsed, VoteOpenedPayload)

    raw = envelope.model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(Exception):
        Envelope.model_validate(raw)
    raw = envelope.model_dump(mode="json")
    raw["version"] = 2
    with pytest.raises(Exception):
        Envelope.model_validate(raw)


def test_role_direction_is_enforced():
    payload = VoteOpenedPayload(
        round_id=1,
        options=[option(1), option(2), option(3)],
        vote_duration_ms=10_000,
        remaining_ms=10_000,
    )
    message = make_envelope(MessageType.VOTE_OPENED, room_id="main", seq=1, payload=payload).model_dump_json()
    with pytest.raises(Exception):
        parse_message(message, role=ClientRole.BOT)


def test_snapshot_totals_and_ack_semantics():
    snapshot = VoteSnapshotPayload(
        round_id=1,
        phase=GamePhase.VOTING,
        options=counts(),
        total_votes=3,
        unique_voters=3,
        remaining_ms=5000,
    )
    assert snapshot.total_votes == 3
    with pytest.raises(ValidationError):
        VoteSnapshotPayload(
            round_id=1,
            phase=GamePhase.VOTING,
            options=counts(),
            total_votes=2,
            unique_voters=1,
            remaining_ms=5000,
        )
    accepted = VoteAckPayload(
        cast_id="550e8400-e29b-41d4-a716-446655440000",
        round_id=1,
        choice=2,
        status=VoteAckStatus.ACCEPTED,
        reason=VoteAckReason.ACCEPTED,
        counted=True,
    )
    assert accepted.counted


def test_closed_tie_and_no_votes_rules():
    tie = VoteClosedPayload(
        round_id=1,
        reason=VoteCloseReason.TIE_ALL,
        winner_event_ids=[11, 12, 13],
        winner_choices=[1, 2, 3],
        winning_votes=2,
        total_votes=6,
        final_options=counts((2, 2, 2)),
    )
    assert len(tie.winner_event_ids) == 3
    empty = VoteClosedPayload(
        round_id=2,
        reason=VoteCloseReason.NO_VOTES_RANDOM,
        winner_event_ids=[11],
        winner_choices=[1],
        winning_votes=0,
        total_votes=0,
        final_options=counts((0, 0, 0)),
    )
    assert empty.reason == VoteCloseReason.NO_VOTES_RANDOM


def test_effect_resolved_requires_command_id_and_status():
    effect = EffectResolvedPayload(
        round_id=1,
        command_id="6217451873653358592",
        event_id=11,
        event_key="chaos.event_1",
        status=EffectStatus.APPLIED,
        result_code="applied",
        applied_game_time_ms=123,
    )
    assert effect.status == EffectStatus.APPLIED


@pytest.mark.parametrize('mutation,code', [
    ({'version': 2}, ErrorCode.PROTOCOL_VERSION_UNSUPPORTED),
    ({'type': 'unknown.kind'}, ErrorCode.PROTOCOL_UNKNOWN_MESSAGE_TYPE),
    ({'unexpected': True}, ErrorCode.PROTOCOL_UNKNOWN_FIELD),
    ({'seq': '1'}, ErrorCode.PROTOCOL_MALFORMED_MESSAGE),
])
def test_parser_returns_precise_error_codes(mutation, code):
    from thchaos_backend.protocol import HeartbeatPayload
    raw = make_envelope(MessageType.HEARTBEAT_PING, room_id='main', seq=1,
                        payload=HeartbeatPayload(nonce='test-ping')).model_dump(mode='json')
    raw.update(mutation)
    with pytest.raises(ProtocolError) as result:
        parse_message(json.dumps(raw))
    assert result.value.code == code


def test_parser_honors_frame_limit_and_rejects_client_error():
    raw = make_envelope(MessageType.ERROR, room_id='main', seq=1,
                        payload=ErrorPayload(code=ErrorCode.SERVER_INTERNAL_ERROR, message='test')).model_dump_json()
    for role in (ClientRole.GAME, ClientRole.BOT):
        with pytest.raises(ProtocolError) as result:
            parse_message(raw, role=role)
        assert result.value.code == ErrorCode.PROTOCOL_DIRECTION_NOT_ALLOWED
    for frame in (raw, raw.encode()):
        with pytest.raises(ProtocolError) as result:
            parse_message(frame, max_frame_bytes=10)
        assert result.value.code == ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE


def test_unknown_payload_fields_do_not_echo_secrets():
    from thchaos_backend.protocol import HelloPayload
    raw = make_envelope(MessageType.HELLO, room_id='main', seq=1,
        payload=HelloPayload(role=ClientRole.BOT, client_id='test', client_version='test-1',
                             token='secret-value', room_id='main')).model_dump(mode='json')
    raw['payload']['unexpected'] = 'secret-value'
    with pytest.raises(ProtocolError) as result:
        parse_message(json.dumps(raw))
    assert result.value.code == ErrorCode.PROTOCOL_UNKNOWN_FIELD
    assert 'secret-value' not in result.value.to_payload().model_dump_json()
