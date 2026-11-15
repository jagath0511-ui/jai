"""Google Workspace integration package for JAI.

Provides OAuth2 authentication and connectors for Gmail, Google Calendar, and Google Drive.
"""

from .auth import GoogleAuthManager
from .calendar_service import CalendarService
from .client import GoogleWorkspaceClient
from .drive_service import DriveService
from .gmail_service import GmailService
from .tasks_service import TasksService

__all__ = [
    "GoogleAuthManager",
    "GmailService",
    "CalendarService",
    "DriveService",
    "TasksService",
    "GoogleWorkspaceClient",
]

