"""Unified Google Workspace Client for JAI."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.database.db import DatabaseManager, get_db
from .auth import GoogleAuthManager
from .calendar_service import CalendarService
from .drive_service import DriveService
from .gmail_service import GmailService
from .tasks_service import TasksService

logger = logging.getLogger("jai.workspace_client")


class GoogleWorkspaceClient:
    """Unified client orchestrating Google OAuth, Gmail, Calendar, Drive, and Tasks."""

    def __init__(
        self,
        auth_manager: Optional[GoogleAuthManager] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.db = db or get_db()
        self.auth = auth_manager or GoogleAuthManager(db=self.db)
        self.gmail = GmailService(auth_manager=self.auth, db=self.db)
        self.calendar = CalendarService(auth_manager=self.auth, db=self.db)
        self.drive = DriveService(auth_manager=self.auth, db=self.db)
        self.tasks = TasksService(auth_manager=self.auth, db=self.db)

    def is_authenticated(self) -> bool:
        """Check if user has authenticated with Google."""
        return self.auth.is_authenticated()

    def get_status(self) -> Dict[str, Any]:
        """Return comprehensive status of Google services and local SQLite database."""
        authenticated = self.is_authenticated()
        email = self.auth.get_account_email() if authenticated else None
        db_counts = self.db.get_table_counts()
        sync_states = self.gmail.sync_repo.get_all_states()

        return {
            "authenticated": authenticated,
            "account_email": email,
            "db_path": str(self.db.db_path),
            "table_counts": db_counts,
            "sync_states": {k: v.to_dict() for k, v in sync_states.items()},
        }

    def sync_all(
        self,
        gmail_limit: int = 50,
        calendar_days_ahead: int = 30,
        calendar_days_back: int = 7,
        drive_limit: int = 100,
    ) -> Dict[str, Any]:
        """Synchronize all services (Gmail, Calendar, Drive) into SQLite database."""
        results: Dict[str, Any] = {}

        # 1. Sync Gmail
        try:
            gmail_count = self.gmail.sync(max_messages=gmail_limit)
            results["gmail"] = {"status": "success", "synced": gmail_count}
        except Exception as e:
            logger.error(f"Error syncing Gmail: {e}")
            results["gmail"] = {"status": "error", "error": str(e)}

        # 2. Sync Calendar
        try:
            cal_count = self.calendar.sync(
                days_ahead=calendar_days_ahead, days_back=calendar_days_back
            )
            results["calendar"] = {"status": "success", "synced": cal_count}
        except Exception as e:
            logger.error(f"Error syncing Calendar: {e}")
            results["calendar"] = {"status": "error", "error": str(e)}

        # 3. Sync Drive
        try:
            drive_count = self.drive.sync(max_files=drive_limit)
            results["drive"] = {"status": "success", "synced": drive_count}
        except Exception as e:
            logger.error(f"Error syncing Drive: {e}")
            results["drive"] = {"status": "error", "error": str(e)}

        # 4. Sync Google Tasks
        try:
            tasks_count = self.tasks.sync_tasks_to_sqlite()
            results["tasks"] = {"status": "success", "synced": tasks_count}
        except Exception as e:
            logger.error(f"Error syncing Google Tasks: {e}")
            results["tasks"] = {"status": "error", "error": str(e)}

        return results

    def search_all(self, query: str, limit: int = 10) -> Dict[str, List[Any]]:
        """Search across local Gmail messages, Calendar events, Drive files, and Tasks using FTS5."""
        return {
            "gmail": [m.to_dict() for m in self.gmail.search_local(query, limit=limit)],
            "calendar": [e.to_dict() for e in self.calendar.search_local(query, limit=limit)],
            "drive": [f.to_dict() for f in self.drive.search_local(query, limit=limit)],
            "tasks": [t.to_dict() for t in self.tasks.tasks_repo.search(query, limit=limit)],
        }


