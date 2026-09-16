"""每连接有界发送队列：业务处理只入队，网络写入由单独任务保序执行。"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import WebSocket

from ..protocol.envelope import MessageType, make_envelope
from ..protocol.payloads import ClientRole


@dataclass
class Outbound:
    kind: MessageType
    payload: Any
    instance: str | None
    valid: Callable[[], bool] | None = None
    on_start: Callable[[], None] | None = None
    on_sent: Callable[[], None] | None = None


@dataclass(eq=False)
class ClientConnection:
    websocket: WebSocket
    role: ClientRole
    room_id: str
    client_id: str
    game_instance_id: str | None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    inbound_seq: int = 0
    outbound_seq: int = 0
    cast_timestamps: deque[float] = field(default_factory=deque)
    queue_limit: int = 128
    send_timeout: float = 2.0
    closed: bool = False
    _writer: asyncio.Task | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    _queue: asyncio.Queue = field(init=False)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.queue_limit)

    def start(self, on_disconnect: Callable[[ClientConnection], Awaitable[None]]) -> None:
        self._writer = asyncio.create_task(self._run(on_disconnect), name=f"send-{self.session_id}")

    async def send(self, message_type: MessageType, payload: Any, *,
                   game_instance_id: str | None = None,
                   valid: Callable[[], bool] | None = None,
                   on_start: Callable[[], None] | None = None,
                   on_sent: Callable[[], None] | None = None) -> None:
        # No suspension: checking room state and enqueueing is atomic on this loop.
        if self.closed:
            raise ConnectionError("connection closed")
        item = Outbound(message_type, payload,
                        game_instance_id if game_instance_id is not None else self.game_instance_id,
                        valid, on_start, on_sent)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            self.abort()
            raise ConnectionError("outbound queue full") from exc

    async def _run(self, on_disconnect: Callable[[ClientConnection], Awaitable[None]]) -> None:
        self._started = True
        try:
            while not self.closed:
                item = await self._queue.get()
                try:
                    if item.valid is not None and not item.valid():
                        continue
                    # Allocate seq only for frames that will actually be sent.
                    self.outbound_seq += 1
                    envelope = make_envelope(item.kind, room_id=self.room_id,
                                             game_instance_id=item.instance,
                                             seq=self.outbound_seq, payload=item.payload)
                    if item.on_start is not None:
                        item.on_start()
                    async with asyncio.timeout(self.send_timeout):
                        await self.websocket.send_text(envelope.model_dump_json())
                    if item.on_sent is not None:
                        item.on_sent()
                finally:
                    self._queue.task_done()
        except (Exception, asyncio.CancelledError):
            # A partial send is ambiguous. Disconnect; never retry it here.
            pass
        finally:
            self.closed = True
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            await on_disconnect(self)
            try:
                await asyncio.wait_for(self.websocket.close(code=1008), self.send_timeout)
            except Exception:
                pass

    def abort(self) -> None:
        self.closed = True
        # Cancelling a never-started task skips its finally block. Let it start
        # and observe closed instead, so overflow always closes the socket.
        if self._writer is not None and self._started:
            self._writer.cancel()

    async def flush(self) -> None:
        await asyncio.wait_for(self._queue.join(), self.send_timeout)

    async def close(self) -> None:
        self.abort()
        if self._writer is not None:
            await asyncio.gather(self._writer, return_exceptions=True)
