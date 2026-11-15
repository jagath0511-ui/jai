"""Google Calendar API connector and synchronization service for JAI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import Resource, build

from src.database.db import DatabaseManager, get_db
from src.database.models import CalendarEventRecord, now_utc_iso
from src.database.repository import CalendarRepository, SyncRepository
from .auth import GoogleAuthManager

logger = logging.getLogger("jai.calendar")


class CalendarService:
    """Connects to Google Calendar API, retrieves events, and synchronizes with SQLite."""

    SERVICE_NAME = "calendar"

    def __init__(
        self,
        auth_manager: Optional[GoogleAuthManager] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.auth = auth_manager or GoogleAuthManager(db=db)
        self.db = db or get_db()
        self.calendar_repo = CalendarRepository(self.db)
        self.sync_repo = SyncRepository(self.db)
        self._client: Optional[Resource] = None

    def get_client(self) -> Resource:
        """Return authenticated Google Calendar API service resource."""
        if self._client is None:
            creds = self.auth.get_credentials()
            self._client = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._client

    def parse_event(self, raw_event: Dict[str, Any]) -> CalendarEventRecord:
        """Parse raw Google Calendar event dict into CalendarEventRecord."""
        event_id = raw_event["id"]
        summary = raw_event.get("summary", "Untitled Event")
        description = raw_event.get("description")
        location = raw_event.get("location")
        html_link = raw_event.get("htmlLink")
        status = raw_event.get("status", "confirmed")
        updated_at = raw_event.get("updated")

        # Handle start and end timestamps (either datetime or all-day date)
        start_dict = raw_event.get("start", {})
        end_dict = raw_event.get("end", {})

        all_day = False
        start_time = start_dict.get("dateTime")
        if not start_time:
            start_time = start_dict.get("date")
            all_day = True

        end_time = end_dict.get("dateTime")
        if not end_time:
            end_time = end_dict.get("date")
            all_day = True

        # Attendees
        attendees_raw = raw_event.get("attendees", [])
        attendees: List[str] = []
        for att in attendees_raw:
            email_addr = att.get("email")
            if email_addr:
                display = att.get("displayName")
                attendees.append(f"{display} <{email_addr}>" if display else email_addr)

        return CalendarEventRecord(
            id=event_id,
            summary=summary,
            description=description,
            start_time=start_time,
            end_time=end_time,
            all_day=all_day,
            location=location,
            attendees=attendees,
            html_link=html_link,
            status=status,
            updated_at=updated_at,
        )

    def fetch_events(
        self,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = 100,
        calendar_id: str = "primary",
    ) -> List[CalendarEventRecord]:
        """Fetch events within a time window from Google Calendar API."""
        client = self.get_client()

        params: Dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max

        response = client.events().list(**params).execute()
        items = response.get("items", [])
        records = [self.parse_event(item) for item in items]
        return records

    def sync(
        self,
        days_ahead: int = 30,
        days_back: int = 7,
        calendar_id: str = "primary",
    ) -> int:
        """Synchronize calendar events within the given day window into SQLite.
        
        Returns the number of events synchronized.
        """
        self.sync_repo.update_sync_state(self.SERVICE_NAME, status="in_progress")
        try:
            now = datetime.now(timezone.utc)
            time_min = (now - timedelta(days=days_back)).isoformat()
            time_max = (now + timedelta(days=days_ahead)).isoformat()

            records = self.fetch_events(
                time_min=time_min,
                time_max=time_max,
                max_results=250,
                calendar_id=calendar_id,
            )
            count = self.calendar_repo.upsert_events(records)
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="success",
                items_synced=count,
            )
            logger.info(f"Synchronized {count} Calendar events to SQLite.")
            return count
        except Exception as e:
            logger.error(f"Calendar synchronization failed: {e}")
            self.sync_repo.update_sync_state(
                self.SERVICE_NAME,
                status="error",
                error_message=str(e),
            )
            raise

    def list_upcoming(self, limit: int = 20) -> List[CalendarEventRecord]:
        """List upcoming events from local SQLite database."""
        return self.calendar_repo.list_upcoming(limit=limit)

    def search_local(self, query: str, limit: int = 20) -> List[CalendarEventRecord]:
        """Fast offline keyword search across local SQLite calendar events."""
        return self.calendar_repo.search(query=query, limit=limit)

    def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: Optional[str] = None,
        location: Optional[str] = None,
        calendar_id: str = "primary",
    ) -> Dict[str, Any]:
        """Create a new event in Google Calendar."""
        client = self.get_client()
        body: Dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_time} if "T" in start_time else {"date": start_time},
            "end": {"dateTime": end_time} if "T" in end_time else {"date": end_time},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        event = client.events().insert(calendarId=calendar_id, body=body).execute()
        logger.info(f"Created Calendar event id={event.get('id')} summary='{summary}'")
        return event

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> None:
        """Delete an event from Google Calendar."""
        client = self.get_client()
        client.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info(f"Deleted Calendar event id={event_id}")

    def quick_add_event(self, text: str, calendar_id: str = "primary") -> Dict[str, Any]:
        """Quick add an event from natural language text."""
        client = self.get_client()
        event = client.events().quickAdd(calendarId=calendar_id, text=text).execute()
        logger.info(f"Quick-added Calendar event id={event.get('id')}")
        return event


