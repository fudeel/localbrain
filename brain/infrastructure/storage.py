"""SQLite persistence for conversations, connected devices and the mount list."""

lazy import sqlite3

import json
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brain.domain.conversation import UNSET, Conversation, Role, StoredMessage
from brain.domain.devices import Device, Reading

_MIGRATIONS = (
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        agent TEXT NOT NULL,
        system TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        thinking TEXT,
        agent TEXT,
        interrupted INTEGER NOT NULL DEFAULT 0,
        stats TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX messages_by_conversation ON messages(conversation_id, id);
    CREATE TABLE devices (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        meta TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL
    );
    CREATE TABLE readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        metric TEXT NOT NULL,
        value TEXT NOT NULL,
        unit TEXT,
        at REAL NOT NULL
    );
    CREATE INDEX readings_by_device ON readings(device_id, metric, at);
    CREATE TABLE wanted_mounts (name TEXT PRIMARY KEY);
    """,
)

_PRUNE_EVERY_N_INSERTS = 500


def _from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _frozen_json(text: str | None):
    if text is None:
        return None
    return json.loads(text, object_pairs_hook=frozendict, array_hook=tuple)


class Database:
    """One connection, serialized by a lock. Simple, and plenty for a home-scale hub."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700, parent_mode=0o755)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        path.chmod(0o600)  # Conversations are private, whatever folder this was pointed at.
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            self._connection.executescript(script)
            self._connection.execute(f"PRAGMA user_version={number}")

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[Any]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        with self._lock:
            return self._connection.execute(sql, parameters).rowcount

    @contextmanager
    def transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class SqliteConversationRepository:
    _LIST_SQL = """
        SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
        FROM conversations c
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def ensure(self, conversation_id: str, *, agent: str, title: str) -> Conversation:
        now = time.time()
        self._db.execute(
            "INSERT OR IGNORE INTO conversations (id, title, agent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, title, agent, now, now),
        )
        conversation = self.get(conversation_id)
        assert conversation is not None
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        rows = self._db.query(f"{self._LIST_SQL} WHERE c.id = ?", (conversation_id,))
        return self._to_conversation(rows[0]) if rows else None

    def list(self, *, limit: int = 100) -> tuple[Conversation, ...]:
        rows = self._db.query(f"{self._LIST_SQL} ORDER BY c.updated_at DESC LIMIT ?", (limit,))
        return tuple(self._to_conversation(row) for row in rows)

    def messages(self, conversation_id: str) -> tuple[StoredMessage, ...]:
        rows = self._db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        )
        return tuple(
            StoredMessage(
                id=row["id"],
                conversation_id=row["conversation_id"],
                role=Role(row["role"]),
                content=row["content"],
                thinking=row["thinking"],
                agent=row["agent"],
                created_at=_from_timestamp(row["created_at"]),
                interrupted=bool(row["interrupted"]),
                stats=_frozen_json(row["stats"]),
            )
            for row in rows
        )

    def append(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str,
        thinking: str | None = None,
        agent: str | None = None,
        interrupted: bool = False,
        stats: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO messages "
                "(conversation_id, role, content, thinking, agent, interrupted, stats, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    content,
                    thinking,
                    agent,
                    int(interrupted),
                    json.dumps(stats) if stats else None,
                    now,
                ),
            )
            self._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )

    def update(
        self,
        conversation_id: str,
        *,
        system: str | None | UNSET = UNSET,
        title: str | None = None,
        agent: str | None = None,
    ) -> None:
        with self._db.transaction():
            if system is not UNSET:
                self._db.execute(
                    "UPDATE conversations SET system = ? WHERE id = ?", (system, conversation_id)
                )
            if title is not None:
                self._db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
                )
            if agent is not None:
                self._db.execute(
                    "UPDATE conversations SET agent = ? WHERE id = ?", (agent, conversation_id)
                )

    def delete(self, conversation_id: str) -> bool:
        return self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)) > 0

    @staticmethod
    def _to_conversation(row: Any) -> Conversation:
        return Conversation(
            id=row["id"],
            title=row["title"],
            agent=row["agent"],
            system=row["system"],
            created_at=_from_timestamp(row["created_at"]),
            updated_at=_from_timestamp(row["updated_at"]),
            message_count=row["message_count"],
        )


class SqliteDeviceRepository:
    def __init__(self, database: Database, *, retention_days: int = 30) -> None:
        self._db = database
        self._retention_seconds = retention_days * 86_400
        self._lock = threading.Lock()
        self._inserts_until_prune = _PRUNE_EVERY_N_INSERTS

    def record(
        self,
        device_id: str,
        *,
        readings: Sequence[Reading],
        name: str | None = None,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Device:
        now = time.time()
        with self._db.transaction():
            self._db.execute(
                "INSERT INTO devices (id, name, kind, meta, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name = COALESCE(?, name), kind = COALESCE(?, kind), "
                "meta = COALESCE(?, meta), last_seen = excluded.last_seen",
                (
                    device_id,
                    name or device_id,
                    kind or "sensor",
                    json.dumps(meta or {}),
                    now,
                    now,
                    name,
                    kind,
                    json.dumps(meta) if meta is not None else None,
                ),
            )
            for reading in readings:
                self._db.execute(
                    "INSERT INTO readings (device_id, metric, value, unit, at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (device_id, reading.metric, json.dumps(reading.value), reading.unit, now),
                )
        self._prune_occasionally(len(readings))
        device = self.get(device_id)
        assert device is not None
        return device

    def list(self) -> tuple[Device, ...]:
        rows = self._db.query("SELECT * FROM devices ORDER BY last_seen DESC")
        return tuple(self._to_device(row) for row in rows)

    def get(self, device_id: str) -> Device | None:
        rows = self._db.query("SELECT * FROM devices WHERE id = ?", (device_id,))
        return self._to_device(rows[0]) if rows else None

    def history(
        self, device_id: str, *, metric: str | None = None, limit: int = 200
    ) -> tuple[Reading, ...]:
        sql = "SELECT * FROM readings WHERE device_id = ?"
        parameters: list[Any] = [device_id]
        if metric is not None:
            sql += " AND metric = ?"
            parameters.append(metric)
        rows = self._db.query(f"{sql} ORDER BY at DESC, id DESC LIMIT ?", (*parameters, limit))
        return tuple(self._to_reading(row) for row in rows)

    def delete(self, device_id: str) -> bool:
        return self._db.execute("DELETE FROM devices WHERE id = ?", (device_id,)) > 0

    def _to_device(self, row: Any) -> Device:
        latest = self._db.query(
            "SELECT r.* FROM readings r JOIN ("
            "  SELECT metric, MAX(id) AS id FROM readings WHERE device_id = ? GROUP BY metric"
            ") newest ON r.id = newest.id ORDER BY r.metric",
            (row["id"],),
        )
        return Device(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            meta=_frozen_json(row["meta"]) or frozendict(),
            first_seen=_from_timestamp(row["first_seen"]),
            last_seen=_from_timestamp(row["last_seen"]),
            latest=tuple(self._to_reading(reading) for reading in latest),
        )

    @staticmethod
    def _to_reading(row: Any) -> Reading:
        return Reading(
            metric=row["metric"],
            value=json.loads(row["value"]),
            unit=row["unit"],
            at=_from_timestamp(row["at"]),
        )

    def _prune_occasionally(self, inserted: int) -> None:
        with self._lock:
            self._inserts_until_prune -= inserted
            due = self._inserts_until_prune <= 0
            if due:
                self._inserts_until_prune = _PRUNE_EVERY_N_INSERTS
        if due:
            self._db.execute(
                "DELETE FROM readings WHERE at < ?", (time.time() - self._retention_seconds,)
            )


class SqliteMountRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def remember(self, name: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO wanted_mounts (name) VALUES (?)", (name,))

    def forget(self, name: str) -> None:
        self._db.execute("DELETE FROM wanted_mounts WHERE name = ?", (name,))

    def wanted(self) -> tuple[str, ...]:
        return tuple(row["name"] for row in self._db.query("SELECT name FROM wanted_mounts"))
