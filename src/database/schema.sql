-- =============================================================================
-- JAI SQLite Database Schema
-- Location: data/jai_memory.db
-- Supports: OAuth Tokens, Sync State, Gmail Messages, Calendar Events, Drive Files,
--           and Full-Text Search (FTS5)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. OAuth Credentials Storage
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oauth_tokens (
    service TEXT PRIMARY KEY,               -- e.g. 'google'
    account_email TEXT,                     -- User's primary email (e.g. user@gmail.com)
    token_json TEXT NOT NULL,               -- Full serialized credentials JSON
    scopes TEXT,                            -- Space or comma-delimited scopes
    created_at TEXT NOT NULL,               -- ISO 8601 UTC timestamp
    updated_at TEXT NOT NULL,               -- ISO 8601 UTC timestamp
    deleted_at TEXT                         -- ISO 8601 UTC timestamp (soft-delete)
);

-- -----------------------------------------------------------------------------
-- 2. Sync State Tracking
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    service TEXT PRIMARY KEY,               -- 'gmail', 'calendar', 'drive'
    last_sync_at TEXT,                      -- ISO 8601 UTC timestamp
    sync_cursor TEXT,                       -- Page token, historyId, or syncToken
    items_synced INTEGER DEFAULT 0,         -- Total items synced in last run
    status TEXT DEFAULT 'idle',             -- 'idle', 'in_progress', 'success', 'error'
    error_message TEXT                      -- Details on failure, if any
);

-- -----------------------------------------------------------------------------
-- 3. Gmail Messages
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gmail_messages (
    id TEXT PRIMARY KEY,                    -- Gmail message ID
    thread_id TEXT,                         -- Gmail conversation thread ID
    sender TEXT,                            -- From header (Name <email>)
    recipient TEXT,                         -- To header
    subject TEXT,                           -- Subject line
    snippet TEXT,                           -- Short preview snippet
    body_text TEXT,                         -- Plain text body extracted
    date TEXT,                              -- Formatted date string or ISO
    internal_date INTEGER,                  -- Gmail internal timestamp (ms epoch)
    labels TEXT,                            -- JSON array of label IDs (INBOX, UNREAD, etc.)
    is_unread INTEGER DEFAULT 0,            -- 1 if unread, 0 if read
    synced_at TEXT NOT NULL                 -- ISO 8601 timestamp of local sync
);

CREATE INDEX IF NOT EXISTS idx_gmail_thread ON gmail_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_gmail_internal_date ON gmail_messages(internal_date DESC);
CREATE INDEX IF NOT EXISTS idx_gmail_is_unread ON gmail_messages(is_unread);

-- -----------------------------------------------------------------------------
-- 4. Google Calendar Events
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,                    -- Calendar event ID
    summary TEXT,                           -- Event title/summary
    description TEXT,                       -- Event description / notes
    start_time TEXT,                        -- ISO 8601 start datetime
    end_time TEXT,                          -- ISO 8601 end datetime
    all_day INTEGER DEFAULT 0,              -- 1 if all-day event, 0 otherwise
    location TEXT,                          -- Physical or virtual location / link
    attendees TEXT,                         -- JSON array of attendee emails/names
    html_link TEXT,                         -- Link to open in Google Calendar
    status TEXT DEFAULT 'confirmed',        -- 'confirmed', 'tentative', 'cancelled'
    updated_at TEXT,                        -- Google updated timestamp
    synced_at TEXT NOT NULL                 -- ISO 8601 timestamp of local sync
);

CREATE INDEX IF NOT EXISTS idx_cal_start_time ON calendar_events(start_time);
CREATE INDEX IF NOT EXISTS idx_cal_end_time ON calendar_events(end_time);
CREATE INDEX IF NOT EXISTS idx_cal_status ON calendar_events(status);

-- -----------------------------------------------------------------------------
-- 5. Google Drive Files
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drive_files (
    id TEXT PRIMARY KEY,                    -- Google Drive file ID
    name TEXT NOT NULL,                     -- File name with extension
    mime_type TEXT,                         -- MIME type (e.g. application/pdf, google-apps.document)
    size_bytes INTEGER DEFAULT 0,           -- File size in bytes (0 for cloud docs)
    web_view_link TEXT,                     -- URL to view/edit file in Google Drive
    modified_time TEXT,                     -- ISO 8601 modified datetime
    created_time TEXT,                      -- ISO 8601 created datetime
    parents TEXT,                           -- JSON array of parent folder IDs
    trashed INTEGER DEFAULT 0,              -- 1 if file is in trash
    synced_at TEXT NOT NULL                 -- ISO 8601 timestamp of local sync
);

CREATE INDEX IF NOT EXISTS idx_drive_modified_time ON drive_files(modified_time DESC);
CREATE INDEX IF NOT EXISTS idx_drive_mime_type ON drive_files(mime_type);
CREATE INDEX IF NOT EXISTS idx_drive_trashed ON drive_files(trashed);

-- -----------------------------------------------------------------------------
-- 6. Full-Text Search (FTS5) Virtual Tables & Sync Triggers
-- -----------------------------------------------------------------------------

-- Gmail FTS
CREATE VIRTUAL TABLE IF NOT EXISTS gmail_fts USING fts5(
    id UNINDEXED,
    subject,
    snippet,
    body_text,
    sender,
    content='gmail_messages',
    content_rowid='rowid'
);

-- Triggers to maintain gmail_fts
CREATE TRIGGER IF NOT EXISTS trg_gmail_ai AFTER INSERT ON gmail_messages BEGIN
    INSERT INTO gmail_fts(rowid, id, subject, snippet, body_text, sender)
    VALUES (new.rowid, new.id, new.subject, new.snippet, new.body_text, new.sender);
END;

CREATE TRIGGER IF NOT EXISTS trg_gmail_ad AFTER DELETE ON gmail_messages BEGIN
    INSERT INTO gmail_fts(gmail_fts, rowid, id, subject, snippet, body_text, sender)
    VALUES ('delete', old.rowid, old.id, old.subject, old.snippet, old.body_text, old.sender);
END;

CREATE TRIGGER IF NOT EXISTS trg_gmail_au AFTER UPDATE ON gmail_messages BEGIN
    INSERT INTO gmail_fts(gmail_fts, rowid, id, subject, snippet, body_text, sender)
    VALUES ('delete', old.rowid, old.id, old.subject, old.snippet, old.body_text, old.sender);
    INSERT INTO gmail_fts(rowid, id, subject, snippet, body_text, sender)
    VALUES (new.rowid, new.id, new.subject, new.snippet, new.body_text, new.sender);
END;

-- Calendar FTS
CREATE VIRTUAL TABLE IF NOT EXISTS calendar_fts USING fts5(
    id UNINDEXED,
    summary,
    description,
    location,
    content='calendar_events',
    content_rowid='rowid'
);

-- Triggers to maintain calendar_fts
CREATE TRIGGER IF NOT EXISTS trg_calendar_ai AFTER INSERT ON calendar_events BEGIN
    INSERT INTO calendar_fts(rowid, id, summary, description, location)
    VALUES (new.rowid, new.id, new.summary, new.description, new.location);
END;

CREATE TRIGGER IF NOT EXISTS trg_calendar_ad AFTER DELETE ON calendar_events BEGIN
    INSERT INTO calendar_fts(calendar_fts, rowid, id, summary, description, location)
    VALUES ('delete', old.rowid, old.id, old.summary, old.description, old.location);
END;

CREATE TRIGGER IF NOT EXISTS trg_calendar_au AFTER UPDATE ON calendar_events BEGIN
    INSERT INTO calendar_fts(calendar_fts, rowid, id, summary, description, location)
    VALUES ('delete', old.rowid, old.id, old.summary, old.description, old.location);
    INSERT INTO calendar_fts(rowid, id, summary, description, location)
    VALUES (new.rowid, new.id, new.summary, new.description, new.location);
END;

-- Drive FTS
CREATE VIRTUAL TABLE IF NOT EXISTS drive_fts USING fts5(
    id UNINDEXED,
    name,
    mime_type,
    content='drive_files',
    content_rowid='rowid'
);

-- Triggers to maintain drive_fts
CREATE TRIGGER IF NOT EXISTS trg_drive_ai AFTER INSERT ON drive_files BEGIN
    INSERT INTO drive_fts(rowid, id, name, mime_type)
    VALUES (new.rowid, new.id, new.name, new.mime_type);
END;

CREATE TRIGGER IF NOT EXISTS trg_drive_ad AFTER DELETE ON drive_files BEGIN
    INSERT INTO drive_fts(drive_fts, rowid, id, name, mime_type)
    VALUES ('delete', old.rowid, old.id, old.name, old.mime_type);
END;

CREATE TRIGGER IF NOT EXISTS trg_drive_au AFTER UPDATE ON drive_files BEGIN
    INSERT INTO drive_fts(drive_fts, rowid, id, name, mime_type)
    VALUES ('delete', old.rowid, old.id, old.name, old.mime_type);
    INSERT INTO drive_fts(rowid, id, name, mime_type)
    VALUES (new.rowid, new.id, new.name, new.mime_type);
END;

-- -----------------------------------------------------------------------------
-- 6. Google Tasks Storage
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS google_tasks (
    id TEXT PRIMARY KEY,                    -- Google Task ID
    tasklist_id TEXT NOT NULL,              -- Parent Tasklist ID
    title TEXT NOT NULL,                    -- Task title
    notes TEXT,                             -- Description / notes
    status TEXT DEFAULT 'needsAction',      -- 'needsAction' or 'completed'
    due TEXT,                               -- RFC 3339 timestamp or date
    completed_at TEXT,                      -- Timestamp when marked complete
    updated_at TEXT,                        -- Last modified timestamp in Google
    position TEXT,                          -- Task position string
    synced_at TEXT NOT NULL                 -- Local sync timestamp
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON google_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_tasklist ON google_tasks(tasklist_id);

-- Tasks FTS
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    id UNINDEXED,
    title,
    notes,
    content='google_tasks',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS trg_tasks_ai AFTER INSERT ON google_tasks BEGIN
    INSERT INTO tasks_fts(rowid, id, title, notes)
    VALUES (new.rowid, new.id, new.title, new.notes);
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_ad AFTER DELETE ON google_tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, id, title, notes)
    VALUES ('delete', old.rowid, old.id, old.title, old.notes);
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_au AFTER UPDATE ON google_tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, id, title, notes)
    VALUES ('delete', old.rowid, old.id, old.title, old.notes);
    INSERT INTO tasks_fts(rowid, id, title, notes)
    VALUES (new.rowid, new.id, new.title, new.notes);
END;


