from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import StoredEvent


class EventStore:
    """Cursor + processed mails + active calendar events with sink ids."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_cursor (
                folder TEXT PRIMARY KEY,
                last_uid INTEGER NOT NULL,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                action TEXT,
                event_row_id INTEGER,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'other',
                title TEXT,
                start_at TEXT,
                end_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source_message_id TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS calendar_sinks (
                event_row_id INTEGER NOT NULL,
                sink TEXT NOT NULL,
                external_id TEXT NOT NULL,
                PRIMARY KEY (event_row_id, sink)
            );

            -- legacy table may still exist; keep readable for migration
            CREATE TABLE IF NOT EXISTS synced_events (
                message_id TEXT NOT NULL,
                sink TEXT NOT NULL,
                title TEXT,
                start_at TEXT,
                external_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, sink)
            );
            """
        )
        self._conn.commit()
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        rows = self._conn.execute("SELECT message_id FROM synced_events").fetchall()
        now = datetime.utcnow().isoformat(timespec="seconds")
        for row in rows:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages
                (message_id, action, event_row_id, processed_at)
                VALUES (?, 'create', NULL, ?)
                """,
                (row["message_id"], now),
            )
        self._conn.commit()

    def get_last_uid(self, folder: str = "INBOX") -> Optional[int]:
        row = self._conn.execute(
            "SELECT last_uid FROM sync_cursor WHERE folder = ?", (folder,)
        ).fetchone()
        return int(row["last_uid"]) if row else None

    def set_last_uid(self, uid: int, folder: str = "INBOX") -> None:
        self._conn.execute(
            """
            INSERT INTO sync_cursor (folder, last_uid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(folder) DO UPDATE SET
              last_uid = excluded.last_uid,
              updated_at = excluded.updated_at
            """,
            (folder, uid, datetime.utcnow().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def already_processed(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None

    def mark_processed(
        self, message_id: str, action: str, event_row_id: Optional[int] = None
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_messages
            (message_id, action, event_row_id, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                message_id,
                action,
                event_row_id,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def _load_sinks(self, event_row_id: int) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT sink, external_id FROM calendar_sinks WHERE event_row_id = ?",
            (event_row_id,),
        ).fetchall()
        return {r["sink"]: r["external_id"] for r in rows}

    def _row_to_event(self, row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            id=row["id"],
            company=row["company"] or "",
            event_type=row["event_type"] or "other",
            title=row["title"] or "",
            start_at=row["start_at"] or "",
            end_at=row["end_at"] or "",
            status=row["status"],
            source_message_id=row["source_message_id"] or "",
            sinks=self._load_sinks(row["id"]),
        )

    def find_active_event(
        self,
        *,
        company: str = "",
        event_type: str = "",
        references: Optional[list[str]] = None,
    ) -> Optional[StoredEvent]:
        refs = [r for r in (references or []) if r]
        if refs:
            placeholders = ",".join("?" * len(refs))
            row = self._conn.execute(
                f"""
                SELECT * FROM calendar_events
                WHERE status = 'active' AND source_message_id IN ({placeholders})
                ORDER BY id DESC LIMIT 1
                """,
                refs,
            ).fetchone()
            if row:
                return self._row_to_event(row)

        if company:
            row = self._conn.execute(
                """
                SELECT * FROM calendar_events
                WHERE status = 'active' AND company = ?
                  AND (? = '' OR event_type = ?)
                ORDER BY start_at DESC, id DESC
                LIMIT 1
                """,
                (company, event_type, event_type),
            ).fetchone()
            if row:
                return self._row_to_event(row)

        return None

    def create_event(
        self,
        *,
        company: str,
        event_type: str,
        title: str,
        start_at: str,
        end_at: str,
        source_message_id: str,
        sinks: Optional[dict[str, str]] = None,
    ) -> int:
        now = datetime.utcnow().isoformat(timespec="seconds")
        cur = self._conn.execute(
            """
            INSERT INTO calendar_events
            (company, event_type, title, start_at, end_at, status,
             source_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                company,
                event_type,
                title,
                start_at,
                end_at,
                source_message_id,
                now,
                now,
            ),
        )
        event_row_id = int(cur.lastrowid)
        for sink, external_id in (sinks or {}).items():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO calendar_sinks (event_row_id, sink, external_id)
                VALUES (?, ?, ?)
                """,
                (event_row_id, sink, external_id),
            )
        self._conn.commit()
        return event_row_id

    def update_event(
        self,
        event_row_id: int,
        *,
        title: str,
        start_at: str,
        end_at: str,
        source_message_id: str,
        sinks: Optional[dict[str, str]] = None,
    ) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        self._conn.execute(
            """
            UPDATE calendar_events
            SET title=?, start_at=?, end_at=?, source_message_id=?, updated_at=?
            WHERE id=?
            """,
            (title, start_at, end_at, source_message_id, now, event_row_id),
        )
        for sink, external_id in (sinks or {}).items():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO calendar_sinks (event_row_id, sink, external_id)
                VALUES (?, ?, ?)
                """,
                (event_row_id, sink, external_id),
            )
        self._conn.commit()

    def cancel_event(self, event_row_id: int, source_message_id: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        self._conn.execute(
            """
            UPDATE calendar_events
            SET status='cancelled', source_message_id=?, updated_at=?
            WHERE id=?
            """,
            (source_message_id, now, event_row_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
