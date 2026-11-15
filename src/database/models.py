"""Data models for SQLite entities in JAI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional


def now_utc_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OAuthTokenRecord:
    """Represents OAuth credentials stored in SQLite."""
    service: str  # e.g., 'google'
    token_json: str
    account_email: Optional[str] = None
    scopes: Optional[str] = None
    created_at: str = field(default_factory=now_utc_iso)
    updated_at: str = field(default_factory=now_utc_iso)
    deleted_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> OAuthTokenRecord:
        return cls(
            service=row["service"],
            token_json=row["token_json"],
            account_email=row.get("account_email"),
            scopes=row.get("scopes"),
            created_at=row.get("created_at") or now_utc_iso(),
            updated_at=row.get("updated_at") or now_utc_iso(),
            deleted_at=row.get("deleted_at"),
        )


@dataclass
class SyncStateRecord:
    """Represents sync state and cursor for an external service."""
    service: str  # 'gmail', 'calendar', 'drive'
    last_sync_at: Optional[str] = None
    sync_cursor: Optional[str] = None
    items_synced: int = 0
    status: str = "idle"
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> SyncStateRecord:
        return cls(
            service=row["service"],
            last_sync_at=row.get("last_sync_at"),
            sync_cursor=row.get("sync_cursor"),
            items_synced=row.get("items_synced", 0) or 0,
            status=row.get("status", "idle") or "idle",
            error_message=row.get("error_message"),
        )


@dataclass
class GmailMessageRecord:
    """Represents an email message stored in SQLite."""
    id: str
    thread_id: Optional[str] = None
    sender: Optional[str] = None
    recipient: Optional[str] = None
    subject: Optional[str] = None
    snippet: Optional[str] = None
    body_text: Optional[str] = None
    date: Optional[str] = None
    internal_date: Optional[int] = None
    labels: List[str] = field(default_factory=list)
    is_unread: bool = False
    synced_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["labels"] = json.dumps(self.labels)
        data["is_unread"] = 1 if self.is_unread else 0
        return data

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> GmailMessageRecord:
        labels_raw = row.get("labels")
        if isinstance(labels_raw, str):
            try:
                labels = json.loads(labels_raw)
            except Exception:
                labels = [labels_raw] if labels_raw else []
        elif isinstance(labels_raw, list):
            labels = labels_raw
        else:
            labels = []

        return cls(
            id=row["id"],
            thread_id=row.get("thread_id"),
            sender=row.get("sender"),
            recipient=row.get("recipient"),
            subject=row.get("subject"),
            snippet=row.get("snippet"),
            body_text=row.get("body_text"),
            date=row.get("date"),
            internal_date=row.get("internal_date"),
            labels=labels,
            is_unread=bool(row.get("is_unread", 0)),
            synced_at=row.get("synced_at") or now_utc_iso(),
        )


@dataclass
class CalendarEventRecord:
    """Represents a calendar event stored in SQLite."""
    id: str
    summary: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    all_day: bool = False
    location: Optional[str] = None
    attendees: List[str] = field(default_factory=list)
    html_link: Optional[str] = None
    status: str = "confirmed"
    updated_at: Optional[str] = None
    synced_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attendees"] = json.dumps(self.attendees)
        data["all_day"] = 1 if self.all_day else 0
        return data

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> CalendarEventRecord:
        att_raw = row.get("attendees")
        if isinstance(att_raw, str):
            try:
                attendees = json.loads(att_raw)
            except Exception:
                attendees = [att_raw] if att_raw else []
        elif isinstance(att_raw, list):
            attendees = att_raw
        else:
            attendees = []

        return cls(
            id=row["id"],
            summary=row.get("summary"),
            description=row.get("description"),
            start_time=row.get("start_time"),
            end_time=row.get("end_time"),
            all_day=bool(row.get("all_day", 0)),
            location=row.get("location"),
            attendees=attendees,
            html_link=row.get("html_link"),
            status=row.get("status", "confirmed") or "confirmed",
            updated_at=row.get("updated_at"),
            synced_at=row.get("synced_at") or now_utc_iso(),
        )


@dataclass
class DriveFileRecord:
    """Represents Google Drive file metadata stored in SQLite."""
    id: str
    name: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    web_view_link: Optional[str] = None
    modified_time: Optional[str] = None
    created_time: Optional[str] = None
    parents: List[str] = field(default_factory=list)
    trashed: bool = False
    synced_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["parents"] = json.dumps(self.parents)
        data["trashed"] = 1 if self.trashed else 0
        return data

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> DriveFileRecord:
        parents_raw = row.get("parents")
        if isinstance(parents_raw, str):
            try:
                parents = json.loads(parents_raw)
            except Exception:
                parents = [parents_raw] if parents_raw else []
        elif isinstance(parents_raw, list):
            parents = parents_raw
        else:
            parents = []

        return cls(
            id=row["id"],
            name=row.get("name", "Untitled"),
            mime_type=row.get("mime_type"),
            size_bytes=int(row.get("size_bytes", 0) or 0),
            web_view_link=row.get("web_view_link"),
            modified_time=row.get("modified_time"),
            created_time=row.get("created_time"),
            parents=parents,
            trashed=bool(row.get("trashed", 0)),
            synced_at=row.get("synced_at") or now_utc_iso(),
        )


@dataclass
class GoogleTaskRecord:
    """Represents a Google Task stored in SQLite."""
    id: str
    tasklist_id: str
    title: str
    notes: Optional[str] = None
    status: str = "needsAction"
    due: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None
    position: Optional[str] = None
    synced_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> GoogleTaskRecord:
        return cls(
            id=row["id"],
            tasklist_id=row.get("tasklist_id", "@default"),
            title=row.get("title", "Untitled Task"),
            notes=row.get("notes"),
            status=row.get("status", "needsAction"),
            due=row.get("due"),
            completed_at=row.get("completed_at"),
            updated_at=row.get("updated_at"),
            position=row.get("position"),
            synced_at=row.get("synced_at") or now_utc_iso(),
        )


