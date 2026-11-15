"""src/telegram_approval.py - Cross-Process Human-in-the-Loop Gate.

Coordinates approval requests between AgentLoop and Telegram via SQLite WAL.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request
import uuid

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("jai.telegram_approval")

DB_PATH = _REPO_ROOT / "data" / "jai_memory.db"
DEFAULT_TIMEOUT_SEC = 600.0  # 10 minutes per JAI_BROWSER_RULES.md Section 3


def _get_env_var(key: str, default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if not val:
        env_file = _REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
    return val or default


TG_BOT_TOKEN = _get_env_var("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = _get_env_var("TELEGRAM_CHAT_ID", "")


def init_approval_db(db_path: Optional[Path] = None) -> None:
    """Ensure pending_approvals table exists in WAL mode."""
    target_path = db_path or DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target_path, timeout=10.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                approval_id TEXT PRIMARY KEY,
                action_name TEXT NOT NULL,
                summary TEXT NOT NULL,
                arguments_json TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
        """)
        # If pending_approvals already existed without arguments_json, add it
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(pending_approvals)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "arguments_json" not in existing_cols:
            try:
                conn.execute("ALTER TABLE pending_approvals ADD COLUMN arguments_json TEXT;")
            except Exception as e:
                logger.debug("ALTER TABLE pending_approvals failed (may already exist): %s", e)
        conn.commit()


init_approval_db()


def get_approval_keyboard(approval_id: str, is_sensitive: bool = False) -> dict:
    """Returns the inline keyboard markup for approval cards."""
    btn_text = "▶️ Continue (Typed in Browser)" if is_sensitive else "✅ Approve"
    return {
        "inline_keyboard": [
            [
                {"text": btn_text, "callback_data": f"appr_{approval_id}_yes"},
                {"text": "❌ Cancel", "callback_data": f"appr_{approval_id}_no"}
            ]
        ]
    }


def dispatch_telegram_card(approval_id: str, action_name: str, summary: str, ttl: float, is_sensitive: bool = False, bot_token: Optional[str] = None) -> bool:
    """Send an interactive Telegram card with inline Approve / Cancel buttons."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        logger.debug(f"Skipping dispatch_telegram_card for {approval_id} during unit test.")
        return True
    token = bot_token or _get_env_var("BROWSER_BOT_TOKEN") or _get_env_var("TELEGRAM_BOT_TOKEN")
    chat_id = _get_env_var("TELEGRAM_CHAT_ID", ALLOWED_CHAT_ID)
    if not token or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Cannot send approval prompt.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    keyboard = get_approval_keyboard(approval_id, is_sensitive=is_sensitive)
    
    header = "🟡 <b>SENSITIVE CREDENTIAL PAUSE (TYPE IN BROWSER)</b>" if is_sensitive else "🚨 <b>RULE 0 APPROVAL REQUIRED</b>"
    instruction = "<i>Please type your password/OTP directly in Chrome, then tap Continue.</i>" if is_sensitive else "<i>Tap below to authorize or abort execution.</i>"

    text = (
        f"{header}\n\n"
        f"• <b>Action:</b> <code>{action_name}</code>\n"
        f"• <b>Details:</b> {summary}\n"
        f"• <b>Timeout:</b> {int(ttl)}s (10 min)\n\n"
        f"{instruction}"
    )

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as e:
        logger.error(f"Failed to post approval card to Telegram: {e}")
        return False


def resolve_approval(approval_id: str, decision: bool, db_path: Optional[Path] = None) -> bool:
    """Update approval status in SQLite.
    Updates status to 'APPROVED' if decision=True or 'CANCELLED' if decision=False.
    Returns True if a row was updated, False otherwise.
    """
    status = "APPROVED" if decision else "CANCELLED"
    target_path = db_path or DB_PATH
    try:
        with sqlite3.connect(target_path, timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            cur = conn.cursor()
            cur.execute(
                "UPDATE pending_approvals SET status = ? WHERE approval_id = ? AND status = 'PENDING'",
                (status, approval_id)
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Error resolving approval {approval_id}: {e}")
        return False


def telegram_approval(action_name: str, arguments: Optional[Dict[str, Any]] = None, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> bool:
    """Blocking gateway callback. 
    Inserts PENDING approval, alerts Telegram, and polls SQLite until human answers.
    """
    args = arguments or {}
    approval_id = str(uuid.uuid4())[:8]
    now = time.time()
    expires_at = now + timeout_sec

    # Detect if this is a sensitive field pause
    is_sensitive = bool(args.get("is_sensitive")) or any(
        bool(re.search(r"(password|pwd|otp|mpin|cvv|card|pin|credential)", str(k).lower() + " " + str(v).lower()))
        for k, v in args.items()
    )

    # Sanitize arguments so plaintext secrets NEVER reach summary, Telegram, or SQLite DB
    sanitized_args: Dict[str, Any] = {}
    for k, v in args.items():
        k_low = str(k).lower()
        if re.search(r"(key|secret|password|pwd|token|otp|mpin|cvv|card|pin|auth|credential)", k_low):
            sanitized_args[k] = "[REDACTED_SENSITIVE_INPUT]"
        elif is_sensitive and k_low in ("text", "value", "content", "input_text", "query", "string"):
            sanitized_args[k] = "[REDACTED_SENSITIVE_INPUT]"
        else:
            sanitized_args[k] = v

    # Format human-readable summary
    summary_parts = []
    for k, v in sanitized_args.items():
        if k in ("element_id", "label", "text", "url", "file_path", "target", "path"):
            summary_parts.append(f"{k}='{v}'")
    
    if is_sensitive:
        elem_desc = sanitized_args.get("label") or sanitized_args.get("name") or sanitized_args.get("element_id") or "credential field"
        summary = f"⚠️ SENSITIVE FIELD PAUSE: [{elem_desc}]. JAI paused. Please type directly into Chrome and tap Continue."
    else:
        summary = ", ".join(summary_parts) if summary_parts else json.dumps(sanitized_args)[:200]
    
    args_json = json.dumps(sanitized_args, default=str)

    # 1. Insert PENDING approval into SQLite WAL
    init_approval_db()
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_approvals
                (approval_id, action_name, summary, arguments_json, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (approval_id, action_name, summary, args_json, now, expires_at)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to record pending approval in DB: {e}", exc_info=True)

    # 2. Dispatch Telegram notification card (direct)
    dispatch_telegram_card(approval_id, action_name, summary, timeout_sec, is_sensitive=is_sensitive)

    logger.info(f"Dispatched approval request for [{action_name}] (ID: {approval_id}). Raising ActionApprovalRequiredError immediately without blocking sleep (Rule 4).")
    try:
        from src.action_gateway import ActionApprovalRequiredError
    except ImportError:
        from action_gateway import ActionApprovalRequiredError
    raise ActionApprovalRequiredError(
        action_name=action_name,
        reason=f"Human approval required: {summary} (ID: {approval_id})",
        details=sanitized_args,
    )


# ---------------------------------------------------------------------------
# Cloud D1 Workspace Cache Sync Helpers
# ---------------------------------------------------------------------------
WORKER_URL = _get_env_var("JAI_WORKER_URL", "").rstrip("/")
POOL_SECRET = _get_env_var("POOL_SECRET", "")


def _call_worker(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not WORKER_URL or not POOL_SECRET:
        return {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        WORKER_URL + path,
        data=data,
        method=method,
        headers={
            "X-Pool-Secret": POOL_SECRET,
            "Content-Type": "application/json",
            "User-Agent": "JAI-Laptop/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_emails_to_worker(emails: list[dict[str, Any]], full_snapshot: bool = True) -> dict[str, Any]:
    """Push unread email snapshot to Cloudflare D1 via Worker using X-Pool-Secret."""
    if not WORKER_URL or not POOL_SECRET:
        return {"status": "skipped", "reason": "WORKER_URL or POOL_SECRET not set"}
    payload = {
        "full_snapshot": full_snapshot,
        "emails": emails[:50],
    }
    return _call_worker("POST", "/api/sync/emails", payload)


def sync_calendar_to_worker(events: list[dict[str, Any]], full_snapshot: bool = True) -> dict[str, Any]:
    """Push active calendar snapshot to Cloudflare D1 via Worker using X-Pool-Secret."""
    if not WORKER_URL or not POOL_SECRET:
        return {"status": "skipped", "reason": "WORKER_URL or POOL_SECRET not set"}
    payload = {
        "full_snapshot": full_snapshot,
        "events": events[:50],
    }
    return _call_worker("POST", "/api/sync/calendar", payload)


__all__ = [
    "telegram_approval",
    "resolve_approval",
    "dispatch_telegram_card",
    "get_approval_keyboard",
    "init_approval_db",
    "sync_emails_to_worker",
    "sync_calendar_to_worker",
    "DEFAULT_TIMEOUT_SEC",
    "DB_PATH",
]
