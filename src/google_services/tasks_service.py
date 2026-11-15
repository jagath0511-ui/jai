"""Google Tasks API connector and synchronization service for JAI."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import Resource, build

from src.database.db import DatabaseManager, get_db
from src.database.models import GoogleTaskRecord, now_utc_iso
from src.database.repository import SyncRepository, TasksRepository
from .auth import GoogleAuthManager

logger = logging.getLogger("jai.tasks")


class TasksService:
    """Connects to Google Tasks API, retrieves tasks, creates items, and syncs with SQLite."""

    SERVICE_NAME = "tasks"

    def __init__(
        self,
        auth_manager: Optional[GoogleAuthManager] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.auth = auth_manager or GoogleAuthManager(db=db)
        self.db = db or get_db()
        self.tasks_repo = TasksRepository(self.db)
        self.sync_repo = SyncRepository(self.db)
        self._client: Optional[Resource] = None

    def get_client(self) -> Resource:
        """Return authenticated Google Tasks API service resource."""
        if self._client is None:
            creds = self.auth.get_credentials()
            self._client = build("tasks", "v1", credentials=creds, cache_discovery=False)
        return self._client

    def list_tasklists(self, max_results: int = 20) -> List[Dict[str, Any]]:
        """List user's task lists."""
        client = self.get_client()
        res = client.tasklists().list(maxResults=max_results).execute()
        return res.get("items", [])

    def list_tasks(
        self,
        tasklist_id: str = "@default",
        show_completed: bool = False,
        show_hidden: bool = False,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch tasks from specified tasklist via Google Tasks API."""
        client = self.get_client()
        res = client.tasks().list(
            tasklist=tasklist_id,
            showCompleted=show_completed,
            showHidden=show_hidden,
            maxResults=max_results,
        ).execute()
        return res.get("items", [])

    def create_task(
        self,
        title: str,
        notes: str = "",
        due: Optional[str] = None,
        tasklist_id: str = "@default",
    ) -> Dict[str, Any]:
        """Create a new task in Google Tasks."""
        client = self.get_client()
        body: Dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            # due must be RFC 3339 timestamp (e.g. 2026-10-04T00:00:00.000Z)
            if not due.endswith("Z") and not "+" in due:
                due = f"{due}T00:00:00.000Z"
            body["due"] = due

        task = client.tasks().insert(tasklist=tasklist_id, body=body).execute()
        # Also cache locally in SQLite
        rec = self.parse_task(task, tasklist_id=tasklist_id)
        self.tasks_repo.upsert_task(rec)
        logger.info(f"Created task '{title}' (ID: {task.get('id')})")
        return task

    def complete_task(
        self,
        task_id: str,
        tasklist_id: str = "@default",
    ) -> bool:
        """Mark a task as completed in Google Tasks and SQLite."""
        client = self.get_client()
        task = client.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        task["status"] = "completed"
        task["completed"] = now_utc_iso()
        updated = client.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()

        rec = self.parse_task(updated, tasklist_id=tasklist_id)
        self.tasks_repo.upsert_task(rec)
        logger.info(f"Marked task {task_id} as completed.")
        return True

    def parse_task(self, raw_task: Dict[str, Any], tasklist_id: str = "@default") -> GoogleTaskRecord:
        """Parse raw Google API task dict into GoogleTaskRecord."""
        return GoogleTaskRecord(
            id=raw_task["id"],
            tasklist_id=tasklist_id,
            title=raw_task.get("title", "Untitled Task"),
            notes=raw_task.get("notes"),
            status=raw_task.get("status", "needsAction"),
            due=raw_task.get("due"),
            completed_at=raw_task.get("completed"),
            updated_at=raw_task.get("updated"),
            position=raw_task.get("position"),
            synced_at=now_utc_iso(),
        )

    def sync_tasks_to_sqlite(self) -> int:
        """Sync active tasks across all tasklists into SQLite."""
        self.sync_repo.update_sync_state(self.SERVICE_NAME, status="in_progress")
        try:
            lists = self.list_tasklists()
            total_synced = 0

            for tl in lists:
                tl_id = tl["id"]
                raw_tasks = self.list_tasks(tasklist_id=tl_id, show_completed=True, max_results=100)
                records = [self.parse_task(t, tasklist_id=tl_id) for t in raw_tasks]
                self.tasks_repo.upsert_tasks(records)
                total_synced += len(records)

            self.sync_repo.update_sync_state(self.SERVICE_NAME, status="success", items_synced=total_synced)
            logger.info(f"Synced {total_synced} Google Tasks to SQLite.")
            return total_synced
        except Exception as exc:
            self.sync_repo.update_sync_state(self.SERVICE_NAME, status="error", error_message=str(exc))
            logger.error(f"Google Tasks sync failed: {exc}")
            raise
