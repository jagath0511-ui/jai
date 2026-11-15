"""Gmail API connector and synchronization service for JAI."""

from __future__ import annotations

import base64
import email
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import Resource, build

from src.database.db import DatabaseManager, get_db
from src.database.models import GmailMessageRecord
from src.database.repository import GmailRepository, SyncRepository
from .auth import GoogleAuthManager

logger = logging.getLogger("jai.gmail")


def _decode_mime_header(header_value: Optional[str]) -> str:
    """Safely decode RFC 2047 encoded email header."""
    if not header_value:
        return ""
    try:
        decoded_fragments = decode_header(header_value)
        result = []
        for text, encoding in decoded_fragments:
            if isinstance(text, bytes):
                result.append(text.decode(encoding or "utf-8", errors="replace"))
            else:
                result.append(str(text))
        return "".join(result)
    except Exception:
        return header_value


class GmailService:
    """Connects to Gmail API, fetches emails, and synchronizes with SQLite."""

    SERVICE_NAME = "gmail"

    def __init__(
        self,
        auth_manager: Optional[GoogleAuthManager] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.auth = auth_manager or GoogleAuthManager(db=db)
        self.db = db or get_db()
        self.gmail_repo = GmailRepository(self.db)
        self.sync_repo = SyncRepository(self.db)
        self._client: Optional[Resource] = None

    def get_client(self) -> Resource:
        """Return authenticated Gmail API service resource."""
        if self._client is None:
            creds = self.auth.get_credentials()
            self._client = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._client

    def _extract_body(self, payload: Dict[str, Any]) -> str:
        """Extract plain text body from a message payload recursively."""
        mime_type = payload.get("mimeType", "")
        body = payload.get("body", {})

        # Direct plain text body
        if mime_type == "text/plain" and body.get("data"):
            try:
                raw_data = base64.urlsafe_b64decode(body["data"])
                return raw_data.decode("utf-8", errors="replace")
            except Exception:
                pass

        # Multipart message
        parts = payload.get("parts", [])
        extracted_text: List[str] = []
        for part in parts:
            part_mime = part.get("mimeType", "")
            part_body = part.get("body", {})
            if part_mime == "text/plain" and part_body.get("data"):
                try:
                    raw_data = base64.urlsafe_b64decode(part_body["data"])
                    extracted_text.append(raw_data.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            elif part.get("parts"):
                # Nested multipart (e.g. multipart/mixed inside multipart/alternative)
                nested = self._extract_body(part)
                if nested:
                    extracted_text.append(nested)

        return "\n".join(extracted_text)

    def parse_message(self, raw_msg: Dict[str, Any]) -> GmailMessageRecord:
        """Parse raw Gmail API response dict into a typed GmailMessageRecord."""
        msg_id = raw_msg["id"]
        thread_id = raw_msg.get("threadId")
        snippet = raw_msg.get("snippet", "")
        internal_date_raw = raw_msg.get("internalDate")
        internal_date = int(internal_date_raw) if internal_date_raw else None
        labels = raw_msg.get("labelIds", [])
        is_unread = "UNREAD" in labels

        # Headers extraction
        headers = raw_msg.get("payload", {}).get("headers", [])
        headers_map: Dict[str, str] = {}
        for h in headers:
            headers_map[h.get("name", "").lower()] = h.get("value", "")

        subject = _decode_mime_header(headers_map.get("subject", ""))
        sender = _decode_mime_header(headers_map.get("from", ""))
        recipient = _decode_mime_header(headers_map.get("to", ""))
        date_str = headers_map.get("date", "")

        body_text = self._extract_body(raw_msg.get("payload", {}))

        return GmailMessageRecord(
            id=msg_id,
            thread_id=thread_id,
            sender=sender,
            recipient=recipient,
            subject=subject,
            snippet=snippet,
            body_text=body_text,
            date=date_str,
            internal_date=internal_date,
            labels=labels,
            is_unread=is_unread,
        )

    def fetch_messages(
        self,
        max_results: int = 20,
        query: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
    ) -> List[GmailMessageRecord]:
        """Fetch messages from Gmail API and return parsed records."""
        client = self.get_client()
        request = client.users().messages().list(
            userId="me",
            maxResults=max_results,
            q=query or "",
            labelIds=label_ids or [],
        )
        response = request.execute()
        message_summaries = response.get("messages", [])
        if not message_summaries:
            return []

        records: List[GmailMessageRecord] = []
        for summary in message_summaries:
            try:
                raw_msg = (
                    client.users()
                    .messages()
                    .get(userId="me", id=summary["id"], format="full")
                    .execute()
                )
                records.append(self.parse_message(raw_msg))
            except Exception as e:
                logger.warning(f"Failed to fetch Gmail message {summary['id']}: {e}")

        return records

    def sync(self, max_messages: int = 50, query: Optional[str] = None) -> int:
        """Synchronize recent emails into SQLite database.
        
        Returns the number of messages synchronized.
        """
        self.sync_repo.update_sync_state(self.SERVICE_NAME, status="in_progress")
        try:
            records = self.fetch_messages(max_results=max_messages, query=query)
            count = self.gmail_repo.upsert_messages(records)
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="success",
                items_synced=count,
            )
            logger.info(f"Synchronized {count} Gmail messages to SQLite.")
            return count
        except Exception as e:
            logger.error(f"Gmail synchronization failed: {e}")
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="error",
                error_message=str(e),
            )
            raise

    def list_unread(self, limit: int = 20) -> List[GmailMessageRecord]:
        """List unread emails from local SQLite database."""
        return self.gmail_repo.list_unread(limit=limit)

    def list_recent(self, limit: int = 20) -> List[GmailMessageRecord]:
        """List recent emails from local SQLite database."""
        return self.gmail_repo.list_recent(limit=limit)

    def search_local(self, query: str, limit: int = 20) -> List[GmailMessageRecord]:
        """Fast offline keyword search across local SQLite emails."""
        return self.gmail_repo.search(query=query, limit=limit)

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        is_html: bool = False,
    ) -> Dict[str, Any]:
        """Create an email draft in Gmail."""
        client = self.get_client()
        message = MIMEText(body, "html" if is_html else "plain", "utf-8")
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if bcc:
            message["bcc"] = bcc

        raw_b64 = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft = client.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw_b64}}
        ).execute()
        logger.info(f"Created Gmail draft id={draft.get('id')} to={to}")
        return draft

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        thread_id: Optional[str] = None,
        is_html: bool = False,
    ) -> Dict[str, Any]:
        """Send an email directly through Gmail API."""
        client = self.get_client()
        message = MIMEText(body, "html" if is_html else "plain", "utf-8")
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if bcc:
            message["bcc"] = bcc

        raw_b64 = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        send_body: Dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            send_body["threadId"] = thread_id

        sent_msg = client.users().messages().send(
            userId="me",
            body=send_body
        ).execute()
        logger.info(f"Successfully sent Gmail message id={sent_msg.get('id')} to={to}")
        return sent_msg

    def send_draft(self, draft_id: str) -> Dict[str, Any]:
        """Send an existing Gmail draft."""
        client = self.get_client()
        res = client.users().drafts().send(
            userId="me",
            body={"id": draft_id}
        ).execute()
        logger.info(f"Sent Gmail draft id={draft_id}")
        return res

    def trash_message(self, message_id: str) -> Dict[str, Any]:
        """Move an email to Trash."""
        client = self.get_client()
        res = client.users().messages().trash(userId="me", id=message_id).execute()
        logger.info(f"Trashed Gmail message id={message_id}")
        return res


