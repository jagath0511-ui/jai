"""Data Access Repository layer for SQLite tables."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional

from .db import DatabaseManager, get_db
from .models import (
    CalendarEventRecord,
    DriveFileRecord,
    GmailMessageRecord,
    GoogleTaskRecord,
    OAuthTokenRecord,
    SyncStateRecord,
    now_utc_iso,
)

logger = logging.getLogger("jai.repository")


class BaseRepository:
    """Base class for database repositories."""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db or get_db()


class OAuthRepository(BaseRepository):
    """Repository for OAuth tokens."""

    def save_token(
        self,
        service: str,
        token_json: str,
        account_email: Optional[str] = None,
        scopes: Optional[str] = None,
    ) -> OAuthTokenRecord:
        now = now_utc_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO oauth_tokens (service, account_email, token_json, scopes, created_at, updated_at, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(service) DO UPDATE SET
                    account_email = COALESCE(excluded.account_email, oauth_tokens.account_email),
                    token_json = excluded.token_json,
                    scopes = COALESCE(excluded.scopes, oauth_tokens.scopes),
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (service, account_email, token_json, scopes, now, now),
            )
        return OAuthTokenRecord(
            service=service,
            token_json=token_json,
            account_email=account_email,
            scopes=scopes,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )

    def get_token(self, service: str = "google") -> Optional[OAuthTokenRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE service = ? AND deleted_at IS NULL", (service,)
            ).fetchone()
            if row:
                return OAuthTokenRecord.from_row(dict(row))
        return None

    def delete_token(self, service: str = "google") -> bool:
        now = now_utc_iso()
        with self.db.get_connection() as conn:
            res = conn.execute(
                "UPDATE oauth_tokens SET deleted_at = ? WHERE service = ? AND deleted_at IS NULL",
                (now, service),
            )
            return res.rowcount > 0


class SyncRepository(BaseRepository):
    """Repository for tracking service sync state."""

    def update_sync_state(
        self,
        service: str,
        status: str,
        items_synced: int = 0,
        sync_cursor: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> SyncStateRecord:
        now = now_utc_iso()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (service, last_sync_at, sync_cursor, items_synced, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    last_sync_at = excluded.last_sync_at,
                    sync_cursor = COALESCE(excluded.sync_cursor, sync_state.sync_cursor),
                    items_synced = excluded.items_synced,
                    status = excluded.status,
                    error_message = excluded.error_message
                """,
                (service, now, sync_cursor, items_synced, status, error_message),
            )
        return SyncStateRecord(
            service=service,
            last_sync_at=now,
            sync_cursor=sync_cursor,
            items_synced=items_synced,
            status=status,
            error_message=error_message,
        )

    def get_sync_state(self, service: str) -> Optional[SyncStateRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE service = ?", (service,)
            ).fetchone()
            if row:
                return SyncStateRecord.from_row(dict(row))
        return None

    def get_all_states(self) -> Dict[str, SyncStateRecord]:
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM sync_state").fetchall()
            return {r["service"]: SyncStateRecord.from_row(dict(r)) for r in rows}


class GmailRepository(BaseRepository):
    """Repository for Gmail messages with FTS5 search."""

    def upsert_message(self, record: GmailMessageRecord) -> None:
        data = record.to_dict()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO gmail_messages (
                    id, thread_id, sender, recipient, subject, snippet, body_text,
                    date, internal_date, labels, is_unread, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    sender = excluded.sender,
                    recipient = excluded.recipient,
                    subject = excluded.subject,
                    snippet = excluded.snippet,
                    body_text = excluded.body_text,
                    date = excluded.date,
                    internal_date = excluded.internal_date,
                    labels = excluded.labels,
                    is_unread = excluded.is_unread,
                    synced_at = excluded.synced_at
                """,
                (
                    data["id"],
                    data["thread_id"],
                    data["sender"],
                    data["recipient"],
                    data["subject"],
                    data["snippet"],
                    data["body_text"],
                    data["date"],
                    data["internal_date"],
                    data["labels"],
                    data["is_unread"],
                    data["synced_at"],
                ),
            )

    def upsert_messages(self, records: List[GmailMessageRecord]) -> int:
        if not records:
            return 0
        with self.db.get_connection() as conn:
            params = []
            for r in records:
                d = r.to_dict()
                params.append((
                    d["id"], d["thread_id"], d["sender"], d["recipient"], d["subject"],
                    d["snippet"], d["body_text"], d["date"], d["internal_date"],
                    d["labels"], d["is_unread"], d["synced_at"]
                ))
            conn.executemany(
                """
                INSERT INTO gmail_messages (
                    id, thread_id, sender, recipient, subject, snippet, body_text,
                    date, internal_date, labels, is_unread, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    sender = excluded.sender,
                    recipient = excluded.recipient,
                    subject = excluded.subject,
                    snippet = excluded.snippet,
                    body_text = excluded.body_text,
                    date = excluded.date,
                    internal_date = excluded.internal_date,
                    labels = excluded.labels,
                    is_unread = excluded.is_unread,
                    synced_at = excluded.synced_at
                """,
                params,
            )
        return len(records)

    def get_message(self, message_id: str) -> Optional[GmailMessageRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gmail_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row:
                return GmailMessageRecord.from_row(dict(row))
        return None

    def list_unread(self, limit: int = 20) -> List[GmailMessageRecord]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gmail_messages WHERE is_unread = 1 ORDER BY internal_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [GmailMessageRecord.from_row(dict(r)) for r in rows]

    def list_recent(self, limit: int = 20, offset: int = 0) -> List[GmailMessageRecord]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM gmail_messages ORDER BY internal_date DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [GmailMessageRecord.from_row(dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> List[GmailMessageRecord]:
        """Search emails using FTS5 full text index."""
        # Sanitize simple query by escaping double quotes
        safe_q = query.replace('"', '""')
        sanitized = f'"{safe_q}"'
        with self.db.get_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT m.* FROM gmail_messages m
                    JOIN gmail_fts f ON m.id = f.id
                    WHERE gmail_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (sanitized, limit),
                ).fetchall()
                return [GmailMessageRecord.from_row(dict(r)) for r in rows]
            except Exception as e:
                logger.warning(f"FTS search failed, falling back to LIKE: {e}")
                like_param = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM gmail_messages
                    WHERE subject LIKE ? OR snippet LIKE ? OR sender LIKE ?
                    ORDER BY internal_date DESC
                    LIMIT ?
                    """,
                    (like_param, like_param, like_param, limit),
                ).fetchall()
                return [GmailMessageRecord.from_row(dict(r)) for r in rows]


class CalendarRepository(BaseRepository):
    """Repository for Google Calendar events with FTS5 search."""

    def upsert_event(self, record: CalendarEventRecord) -> None:
        data = record.to_dict()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO calendar_events (
                    id, summary, description, start_time, end_time, all_day,
                    location, attendees, html_link, status, updated_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    description = excluded.description,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    all_day = excluded.all_day,
                    location = excluded.location,
                    attendees = excluded.attendees,
                    html_link = excluded.html_link,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    synced_at = excluded.synced_at
                """,
                (
                    data["id"], data["summary"], data["description"], data["start_time"],
                    data["end_time"], data["all_day"], data["location"], data["attendees"],
                    data["html_link"], data["status"], data["updated_at"], data["synced_at"],
                ),
            )

    def upsert_events(self, records: List[CalendarEventRecord]) -> int:
        if not records:
            return 0
        with self.db.get_connection() as conn:
            params = []
            for r in records:
                d = r.to_dict()
                params.append((
                    d["id"], d["summary"], d["description"], d["start_time"],
                    d["end_time"], d["all_day"], d["location"], d["attendees"],
                    d["html_link"], d["status"], d["updated_at"], d["synced_at"]
                ))
            conn.executemany(
                """
                INSERT INTO calendar_events (
                    id, summary, description, start_time, end_time, all_day,
                    location, attendees, html_link, status, updated_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    description = excluded.description,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    all_day = excluded.all_day,
                    location = excluded.location,
                    attendees = excluded.attendees,
                    html_link = excluded.html_link,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    synced_at = excluded.synced_at
                """,
                params,
            )
        return len(records)

    def get_event(self, event_id: str) -> Optional[CalendarEventRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM calendar_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row:
                return CalendarEventRecord.from_row(dict(row))
        return None

    def list_upcoming(
        self, start_after: Optional[str] = None, limit: int = 20
    ) -> List[CalendarEventRecord]:
        if start_after is None:
            start_after = now_utc_iso()
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM calendar_events
                WHERE end_time >= ? AND status != 'cancelled'
                ORDER BY start_time ASC
                LIMIT ?
                """,
                (start_after, limit),
            ).fetchall()
            return [CalendarEventRecord.from_row(dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> List[CalendarEventRecord]:
        """Search calendar events using FTS5."""
        safe_q = query.replace('"', '""')
        sanitized = f'"{safe_q}"'
        with self.db.get_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT e.* FROM calendar_events e
                    JOIN calendar_fts f ON e.id = f.id
                    WHERE calendar_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (sanitized, limit),
                ).fetchall()
                return [CalendarEventRecord.from_row(dict(r)) for r in rows]
            except Exception as e:
                logger.warning(f"FTS search failed on calendar, using LIKE: {e}")
                like_param = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM calendar_events
                    WHERE summary LIKE ? OR description LIKE ? OR location LIKE ?
                    ORDER BY start_time ASC
                    LIMIT ?
                    """,
                    (like_param, like_param, like_param, limit),
                ).fetchall()
                return [CalendarEventRecord.from_row(dict(r)) for r in rows]


class DriveRepository(BaseRepository):
    """Repository for Google Drive files with FTS5 search."""

    def upsert_file(self, record: DriveFileRecord) -> None:
        data = record.to_dict()
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO drive_files (
                    id, name, mime_type, size_bytes, web_view_link, modified_time,
                    created_time, parents, trashed, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    web_view_link = excluded.web_view_link,
                    modified_time = excluded.modified_time,
                    created_time = excluded.created_time,
                    parents = excluded.parents,
                    trashed = excluded.trashed,
                    synced_at = excluded.synced_at
                """,
                (
                    data["id"], data["name"], data["mime_type"], data["size_bytes"],
                    data["web_view_link"], data["modified_time"], data["created_time"],
                    data["parents"], data["trashed"], data["synced_at"],
                ),
            )

    def upsert_files(self, records: List[DriveFileRecord]) -> int:
        if not records:
            return 0
        with self.db.get_connection() as conn:
            params = []
            for r in records:
                d = r.to_dict()
                params.append((
                    d["id"], d["name"], d["mime_type"], d["size_bytes"],
                    d["web_view_link"], d["modified_time"], d["created_time"],
                    d["parents"], d["trashed"], d["synced_at"]
                ))
            conn.executemany(
                """
                INSERT INTO drive_files (
                    id, name, mime_type, size_bytes, web_view_link, modified_time,
                    created_time, parents, trashed, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    web_view_link = excluded.web_view_link,
                    modified_time = excluded.modified_time,
                    created_time = excluded.created_time,
                    parents = excluded.parents,
                    trashed = excluded.trashed,
                    synced_at = excluded.synced_at
                """,
                params,
            )
        return len(records)

    def get_file(self, file_id: str) -> Optional[DriveFileRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM drive_files WHERE id = ?", (file_id,)
            ).fetchone()
            if row:
                return DriveFileRecord.from_row(dict(row))
        return None

    def list_recent(
        self, limit: int = 30, offset: int = 0, exclude_trashed: bool = True
    ) -> List[DriveFileRecord]:
        clause = "WHERE trashed = 0" if exclude_trashed else ""
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM drive_files {clause} ORDER BY modified_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [DriveFileRecord.from_row(dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> List[DriveFileRecord]:
        """Search drive files using FTS5."""
        safe_q = query.replace('"', '""')
        sanitized = f'"{safe_q}"'
        with self.db.get_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT d.* FROM drive_files d
                    JOIN drive_fts f ON d.id = f.id
                    WHERE drive_fts MATCH ? AND d.trashed = 0
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (sanitized, limit),
                ).fetchall()
                return [DriveFileRecord.from_row(dict(r)) for r in rows]
            except Exception as e:
                logger.warning(f"FTS search failed on drive, using LIKE: {e}")
                like_param = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM drive_files
                    WHERE name LIKE ? AND trashed = 0
                    ORDER BY modified_time DESC
                    LIMIT ?
                    """,
                    (like_param, limit),
                ).fetchall()
                return [DriveFileRecord.from_row(dict(r)) for r in rows]


class TasksRepository(BaseRepository):
    """Repository for Google Tasks."""

    def upsert_task(self, task: GoogleTaskRecord) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO google_tasks (
                    id, tasklist_id, title, notes, status, due, completed_at, updated_at, position, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    tasklist_id = excluded.tasklist_id,
                    title = excluded.title,
                    notes = excluded.notes,
                    status = excluded.status,
                    due = excluded.due,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at,
                    position = excluded.position,
                    synced_at = excluded.synced_at
                """,
                (
                    task.id,
                    task.tasklist_id,
                    task.title,
                    task.notes,
                    task.status,
                    task.due,
                    task.completed_at,
                    task.updated_at,
                    task.position,
                    task.synced_at,
                ),
            )

    def upsert_tasks(self, tasks: List[GoogleTaskRecord]) -> int:
        for t in tasks:
            self.upsert_task(t)
        return len(tasks)

    def get_task(self, task_id: str) -> Optional[GoogleTaskRecord]:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM google_tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                return GoogleTaskRecord.from_row(dict(row))
        return None

    def list_tasks(
        self,
        tasklist_id: Optional[str] = None,
        status: Optional[str] = "needsAction",
        limit: int = 50,
    ) -> List[GoogleTaskRecord]:
        query = "SELECT * FROM google_tasks WHERE 1=1"
        params: List[Any] = []
        if tasklist_id:
            query += " AND tasklist_id = ?"
            params.append(tasklist_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self.db.get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [GoogleTaskRecord.from_row(dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> List[GoogleTaskRecord]:
        """Search tasks using FTS5."""
        safe_q = query.replace('"', '""')
        sanitized = f'"{safe_q}"'
        with self.db.get_connection() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT t.* FROM google_tasks t
                    JOIN tasks_fts f ON t.id = f.id
                    WHERE tasks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (sanitized, limit),
                ).fetchall()
                return [GoogleTaskRecord.from_row(dict(r)) for r in rows]
            except Exception as e:
                logger.warning(f"FTS search failed on tasks, using LIKE: {e}")
                like_param = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM google_tasks
                    WHERE title LIKE ? OR notes LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (like_param, like_param, limit),
                ).fetchall()
                return [GoogleTaskRecord.from_row(dict(r)) for r in rows]

