"""Database package for JAI.

Provides SQLite connection management, schema initialization, models, and repositories.
"""

from .db import DatabaseManager, get_db
from .models import (
    CalendarEventRecord,
    DriveFileRecord,
    GmailMessageRecord,
    OAuthTokenRecord,
    SyncStateRecord,
)
from .repository import (
    CalendarRepository,
    DriveRepository,
    GmailRepository,
    OAuthRepository,
    SyncRepository,
)

__all__ = [
    "DatabaseManager",
    "get_db",
    "OAuthTokenRecord",
    "SyncStateRecord",
    "GmailMessageRecord",
    "CalendarEventRecord",
    "DriveFileRecord",
    "OAuthRepository",
    "SyncRepository",
    "GmailRepository",
    "CalendarRepository",
    "DriveRepository",
]

