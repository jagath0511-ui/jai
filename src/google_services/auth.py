"""Google OAuth 2.0 Authentication Manager for JAI.

Handles credential storage in SQLite, automated token refresh, and the interactive
OAuth2 consent flow for Gmail, Google Calendar, and Google Drive APIs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Union

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from src.database.db import DatabaseManager, get_db
from src.database.repository import OAuthRepository

try:
    from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES
except Exception:
    GOOGLE_CREDENTIALS_FILE = Path("credentials.json")
    GOOGLE_SCOPES = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/tasks",
    ]

logger = logging.getLogger("jai.google_auth")


class GoogleAuthError(Exception):
    """Exception raised for Google Authentication errors."""
    pass


class GoogleAuthManager:
    """Manages Google OAuth2 credentials, SQLite persistence, and token refreshes."""

    SERVICE_NAME = "google"

    def __init__(
        self,
        credentials_file: Optional[Union[str, Path]] = None,
        scopes: Optional[List[str]] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.credentials_file = Path(credentials_file or GOOGLE_CREDENTIALS_FILE)
        self.scopes = scopes or GOOGLE_SCOPES
        self.db = db or get_db()
        self.oauth_repo = OAuthRepository(self.db)
        self._cached_creds: Optional[Credentials] = None

    def get_stored_credentials(self) -> Optional[Credentials]:
        """Load stored credentials from SQLite database."""
        token_record = self.oauth_repo.get_token(self.SERVICE_NAME)
        if not token_record or not token_record.token_json:
            return None

        try:
            token_info = json.loads(token_record.token_json)
            creds = Credentials.from_authorized_user_info(token_info, scopes=self.scopes)
            return creds
        except Exception as e:
            logger.warning(f"Failed to parse stored Google credentials: {e}")
            return None

    def save_credentials(self, creds: Credentials, email: Optional[str] = None) -> None:
        """Serialize and save credentials to SQLite database."""
        token_json = creds.to_json()
        scopes_str = " ".join(creds.scopes or self.scopes)
        self.oauth_repo.save_token(
            service=self.SERVICE_NAME,
            token_json=token_json,
            account_email=email,
            scopes=scopes_str,
        )
        self._cached_creds = creds
        logger.info(f"Saved Google credentials to database (Account: {email or 'Unknown'})")

    def get_credentials(self) -> Credentials:
        """Return valid Google credentials, refreshing expired tokens automatically.
        
        Raises GoogleAuthError if credentials are not found or cannot be refreshed.
        """
        creds = self._cached_creds or self.get_stored_credentials()

        if not creds:
            raise GoogleAuthError(
                "No Google credentials found in database. "
                "Please run authentication first: `python -m src.cli auth`"
            )

        if creds.expired and creds.refresh_token:
            logger.info("Google token expired. Refreshing token via Google OAuth endpoint...")
            try:
                creds.refresh(Request())
                email = self.get_account_email()
                self.save_credentials(creds, email=email)
            except Exception as e:
                logger.error(f"Failed to refresh Google credentials: {e}")
                raise GoogleAuthError(f"Failed to refresh Google credentials: {e}")

        if not creds.valid:
            raise GoogleAuthError("Google credentials exist but are invalid or expired.")

        self._cached_creds = creds
        return creds

    def is_authenticated(self) -> bool:
        """Check whether valid or refreshable Google credentials exist."""
        try:
            creds = self.get_stored_credentials()
            if creds and (creds.valid or creds.refresh_token):
                return True
        except Exception:
            pass
        return False

    def get_account_email(self) -> Optional[str]:
        """Fetch the account email from the database or via Google API."""
        record = self.oauth_repo.get_token(self.SERVICE_NAME)
        if record and record.account_email:
            return record.account_email

        # If not saved yet, attempt to query google userinfo
        try:
            creds = self.get_stored_credentials()
            if creds and creds.valid:
                from googleapiclient.discovery import build
                service = build("oauth2", "v2", credentials=creds)
                info = service.userinfo().get().execute()
                email = info.get("email")
                if email:
                    self.save_credentials(creds, email=email)
                    return email
        except Exception as e:
            logger.debug(f"Could not fetch userinfo email: {e}")
        return None

    def authenticate_interactive(
        self,
        port: int = 0,
        open_browser: bool = True,
        access_type: str = "offline",
    ) -> Credentials:
        """Run the interactive OAuth 2.0 authorization code flow.
        
        Requires `credentials.json` downloaded from Google Cloud Console.
        """
        if not self.credentials_file.is_file():
            raise FileNotFoundError(
                f"\n{'=' * 70}\n"
                f"MISSING GOOGLE CLIENT CREDENTIALS FILE!\n"
                f"{'=' * 70}\n"
                f"Could not find OAuth client secrets at: {self.credentials_file.resolve()}\n\n"
                f"To connect to Gmail, Calendar, and Google Drive:\n"
                f"1. Go to Google Cloud Console (https://console.cloud.google.com)\n"
                f"2. Enable Gmail API, Google Calendar API, and Google Drive API\n"
                f"3. Create OAuth 2.0 Client ID (Application type: Desktop App)\n"
                f"4. Download JSON and place it as 'credentials.json' in your project root.\n"
                f"Refer to GOOGLE_SETUP.md for complete step-by-step instructions.\n"
                f"{'=' * 70}\n"
            )

        logger.info("Starting Google OAuth 2.0 authorization flow...")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_file),
            scopes=self.scopes,
        )

        creds = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            access_type=access_type,
            prompt="consent",
        )

        # Retrieve email address
        email: Optional[str] = None
        try:
            from googleapiclient.discovery import build
            service = build("oauth2", "v2", credentials=creds)
            user_info = service.userinfo().get().execute()
            email = user_info.get("email")
        except Exception as e:
            logger.warning(f"Could not fetch userinfo after authorization: {e}")

        self.save_credentials(creds, email=email)
        return creds

    def revoke(self) -> bool:
        """Remove stored credentials from SQLite database."""
        self._cached_creds = None
        return self.oauth_repo.delete_token(self.SERVICE_NAME)

