"""Memory Vault Module for JAI.

Provides persistent memory storage backed by SQLite with FTS5 (full-text search)
and BM25 ranking. Enforces strict vault_scope isolation and soft-delete capabilities.
Hard deletion is strictly prohibited in this phase.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Generator, List, Optional, Set, Union

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
VALID_SOURCES: Set[str] = {
    "voice",
    "telegram",
    "browser_tab",
    "email",
    "manual",
}

BASE_DIR: Path = Path(__file__).resolve().parent
if BASE_DIR.name == "src":
    BASE_DIR = BASE_DIR.parent

# Attempt to load database path from config; fallback safely if config is unavailable
try:
    from config import JAI_MEMORY_DB_PATH
    DEFAULT_DB_PATH: Path = JAI_MEMORY_DB_PATH
except Exception:
    DEFAULT_DB_PATH: Path = BASE_DIR / "data" / "jai_memory.db"


def _resolve_db_path(db_path: Optional[Union[str, Path]]) -> str:
    """Resolve database path and ensure directory exists."""
    if db_path is None:
        target = DEFAULT_DB_PATH
    elif isinstance(db_path, str) and db_path == ":memory:":
        return ":memory:"
    else:
        target = Path(db_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


@contextmanager
def get_db_connection(
    db_path: Optional[Union[str, Path]] = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional SQLite database connection with row factory configured."""
    resolved_path = _resolve_db_path(db_path)
    conn = sqlite3.connect(resolved_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if resolved_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Union[str, Path]] = None) -> None:
    """Initialize the SQLite schema, FTS5 virtual table, and synchronization triggers.
    
    Safe to call multiple times (idempotent).
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Main memories table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('voice', 'telegram', 'browser_tab', 'email', 'manual')),
                vault_scope TEXT NOT NULL,
                deleted_at TEXT DEFAULT NULL
            );
            """
        )

        # 2. Performance indexes
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_scope_deleted 
            ON memories (vault_scope, deleted_at);
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_timestamp 
            ON memories (timestamp);
            """
        )

        # 3. FTS5 Virtual Table for full-text search with Porter stemming
        cursor.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5 (
                content,
                content='memories',
                content_rowid='id',
                tokenize='porter unicode61'
            );
            """
        )

        # 4. Triggers to keep FTS index synchronized with memories table
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )

        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )


def _sanitize_fts_query(raw_query: str) -> str:
    """Format and sanitize query string to prevent FTS5 syntax errors.
    
    Handles special characters, unclosed quotes, and quotes individual terms
    to ensure robust execution while preserving stemming capabilities.
    """
    cleaned = raw_query.strip()
    if not cleaned:
        return ""

    # Remove unbalanced quotes
    if cleaned.count('"') % 2 != 0:
        cleaned = cleaned.replace('"', ' ')

    # Extract quoted phrases or individual whitespace-delimited tokens
    pattern = r'("[^"]+"|\S+)'
    matches = re.findall(pattern, cleaned)

    tokens: List[str] = []
    for token in matches:
        token = token.strip()
        if not token:
            continue
        if token.startswith('"') and token.endswith('"'):
            inner = token[1:-1].strip()
            if inner:
                tokens.append(f'"{inner}"')
        elif token in {"AND", "OR", "NOT"}:
            tokens.append(token)
        elif token.endswith("*") and len(token) > 1 and token[:-1].isalnum():
            tokens.append(token)
        else:
            # Strip quotes and quote safe term
            term = token.replace('"', '')
            if term:
                tokens.append(f'"{term}"')

    return " ".join(tokens)


# -----------------------------------------------------------------------------
# Core Vault Operations
# -----------------------------------------------------------------------------

def insert(
    content: str,
    source: str,
    vault_scope: str,
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """Insert a new memory row into the vault.

    Args:
        content: The text content to store. Must not be empty.
        source: Ingestion source. Must be one of:
                'voice', 'telegram', 'browser_tab', 'email', 'manual'.
        vault_scope: Vault scope identifier ('personal' or a client name).
        db_path: Optional custom database path (defaults to data/jai_memory.db).

    Returns:
        The integer ID of the newly inserted memory.

    Raises:
        ValueError: If content or vault_scope is empty, or source is invalid.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Memory content must be a non-empty string.")

    if source not in VALID_SOURCES:
        raise ValueError(
            f"Invalid source '{source}'. Must be one of: {sorted(VALID_SOURCES)}"
        )

    if not isinstance(vault_scope, str) or not vault_scope.strip():
        raise ValueError("vault_scope must be a non-empty string (e.g. 'personal' or client name).")

    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_content = content.strip()
    clean_scope = vault_scope.strip()

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO memories (content, timestamp, source, vault_scope, deleted_at)
            VALUES (?, ?, ?, ?, NULL);
            """,
            (clean_content, now_iso, source, clean_scope),
        )
        return int(cursor.lastrowid)


def search(
    query: str,
    vault_scope: Optional[str] = None,
    limit: int = 10,
    db_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Search memories using SQLite FTS5 and BM25 ranking.

    Soft-deleted rows (deleted_at IS NOT NULL) are strictly excluded.
    If vault_scope is specified, search is strictly isolated to that scope.

    Args:
        query: The keyword or phrase search query.
        vault_scope: Optional scope ('personal' or client name). If provided,
                     only matches within this scope are returned.
        limit: Maximum number of ranked results to return (default 10).
        db_path: Optional custom database path (defaults to data/jai_memory.db).

    Returns:
        List of matching memory dictionaries ordered by relevance (BM25 rank).
    """
    if not isinstance(query, str) or not query.strip():
        return []

    if limit <= 0:
        return []

    init_db(db_path)

    # Base query for FTS5 + BM25 ranking joined with memories table
    sql = """
        SELECT
            m.id,
            m.content,
            m.timestamp,
            m.source,
            m.vault_scope,
            m.deleted_at,
            bm25(memories_fts) AS rank
        FROM memories_fts
        JOIN memories m ON m.id = memories_fts.rowid
        WHERE memories_fts MATCH ?
          AND m.deleted_at IS NULL
    """
    params: List[Any] = []

    # Strict vault_scope filtering if specified
    if vault_scope is not None:
        clean_scope = vault_scope.strip()
        sql += " AND m.vault_scope = ?"
        scope_param = clean_scope
    else:
        scope_param = None

    sql += " ORDER BY rank ASC LIMIT ?"

    # Execute search with fallback query sanitization if raw query encounters FTS5 syntax errors
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        query_candidates = [query.strip(), _sanitize_fts_query(query)]
        rows = None
        last_error = None

        for candidate in query_candidates:
            if not candidate:
                continue
            exec_params = [candidate]
            if scope_param is not None:
                exec_params.append(scope_param)
            exec_params.append(limit)

            try:
                cursor.execute(sql, exec_params)
                rows = cursor.fetchall()
                break
            except sqlite3.OperationalError as e:
                last_error = e
                continue

        if rows is None:
            if last_error:
                # Log or return empty list on irrecoverable syntax errors
                return []
            return []

        results: List[Dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                    "source": row["source"],
                    "vault_scope": row["vault_scope"],
                    "deleted_at": row["deleted_at"],
                    "rank": float(row["rank"]),
                }
            )
        return results


def soft_delete(
    id: int,
    db_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Soft-delete a memory by setting deleted_at timestamp.
    
    The row is never removed from the database.

    Args:
        id: The integer ID of the memory row to soft-delete.
        db_path: Optional custom database path (defaults to data/jai_memory.db).

    Returns:
        True if the row was found and marked deleted, False otherwise.
    """
    if not isinstance(id, int):
        raise TypeError("Memory ID must be an integer.")

    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE memories
            SET deleted_at = ?
            WHERE id = ? AND deleted_at IS NULL;
            """,
            (now_iso, id),
        )
        return cursor.rowcount > 0


def get_by_id(
    id: int,
    include_deleted: bool = False,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve a single memory row by ID.

    Args:
        id: The integer ID of the memory to fetch.
        include_deleted: Whether to return the memory if it has been soft-deleted.
        db_path: Optional custom database path (defaults to data/jai_memory.db).

    Returns:
        Dict representing the row, or None if not found.
    """
    if not isinstance(id, int):
        raise TypeError("Memory ID must be an integer.")

    init_db(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if include_deleted:
            cursor.execute("SELECT * FROM memories WHERE id = ?;", (id,))
        else:
            cursor.execute("SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL;", (id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "vault_scope": row["vault_scope"],
            "deleted_at": row["deleted_at"],
        }


__all__ = [
    "insert",
    "search",
    "soft_delete",
    "get_by_id",
    "init_db",
    "VALID_SOURCES",
]




# --- RESCUED HIGH-VALUE FEATURES: AST CODE CHUNKER & 24H REM CONTEXT COMPRESSOR ---
def ingest_code_file_ast_chunks(file_path: str, vault_scope: str = "personal", db_path=None) -> list[int]:
    """Splits a Python file along exact AST function/class boundaries (never mid-function) and indexes in FTS5."""
    import ast as _ast
    import pathlib as _pathlib
    p = _pathlib.Path(file_path)
    source_text = p.read_text(encoding="utf-8", errors="ignore")
    lines = source_text.splitlines()
    tree = _ast.parse(source_text)
    inserted_ids = []
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            start = getattr(node, "lineno", 1) - 1
            end = getattr(node, "end_lineno", start + 15)
            chunk = "\n".join(lines[start:end])
            header = f"[AST_CHUNK {p.name}::{node.name} lines {start+1}-{end}]\n"
            rec_id = insert(content=header + chunk, source="manual", vault_scope=vault_scope, db_path=db_path)
            inserted_ids.append(rec_id)
    return inserted_ids

def run_rem_sleep_compressor(raw_logs: list[str], vault_scope: str = "personal", db_path=None) -> dict:
    """24-Hour REM Sleep Compressor: Extracts recurring error traps & milestones into permanent FTS5 memory."""
    traps = [line.strip() for line in raw_logs if any(k in line.lower() for k in ["error", "trap", "bug", "fix", "lesson", "milestone"])]
    if not traps:
        traps = [line.strip() for line in raw_logs[:5] if line.strip()]
    summary = "[REM_SLEEP_DISTILLATION] " + " | ".join(traps)
    rec_id = insert(content=summary, source="manual", vault_scope=vault_scope, db_path=db_path)
    return {"distilled_id": rec_id, "trap_count": len(traps), "summary": summary}
