"""极小的 SQLite 审计存储。

普通审计和状态更新通过有界队列批量提交；投票去重单独使用同步事务，
调用方必须等待该事务成功后才能转发。所有磁盘操作在线程中运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class AuditStore:
    def __init__(self, path: Path, queue_size: int = 4096) -> None:
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task | None = None
        self.dropped = 0
        self.failures = 0
        self._closing = False
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    room_id TEXT NOT NULL,
                    game_instance_id TEXT,
                    source_role TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    UNIQUE(room_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS vote_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    room_id TEXT NOT NULL,
                    game_instance_id TEXT NOT NULL,
                    round_id INTEGER NOT NULL,
                    cast_id TEXT NOT NULL,
                    voter_id TEXT NOT NULL,
                    choice INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(room_id, cast_id),
                    UNIQUE(room_id, game_instance_id, round_id, voter_id)
                );
                """
            )
            self._connection.commit()

            # Logical redaction of legacy hello rows. Old backups/WAL copies
            # still require token rotation; never claim physical secure erasure.
            self._connection.execute("""UPDATE events
                SET body = json_remove(body, '$.payload.token')
                WHERE message_type = 'hello' AND json_valid(body)
                AND json_type(body, '$.payload.token') IS NOT NULL""")
            self._connection.execute("""UPDATE vote_attempts SET status = 'unknown:restart'
                WHERE status IN ('reserved', 'forwarded')""")
            self._connection.commit()

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="audit-writer")

    def _enqueue(self, operation: str, row: tuple[Any, ...]) -> None:
        if self._closing:
            raise RuntimeError("audit store is closing")
        self.start()
        try:
            self._queue.put_nowait((operation, row))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("审计队列已满，丢弃记录累计 %d", self.dropped)

    def _batch_sync(self, jobs: list[tuple[str, tuple[Any, ...]]]) -> None:
        with self._lock, self._connection:
            for operation, row in jobs:
                if operation == "event":
                    self._connection.execute("""INSERT OR IGNORE INTO events
                        (room_id, game_instance_id, source_role, message_id, message_type, seq, body)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""", row)
                else:
                    # Never overwrite a game verdict with a delayed send/timeout status.
                    status = row[0]
                    condition = ""
                    if status == "forwarded":
                        condition = " AND status = 'reserved'"
                    elif status.startswith(("unknown:", "not_sent:")):
                        condition = " AND status NOT LIKE 'accepted%' AND status NOT LIKE 'rejected:%'"
                    self._connection.execute(
                        "UPDATE vote_attempts SET status = ? WHERE room_id = ? "
                        "AND game_instance_id = ? AND round_id = ? AND cast_id = ?" + condition, row)

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            if first is None:
                self._queue.task_done()
                return
            jobs = [first]
            # Bounded coalescing window also batches traffic from a single producer.
            await asyncio.sleep(0.01)
            while len(jobs) < 100 and not self._queue.empty():
                jobs.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self._batch_sync, jobs)
            except Exception:
                self.failures += len(jobs)
                logger.warning("审计批量写入失败", exc_info=True)
            finally:
                for _ in jobs:
                    self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()

    async def aclose(self) -> None:
        self._closing = True
        if self._worker is not None:
            await self.flush()
            await self._queue.put(None)
            await self._worker
        await asyncio.to_thread(self.close)

    async def record_event(
        self,
        *,
        room_id: str,
        game_instance_id: str | None,
        source_role: str,
        message_id: str,
        message_type: str,
        seq: int,
        body: dict[str, Any],
    ) -> None:
        # Redact at the storage boundary too, so future callers cannot persist tokens.
        body = dict(body)
        if message_type == "hello":
            body["payload"] = {key: value for key, value in body.get("payload", {}).items() if key != "token"}
        self._enqueue("event", (room_id, game_instance_id, source_role, message_id,
                               message_type, seq, json.dumps(body, ensure_ascii=False)))

    def _record_vote_sync(self, row: tuple[Any, ...]) -> bool:
        with self._lock:
            try:
                self._connection.execute(
                    """INSERT INTO vote_attempts
                    (room_id, game_instance_id, round_id, cast_id, voter_id, choice, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                self._connection.commit()
                return True
            except sqlite3.IntegrityError:
                self._connection.rollback()
                return False
            except Exception:
                self._connection.rollback()
                raise

    async def record_vote(
        self,
        *,
        room_id: str,
        game_instance_id: str,
        round_id: int,
        cast_id: str,
        voter_id: str,
        choice: int,
        status: str = "reserved",
    ) -> bool:
        return await asyncio.to_thread(
            self._record_vote_sync,
            (room_id, game_instance_id, round_id, cast_id, voter_id, choice, status),
        )

    def queue_vote_status(self, *, room_id: str, game_instance_id: str,
                          round_id: int, cast_id: str, status: str) -> None:
        self._enqueue("status", (status, room_id, game_instance_id, round_id, cast_id))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
