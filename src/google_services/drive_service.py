"""Google Drive API connector and synchronization service for JAI."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import Resource, build

from src.database.db import DatabaseManager, get_db
from src.database.models import DriveFileRecord
from src.database.repository import DriveRepository, SyncRepository
from .auth import GoogleAuthManager

logger = logging.getLogger("jai.drive")


class DriveService:
    """Connects to Google Drive API, retrieves file metadata, and synchronizes with SQLite."""

    SERVICE_NAME = "drive"

    def __init__(
        self,
        auth_manager: Optional[GoogleAuthManager] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.auth = auth_manager or GoogleAuthManager(db=db)
        self.db = db or get_db()
        self.drive_repo = DriveRepository(self.db)
        self.sync_repo = SyncRepository(self.db)
        self._client: Optional[Resource] = None

    def get_client(self) -> Resource:
        """Return authenticated Google Drive API service resource."""
        if self._client is None:
            creds = self.auth.get_credentials()
            self._client = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._client

    def parse_file(self, raw_file: Dict[str, Any]) -> DriveFileRecord:
        """Parse raw Drive file dict into DriveFileRecord."""
        file_id = raw_file["id"]
        name = raw_file.get("name", "Untitled")
        mime_type = raw_file.get("mimeType")
        size_raw = raw_file.get("size")
        size_bytes = int(size_raw) if size_raw else 0
        web_view_link = raw_file.get("webViewLink")
        modified_time = raw_file.get("modifiedTime")
        created_time = raw_file.get("createdTime")
        parents = raw_file.get("parents", [])
        trashed = bool(raw_file.get("trashed", False))

        return DriveFileRecord(
            id=file_id,
            name=name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            web_view_link=web_view_link,
            modified_time=modified_time,
            created_time=created_time,
            parents=parents,
            trashed=trashed,
        )

    def fetch_files(
        self,
        max_results: int = 50,
        query: Optional[str] = None,
        order_by: str = "modifiedTime desc",
    ) -> List[DriveFileRecord]:
        """Fetch file metadata from Google Drive API."""
        client = self.get_client()

        fields = (
            "nextPageToken, "
            "files(id, name, mimeType, size, webViewLink, modifiedTime, createdTime, parents, trashed)"
        )

        params: Dict[str, Any] = {
            "pageSize": max_results,
            "fields": fields,
            "orderBy": order_by,
        }
        if query:
            params["q"] = query

        response = client.files().list(**params).execute()
        files = response.get("files", [])
        records = [self.parse_file(f) for f in files]
        return records

    def sync(self, max_files: int = 100, query: Optional[str] = None) -> int:
        """Synchronize recent drive file metadata into SQLite.
        
        Returns the number of files synchronized.
        """
        self.sync_repo.update_sync_state(self.SERVICE_NAME, status="in_progress")
        try:
            # By default exclude trashed files unless query specifies
            sync_query = query or "trashed = false"
            records = self.fetch_files(max_results=max_files, query=sync_query)
            count = self.drive_repo.upsert_files(records)
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="success",
                items_synced=count,
            )
            logger.info(f"Synchronized {count} Google Drive files to SQLite.")
            return count
        except Exception as e:
            logger.error(f"Google Drive synchronization failed: {e}")
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="error",
                error_message=str(e),
            )
            raise

    def list_recent(self, limit: int = 30) -> List[DriveFileRecord]:
        """List recently modified drive files from local SQLite database."""
        return self.drive_repo.list_recent(limit=limit)

    def search_local(self, query: str, limit: int = 20) -> List[DriveFileRecord]:
        """Fast offline keyword search across local SQLite Drive file names."""
        return self.drive_repo.search(query=query, limit=limit)

    def upload_file(
        self,
        file_path: Union[str, Path],
        name: Optional[str] = None,
        parent_folder_id: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a local file to Google Drive."""
        from googleapiclient.http import MediaFileUpload
        client = self.get_client()
        p = Path(file_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")

        file_metadata: Dict[str, Any] = {"name": name or p.name}
        if parent_folder_id:
            file_metadata["parents"] = [parent_folder_id]

        media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)
        uploaded = client.files().create(body=file_metadata, media_body=media, fields="id, name, webViewLink").execute()
        logger.info(f"Uploaded file '{p.name}' to Drive id={uploaded.get('id')}")
        return uploaded

    def delete_file(self, file_id: str) -> None:
        """Permanently delete a file from Google Drive."""
        client = self.get_client()
        client.files().delete(fileId=file_id).execute()
        logger.info(f"Deleted Drive file id={file_id}")


