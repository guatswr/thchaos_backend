import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from thchaos_backend.protocol import (
    ClientRole, GamePhase, GameStatePayload, GameSyncPayload, HeartbeatPayload,
    HelloPayload, MessageType, VoteAckPayload, VoteAckReason, VoteAckStatus,
    VoteCastPayload, make_envelope,
)
from thchaos_backend.protocol.errors import ErrorCode, ProtocolError
from thchaos_backend.protocol.types import new_message_id
from thchaos_backend.server.app import create_app
from thchaos_backend.server.config import Settings
from thchaos_backend.server.connection import ClientConnection
from thchaos_backend.server.hub import Hub
from thchaos_backend.server.storage import AuditStore
from tests.test_hub import opened


class FakeSocket:
    def __init__(self, role='bot', blocked=False):
        self.url = SimpleNamespace(path=f'/ws/{role}')
        self.incoming = asyncio.Queue()
        self.sent = []
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        if not blocked:
            self.gate.set()

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, raw):
        self.entered.set()
        await self.gate.wait()
        self.sent.append(json.loads(raw))

    async def close(self, code=1000):
        if not self.closed.is_set():
            self.closed.set()
            self.incoming.put_nowait({'type': 'websocket.disconnect', 'code': code})

    def feed(self, envelope):
        self.incoming.put_nowait({'type': 'websocket.receive', 'text': envelope.model_dump_json()})


def message(kind, payload, *, seq=2, instance='game-a', room='main', message_id=None):
    return make_envelope(kind, room_id=room, game_instance_id=instance, seq=seq,
                         payload=payload, message_id=message_id)


def cast(voter='voter-a'):
    return VoteCastPayload(cast_id=new_message_id(), round_id=1, choice=1,
                           voter_id=voter, source_group_id='123', source_message_id='456')


def ack(payload):
    return VoteAckPayload(cast_id=payload.cast_id, round_id=payload.round_id, choice=payload.choice,
                          status=VoteAckStatus.ACCEPTED, reason=VoteAckReason.ACCEPTED, counted=True)


@pytest_asyncio.fixture
async def hub(tmp_path):
    value = Hub(Settings(database_path=tmp_path / 'audit.sqlite3'))
    value.start()
    try:
        yield value
    finally:
        await value.close()


async def connect(hub, role, *, blocked=False, queue_limit=128, timeout=2):
    ws = FakeSocket(role.value, blocked)
    conn = ClientConnection(ws, role, 'main', role.value,
                            'game-a' if role == ClientRole.GAME else None,
                            inbound_seq=1, queue_limit=queue_limit, send_timeout=timeout)
    conn.start(hub._drop)
    hub.connections.add(conn)
    await hub._register(conn)
    return conn


async def voting(hub):
    game = await connect(hub, ClientRole.GAME)
    bot = await connect(hub, ClientRole.BOT)
    payload = opened()
    await hub._dispatch(game, message(MessageType.VOTE_OPENED, payload), payload)
    await bot.flush()
    bot.websocket.sent.clear()
    return game, bot, hub.room('main')


async def status(hub, payload):
    await hub.store.flush()
    return hub.store._connection.execute(
        'SELECT status FROM vote_attempts WHERE cast_id=?', (payload.cast_id,)).fetchone()[0]


@pytest.mark.asyncio
async def test_token_redaction_and_legacy_migration(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    store = AuditStore(path)
    body = {'payload': {'token': 'legacy-secret', 'client_id': 'test'}}
    store._connection.execute('''INSERT INTO events
        (room_id, source_role, message_id, message_type, seq, body) VALUES (?, ?, ?, ?, ?, ?)''',
        ('main', 'bot', new_message_id(), 'hello', 1, json.dumps(body)))
    store._connection.commit()
    store.close()
    store = AuditStore(path)
    await store.record_event(room_id='main', game_instance_id=None, source_role='bot',
        message_id=new_message_id(), message_type='hello', seq=1, body=body)
    await store.aclose()
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT body FROM events').fetchall()
    assert len(rows) == 2
    assert all('token' not in json.loads(row[0])['payload'] for row in rows)
    assert body['payload']['token'] == 'legacy-secret'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['round', 'disconnect', 'sync'])
async def test_state_change_during_vote_reservation_never_forwards(hub, monkeypatch, change):
    game, bot, room = await voting(hub)
    started, resume = asyncio.Event(), asyncio.Event()
    original = hub.store.record_vote
    async def delayed(**kwargs):
        result = await original(**kwargs)
        started.set()
        await resume.wait()
        return result
    monkeypatch.setattr(hub.store, 'record_vote', delayed)
    payload = cast()
    task = asyncio.create_task(hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload))
    await started.wait()
    if change == 'round':
        next_round = opened(2)
        await hub._dispatch(game, message(MessageType.VOTE_OPENED, next_round, seq=3), next_round)
    elif change == 'disconnect':
        await hub._drop(game)
    else:
        sync = GameSyncPayload(state=room.state, active_vote=room.active_vote)
        await hub._dispatch(game, message(MessageType.GAME_SYNC, sync, seq=3), sync)
    resume.set()
    with pytest.raises(ProtocolError, match='round.stale'):
        await task
    assert not room.pending_casts
    assert not game.websocket.sent
    assert await status(hub, payload) == 'not_sent:state_changed'
    # A disconnected game removed from hub.connections still needs its writer closed.
    await game.close()


@pytest.mark.asyncio
async def test_queued_old_round_is_cancelled_without_sequence_gap(hub):
    game, bot, room = await voting(hub)
    game.websocket.gate.clear()
    ping = HeartbeatPayload(nonce='test-ping')
    await game.send(MessageType.HEARTBEAT_PONG, ping)
    await game.websocket.entered.wait()
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    next_round = opened(2)
    await hub._dispatch(game, message(MessageType.VOTE_OPENED, next_round, seq=3), next_round)
    game.websocket.gate.set()
    await game.send(MessageType.HEARTBEAT_PONG, ping)
    await game.flush()
    assert [item['type'] for item in game.websocket.sent] == ['heartbeat.pong', 'heartbeat.pong']
    assert [item['seq'] for item in game.websocket.sent] == [1, 2]
    assert await status(hub, payload) == 'not_sent:round_changed'


@pytest.mark.asyncio
async def test_slow_bot_does_not_delay_other_bot_or_game_ack(hub):
    game, bot, room = await voting(hub)
    slow = await connect(hub, ClientRole.BOT, blocked=True)
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    await game.flush()
    state = GameStatePayload(phase=GamePhase.VOTING)
    await asyncio.wait_for(hub._dispatch(game, message(MessageType.GAME_STATE_CHANGED, state, seq=3), state), .5)
    verdict = ack(payload)
    await asyncio.wait_for(hub._dispatch(game, message(MessageType.VOTE_ACK, verdict, seq=4), verdict), .5)
    await bot.flush()
    assert [item['type'] for item in bot.websocket.sent] == ['game.state_changed', 'vote.ack']
    assert not slow.websocket.sent
    assert await status(hub, payload) == 'accepted'


@pytest.mark.asyncio
async def test_slow_writer_times_out_and_is_removed(hub):
    bot = await connect(hub, ClientRole.BOT, blocked=True, timeout=.02)
    await bot.send(MessageType.HEARTBEAT_PONG, HeartbeatPayload(nonce='test-ping'))
    await asyncio.wait_for(bot.websocket.closed.wait(), 1)
    assert bot not in hub.room('main').bots
    assert bot.closed


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_and_disconnects(hub):
    bot = await connect(hub, ClientRole.BOT, blocked=True, queue_limit=1)
    ping = HeartbeatPayload(nonce='test-ping')
    await bot.send(MessageType.HEARTBEAT_PONG, ping)
    await bot.websocket.entered.wait()
    await bot.send(MessageType.HEARTBEAT_PONG, ping)
    with pytest.raises(ConnectionError):
        await bot.send(MessageType.HEARTBEAT_PONG, ping)
    await asyncio.wait_for(bot.websocket.closed.wait(), 1)
    assert bot not in hub.room('main').bots


@pytest.mark.asyncio
async def test_ack_timeout_is_unknown_and_late_ack_reconciles(hub):
    game, bot, room = await voting(hub)
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    await game.flush()
    room.pending_casts[payload.cast_id].deadline = 0
    await hub._expire_pending()
    await bot.flush()
    error = bot.websocket.sent[-1]['payload']
    assert error['code'] == 'round.result_unknown'
    assert error['retryable'] is False
    assert error['cast_id'] == payload.cast_id
    assert await status(hub, payload) == 'unknown:ack_timeout'
    verdict = ack(payload)
    await hub._dispatch(game, message(MessageType.VOTE_ACK, verdict, seq=3), verdict)
    assert await status(hub, payload) == 'accepted'
    with pytest.raises(ProtocolError, match='round.duplicate_vote'):
        retry = cast()
        await hub._dispatch(bot, message(MessageType.VOTE_CAST, retry, seq=3), retry)


@pytest.mark.asyncio
async def test_ack_mismatch_cannot_resolve_pending(hub):
    game, bot, room = await voting(hub)
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    await game.flush()
    verdict = ack(payload).model_copy(update={'round_id': 2})
    with pytest.raises(ProtocolError, match='origin_mismatch'):
        await hub._dispatch(game, message(MessageType.VOTE_ACK, verdict, seq=3), verdict)
    assert payload.cast_id in room.pending_casts
    assert await status(hub, payload) == 'forwarded'


@pytest.mark.asyncio
async def test_wrong_room_rejected_before_audit(hub):
    bot = await connect(hub, ClientRole.BOT)
    ping = HeartbeatPayload(nonce='test-ping')
    with pytest.raises(ProtocolError, match='origin_mismatch'):
        await hub._dispatch(bot, message(MessageType.HEARTBEAT_PING, ping, room='other'), ping)
    await hub.store.flush()
    assert hub.store._connection.execute('SELECT count(*) FROM events').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_duplicate_game_message_does_not_clear_pending(hub):
    game, bot, room = await voting(hub)
    original = message(MessageType.GAME_STATE_CHANGED, GameStatePayload(phase=GamePhase.VOTING), seq=3)
    await hub._dispatch(game, original, GameStatePayload(phase=GamePhase.VOTING))
    await hub._dispatch(game, original.model_copy(update={'seq': 4}), GameStatePayload(phase=GamePhase.VOTING))
    await bot.flush()
    assert len(bot.websocket.sent) == 1
    with pytest.raises(ProtocolError, match='message_id'):
        changed = original.model_copy(update={'seq': 5, 'payload': GameStatePayload(phase=GamePhase.TITLE).model_dump(mode='json')})
        await hub._dispatch(game, changed, GameStatePayload(phase=GamePhase.TITLE))


@pytest.mark.asyncio
async def test_reconnect_needs_sync_and_retains_last_closed_round(hub):
    game, bot, room = await voting(hub)
    await game.close()
    new_game = await connect(hub, ClientRole.GAME)
    with pytest.raises(ProtocolError, match='game.sync'):
        await hub._dispatch(new_game, message(MessageType.VOTE_OPENED, opened(2)), opened(2))
    sync = GameSyncPayload(state=GameStatePayload(phase=GamePhase.WAITING), last_closed_round=1)
    await hub._dispatch(new_game, message(MessageType.GAME_SYNC, sync, seq=3), sync)
    assert room.ready and room.last_closed_round == 1
    await hub._send_server_state(bot, room)
    await bot.flush()
    assert bot.websocket.sent[-1]['payload']['last_closed_round'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code', [
    ('invalid_token', 'auth.invalid_token'), ('wrong_room', 'protocol.origin_mismatch'),
    ('wrong_path', 'auth.role_mismatch'), ('timeout', 'protocol.not_authenticated'),
    ('binary', 'protocol.malformed_message'), ('malformed', 'protocol.malformed_message'),
])
async def test_handshake_errors_are_sent_before_close(tmp_path, mode, code):
    hub = Hub(Settings(database_path=tmp_path / 'audit.sqlite3', handshake_timeout=.02))
    ws = FakeSocket()
    hello = HelloPayload(role=ClientRole.BOT, client_id='test', client_version='test-1',
                         token='dev-bot-token', room_id='main')
    if mode == 'invalid_token':
        hello = hello.model_copy(update={'token': 'secret-test-token'})
    if mode == 'wrong_path':
        ws.url.path = '/ws/game'
    if mode == 'binary':
        ws.incoming.put_nowait({'type': 'websocket.receive', 'bytes': b'hello'})
    elif mode == 'malformed':
        ws.incoming.put_nowait({'type': 'websocket.receive', 'text': '{secret-test-token'})
    elif mode != 'timeout':
        ws.feed(message(MessageType.HELLO, hello, seq=1, instance=None,
                        room='other' if mode == 'wrong_room' else 'main'))
    try:
        await asyncio.wait_for(hub.handle(ws), 1)
        assert ws.closed.is_set()
        assert ws.sent[0]['payload']['code'] == code
        assert ws.sent[0]['payload']['fatal'] is True
        assert 'secret-test-token' not in json.dumps(ws.sent)
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_audit_queue_overflow_and_failure_do_not_break_votes(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / 'audit.sqlite3', queue_size=1)
    row = dict(room_id='main', game_instance_id=None, source_role='bot',
               message_id=new_message_id(), message_type='hello', seq=1, body={'payload': {'token': 'secret'}})
    await store.record_event(**row)
    await store.record_event(**row)
    assert store.dropped == 1
    def fail(jobs):
        raise sqlite3.OperationalError('injected failure')
    monkeypatch.setattr(store, '_batch_sync', fail)
    await store.flush()
    assert store.failures == 1
    assert await store.record_vote(room_id='main', game_instance_id='game-a', round_id=1,
                                  cast_id=new_message_id(), voter_id='test', choice=1)
    await store.aclose()


@pytest.mark.asyncio
async def test_lifespan_owns_database_and_flushes_on_exit(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    app = create_app(Settings(database_path=path))
    assert not path.exists()
    async with app.router.lifespan_context(app):
        hub = app.state.hub
        await hub.store.record_event(room_id='main', game_instance_id=None, source_role='bot',
            message_id=new_message_id(), message_type='hello', seq=1, body={'payload': {}})
    assert hub.store._worker.done()
    assert hub._sweeper.done()
    with pytest.raises(sqlite3.ProgrammingError):
        hub.store._connection.execute('SELECT 1')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_rate_limit_precedes_audit_and_vote_storage(hub):
    game, bot, room = await voting(hub)
    hub.settings = Settings(database_path=Path(':memory:'), max_casts_per_second=1)
    first, second = cast('a'), cast('b')
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, first), first)
    with pytest.raises(ProtocolError, match='rate_limited'):
        await hub._dispatch(bot, message(MessageType.VOTE_CAST, second, seq=3), second)
    await hub.store.flush()
    assert hub.store._connection.execute("SELECT count(*) FROM events WHERE message_type='vote.cast'").fetchone()[0] == 1
    assert hub.store._connection.execute('SELECT count(*) FROM vote_attempts').fetchone()[0] == 1

@pytest.mark.asyncio
async def test_overflow_before_writer_starts_still_closes_socket(hub):
    bot = await connect(hub, ClientRole.BOT, queue_limit=1)
    ping = HeartbeatPayload(nonce='test-ping')
    await bot.send(MessageType.HEARTBEAT_PONG, ping)
    with pytest.raises(ConnectionError):
        await bot.send(MessageType.HEARTBEAT_PONG, ping)
    await asyncio.wait_for(bot.websocket.closed.wait(), 1)
    assert bot not in hub.room('main').bots


@pytest.mark.asyncio
async def test_concurrent_same_voter_has_one_forward(hub):
    game, bot, room = await voting(hub)
    other = await connect(hub, ClientRole.BOT)
    a, b = cast(), cast()
    results = await asyncio.gather(
        hub._dispatch(bot, message(MessageType.VOTE_CAST, a), a),
        hub._dispatch(other, message(MessageType.VOTE_CAST, b), b), return_exceptions=True)
    await game.flush()
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ProtocolError) and result.code == ErrorCode.ROUND_DUPLICATE_VOTE for result in results) == 1
    assert len(game.websocket.sent) == 1


@pytest.mark.asyncio
async def test_game_disconnect_resolves_attempted_vote(hub):
    game, bot, room = await voting(hub)
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    await game.flush()
    await game.close()
    await bot.flush()
    assert not room.pending_casts
    assert [m['type'] for m in bot.websocket.sent] == ['error', 'game.offline']
    assert bot.websocket.sent[0]['payload']['code'] == 'round.result_unknown'
    assert await status(hub, payload) == 'unknown:game_offline'


@pytest.mark.asyncio
async def test_vote_uniqueness_survives_restart(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    store = AuditStore(path)
    row = dict(room_id='main', game_instance_id='game-a', round_id=1,
               cast_id=new_message_id(), voter_id='a', choice=1)
    assert await store.record_vote(**row)
    await store.aclose()
    store = AuditStore(path)
    try:
        assert not await store.record_vote(**row)
        assert store._connection.execute('SELECT status FROM vote_attempts').fetchone()[0] == 'unknown:restart'
        assert not store._connection.in_transaction
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_management_reads_require_auth_and_do_not_create_rooms(tmp_path):
    from fastapi import HTTPException
    app = create_app(Settings(database_path=tmp_path / 'audit.sqlite3', admin_token='admin'))
    state = next(route.endpoint for route in app.routes if route.path == '/rooms/{room_id}/state')
    metrics = next(route.endpoint for route in app.routes if route.path == '/metrics')
    async with app.router.lifespan_context(app):
        with pytest.raises(HTTPException) as missing:
            await state('unknown', x_admin_token='admin')
        assert missing.value.status_code == 404
        assert not app.state.hub.rooms
        with pytest.raises(HTTPException):
            await metrics(x_admin_token='wrong')
        result = await metrics(x_admin_token='admin')
        assert result['pending_casts'] == 0
        assert result['forward']['samples'] == 0


@pytest.mark.parametrize('kwargs', [
    {'port': 0}, {'send_timeout': float('nan')}, {'ack_timeout': -1},
    {'audit_queue_size': 0}, {'game_tokens': {'same': 'main'}, 'bot_tokens': {'same': 'main'}},
    {'game_tokens': {'token': 'bad room'}},
])
def test_invalid_settings_fail_at_startup(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)

@pytest.mark.asyncio
async def test_duplicate_open_preserves_outstanding_vote(hub):
    game = await connect(hub, ClientRole.GAME)
    bot = await connect(hub, ClientRole.BOT)
    opening = opened()
    original = message(MessageType.VOTE_OPENED, opening)
    await hub._dispatch(game, original, opening)
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    await game.flush()
    await hub._dispatch(game, original.model_copy(update={'seq': 3}), opening)
    assert payload.cast_id in hub.room('main').pending_casts
    await bot.flush()
    assert [frame['type'] for frame in bot.websocket.sent] == ['vote.opened']
    verdict = ack(payload)
    await hub._dispatch(game, message(MessageType.VOTE_ACK, verdict, seq=4), verdict)
    await bot.flush()
    assert bot.websocket.sent[-1]['type'] == 'vote.ack'


@pytest.mark.asyncio
async def test_expired_queued_vote_never_starts_send(hub):
    game, bot, room = await voting(hub)
    game.websocket.gate.clear()
    await game.send(MessageType.HEARTBEAT_PONG, HeartbeatPayload(nonce='test-ping'))
    await game.websocket.entered.wait()
    payload = cast()
    await hub._dispatch(bot, message(MessageType.VOTE_CAST, payload), payload)
    room.pending_casts[payload.cast_id].deadline = 0
    game.websocket.gate.set()
    await game.flush()
    assert [frame['type'] for frame in game.websocket.sent] == ['heartbeat.pong']
    await hub._expire_pending()
    assert await status(hub, payload) == 'not_sent:ack_timeout'
