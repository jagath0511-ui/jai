"""Vault Manager Module for JAI.

Manages per-client vault scoping on top of memory_vault.py.
Provides:
- Directory creation and lifecycle for client files: vault/clients/<client_name>/
- SQLite registration and discovery of client vaults.
- Strict scope isolation: guarantees all client memory operations explicitly pass
  vault_scope=<client_name>, strictly preventing accidental cross-scope writes or reads.
- Personal vault isolation helpers.
- Soft-delete only: no hard-deletion capabilities exist anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Union

import memory_vault

# -----------------------------------------------------------------------------
# Base Directories & Configuration
# -----------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
if BASE_DIR.name == "src":
    BASE_DIR = BASE_DIR.parent

try:
    from config import VAULT_CLIENTS_PATH, VAULT_PERSONAL_PATH, JAI_MEMORY_DB_PATH
    DEFAULT_CLIENTS_DIR: Path = VAULT_CLIENTS_PATH
    DEFAULT_PERSONAL_DIR: Path = VAULT_PERSONAL_PATH
    DEFAULT_DB_PATH: Path = JAI_MEMORY_DB_PATH
except Exception:
    DEFAULT_CLIENTS_DIR: Path = BASE_DIR / "vault" / "clients"
    DEFAULT_PERSONAL_DIR: Path = BASE_DIR / "vault" / "personal"
    DEFAULT_DB_PATH: Path = BASE_DIR / "data" / "jai_memory.db"

RESERVED_SCOPES: Set[str] = {"personal", "global", "system"}


# -----------------------------------------------------------------------------
# Validation Helpers
# -----------------------------------------------------------------------------
def validate_client_name(client_name: str) -> str:
    """Validate and sanitize a client name for safe filesystem and scope usage.

    Args:
        client_name: The client identifier.

    Returns:
        Sanitized, non-empty client name.

    Raises:
        ValueError: If client_name is empty, reserved, or contains illegal characters.
    """
    if not isinstance(client_name, str):
        raise ValueError("client_name must be a string.")

    cleaned = client_name.strip()
    if not cleaned:
        raise ValueError("client_name cannot be empty or whitespace.")

    if cleaned.lower() in RESERVED_SCOPES:
        raise ValueError(
            f"'{cleaned}' is a reserved scope name and cannot be used as a client vault."
        )

    # Disallow path traversal or directory separators
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or "\0" in cleaned:
        raise ValueError(f"Invalid client_name '{cleaned}': path traversal and separators are forbidden.")

    # Disallow special control or reserved filesystem characters
    if not re.match(r"^[a-zA-Z0-9_\-\. ]+$", cleaned):
        raise ValueError(
            f"Invalid client_name '{cleaned}': must only contain alphanumeric characters, spaces, dashes, dots, or underscores."
        )

    if cleaned in {".", ".."}:
        raise ValueError("client_name cannot be '.' or '..'.")

    return cleaned


# -----------------------------------------------------------------------------
# Database Schema Initialization
# -----------------------------------------------------------------------------
def _init_client_vaults_db(db_path: Optional[Union[str, Path]] = None) -> None:
    """Ensure client_vaults metadata table and memory_vault tables exist."""
    # Ensure memory_vault tables (memories, FTS5, triggers) are initialized
    memory_vault.init_db(db_path)

    with memory_vault.get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS client_vaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT UNIQUE NOT NULL,
                folder_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_client_vaults_name 
            ON client_vaults (client_name);
            """
        )


# -----------------------------------------------------------------------------
# Client Vault Registration & Folder Lifecycle
# -----------------------------------------------------------------------------
def get_client_vault_path(
    client_name: str,
    clients_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Return the filesystem Path to a client's vault folder.

    Args:
        client_name: Name of the client.
        clients_dir: Optional custom clients root directory.

    Returns:
        Path to vault/clients/<client_name>/
    """
    clean_name = validate_client_name(client_name)
    base = Path(clients_dir) if clients_dir is not None else DEFAULT_CLIENTS_DIR
    return base / clean_name


def create_client_vault(
    client_name: str,
    clients_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Register a new client scope and create vault/clients/<client_name>/ folder.

    Idempotent: if the client vault already exists on disk and in database,
    it safely returns the existing folder path.

    Args:
        client_name: Unique client identifier (cannot be 'personal').
        clients_dir: Optional custom clients root directory (default vault/clients).
        db_path: Optional custom SQLite database path.

    Returns:
        Path to the client's folder: vault/clients/<client_name>/

    Raises:
        ValueError: If client_name is invalid or reserved.
    """
    clean_name = validate_client_name(client_name)
    target_folder = get_client_vault_path(clean_name, clients_dir=clients_dir)

    # 1. Create client-specific filesystem directory
    target_folder.mkdir(parents=True, exist_ok=True)

    # 2. Register client scope in SQLite
    _init_client_vaults_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    with memory_vault.get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO client_vaults (client_name, folder_path, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(client_name) DO UPDATE SET
                folder_path = excluded.folder_path;
            """,
            (clean_name, str(target_folder.resolve()), now_iso),
        )

    return target_folder


def client_vault_exists(
    client_name: str,
    clients_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Check whether a client vault exists in the database or filesystem.

    Args:
        client_name: Name of the client to check.
        clients_dir: Optional custom clients directory.
        db_path: Optional custom database path.

    Returns:
        True if the client is registered or its folder exists, False otherwise.
    """
    try:
        clean_name = validate_client_name(client_name)
    except ValueError:
        return False

    folder = get_client_vault_path(clean_name, clients_dir=clients_dir)
    if folder.exists() and folder.is_dir():
        return True

    _init_client_vaults_db(db_path)
    with memory_vault.get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM client_vaults WHERE client_name = ?;", (clean_name,))
        return cursor.fetchone() is not None


def list_client_vaults(
    clients_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> List[str]:
    """Return all registered client vault names.

    Reconciles client names registered in the database with existing folders
    under the clients directory, returning a deduplicated, sorted list.

    Args:
        clients_dir: Optional custom clients root directory.
        db_path: Optional custom SQLite database path.

    Returns:
        Sorted list of client name strings.
    """
    clients: Set[str] = set()

    # 1. Read from SQLite registry
    _init_client_vaults_db(db_path)
    with memory_vault.get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT client_name FROM client_vaults;")
        for row in cursor.fetchall():
            name = row["client_name"]
            if name:
                clients.add(name)

    # 2. Discover from filesystem
    base = Path(clients_dir) if clients_dir is not None else DEFAULT_CLIENTS_DIR
    if base.exists() and base.is_dir():
        for entry in base.iterdir():
            if entry.is_dir():
                try:
                    valid_name = validate_client_name(entry.name)
                    clients.add(valid_name)
                except ValueError:
                    continue

    return sorted(clients)


# -----------------------------------------------------------------------------
# Scoped Memory Operations (Memory Vault Interop)
# -----------------------------------------------------------------------------
def insert_client_memory(
    client_name: str,
    content: str,
    source: str,
    clients_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
    auto_create_vault: bool = False,
) -> int:
    """Insert a memory record strictly scoped to a specific client.

    Guarantees that vault_scope is explicitly set to client_name, preventing
    accidental writes to personal or other client scopes.

    Args:
        client_name: Target client scope name.
        content: Memory text content.
        source: Ingestion source ('voice', 'telegram', 'browser_tab', 'email', 'manual').
        clients_dir: Optional custom clients root directory.
        db_path: Optional custom database path.
        auto_create_vault: If True, automatically creates the client vault if missing.
                           If False, raises ValueError if the client is not registered.

    Returns:
        Integer ID of the inserted memory.

    Raises:
        ValueError: If client_name is invalid, reserved ('personal'), or unregistered
                    when auto_create_vault is False.
    """
    clean_name = validate_client_name(client_name)

    if not client_vault_exists(clean_name, clients_dir=clients_dir, db_path=db_path):
        if auto_create_vault:
            create_client_vault(clean_name, clients_dir=clients_dir, db_path=db_path)
        else:
            raise ValueError(
                f"Client vault '{clean_name}' does not exist. Call create_client_vault('{clean_name}') first, "
                "or pass auto_create_vault=True."
            )

    # Explicitly enforce client_name as vault_scope
    return memory_vault.insert(
        content=content,
        source=source,
        vault_scope=clean_name,
        db_path=db_path,
    )


def search_client_vault(
    client_name: str,
    query: str,
    limit: int = 10,
    db_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Search memories strictly isolated to a specific client's vault scope.

    Ensures vault_scope=client_name is explicitly passed to memory_vault.search,
    making it impossible to retrieve personal or other client memories.

    Args:
        client_name: Target client scope.
        query: FTS5 query string.
        limit: Maximum results (default 10).
        db_path: Optional custom database path.

    Returns:
        List of matching memory dictionaries strictly within client_name scope.
    """
    clean_name = validate_client_name(client_name)

    # Explicitly enforce client scope filtering
    results = memory_vault.search(
        query=query,
        vault_scope=clean_name,
        limit=limit,
        db_path=db_path,
    )

    # Double verify that every returned row matches the requested client scope
    return [r for r in results if r.get("vault_scope") == clean_name]


def insert_personal_memory(
    content: str,
    source: str,
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """Insert a memory record explicitly scoped to the personal vault.

    Args:
        content: Memory text content.
        source: Ingestion source.
        db_path: Optional custom database path.

    Returns:
        Integer ID of inserted memory.
    """
    return memory_vault.insert(
        content=content,
        source=source,
        vault_scope="personal",
        db_path=db_path,
    )


def search_personal_vault(
    query: str,
    limit: int = 10,
    db_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Search memories strictly isolated to the personal vault.

    Args:
        query: Search query.
        limit: Maximum results.
        db_path: Optional custom database path.

    Returns:
        List of matching personal memories.
    """
    results = memory_vault.search(
        query=query,
        vault_scope="personal",
        limit=limit,
        db_path=db_path,
    )
    return [r for r in results if r.get("vault_scope") == "personal"]


def soft_delete_client_memory(
    id: int,
    client_name: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Soft-delete a memory row, optionally verifying client scope ownership.

    Strictly soft-deletes by setting deleted_at. Hard deletion is impossible.

    Args:
        id: Memory row ID to soft-delete.
        client_name: Optional client name. If provided, verifies memory belongs
                     to this client before performing soft-delete.
        db_path: Optional custom database path.

    Returns:
        True if soft-deleted successfully, False if row not found or scope mismatch.
    """
    if client_name is not None:
        clean_name = validate_client_name(client_name)
        row = memory_vault.get_by_id(id, include_deleted=False, db_path=db_path)
        if not row or row.get("vault_scope") != clean_name:
            return False

    return memory_vault.soft_delete(id, db_path=db_path)


# -----------------------------------------------------------------------------
# Object-Oriented Client Scope Wrapper
# -----------------------------------------------------------------------------
class ClientVault:
    """Encapsulates all storage and memory operations for a single client scope."""

    def __init__(
        self,
        client_name: str,
        clients_dir: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        auto_create: bool = True,
    ):
        self._client_name = validate_client_name(client_name)
        self._clients_dir = clients_dir
        self._db_path = db_path

        if auto_create:
            self._folder_path = create_client_vault(
                self._client_name, clients_dir=self._clients_dir, db_path=self._db_path
            )
        else:
            self._folder_path = get_client_vault_path(
                self._client_name, clients_dir=self._clients_dir
            )

    @property
    def client_name(self) -> str:
        """The client scope identifier."""
        return self._client_name

    @property
    def project_id(self) -> str:
        """The project identifier (T17 Project Boundary Isolation)."""
        return self._client_name

    @property
    def client_id(self) -> str:
        """The client identifier."""
        return self._client_name

    @property
    def scope(self) -> str:
        """The vault scope identifier."""
        return self._client_name

    @property
    def folder_path(self) -> Path:
        """Filesystem directory path for client-specific files."""
        return self._folder_path

    def insert(self, content: str, source: str) -> int:
        """Insert memory scoped to this client."""
        return insert_client_memory(
            client_name=self._client_name,
            content=content,
            source=source,
            clients_dir=self._clients_dir,
            db_path=self._db_path,
            auto_create_vault=False,
        )

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search memories strictly within this client's scope."""
        return search_client_vault(
            client_name=self._client_name,
            query=query,
            limit=limit,
            db_path=self._db_path,
        )

    def soft_delete(self, id: int) -> bool:
        """Soft-delete a memory belonging to this client."""
        return soft_delete_client_memory(
            id=id,
            client_name=self._client_name,
            db_path=self._db_path,
        )

    def get(self, id: int, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Retrieve memory row by ID only if it belongs to this client."""
        row = memory_vault.get_by_id(id, include_deleted=include_deleted, db_path=self._db_path)
        if row and row.get("vault_scope") == self._client_name:
            return row
        return None

    def __repr__(self) -> str:
        return f"<ClientVault client_name='{self._client_name}' path='{self._folder_path}'>"


# -----------------------------------------------------------------------------
# T17 Project Boundary Isolation Helpers (project_id / client_id / scope)
# -----------------------------------------------------------------------------
def get_project_vault_path(project_id: str, clients_dir: Optional[Union[str, Path]] = None) -> Path:
    """Return the filesystem directory path for a given project_id / client scope."""
    return get_client_vault_path(client_name=project_id, clients_dir=clients_dir)


def insert_project_memory(
    project_id: str,
    content: str,
    source: str,
    clients_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
    auto_create_vault: bool = True,
) -> int:
    """Insert a memory record strictly scoped to a specific project_id (T17 Boundary Isolation)."""
    return insert_client_memory(
        client_name=project_id,
        content=content,
        source=source,
        clients_dir=clients_dir,
        db_path=db_path,
        auto_create_vault=auto_create_vault,
    )


def search_project_vault(
    project_id: str,
    query: str,
    limit: int = 10,
    db_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Search memories strictly isolated to a specific project_id scope (T17 Boundary Isolation)."""
    return search_client_vault(
        client_name=project_id,
        query=query,
        limit=limit,
        db_path=db_path,
    )


__all__ = [
    "create_client_vault",
    "list_client_vaults",
    "client_vault_exists",
    "get_client_vault_path",
    "validate_client_name",
    "insert_client_memory",
    "search_client_vault",
    "insert_personal_memory",
    "search_personal_vault",
    "soft_delete_client_memory",
    "ClientVault",
    "get_project_vault_path",
    "insert_project_memory",
    "search_project_vault",
]
