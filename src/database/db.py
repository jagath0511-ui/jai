"""SQLite Connection & Lifecycle Management for JAI."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
from typing import Dict, Generator, Optional, Union

# Attempt to load path from config, fallback to default relative path
try:
    from config import JAI_MEMORY_DB_PATH
except Exception:
    JAI_MEMORY_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "jai_memory.db"

logger = logging.getLogger("jai.database")
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


class DatabaseManager:
    """Manages SQLite database connections, configuration, and migrations."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            self.db_path = Path(JAI_MEMORY_DB_PATH)
        elif str(db_path) == ":memory:":
            self.db_path = ":memory:"
        else:
            self.db_path = Path(db_path)

        self._is_memory = str(self.db_path) == ":memory:"
        self._shared_memory_conn: Optional[sqlite3.Connection] = None

        if not self._is_memory:
            assert isinstance(self.db_path, Path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Apply performance and integrity PRAGMAs."""
        conn.row_factory = sqlite3.Row
        # foreign_keys must be set per connection
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")

        if not self._is_memory:
            # WAL mode and NORMAL synchronous provide high throughput and concurrent reads
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except sqlite3.Error as e:
                logger.warning(f"Could not set WAL mode on {self.db_path}: {e}")

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding an active SQLite connection with automatic commit/rollback."""
        if self._is_memory:
            if self._shared_memory_conn is None:
                self._shared_memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._configure_connection(self._shared_memory_conn)
            conn = self._shared_memory_conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
            self._configure_connection(conn)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def init_db(self, schema_file: Optional[Path] = None) -> None:
        """Initialize all database tables, indexes, and FTS5 triggers."""
        path = schema_file or SCHEMA_FILE
        if not path.is_file():
            raise FileNotFoundError(f"Schema file not found: {path}")

        schema_sql = path.read_text(encoding="utf-8")
        with self.get_connection() as conn:
            conn.executescript(schema_sql)
            try:
                conn.execute("ALTER TABLE oauth_tokens ADD COLUMN deleted_at TEXT;")
            except sqlite3.OperationalError:
                pass
        logger.info(f"Initialized JAI database schema at: {self.db_path}")

    def get_table_counts(self) -> Dict[str, int]:
        """Return the row counts for main data tables."""
        tables = ["oauth_tokens", "sync_state", "gmail_messages", "calendar_events", "drive_files"]
        counts: Dict[str, int] = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for tbl in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {tbl}")
                    counts[tbl] = cursor.fetchone()[0]
                except sqlite3.OperationalError:
                    counts[tbl] = -1  # table doesn't exist
        return counts

    def close(self) -> None:
        """Close shared in-memory connection if active."""
        if self._shared_memory_conn:
            try:
                self._shared_memory_conn.close()
            except Exception:
                pass
            self._shared_memory_conn = None


# Default singleton manager instance
_default_db_mgr: Optional[DatabaseManager] = None


def get_db(db_path: Optional[Union[str, Path]] = None) -> DatabaseManager:
    """Return default or custom DatabaseManager instance."""
    global _default_db_mgr
    if db_path is not None:
        return DatabaseManager(db_path)
    if _default_db_mgr is None:
        _default_db_mgr = DatabaseManager()
    return _default_db_mgr

