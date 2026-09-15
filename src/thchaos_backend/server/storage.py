"""极小的 SQLite 审计存储。

连接和当前房间状态在内存中维护；SQLite 只记录重启后仍有价值的审计信息，
从而不把数据库延迟引入游戏的实时路径。写入通过 ``asyncio.to_thread`` 完成。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class AuditStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
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

    def _record_event_sync(self, row: tuple[Any, ...]) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT OR IGNORE INTO events
                (room_id, game_instance_id, source_role, message_id, message_type, seq, body)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            self._connection.commit()

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
        await asyncio.to_thread(
            self._record_event_sync,
            (room_id, game_instance_id, source_role, message_id, message_type, seq, json.dumps(body, ensure_ascii=False)),
        )

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
                return False

    async def record_vote(
        self,
        *,
        room_id: str,
        game_instance_id: str,
        round_id: int,
        cast_id: str,
        voter_id: str,
        choice: int,
        status: str = "forwarded",
    ) -> bool:
        return await asyncio.to_thread(
            self._record_vote_sync,
            (room_id, game_instance_id, round_id, cast_id, voter_id, choice, status),
        )

    def _update_vote_status_sync(self, room_id: str, cast_id: str, status: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE vote_attempts SET status = ? WHERE room_id = ? AND cast_id = ?",
                (status, room_id, cast_id),
            )
            self._connection.commit()

    async def update_vote_status(self, *, room_id: str, cast_id: str, status: str) -> None:
        await asyncio.to_thread(self._update_vote_status_sync, room_id, cast_id, status)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
