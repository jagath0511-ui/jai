"""JAI Direct Telegram Bridge — Live Local OS Hardware, App Control & Brain Router."""
import logging
logger = logging.getLogger("jai.telegram")

from typing import Any, Optional, Dict, List, Tuple, Callable
import uuid
import sys
import os
import ctypes
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ==============================================================================
# 0. PRE-IMPORT DAEMON INITIALIZATION: Stdout/Stderr Redirection & Kernel Mutex
# ==============================================================================
_IS_PYTHONW = (
    sys.stdout is None
    or not hasattr(sys.stdout, "write")
    or ("pythonw" in (sys.executable or "").lower())
)

if _IS_PYTHONW:
    _log_dir = _REPO_ROOT / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = _log_dir / "jai_daemon.log"
    try:
        _daemon_log_fh = open(_log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = _daemon_log_fh
        sys.stderr = _daemon_log_fh
    except Exception as _suppressed_exc:
        logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
# Kernel Single-Instance Mutex (Global\JAI_Browser_Daemon_Mutex)
_DAEMON_MUTEX = None


def _acquire_daemon_mutex(force: bool = False, mutex_name: str = "Global\\JAI_Browser_Daemon_Mutex") -> Any:
    """Acquire named mutex to ensure single instance running in production."""
    global _DAEMON_MUTEX
    if _DAEMON_MUTEX is not None and not force:
        return _DAEMON_MUTEX

    should_acquire = force or (
        sys.platform == "win32"
        and "pytest" not in sys.modules
        and "unittest" not in sys.modules
        and not os.environ.get("PYTEST_CURRENT_TEST")
        and not os.environ.get("JAI_SKIP_MUTEX")
    )
    if should_acquire and sys.platform == "win32":
        try:
            _DAEMON_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                print("Daemon already active; exiting duplicate", flush=True)
                if not force:
                    sys.exit(0)
        except Exception as _mutex_err:
            print(f"Warning: Failed to acquire named mutex: {_mutex_err}", flush=True)
    return _DAEMON_MUTEX

import uuid
import datetime
import html
import io
import json
import pathlib
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
import sqlite3
from typing import Dict, List, Tuple, Any, Optional

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as _suppressed_exc:
        logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
ROOT = pathlib.Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception as _suppressed_exc:
    logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BROWSER_BOT_TOKEN = os.getenv("BROWSER_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
AGENT_NAME = os.getenv("AGENT_NAME", "Browser Agent").strip()


def get_allowed_chat_id() -> str:
    """Return active ALLOWED_CHAT_ID from env or module constant."""
    return str(os.environ.get("TELEGRAM_CHAT_ID", ALLOWED_CHAT_ID)).strip()


def verify_telegram_startup_auth() -> str:
    """Validate that TELEGRAM_CHAT_ID is present and non-empty.
    
    Fail-closed policy: Refuse to start if TELEGRAM_CHAT_ID is missing or empty.
    """
    chat_id = get_allowed_chat_id()
    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing or empty in environment. Authorization must fail closed. Refusing to start."
        )
    return chat_id

# Concurrency, Task Lifecycle & Missing Fact States
BROWSER_TASK_LOCK = threading.Lock()
ACTIVE_BROWSER_KILL_SWITCHES: Dict[str, threading.Event] = {}
ACTIVE_MISSING_FACT_SESSIONS: Dict[str, Dict[str, Any]] = {}

# Inline keyboard buttons shown under browser results
BROWSER_INLINE_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "Try again", "callback_data": "try again"},
            {"text": "Scroll down", "callback_data": "scroll down"},
            {"text": "Close tab", "callback_data": "close tab"},
        ]
    ]
}

_READONLY_PAGE_QUERY_RE = re.compile(
    r"^(what are you seeing|what is on (the )?screen|what do you see|describe (this |the )?(page|tab|screen)|read (this |the )?(page|tab))\??$",
    re.IGNORECASE,
)


def get_approval_keyboard(approval_id: str) -> dict:
    """Return inline keyboard with [ ✅ Approve ] and [ ❌ Cancel ] buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"appr_{approval_id}_yes"},
                {"text": "❌ Cancel", "callback_data": f"appr_{approval_id}_no"},
            ]
        ]
    }


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def is_on_battery() -> bool:
    try:
        status = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return status.ACLineStatus == 0
    except Exception as _suppressed_exc:
        logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
    return False


def get_live_hardware_status() -> str:
    status = SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
    pct = status.BatteryLifePercent if status.BatteryLifePercent <= 100 else "Unknown"
    plugged = "Plugged In (Charging)" if status.ACLineStatus == 1 else "On Battery Power"

    total, used, free = shutil.disk_usage("C:\\")
    total_gb = round(total / (1024**3), 2)
    used_gb = round(used / (1024**3), 2)
    free_gb = round(free / (1024**3), 2)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"💻 JAI LIVE LAPTOP TELEMETRY\n"
        f"• Battery: {pct}% ({plugged})\n"
        f"• Disk (C:): {free_gb} GB Free / {total_gb} GB Total ({used_gb} GB Used)\n"
        f"• System Time: {now_str}\n"
        f"• Host OS: {platform.system()} {platform.release()}"
    )


def markdown_to_telegram_html(text: str) -> str:
    """Convert LLM Markdown formatting to Telegram-compliant HTML.
    
    1. Escapes literal &, <, and > characters first.
    2. Protects code blocks (```) and inline code (`) via placeholders.
    3. Converts headers ('### Title' -> <b>Title</b>).
    4. Converts bullet items ('* item' or '- item' -> '• item').
    5. Converts bold ('**x**' -> <b>x</b>).
    6. Converts italic ('*x*' -> <i>x</i>).
    7. Restores protected code blocks wrapped in <pre><code> and inline code in <code>.
    """
    if not text:
        return ""

    # 1. Escape literal HTML special characters first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Extract code blocks with placeholders
    code_blocks = []
    def _save_code_block(m):
        code_blocks.append(f"<pre><code>{m.group(1)}</code></pre>")
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(?:[a-zA-Z0-9_-]+)?\r?\n?([\s\S]*?)```", _save_code_block, text)

    # 3. Extract inline code with placeholders
    inline_codes = []
    def _save_inline_code(m):
        inline_codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\r\n]+)`", _save_inline_code, text)

    # 4. Headers: "### Title" -> <b>Title</b> (also supports #, ##, etc.)
    text = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*#*$", r"<b>\1</b>", text)

    # 5. Bullet items: "* item" or "- item" -> "• item"
    text = re.sub(r"(?m)^([ \t]*)\*[ \t]+", r"\1• ", text)
    text = re.sub(r"(?m)^([ \t]*)-[ \t]+", r"\1• ", text)

    # 6. Bold: **x** -> <b>x</b>
    text = re.sub(r"\*\*(?!\s)([^*\r\n]*?\S)\*\*", r"<b>\1</b>", text)

    # 7. Italic: *x* -> <i>x</i>
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\r\n]*?\S)\*(?!\*)", r"<i>\1</i>", text)

    # 8. Restore code blocks and inline code
    for i, cb in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", cb)
    for i, ic in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", ic)

    return text


def split_message_text(text: str, max_chars: int = 4000) -> list[str]:
    """Split text longer than max_chars into several chunks, respecting paragraph, line, and word boundaries."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        search_start = max(0, max_chars - 800)

        # 1. Double newline (paragraph boundary)
        idx = remaining.rfind("\n\n", search_start, max_chars)
        if idx != -1:
            split_at = idx + 2
        else:
            # 2. Single newline (line boundary)
            idx = remaining.rfind("\n", search_start, max_chars)
            if idx != -1:
                split_at = idx + 1
            else:
                # 3. Space boundary (word boundary)
                idx = remaining.rfind(" ", search_start, max_chars)
                if idx != -1:
                    split_at = idx + 1
                else:
                    # 4. Hard cut
                    split_at = max_chars

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\r\n")

    if remaining.strip():
        chunks.append(remaining)
    return chunks


def _safe_console_print(msg: str):
    """Print to console safely without crashing on Windows cp1252 terminals."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sanitized = msg.encode(enc, errors="replace").decode(enc)
        print(sanitized, flush=True)


def strip_internal_prefixes(text: str) -> tuple[str, list[str]]:
    """Remove prefixes like '▸ LLM Router' or '▸ JAI Browser (CDP)...' from user view.
    
    Prints each prefix to console and returns (cleaned_text, removed_prefixes).
    """
    clean_text = str(text or "").strip()
    removed_prefixes = []
    while True:
        m = re.match(r"^▸\s*([^\r\n]+)\r?\n*", clean_text)
        if not m:
            break
        prefix = m.group(1).strip()
        removed_prefixes.append(f"▸ {prefix}")
        _safe_console_print(f"[TELEGRAM ROUTE] ▸ {prefix}")
        clean_text = clean_text[m.end():].lstrip("\r\n")

    if not clean_text.strip():
        clean_text = "Done."

    return clean_text, removed_prefixes


def _post_telegram_api(chat_id: str, text: str, parse_mode: str | None = None, reply_markup: dict | None = None, bot_token: str | None = None) -> bool:
    """Low-level POST to Telegram sendMessage API. Automatically chunks messages > 3800 chars."""
    token = bot_token or TG_BOT_TOKEN
    if not token:
        print("[TELEGRAM] No bot token configured in .env; message not sent.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram hard limit is 4096 characters per message
    if len(text) > 3800:
        chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
        all_ok = True
        for idx, chunk in enumerate(chunks):
            chunk_markup = reply_markup if idx == len(chunks) - 1 else None
            ok = _post_telegram_api(chat_id, chunk, parse_mode=parse_mode, reply_markup=chunk_markup, bot_token=token)
            if not ok and parse_mode:
                # Fallback to plain text if HTML tags were split across chunk boundaries
                plain_chunk = re.sub(r"<[^>]+>", "", chunk)
                ok = _post_telegram_api(chat_id, plain_chunk, parse_mode=None, reply_markup=chunk_markup, bot_token=token)
            all_ok = all_ok and ok
            if len(chunks) > 1:
                time.sleep(0.1)
        return all_ok

    payload = {
        "chat_id": str(chat_id),
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            mode_label = parse_mode or "PLAIN"
            _safe_console_print(f"[TELEGRAM SENT ({mode_label})] Replied to {chat_id} ({len(text)} chars)")
            return True
    except urllib.error.HTTPError as http_err:
        err_msg = ""
        try:
            err_msg = http_err.read().decode("utf-8", errors="replace")
        except Exception as _suppressed_exc:
            logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
        mode_label = parse_mode or "PLAIN"
        _safe_console_print(f"[TELEGRAM REJECTED ({mode_label})] HTTP {http_err.code}: {err_msg}")
        if parse_mode:
            plain_fallback = re.sub(r"<[^>]+>", "", text)
            return _post_telegram_api(chat_id, plain_fallback, parse_mode=None, reply_markup=reply_markup, bot_token=token)
        return False
    except Exception as e:
        mode_label = parse_mode or "PLAIN"
        _safe_console_print(f"[TELEGRAM NETWORK ERROR ({mode_label})] {e}")
        return False


def answer_callback_query(callback_query_id: str, text: str | None = None, bot_token: Optional[str] = None) -> bool:
    """Answer a Telegram callback query to dismiss the loading state on the button."""
    tok = bot_token or TG_BOT_TOKEN
    if not tok or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{tok}/answerCallbackQuery"
    payload = {"callback_query_id": str(callback_query_id)}
    if text:
        payload["text"] = text
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return True
    except Exception as e:
        _safe_console_print(f"[TELEGRAM CALLBACK ERROR] {e}")
        return False


def send_chat_action(chat_id: str, action: str = "typing") -> bool:
    """Send a chat action (e.g. 'typing') to Telegram."""
    if not TG_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendChatAction"
    payload = json.dumps({"chat_id": str(chat_id), "action": action}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return True
    except Exception:
        return False


class TypingNotifier:
    """Sends Telegram 'typing' chat action every 4 seconds in a background thread until stopped."""

    def __init__(self, chat_id: str):
        self.chat_id = str(chat_id)
        self.stop_event = threading.Event()
        self.thread = None

    def _loop(self):
        while not self.stop_event.is_set():
            send_chat_action(self.chat_id, "typing")
            if self.stop_event.wait(4.0):
                break

    def start(self):
        if not TG_BOT_TOKEN:
            return
        send_chat_action(self.chat_id, "typing")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


def format_friendly_failure(err_text_or_exception, user_prompt: str = "") -> str:
    """Convert technical error traces into simple, friendly human messages.
    
    The technical error is printed to the console only.
    """
    raw_error = str(err_text_or_exception)
    _safe_console_print(f"[TELEGRAM TECHNICAL ERROR] {raw_error}")

    low_p = (user_prompt or "").lower()
    low_err = raw_error.lower()

    if any(k in low_p for k in ["open", "page", "site", "website", "url", "portal", "link", "search", "browse"]) or any(k in low_err for k in ["browser", "page", "http", "connection", "timeout", "network", "url"]):
        return "I couldn't open that page. Want me to try again?"

    return "I couldn't complete that task. Want me to try again?"


def is_browser_result(ans: str = "", prompt: str = "") -> bool:
    """Determine if a command or its output corresponds to a browser action or page audit."""
    low_prompt = (prompt or "").strip().lower()
    low_ans = (ans or "").strip().lower()

    if low_prompt in ["try again", "retry", "repeat", "do it again"]:
        return True

    if low_prompt.startswith("@"):
        return True

    if is_browser_command(prompt):
        return True

    browser_markers = [
        "jai browser",
        "page summary",
        "multi-tab synthesis",
        "open tabs in jai browser",
        "open tabs (",
        "switched to tab",
        "unreadable page",
        "page title:",
        "scrolled down",
        "scrolled up",
        "closed tab",
        "couldn't open that page",
        "could not reach jai browser",
        "no web page is open in jai browser",
        "went back. now on:",
        "clicked \"",
        "typed: \"",
        "pressed ",
        "tidied up tabs",
        "tabs grouped by domain",
        "opened in background",
        "closed tabs matching",
        "closed all other tabs",
    ]
    if any(marker in low_ans for marker in browser_markers):
        return True

    return False


def get_tabs_interactive_keyboard() -> dict | None:
    """Generate an inline keyboard with buttons for each open tab, plus standard action buttons."""
    try:
        from src.jai_browser import get_open_tabs_info
        tabs = get_open_tabs_info()
        if not tabs or isinstance(tabs, str):
            return None
        keyboard = []
        row = []
        for t in tabs[:6]:
            idx = t.get("index", 1)
            raw_title = t.get("title") or t.get("url") or f"Tab {idx}"
            short_title = raw_title[:14].strip()
            btn = {"text": f"@{idx} {short_title}", "callback_data": f"@{idx}"}
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([
            {"text": "🧹 Tidy tabs", "callback_data": "tidy tabs"},
            {"text": "📁 Group tabs", "callback_data": "group tabs"},
        ])
        keyboard.append([
            {"text": "Try again", "callback_data": "try again"},
            {"text": "Scroll down", "callback_data": "scroll down"},
            {"text": "Close tab", "callback_data": "close tab"},
        ])
        return {"inline_keyboard": keyboard}
    except Exception:
        return None


def send_clean(chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    """Send a cleaned, formatted reply to Telegram through a single unified pipeline.
    
    1. Removes prefixes like '▸ LLM Router' and '▸ JAI Browser (CDP)...' from what the
       user sees, logging them to the console instead.
    2. Splits messages longer than 4000 characters into several messages.
    3. Converts LLM Markdown to Telegram HTML (escaping other < > & first).
    4. Sends with parse_mode='HTML'.
    5. If Telegram rejects the message, resends it as plain text.
    6. Attaches reply_markup (e.g. inline keyboard buttons) to the final message chunk.
    """
    if not TG_BOT_TOKEN:
        print("[TELEGRAM] No bot token configured in .env; reply not sent.", flush=True)
        return False

    if not text or not str(text).strip():
        text = "Done."

    # 1. Remove prefixes like "▸ LLM Router" from user-facing text, log to console
    clean_text, _ = strip_internal_prefixes(text)

    # 2. Split messages safely into raw chunks before HTML formatting (<3200 chars)
    raw_chunks = split_message_text(clean_text, max_chars=3200)

    overall_success = True
    for raw_idx, raw_chunk in enumerate(raw_chunks):
        is_last = (raw_idx == len(raw_chunks) - 1)
        chunk_markup = reply_markup if is_last else None

        # Convert Markdown to Telegram HTML
        html_chunk = markdown_to_telegram_html(raw_chunk)

        # 3. Send with parse_mode="HTML" if within length limit
        if len(html_chunk) <= 4096:
            success = _post_telegram_api(chat_id, html_chunk, parse_mode="HTML", reply_markup=chunk_markup)
        else:
            success = False

        if not success:
            # 4. If rejected or too long, fallback and resend as plain text
            _safe_console_print(f"[TELEGRAM FALLBACK] Resending chunk as plain text...")
            plain_chunk = re.sub(r"<[^>]+>", "", html_chunk) if len(html_chunk) <= 4096 else raw_chunk
            plain_chunk = html.unescape(plain_chunk)
            fallback_success = _post_telegram_api(chat_id, plain_chunk[:4000], parse_mode=None, reply_markup=chunk_markup)
            if not fallback_success:
                overall_success = False

        if len(raw_chunks) > 1:
            time.sleep(0.15)

    return overall_success


def send_telegram_reply(chat_id: str, text: str, reply_markup: dict | None = None):
    """Backward compatibility shim. All replies route through send_clean."""
    return send_clean(chat_id, text, reply_markup=reply_markup)


# In-memory short-term conversation memory: chat_id -> list of {"role": str, "content": str}
CHAT_MEMORY: dict[str, list[dict]] = {}

# Remember the last browser command for each chat (for "try again")
LAST_BROWSER_COMMAND: dict[str, str] = {}


def get_chat_history(chat_id: str) -> list[dict]:
    """Retrieve the conversation history for a chat (up to 10 messages)."""
    return CHAT_MEMORY.setdefault(str(chat_id), [])


def add_chat_message(chat_id: str, role: str, text: str) -> None:
    """Append a message to the chat history, capping at the last 10 messages."""
    cid = str(chat_id)
    history = get_chat_history(cid)
    history.append({"role": role, "content": text})
    if len(history) > 10:
        CHAT_MEMORY[cid] = history[-10:]


def clear_chat_memory(chat_id: str) -> None:
    """Clear the in-memory conversation history and last browser command for a chat."""
    cid = str(chat_id)
    CHAT_MEMORY[cid] = []
    LAST_BROWSER_COMMAND.pop(cid, None)


def format_conversation_context(history: list[dict]) -> str:
    """Format recent conversation history (up to 10 messages) for the LLM."""
    if not history:
        return ""
    lines = ["Recent conversation context:"]
    for msg in history[-10:]:
        role = "User" if msg.get("role") == "user" else "JAI"
        content = msg.get("content", "").strip()
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def is_browser_command(text: str) -> bool:
    """Identify if a message is a browser action or navigation command."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if low.startswith("@"):
        return True
    browser_keywords = [
        "read page", "read the page", "read tab", "read this page", "read",
        "scroll down", "scroll up", "back", "go back", "close tab", "close this tab",
        "tabs", "list tabs", "show tabs", "open tabs", "get tabs",
        "tidy tabs", "tidy up tabs", "clean tabs", "cleanup tabs",
        "group tabs", "grouped tabs", "tab groups", "tabs by domain",
        "close duplicate tabs", "close duplicates",
        "close other tabs", "close others", "keep only this tab", "keep this tab",
        "snapshot", "snap", "inspect", "elements", "page elements",
    ]
    if low in browser_keywords:
        return True
    if re.match(r"^close\s+tab\s+\d+$", low):
        return True
    if re.match(r"^(?:keep\s+tab\s+(\S+)|close\s+other\s+tabs\s+except\s+(\S+))$", low):
        return True
    if re.match(r"^close\s+(?:all\s+)?(\S+)\s+tabs$", low):
        return True
    if re.match(r"^(?:open\s+(\S+)\s+in\s+background|background\s+open\s+(\S+))$", low):
        return True
    if "in background" in low:
        return True
    if re.match(r"^(?:type|write|press|click|fill)\s+", low):
        return True
    if low.startswith("open ") and any(ext in low for ext in [".com", ".org", ".edu", ".net", ".io", ".gov", "http", "chrome", "browser"]):
        return True
    if any(k in low for k in ["search", "google", "check", "registration", "portal", "website", "admission", "notifications"]):
        return True
    try:
        from src.local_actuator import is_multi_tab_query
        if is_multi_tab_query(low):
            return True
    except Exception as _suppressed_exc:
        logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
    return False


def execute_local_or_brain(prompt: str, chat_id: str = "default", is_retry: bool = False) -> str:
    cid = str(chat_id)
    low = prompt.strip().lower()

    # 0. /reset or /new command to clear conversation memory
    if low in ["/reset", "reset", "/new", "new"]:
        clear_chat_memory(cid)
        return "I've reset our conversation memory. What would you like to do next?"

    # 1. "try again" / "retry": repeats the last browser command if one was run
    if not is_retry and low in ["try again", "retry", "repeat", "do it again"]:
        last_cmd = LAST_BROWSER_COMMAND.get(cid)
        if last_cmd:
            print(f"[TELEGRAM] Repeating last browser command for {cid}: {last_cmd}", flush=True)
            return execute_local_or_brain(last_cmd, chat_id=cid, is_retry=True)

    # 2. Instant Greeting
    if low in ["hi", "hello", "hey", "/start", "ping"]:
        ans = "▸ JAI Online\n\nHello Jagath! JAI is live on your laptop.\n\n" + get_live_hardware_status()
        add_chat_message(cid, "user", prompt)
        clean_ans, _ = strip_internal_prefixes(ans)
        add_chat_message(cid, "assistant", clean_ans)
        return ans

    # 3. Live Hardware / Battery / Disk / Status Check
    if any(k in low for k in ["battery", "disk", "storage", "system status", "hardware", "health", "time now"]):
        ans = "▸ Local OS Hardware Telemetry\n\n" + get_live_hardware_status()
        add_chat_message(cid, "user", prompt)
        clean_ans, _ = strip_internal_prefixes(ans)
        add_chat_message(cid, "assistant", clean_ans)
        return ans

    # 4. Local Desktop App Launching
    if low.startswith("open "):
        app = low.replace("open ", "").strip()
        app_map = {
            "calculator": "calc.exe",
            "calc": "calc.exe",
            "notepad": "notepad.exe",
            "cmd": "cmd.exe",
            "explorer": "explorer.exe",
        }
        target = app_map.get(app)
        if target:
            subprocess.Popen([target], shell=False)
            ans = f"▸ Local OS Actuator\n\n✅ Opened {app} on your laptop."
            add_chat_message(cid, "user", prompt)
            clean_ans, _ = strip_internal_prefixes(ans)
            add_chat_message(cid, "assistant", clean_ans)
            return ans

    # Retrieve recent conversation context (last 10 messages)
    context = format_conversation_context(get_chat_history(cid))

    # Remember browser command for "try again" repeat
    if is_browser_command(prompt):
        LAST_BROWSER_COMMAND[cid] = prompt

    try:
        from src.jai_browser import ensure_browser_running
        ensure_browser_running(timeout_seconds=15)
    except Exception as e:
        print(f"[JAI] Browser auto-start failed: {e}", flush=True)

    # 5. Route through local_actuator (with conversation history)
    try:
        from src.local_actuator import execute_or_route
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            res = asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(execute_or_route, prompt, project="ALPHA", mode="auto", conversation_history=context),
                loop
            ).result()
        except RuntimeError:
            res = execute_or_route(prompt, project="ALPHA", mode="auto", conversation_history=context)
        reply = res.get("reply", "")
        step = res.get("step_note", "Actuator")
        if reply and "invalid syntax" not in reply.lower():
            if any(err_sig in reply.lower() for err_sig in [
                "jai browser error:", "command error:", "all llm tiers failed",
                "all routing tiers failed", "could not find any readable content",
                "could not reach jai browser", "i ran into an issue while processing that:"
            ]):
                return format_friendly_failure(reply, user_prompt=prompt)

            ans = f"▸ {step}\n\n{reply}"
            add_chat_message(cid, "user", prompt)
            clean_ans, _ = strip_internal_prefixes(ans)
            add_chat_message(cid, "assistant", clean_ans)
            return ans
    except Exception as e:
        print(f"[ACTUATOR FALLBACK] {e}", flush=True)

    # 6. Direct LLM Router fallback (with conversation history)
    try:
        from src.llm_router import route_query
        hw = get_live_hardware_status()
        if context:
            prompt_with_history = f"{context}\n\n[Live Laptop Context: {hw}]\nUser message: {prompt}"
        else:
            prompt_with_history = f"[Live Laptop Context: {hw}]\nUser message: {prompt}"
        r = route_query(prompt_with_history)
        raw_ans = getattr(r, "text", str(r))
        ans = f"▸ JAI Brain\n\n{raw_ans}"
        add_chat_message(cid, "user", prompt)
        clean_ans, _ = strip_internal_prefixes(ans)
        add_chat_message(cid, "assistant", clean_ans)
        return ans
    except Exception as llm_err:
        return format_friendly_failure(llm_err, user_prompt=prompt)


async def aexecute_local_or_brain(prompt: str, chat_id: str | None = None) -> str:
    """Async wrapper dispatching execute_local_or_brain via asyncio.to_thread."""
    import asyncio
    return await asyncio.to_thread(execute_local_or_brain, prompt, chat_id=chat_id)


# ==============================================================================
# MULTIMODAL INGESTION (Photo / Document / Voice / Audio)
# ==============================================================================

def mask_sensitive_data(text: str) -> str:
    """Redact sensitive fields (credit cards, passwords, OTPs, 12-digit govt IDs/Aadhaar, SSN).
    
    Protects user privacy before text is injected into LLM prompts or persistent vault logs.
    """
    if not text:
        return ""
    # 1. Passwords, secrets, and API tokens
    text = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?([^\s'\"]+)['\"]?",
        r"\1: [REDACTED_SECRET]",
        text,
    )
    # 2. Credit Card numbers (13-19 digits, formatted or continuous)
    text = re.sub(r"\b(?:\d{4}[-\s]?){3}\d{4}\b", "[REDACTED_CARD]", text)
    text = re.sub(r"\b\d{4}[-\s]?\d{6}[-\s]?\d{5}\b", "[REDACTED_CARD]", text)
    # 3. SSN (###-##-####)
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", text)
    # 4. 12-digit Government IDs (Aadhaar: continuous 12 digits or 4-4-4 spaced)
    text = re.sub(r"\b\d{4}\s\d{4}\s\d{4}\b", "[REDACTED_GOVT_ID]", text)
    text = re.sub(r"\b(?<!\d)\d{12}(?!\d)\b", "[REDACTED_GOVT_ID]", text)
    # 5. OTPs and verification codes (4 to 8 digits following keyword)
    text = re.sub(
        r"(?i)\b(otp|one[- ]?time[- ]?password|verification\s*code|pin)\s*(?:is|:|=)?\s*(\d{4,8})\b",
        r"\1: [REDACTED_OTP]",
        text,
    )
    return text


def extract_pdf_text(pdf_source: bytes | str | pathlib.Path, max_pages: int = 15, max_chars: int = 30000) -> str:
    """Extract raw text from PDF source up to max_pages using pypdf.
    
    Fails gracefully if pypdf is not installed with an actionable pip install note.
    """
    try:
        import pypdf
    except ImportError:
        raise RuntimeError("pypdf is not installed. Please run 'pip install pypdf' to enable PDF ingestion.")

    file_obj = io.BytesIO(pdf_source) if isinstance(pdf_source, bytes) else open(pdf_source, "rb")
    try:
        reader = pypdf.PdfReader(file_obj)
        pages_text = []
        for page in reader.pages[:max_pages]:
            txt = page.extract_text() or ""
            if txt.strip():
                pages_text.append(txt.strip())
        full_text = "\n\n".join(pages_text).strip()
        return full_text[:max_chars]
    finally:
        if not isinstance(pdf_source, bytes):
            file_obj.close()


def extract_docx_text(docx_source: bytes | str | pathlib.Path, max_chars: int = 30000) -> str:
    """Extract raw text from DOCX source using Python stdlib zipfile reading word/document.xml (0 extra dependencies)."""
    file_obj = io.BytesIO(docx_source) if isinstance(docx_source, bytes) else open(docx_source, "rb")
    try:
        with zipfile.ZipFile(file_obj) as zf:
            if "word/document.xml" not in zf.namelist():
                return ""
            xml_content = zf.read("word/document.xml")
            try:
                tree = ET.fromstring(xml_content)
                namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = []
                for p in tree.findall(".//w:p", namespaces):
                    texts = [node.text for node in p.findall(".//w:t", namespaces) if node.text]
                    if texts:
                        paragraphs.append("".join(texts))
                if paragraphs:
                    return "\n".join(paragraphs).strip()[:max_chars]
            except Exception as _suppressed_exc:
                logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
            # Fallback regex tag-stripping
            raw_xml = xml_content.decode("utf-8", errors="replace")
            clean_text = re.sub(r"<[^>]+>", " ", raw_xml)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()
            return clean_text[:max_chars]
    finally:
        if not isinstance(docx_source, bytes):
            file_obj.close()


def extract_document_text(file_bytes: bytes, file_name: str, max_chars: int = 30000) -> str:
    """Extract text from supported document types and mask sensitive data."""
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".pdf":
        raw_text = extract_pdf_text(file_bytes, max_pages=15, max_chars=max_chars)
    elif ext == ".docx":
        raw_text = extract_docx_text(file_bytes, max_chars=max_chars)
    elif ext in (
        ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html",
        ".css", ".sh", ".bat", ".ps1", ".yaml", ".yml", ".xml",
        ".log", ".ini", ".env", ".sql", ".rs", ".c", ".cpp", ".h",
        ".java", ".go"
    ):
        raw_text = file_bytes.decode("utf-8", errors="replace")[:max_chars]
    else:
        raise ValueError("⚠️ Unsupported document type. Please send PDF, DOCX, TXT, CSV, or code files.")

    return mask_sensitive_data(raw_text)


def _download_telegram_file(
    file_id: str,
    bot_token: str = "",
    max_size_bytes: int = 20 * 1024 * 1024,
) -> tuple[bytes, str]:
    """Download a file from Telegram by file_id.
    
    1. Queries https://api.telegram.org/bot<token>/getFile?file_id=<file_id>
    2. Inspects file_size; if > 20 MB, aborts with a user-facing size limit error.
    3. Downloads file bytes from https://api.telegram.org/file/bot<token>/<file_path>
    4. Returns (file_bytes, file_name_or_ext).
    """
    token = bot_token or TG_BOT_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")

    info_url = f"https://api.telegram.org/bot{token}/getFile?file_id={urllib.parse.quote(str(file_id))}"
    req = urllib.request.Request(info_url, headers={"User-Agent": "JAI-Telegram-Bridge/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if not data.get("ok"):
        desc = data.get("description", "Unknown Telegram getFile error")
        raise RuntimeError(f"Telegram getFile error: {desc}")

    result = data.get("result", {})
    file_size = result.get("file_size", 0)
    if file_size > max_size_bytes:
        mb = file_size / (1024 * 1024)
        limit_mb = max_size_bytes // (1024 * 1024)
        raise ValueError(f"File size ({mb:.1f} MB) exceeds Telegram bot limit of {limit_mb} MB.")

    file_path = result.get("file_path", "")
    if not file_path:
        raise RuntimeError("Telegram getFile returned empty file_path.")

    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    dreq = urllib.request.Request(download_url, headers={"User-Agent": "JAI-Telegram-Bridge/1.0"})
    with urllib.request.urlopen(dreq, timeout=30) as dresp:
        file_bytes = dresp.read()

    file_name_or_ext = os.path.basename(file_path)
    return file_bytes, file_name_or_ext


async def _adownload_telegram_file(
    file_id: str,
    bot_token: str = "",
    max_size_bytes: int = 20 * 1024 * 1024,
) -> tuple[bytes, str]:
    """Async wrapper for _download_telegram_file."""
    import asyncio
    return await asyncio.to_thread(_download_telegram_file, file_id, bot_token=bot_token, max_size_bytes=max_size_bytes)


def transcribe_audio_groq(
    audio_bytes: bytes,
    file_name: str = "audio.ogg",
    groq_api_key: Optional[str] = None,
) -> str:
    """Post audio bytes to Groq Whisper API (whisper-large-v3-turbo)."""
    import requests

    key = groq_api_key or os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise ValueError("GROQ_API_KEY is not configured for Whisper transcription.")

    ext = os.path.splitext(file_name)[1].lower()
    if ext in (".ogg", ".oga"):
        mime = "audio/ogg"
    elif ext == ".mp3":
        mime = "audio/mpeg"
    elif ext == ".wav":
        mime = "audio/wav"
    else:
        mime = "application/octet-stream"

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {key}"}
    files = {"file": (file_name, audio_bytes, mime)}
    data = {"model": "whisper-large-v3-turbo"}

    resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Groq Whisper transcription failed (HTTP {resp.status_code}): {resp.text[:150]}")

    result = resp.json()
    transcribed_text = (result.get("text") or "").strip()
    return mask_sensitive_data(transcribed_text)


def handle_photo_message(chat_id: str, message: dict, bot_token: Optional[str] = None) -> bool:
    """Process incoming photo using Gemini Vision."""
    token = bot_token or TG_BOT_TOKEN
    photos = message.get("photo") or []
    if not photos:
        return False

    photo_obj = photos[-1]  # Highest resolution
    file_id = photo_obj.get("file_id")
    if not file_id:
        return False

    caption = (message.get("caption") or "Analyze and summarize this image.").strip()
    cid = str(chat_id)

    typing_notifier = TypingNotifier(cid)
    typing_notifier.start()
    try:
        file_bytes, _ = _download_telegram_file(file_id, bot_token=token)
        from src.llm_router import route_image
        res = route_image(
            image_bytes=file_bytes,
            prompt=caption,
            mime_type="image/jpeg",
            target_model=None,
        )
        if res.success and res.text:
            ans = res.text
            add_chat_message(cid, "user", f"[Photo with caption: {caption}]")
            add_chat_message(cid, "assistant", ans)
            send_clean(cid, ans)
            return True
        else:
            send_clean(cid, "I couldn't analyze the image right now. Want me to try again?")
            return False
    except ValueError as ve:
        send_clean(cid, f"⚠️ {ve}")
        return False
    except Exception as ex:
        _safe_console_print(f"[PHOTO INGESTION ERROR] {ex}")
        send_clean(cid, "I couldn't analyze the image. Want me to try again?")
        return False
    finally:
        typing_notifier.stop()


def handle_document_message(chat_id: str, message: dict, bot_token: Optional[str] = None) -> bool:
    """Process incoming document (PDF, DOCX, TXT, CSV, code) with text extraction and sensitive data masking."""
    token = bot_token or TG_BOT_TOKEN
    doc = message.get("document") or {}
    file_id = doc.get("file_id")
    if not file_id:
        return False

    file_name = doc.get("file_name", "document.txt")
    caption = (message.get("caption") or "").strip()
    cid = str(chat_id)

    typing_notifier = TypingNotifier(cid)
    typing_notifier.start()
    try:
        file_bytes, _ = _download_telegram_file(file_id, bot_token=token)
        try:
            extracted_text = extract_document_text(file_bytes, file_name)
        except ValueError as ve:
            send_clean(cid, str(ve))
            return False
        except RuntimeError as re_err:
            send_clean(cid, f"⚠️ {re_err}")
            return False

        if not extracted_text.strip():
            send_clean(cid, f"⚠️ Could not extract readable text from <b>{html.escape(file_name)}</b>.")
            return False

        low_caption = caption.lower()
        is_vault_save = any(k in low_caption for k in [
            "save this to vault", "save to vault", "store in vault", "add to vault", "save in vault"
        ])

        if is_vault_save:
            try:
                from src.memory_vault import add_memory
                mem_id = add_memory(
                    content=f"[Document: {file_name}]\n{extracted_text}",
                    source=f"telegram_doc_{file_name}",
                    vault_scope="personal",
                )
                send_clean(cid, f"✅ Document <b>{html.escape(file_name)}</b> saved to JAI memory vault (ID: {mem_id}).")
                add_chat_message(cid, "user", f"[Document: {file_name}] {caption}")
                add_chat_message(cid, "assistant", f"Saved {file_name} to vault (ID: {mem_id}).")
                return True
            except Exception as vex:
                send_clean(cid, f"⚠️ Could not save document to vault: {vex}")
                return False

        if caption:
            prompt = f"{caption}\n\n[Document: {file_name}]\n{extracted_text}"
        else:
            prompt = f"Analyze and summarize this document ({file_name}):\n\n{extracted_text}"

        ans = execute_local_or_brain(prompt, chat_id=cid)
        send_clean(cid, ans)
        return True
    except ValueError as ve:
        send_clean(cid, f"⚠️ {ve}")
        return False
    except Exception as ex:
        _safe_console_print(f"[DOCUMENT INGESTION ERROR] {ex}")
        send_clean(cid, "I couldn't process that document. Want me to try again?")
        return False
    finally:
        typing_notifier.stop()


def handle_voice_message(chat_id: str, message: dict, bot_token: Optional[str] = None) -> bool:
    """Process incoming voice notes or audio recordings via Groq Whisper API."""
    token = bot_token or TG_BOT_TOKEN
    audio_obj = message.get("voice") or message.get("audio") or {}
    file_id = audio_obj.get("file_id")
    if not file_id:
        return False

    default_name = "voice.oga" if message.get("voice") else "audio.mp3"
    file_name = audio_obj.get("file_name") or default_name
    cid = str(chat_id)

    typing_notifier = TypingNotifier(cid)
    typing_notifier.start()
    try:
        file_bytes, _ = _download_telegram_file(file_id, bot_token=token)
        try:
            transcribed_text = transcribe_audio_groq(file_bytes, file_name=file_name)
        except Exception as tex:
            _safe_console_print(f"[TRANSCRIPTION ERROR] {tex}")
            send_clean(cid, f"⚠️ Audio transcription failed: {tex}")
            return False

        if not transcribed_text:
            send_clean(cid, "⚠️ Could not detect any speech in the audio.")
            return False

        # Send immediate transcription confirmation
        send_clean(cid, f"🎙️ Transcribed: \"{transcribed_text}\"")

        # Feed directly into JAI's standard text message pipeline
        handle_message(cid, transcribed_text)
        return True
    except ValueError as ve:
        send_clean(cid, f"⚠️ {ve}")
        return False
    except Exception as ex:
        _safe_console_print(f"[VOICE INGESTION ERROR] {ex}")
        send_clean(cid, "I couldn't process that audio recording. Want me to try again?")
        return False
    finally:
        typing_notifier.stop()


def handle_telegram_message(chat_id: str, message: dict, bot_token: Optional[str] = None) -> bool:
    """Master message ingestion router handling photos, documents, voice notes, and text."""
    if not message:
        return False
    if message.get("photo"):
        return handle_photo_message(chat_id, message, bot_token=bot_token)
    elif message.get("document"):
        return handle_document_message(chat_id, message, bot_token=bot_token)
    elif message.get("voice") or message.get("audio"):
        return handle_voice_message(chat_id, message, bot_token=bot_token)
    elif message.get("text"):
        handle_message(chat_id, message.get("text"))
        return True
    return False


def process_telegram_update(upd: dict) -> tuple[str, str | dict, str | None] | None:
    """Extract (chat_id, text_or_message, callback_query_id) from an incoming Telegram update.
    
    Returns None if the update is invalid, unauthorized, or contains no actionable content.
    Fail-closed: Rejects when ALLOWED_CHAT_ID is empty OR chat_id differs.
    """
    allowed_chat_id = get_allowed_chat_id()

    callback_query = upd.get("callback_query")
    if callback_query:
        cq_id = str(callback_query.get("id", ""))
        cq_from = callback_query.get("from") or {}
        cq_msg = callback_query.get("message") or {}
        chat_id = str(cq_msg.get("chat", {}).get("id") or cq_from.get("id", "")).strip()
        if not allowed_chat_id or chat_id != allowed_chat_id:
            print(f"[TELEGRAM BLOCKED] Ignored callback from '{chat_id}' (fail-closed policy)", flush=True)
            return None
        text = str(callback_query.get("data", "")).strip()
        if not text:
            return None
        return chat_id, text, cq_id

    msg = upd.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
    if not allowed_chat_id or chat_id != allowed_chat_id:
        print(f"[TELEGRAM BLOCKED] Ignored message from '{chat_id}' (fail-closed policy)", flush=True)
        return None

    # Handle text messages
    text = (msg.get("text") or "").strip()
    if text:
        return chat_id, text, None

    # Handle multimodal media messages (photo, document, voice, audio)
    if any(k in msg for k in ("photo", "document", "voice", "audio")):
        return chat_id, msg, None

    return None


def handle_message(cid, msg_text):
    if isinstance(msg_text, dict):
        return handle_telegram_message(cid, msg_text)

    # 1. Continuous typing indicator
    typing_notifier = TypingNotifier(cid)
    typing_notifier.start()

    # 2. 5-second progress notice ("Working on it...")
    task_done_event = threading.Event()
    def _send_working_notice():
        if not task_done_event.is_set():
            _post_telegram_api(cid, "Working on it...")

    progress_timer = threading.Timer(5.0, _send_working_notice)
    progress_timer.daemon = True
    progress_timer.start()

    try:
        ans = execute_local_or_brain(msg_text, chat_id=cid)
    except Exception as inner_err:
        traceback.print_exc()
        ans = format_friendly_failure(inner_err, user_prompt=msg_text)
    finally:
        task_done_event.set()
        progress_timer.cancel()
        typing_notifier.stop()

    markup = None
    low_text = msg_text.strip().lower()
    if low_text in (
        "tabs", "list tabs", "show tabs", "open tabs", "get tabs",
        "tidy tabs", "tidy up tabs", "clean tabs", "cleanup tabs",
        "group tabs", "grouped tabs", "tab groups", "tabs by domain",
    ):
        markup = get_tabs_interactive_keyboard()
    if markup is None and is_browser_result(ans, prompt=msg_text):
        markup = BROWSER_INLINE_KEYBOARD
    send_clean(cid, ans, reply_markup=markup)


def handle_approval_callback(chat_id: str, callback_data: str, cq_id: str, message: Optional[dict] = None, sync_resume: bool = False) -> bool:
    """Intercept Rule 0 Approval Callbacks (Format: appr_<id>_yes, appr_<id>_no, approve:<id>, or cancel:<id>) and stop buttons.
    Calls resolve_approval(approval_id, decision), acknowledges with answer_callback_query(cq_id),
    edits message text to show recorded status, resumes coroutine if approved, and returns True.
    """
    token = BROWSER_BOT_TOKEN or TG_BOT_TOKEN
    btn_text = str(callback_data or "").strip()

    if btn_text in ("stop_browser_task", "stop_task") or btn_text.startswith("stop_task:"):
        for ke in list(ACTIVE_BROWSER_KILL_SWITCHES.values()):
            ke.set()
        stop_file = _REPO_ROOT / "data" / "STOP"
        try:
            stop_file.touch()
        except Exception as _suppressed_exc:
            logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
        if cq_id:
            try:
                answer_callback_query(cq_id, text="Task stopping...", bot_token=token)
            except Exception as _suppressed_exc:
                logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
        _post_telegram_api(chat_id, "🛑 <b>Browser task stopped by user.</b>", parse_mode="HTML", bot_token=token)
        return True

    if not (btn_text.startswith("appr_") or btn_text.startswith("approve:") or btn_text.startswith("cancel:")):
        return False

    appr_id = None
    decision = False

    if btn_text.startswith("appr_"):
        parts = btn_text.split("_")
        if len(parts) >= 3:
            decision = (parts[-1].lower() == "yes")
            appr_id = "_".join(parts[1:-1])
    elif btn_text.startswith("approve:"):
        appr_id = btn_text.split(":", 1)[1]
        decision = True
    elif btn_text.startswith("cancel:"):
        appr_id = btn_text.split(":", 1)[1]
        decision = False

    if not appr_id:
        return False

    # 1. Resolve approval in SQLite
    try:
        from src.telegram_approval import resolve_approval
        resolve_approval(appr_id, decision)
    except Exception as ex:
        _safe_console_print(f"[APPROVAL RESOLUTION ERROR] {ex}")

    try:
        from core.coroutines import resolve_suspended_task
        resolve_suspended_task(appr_id, approved=decision)
    except Exception as ex:
        _safe_console_print(f"[COROUTINE RESOLUTION ERROR] {ex}")

    # 2. Acknowledge with answer_callback_query(cq_id)
    if cq_id:
        try:
            ack_msg = "Action Approved" if decision else "Action Cancelled"
            answer_callback_query(cq_id, text=ack_msg, bot_token=token)
        except Exception as ex:
            _safe_console_print(f"[CALLBACK ACK ERROR] {ex}")

    # 3. Edit message text to show recorded status
    status_txt = "APPROVED ✅" if decision else "CANCELLED ❌"
    msg = message or {}
    msg_id = msg.get("message_id")
    edited = False
    if msg_id:
        orig_text = msg.get("text", "")
        sep = "\n\n"
        updated_text = (
            f"{orig_text}{sep}<b>Status:</b> {status_txt}"
            if orig_text
            else f"🔒 <b>RULE 0 APPROVAL</b>{sep}<b>Status:</b> {status_txt}"
        )
        try:
            edited = edit_telegram_status_message(chat_id, int(msg_id), updated_text, bot_token=token)
        except Exception as ex:
            _safe_console_print(f"[STATUS MSG EDIT ERROR] {ex}")

    if not edited:
        try:
            _post_telegram_api(chat_id, f"Decision recorded: Action {status_txt}", bot_token=token)
        except Exception as _suppressed_exc:
            logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
    # 4. If approved, spawn daemon thread to resume suspended task and post final answer
    if decision:
        def _resume_worker():
            try:
                from src.agent.loop import AgentLoop, BROWSER_AGENT_TOOLS
                from src.agent.tools import get_default_registry
                reg = get_default_registry()
                loop = AgentLoop(max_steps=8, allowed_tools=BROWSER_AGENT_TOOLS, registry=reg)
                resumed_res = loop.resume_suspended_task(appr_id)
                final_ans = ""
                if hasattr(resumed_res, "final_answer") and resumed_res.final_answer:
                    final_ans = resumed_res.final_answer
                elif hasattr(resumed_res, "observation") and resumed_res.observation:
                    final_ans = resumed_res.observation
                elif isinstance(resumed_res, dict):
                    final_ans = resumed_res.get("final_answer") or resumed_res.get("observation") or str(resumed_res)
                else:
                    final_ans = str(resumed_res)

                if final_ans:
                    _post_telegram_api(chat_id, f"✅ <b>Task Resumed & Finished:</b>\n\n{final_ans}", parse_mode="HTML", bot_token=token)
            except Exception as e:
                _safe_console_print(f"[RESUME WORKER ERROR] {e}")
                _post_telegram_api(chat_id, f"⚠️ <b>Error resuming task:</b> {e}", parse_mode="HTML", bot_token=token)

        if sync_resume:
            _resume_worker()
        else:
            t = threading.Thread(target=_resume_worker, daemon=True, name=f"resume_{appr_id}")
            t.start()

    return True


def _poll_single_bot(token: str, bot_label: str, is_browser: bool):
    """Direct long-polling loop for a single Telegram bot instance."""
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=false", timeout=10)
        print(f"[{bot_label}] Cleared webhook; direct polling active.", flush=True)
    except Exception as e:
        print(f"[{bot_label} WEBHOOK NOTE] {e}", flush=True)

    print(f"[{bot_label}] Live & Listening! Direct long-polling connected.", flush=True)
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=20&offset={offset}"
            with urllib.request.urlopen(url, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    parsed = process_telegram_update(upd)
                    if not parsed:
                        continue
                    chat_id, text_or_msg, cq_id = parsed
                    if cq_id:
                        btn_text = str(text_or_msg)
                        cq_msg = upd.get("callback_query", {}).get("message") or {}

                        # Intercept Rule 0 Approval Callbacks (Format: appr_<id>_yes, appr_<id>_no, approve:<id>, or cancel:<id>)
                        if handle_approval_callback(chat_id, btn_text, cq_id, cq_msg):
                            continue  # STOP: Do not forward callback string to LLM router!

                        answer_callback_query(cq_id, bot_token=token)
                        print(f"\n[{bot_label} BUTTON CLICK] From {chat_id}: {btn_text}", flush=True)
                    else:
                        if isinstance(text_or_msg, dict):
                            print(f"\n[{bot_label} RECEIVED MEDIA] From {chat_id}: keys={list(text_or_msg.keys())}", flush=True)
                            threading.Thread(target=handle_telegram_message, args=(chat_id, text_or_msg), daemon=True).start()
                        else:
                            print(f"\n[{bot_label} RECEIVED] From {chat_id}: {text_or_msg}", flush=True)
                            if is_browser:
                                threading.Thread(target=handle_browser_task, args=(chat_id, text_or_msg), daemon=True).start()
                            else:
                                threading.Thread(target=handle_message, args=(chat_id, text_or_msg), daemon=True).start()
        except Exception as poll_err:
            print(f"[{bot_label} POLL RETRY] {poll_err}", flush=True)
            time.sleep(3)


def poll_telegram_loop():
    """Poll both Browser Bot and Assistant Bot concurrently with zero Cloudflare latency."""
    verify_telegram_startup_auth()
    try:
        from src.jai_browser import ensure_browser_running
        ensure_browser_running(timeout_seconds=15)
        print("[JAI] Browser ready on port 9222", flush=True)
    except Exception as e:
        print(f"[JAI] Could not start browser: {e}", flush=True)

    threads = []
    # 1. Start dedicated browser bot if BROWSER_BOT_TOKEN is configured
    if BROWSER_BOT_TOKEN:
        t_browser = threading.Thread(
            target=_poll_single_bot,
            args=(BROWSER_BOT_TOKEN, "JAI_BROWSER_BOT", True),
            daemon=True,
            name="poll_browser_bot"
        )
        t_browser.start()
        threads.append(t_browser)

    # 2. Start assistant bot if TG_BOT_TOKEN is configured and distinct
    if TG_BOT_TOKEN and TG_BOT_TOKEN != BROWSER_BOT_TOKEN:
        t_assistant = threading.Thread(
            target=_poll_single_bot,
            args=(TG_BOT_TOKEN, "JAI_ASSISTANT_BOT", False),
            daemon=True,
            name="poll_assistant_bot"
        )
        t_assistant.start()
        threads.append(t_assistant)
    elif not BROWSER_BOT_TOKEN and TG_BOT_TOKEN:
        # Fallback: if only TG_BOT_TOKEN is set, use it for browser bot directly
        t_single = threading.Thread(
            target=_poll_single_bot,
            args=(TG_BOT_TOKEN, "JAI_BOT", True),
            daemon=True,
            name="poll_single_bot"
        )
        t_single.start()
        threads.append(t_single)

    if not threads:
        print("[TELEGRAM ERROR] Neither BROWSER_BOT_TOKEN nor TELEGRAM_BOT_TOKEN found in .env!", flush=True)
        return

    while True:
        time.sleep(1)


def start_telegram_daemon():
    verify_telegram_startup_auth()
    t = threading.Thread(target=poll_telegram_loop, daemon=True)
    t.start()
    return t


def main():
    """Main daemon runner for Telegram Bridge (100% Direct Long-Polling, zero latency)."""
    verify_telegram_startup_auth()
    _acquire_daemon_mutex()
    print(f"[{AGENT_NAME.upper()}] Starting 100% Direct Telegram Bridge (Zero Cloudflare / Zero Lag)...", flush=True)
    poll_telegram_loop()


def send_telegram_photo(chat_id: str, photo_path: str, caption: str = "", reply_markup: dict | None = None, bot_token: str | None = None) -> bool:
    """Upload and send a photo to Telegram via multipart/form-data."""
    token = bot_token or BROWSER_BOT_TOKEN or TG_BOT_TOKEN
    if not token or not os.path.exists(photo_path):
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"

    boundary = "----WebKitFormBoundary" + hex(int(time.time() * 1000))[2:]
    body = bytearray()

    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
    if caption:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode("utf-8"))
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode("utf-8"))
    if reply_markup:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"reply_markup\"\r\n\r\n{json.dumps(reply_markup)}\r\n".encode("utf-8"))

    filename = os.path.basename(photo_path)
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
    with open(photo_path, "rb") as f:
        body.extend(f.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    def _build_photo_body(use_html: bool) -> bytes:
        b = bytearray()
        b.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
        if caption:
            # Strip tags for plain text fallback
            cap_text = caption if use_html else re.sub(r"<[^>]+>", "", caption)
            b.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{cap_text[:1024]}\r\n".encode("utf-8"))
            if use_html:
                b.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode("utf-8"))
        if reply_markup:
            b.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"reply_markup\"\r\n\r\n{json.dumps(reply_markup)}\r\n".encode("utf-8"))

        filename = os.path.basename(photo_path)
        b.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
        with open(photo_path, "rb") as f:
            b.extend(f.read())
        b.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return bytes(b)

    req = urllib.request.Request(
        url,
        data=_build_photo_body(use_html=True),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            _safe_console_print(f"[TELEGRAM PHOTO SENT] To {chat_id}: {os.path.basename(photo_path)}")
            return True
    except urllib.error.HTTPError as he:
        # Fallback to plain text caption if HTML parse error occurred
        try:
            req_plain = urllib.request.Request(
                url,
                data=_build_photo_body(use_html=False),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            with urllib.request.urlopen(req_plain, timeout=25) as r:
                _safe_console_print(f"[TELEGRAM PHOTO SENT (PLAIN FALLBACK)] To {chat_id}: {os.path.basename(photo_path)}")
                return True
        except Exception as e2:
            _safe_console_print(f"[PHOTO SEND ERROR] {he} -> fallback failed: {e2}")
            return False
    except Exception as e:
        _safe_console_print(f"[PHOTO SEND ERROR] {e}")
        return False


def send_telegram_status_message(chat_id: str, text: str, bot_token: str | None = None) -> Optional[int]:
    """Send a status message to Telegram and return message_id if available."""
    token = bot_token or BROWSER_BOT_TOKEN or TG_BOT_TOKEN
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode("utf-8", errors="replace"))
            if res.get("ok"):
                return res.get("result", {}).get("message_id")
    except Exception as e:
        _safe_console_print(f"[STATUS MSG SEND ERROR] {e}")
    return None


def edit_telegram_status_message(chat_id: str, message_id: int, text: str, bot_token: str | None = None) -> bool:
    """Edit an existing Telegram message by message_id."""
    token = bot_token or BROWSER_BOT_TOKEN or TG_BOT_TOKEN
    if not token or not message_id:
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True
    except Exception as e:
        _safe_console_print(f"[STATUS MSG EDIT ERROR] {e}")
        return False


BROWSER_CHAT_MEMORY: Dict[str, List[Dict[str, str]]] = {}


def _get_memory_db_path() -> pathlib.Path:
    db_p = ROOT / "data" / "jai_memory.db"
    db_p.parent.mkdir(parents=True, exist_ok=True)
    return db_p


def init_session_state_db(db_path: Optional[pathlib.Path] = None) -> None:
    """Initialize session_state table in data/jai_memory.db with WAL mode."""
    path = db_path or _get_memory_db_path()
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_state (
                    chat_id TEXT PRIMARY KEY,
                    active_domain TEXT,
                    active_url TEXT,
                    page_title TEXT,
                    current_goal TEXT,
                    recent_steps_json TEXT,
                    updated_at REAL
                )
            """)
            conn.commit()
    except Exception as e:
        _safe_console_print(f"[SESSION_STATE INIT ERROR] {e}")


def get_session_state(chat_id: str, db_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Load latest session state frame for chat_id from SQLite."""
    path = db_path or _get_memory_db_path()
    init_session_state_db(path)
    try:
        with sqlite3.connect(str(path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT active_domain, active_url, page_title, current_goal, recent_steps_json, updated_at FROM session_state WHERE chat_id = ?",
                (str(chat_id),),
            )
            row = cursor.fetchone()
            if row:
                recent_steps = []
                try:
                    recent_steps = json.loads(row[4]) if row[4] else []
                except Exception:
                    recent_steps = []
                return {
                    "chat_id": str(chat_id),
                    "active_domain": row[0] or "",
                    "active_url": row[1] or "",
                    "page_title": row[2] or "",
                    "current_goal": row[3] or "",
                    "recent_steps_json": row[4] or "[]",
                    "recent_steps": recent_steps,
                    "updated_at": float(row[5] or 0.0),
                }
    except Exception as e:
        _safe_console_print(f"[SESSION_STATE GET ERROR] {e}")
    return {
        "chat_id": str(chat_id),
        "active_domain": "",
        "active_url": "",
        "page_title": "",
        "current_goal": "",
        "recent_steps_json": "[]",
        "recent_steps": [],
        "updated_at": 0.0,
    }


def save_session_state(
    chat_id: str,
    active_domain: str,
    active_url: str,
    page_title: str,
    current_goal: str,
    recent_steps: list,
    updated_at: float,
    db_path: Optional[pathlib.Path] = None,
) -> None:
    """Persist session state frame for chat_id into SQLite."""
    path = db_path or _get_memory_db_path()
    init_session_state_db(path)
    try:
        steps_json = json.dumps(recent_steps[-3:], ensure_ascii=False)
        with sqlite3.connect(str(path)) as conn:
            conn.execute("""
                INSERT INTO session_state (chat_id, active_domain, active_url, page_title, current_goal, recent_steps_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    active_domain=excluded.active_domain,
                    active_url=excluded.active_url,
                    page_title=excluded.page_title,
                    current_goal=excluded.current_goal,
                    recent_steps_json=excluded.recent_steps_json,
                    updated_at=excluded.updated_at
            """, (str(chat_id), active_domain, active_url, page_title, current_goal, steps_json, float(updated_at)))
            conn.commit()
    except Exception as e:
        _safe_console_print(f"[SESSION_STATE SAVE ERROR] {e}")


def detect_topic_shift(text: str, active_domain: str, last_updated: float = 0.0) -> Tuple[bool, str]:
    """Detect whether user text introduces a new topic/destination vs. continues existing session."""
    now = time.time()
    # 1. 30-minute staleness timeout
    if last_updated > 0 and (now - last_updated > 1800):
        return True, "session_timeout_30m"

    low = text.lower().strip()
    # 2. Explicit website / domain navigation keywords
    nav_pattern = re.compile(
        r"\b(?:open|go\s+to|navigate\s+to|browse\s+to)\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|https?://\S+|gmail|youtube|github|unstop|linkedin|twitter|x\.com)\b",
        re.IGNORECASE,
    )
    match = nav_pattern.search(low)
    if match:
        target = match.group(1).lower()
        if not active_domain:
            return True, f"initial_destination_{target}"
        if (target not in active_domain) and (active_domain not in target):
            return True, f"new_destination_{target}"

    # 3. Known domain switch check (e.g. asking about unstop or github when active session is google)
    known_domains = ("unstop", "github", "leetcode", "youtube", "gmail", "amazon", "google", "linkedin", "twitter", "x.com", "hive", "smartinterviews")
    for kd in known_domains:
        if kd in low:
            if active_domain and (kd not in active_domain) and (active_domain not in kd):
                return True, f"domain_switch_{kd}"

    return False, "continuation"


def parse_browser_intent(cmd: str) -> Dict[str, Any]:
    """Parse incoming browser user text into direct URL or autonomous AgentLoop action."""
    low = (cmd or "").strip().lower()
    has_compound_or_actions = bool(re.search(
        r"\b(?:and|then|check|search|find|extract|read|audit|fill|submit|click|look|see|inspect|verify|get)\b",
        low
    ))

    # 1. Pure Deep-Link Normalization
    deep_link_clean = re.sub(r"^(?:open|go\s+to|visit)\s+", "", low).strip()
    is_pure_deep_link = (
        not has_compound_or_actions
        and (
            deep_link_clean in (
                "youtube history", "yt history",
                "unstop registrations", "unstop registration", "unstop activity",
                "hive", "the hive", "thehive", "hive interviews", "hive interviws",
                "smart interviews", "smartinterviews", "smart interviews hive"
            )
            or any(k in deep_link_clean for k in ("hive", "smartinterview"))
        )
    )
    if is_pure_deep_link:
        from core.adapter_registry import resolve_canonical_deep_link
        target_url = resolve_canonical_deep_link(deep_link_clean)
        if target_url:
            return {"route": "direct_url", "url": target_url, "type": "deep_link"}

    # 2. Strict Fast-Path Direct URL Navigation:
    fast_url_match = re.match(r"^(?:open|go\s+to|visit)\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)$", cmd.strip(), re.IGNORECASE)
    raw_url_match = re.match(r"^https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?$", cmd.strip(), re.IGNORECASE)

    if (fast_url_match or raw_url_match) and not has_compound_or_actions:
        raw_target = fast_url_match.group(1).strip() if fast_url_match else cmd.strip()
        target_url = raw_target if raw_target.startswith(("http://", "https://")) else f"https://{raw_target}"
        return {"route": "direct_url", "url": target_url, "type": "fast_path"}

    # 3. Simple Search Prefix
    if low.startswith("search ") and not has_compound_or_actions:
        q = cmd[7:].strip()
        target_url = (
            f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
            if "youtube" in low
            else f"https://www.google.com/search?q={urllib.parse.quote(q)}"
        )
        return {"route": "direct_url", "url": target_url, "type": "search", "query": q}

    return {"route": "agent_loop", "task": cmd.strip()}


def handle_browser_task(chat_id: str, text: str):
    """Execute a task received from @jai_browser_bot with rolling conversational memory."""
    token = BROWSER_BOT_TOKEN or TG_BOT_TOKEN
    cmd = (text or "").strip()
    low = cmd.lower()

    # Memory reset
    if low in ("/reset", "/clear", "/new", "new", "reset memory", "clear memory"):
        BROWSER_CHAT_MEMORY[str(chat_id)] = []
        save_session_state(str(chat_id), "", "", "", "", [], 0.0)
        _post_telegram_api(chat_id, "🧹 <b>Conversational memory cleared.</b>", parse_mode="HTML", bot_token=token)
        return

    # Missing fact resumption check
    missing_session = ACTIVE_MISSING_FACT_SESSIONS.get(str(chat_id))
    if missing_session and low not in ("/reset", "/clear", "/new", "new", "/stop", "stop", "halt", "cancel"):
        task_id = missing_session.get("task_id")
        pending_fields = missing_session.get("pending_fields") or []
        ACTIVE_MISSING_FACT_SESSIONS.pop(str(chat_id), None)

        provided_facts = {}
        for line in cmd.splitlines():
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                provided_facts[k.strip()] = v.strip()
            elif "=" in line:
                k, v = line.split("=", 1)
                provided_facts[k.strip()] = v.strip()

        if not provided_facts:
            if pending_fields:
                provided_facts[pending_fields[0]] = cmd.strip()
            else:
                provided_facts["user_fact"] = cmd.strip()

        _post_telegram_api(chat_id, "📝 <b>Fact recorded. Resuming form...</b>", parse_mode="HTML", bot_token=token)
        try:
            from src.agent.loop import AgentLoop, BROWSER_AGENT_TOOLS
            loop = AgentLoop(max_steps=12, allowed_tools=BROWSER_AGENT_TOOLS)
            res = loop.learn_and_resume_form(task_id, provided_facts)
            _post_telegram_api(chat_id, "✅ <b>Form resumed and facts saved.</b>", parse_mode="HTML", bot_token=token)
            return
        except Exception as ex:
            _post_telegram_api(chat_id, f"⚠️ Error resuming form: {ex}", parse_mode="HTML", bot_token=token)
            return

    # Task Stop / Cancel
    if low in ("/stop", "stop", "halt", "cancel"):
        stopped = False
        for ke in list(ACTIVE_BROWSER_KILL_SWITCHES.values()):
            ke.set()
            stopped = True
        stop_file = _REPO_ROOT / "data" / "STOP"
        try:
            stop_file.touch()
        except Exception as _suppressed_exc:
            logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
        _post_telegram_api(chat_id, "🛑 <b>Browser task stopped.</b>" if stopped else "No active browser task running.", parse_mode="HTML", bot_token=token)
        return

    # Ensure any previous stop marker is removed when receiving a fresh non-stop command
    stale_stop = _REPO_ROOT / "data" / "STOP"
    if stale_stop.exists():
        try:
            stale_stop.unlink(missing_ok=True)
        except Exception as _stop_clean_err:
            logger.debug('[telegram] Could not clear stale stop file: %s', _stop_clean_err)

    # 1. System / Status
    if low in ["status", "battery", "/status"]:
        hw = get_live_hardware_status()
        tabs_msg = ""
        try:
            from src.jai_browser import get_open_tabs_info, is_running
            if is_running():
                tabs = get_open_tabs_info()
                if isinstance(tabs, list):
                    tabs_msg = f"\n• <b>Open Tabs:</b> {len(tabs)}"
            else:
                tabs_msg = "\n• <b>Chrome:</b> Not connected to port 9222"
        except Exception as _suppressed_exc:
            logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
        _post_telegram_api(chat_id, f"💻 <b>Workstation Status</b>\n\n{hw}{tabs_msg}", parse_mode="HTML", bot_token=token)
        return

    # 2. History & Bookmarks Search
    if low.startswith("history ") or low == "recent" or low == "history":
        from src.jai_browser import search_chrome_history, get_recent_history
        query = cmd[8:].strip() if low.startswith("history ") else ""
        items = search_chrome_history(query, limit=5) if query else get_recent_history(limit=5)
        if not items:
            _post_telegram_api(chat_id, f"🔍 No history found matching <code>{html.escape(query)}</code>.", parse_mode="HTML", bot_token=token)
            return
        msg = f"🔍 <b>Chrome History ({'query: ' + html.escape(query) if query else 'Recent'})</b>\n\n"
        for idx, item in enumerate(items, 1):
            title = html.escape(item['title'][:55])
            u = item['url']
            time_str = f" <i>({item['time']})</i>" if item.get('time') else ""
            msg += f"{idx}. <a href=\"{u}\">{title}</a>{time_str}\n"
        _post_telegram_api(chat_id, msg, parse_mode="HTML", bot_token=token)
        return

    # 3. Tab Listing
    if low in ["tabs", "list tabs", "show tabs", "/tabs"]:
        from src.jai_browser import get_open_tabs_info, is_running
        if not is_running():
            _post_telegram_api(chat_id, "⚠️ Chrome is not running on port 9222. Start Chrome using 'Launch_JAI_Chrome.bat'.", parse_mode="HTML", bot_token=token)
            return
        tabs = get_open_tabs_info()
        if isinstance(tabs, str):
            _post_telegram_api(chat_id, tabs, parse_mode="HTML", bot_token=token)
            return
        msg = f"📑 <b>Open Tabs ({len(tabs)})</b>\n\n"
        for t in tabs[:10]:
            marker = "🟢 " if t.get("active") else "⚪ "
            msg += f"{marker}[{t['index']}] <a href=\"{t['url']}\">{html.escape(t['title'][:40])}</a>\n"
        _post_telegram_api(chat_id, msg, parse_mode="HTML", bot_token=token)
        return

    # 4. Tab-scoped commands (@) or smart tab management
    if cmd.startswith("@") or low in ("tidy tabs", "tidy up tabs", "close duplicate tabs", "close duplicates", "group tabs", "close other tabs"):
        try:
            import src.browser_commands as bc
            bc_res = bc.handle_browser_command(cmd)
            if bc_res and bc_res.get("reply"):
                _post_telegram_api(chat_id, bc_res["reply"], bot_token=token)
                return
        except Exception as bc_err:
            _safe_console_print(f"[BROWSER COMMAND DELEGATION ERROR] {bc_err}")

    # Standard browser actions & checking Chrome connection
    from src.jai_browser import (
        is_running, ensure_browser_running, open_url, scroll_page,
        close_tab_at_index, capture_screenshot
    )

    try:
        ensure_browser_running(timeout_seconds=5)
    except Exception as e:
        logger.error(f"[TELEGRAM BROWSER TASK] ensure_browser_running failed under Python ({sys.executable}): {e}")
        _post_telegram_api(
            chat_id,
            f"⚠️ <b>Chrome Connection Error</b>\n\n{html.escape(str(e))}\n\n"
            "<i>Tip: Start Chrome using 'Launch_JAI_Chrome.bat' so port 9222 is active with your real profile.</i>",
            parse_mode="HTML", bot_token=token
        )
        return

    # Read-Only Visual / Inspection Fast-Path: Bypass AgentLoop completely for observation queries
    if _READONLY_PAGE_QUERY_RE.match(low.strip()):
        from src.jai_browser import capture_screenshot, read_page_text, extract_universal_page_content
        screenshot_path = capture_screenshot()

        page_text = ""
        try:
            pt = read_page_text(max_chars=3000)
            if pt and not str(pt).startswith("Error"):
                page_text = str(pt).strip()
        except Exception as _pt_err:
            logger.debug("[TELEGRAM READONLY] read_page_text error: %s", _pt_err)

        if not page_text:
            try:
                uc = extract_universal_page_content()
                page_text = str(getattr(uc, "text", "") or getattr(uc, "content", "") or "").strip()
            except Exception as _uc_err:
                logger.debug("[TELEGRAM READONLY] extract_universal_page_content error: %s", _uc_err)

        prompt_text = f"Answer the user's question about this browser tab: {cmd}\n\nExtracted page text:\n{page_text[:2000]}"
        description = ""

        # Read image bytes
        image_bytes = b""
        if screenshot_path and os.path.exists(screenshot_path):
            try:
                image_bytes = Path(screenshot_path).read_bytes()
            except Exception as _read_err:
                logger.debug("[TELEGRAM READONLY] Screenshot file read error: %s", _read_err)

        if image_bytes:
            try:
                from src.llm_router import route_image
                img_res = route_image(image_bytes=image_bytes, prompt=prompt_text, mime_type="image/png")
                if img_res and getattr(img_res, "success", False) and getattr(img_res, "text", ""):
                    description = img_res.text.strip()
            except Exception as _img_err:
                logger.debug("[TELEGRAM READONLY] route_image failed: %s", _img_err)

        if not description:
            try:
                from src.llm_router import route_query
                q_res = route_query(prompt_text)
                if q_res and getattr(q_res, "success", False) and getattr(q_res, "text", ""):
                    description = q_res.text.strip()
                elif q_res and getattr(q_res, "text", ""):
                    description = q_res.text.strip()
            except Exception as _q_err:
                logger.debug("[TELEGRAM READONLY] route_query fallback failed: %s", _q_err)

        if not description:
            description = "Here is what is currently visible on the active page."

        # Send screenshot and description directly to Telegram
        sent = False
        if screenshot_path and os.path.exists(screenshot_path):
            if len(description) <= 1024:
                sent = send_telegram_photo(chat_id, screenshot_path, caption=description, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
            else:
                sent = send_telegram_photo(chat_id, screenshot_path, caption=description[:1000] + "...", reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
                _post_telegram_api(chat_id, description, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)

        if not sent:
            _post_telegram_api(chat_id, description, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)

        return

    # Tier 1 Instant Handlers: Explicit open, search prefix, scroll, close
    if low in ["scroll down", "scroll"]:
        scroll_page("down")
        result_text = "⬇️ Scrolled down"
        screenshot_path = capture_screenshot()
        sent = send_telegram_photo(chat_id, screenshot_path, caption=result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        return

    if low in ["scroll up"]:
        scroll_page("up")
        result_text = "⬆️ Scrolled up"
        screenshot_path = capture_screenshot()
        sent = send_telegram_photo(chat_id, screenshot_path, caption=result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        return

    if low in ["close tab", "close"]:
        close_tab_at_index(-1)
        result_text = "❌ Closed active tab"
        _post_telegram_api(chat_id, result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        return

    # -----------------------------------------------------------------------
    # Hardened Intent Router: Strict Fast-Path URL vs. Compound AgentLoop Flow
    # -----------------------------------------------------------------------
    intent = parse_browser_intent(cmd)
    if intent.get("route") == "direct_url":
        target_url = intent["url"]
        from src.jai_browser import open_url, capture_screenshot
        open_url(target_url)
        q = intent.get("query")
        result_text = f"🔎 Searched: {q}" if q else f"🌐 Opened: {target_url}"
        screenshot_path = capture_screenshot()
        sent = send_telegram_photo(chat_id, screenshot_path, caption=result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, result_text, reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        return

    # 4. Unstop 1-Shot Concurrent Multi-Page Fast-Path Bypass:
    # Directly extract all entries across Pages 1-5 in a single turn without 12-step AgentLoop overhead
    if "unstop" in low and any(k in low for k in ("all", "full report", "all pages", "every competition")):
        from src.jai_browser import open_url, get_active_url, harvest_all_unstop_pages_at_once
        unstop_all_url = "https://unstop.com/user/registrations/all/all"
        curr_u = (get_active_url() or "").lower()
        if "unstop.com/user/registrations" not in curr_u:
            open_url(unstop_all_url)
            time.sleep(1.0)

        table_md = harvest_all_unstop_pages_at_once(max_pages=5)
        ans = markdown_to_telegram_html(table_md)
        sent = _post_telegram_api(chat_id, ans, parse_mode="HTML", reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, table_md[:4000], reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        return

    # Tier 2: Autonomous AgentLoop Dispatch with Conversational Context & Topic-Aware Session State
    chat_key = str(chat_id)
    history_turns = BROWSER_CHAT_MEMORY.get(chat_key, [])

    # 1. Fetch live active tab ground truth
    active_u = ""
    active_domain = ""
    active_title = ""
    try:
        from src.jai_browser import get_active_url, get_foreground_page, browser_get_title
        active_u = get_active_url() or ""
        if active_u and not any(k in active_u.lower() for k in ("about:blank", "newtab")):
            parsed = urllib.parse.urlparse(active_u)
            active_domain = (parsed.hostname or "").lower()
            fp = get_foreground_page()
            if fp:
                active_title = browser_get_title(fp) or ""
    except Exception as _suppressed_exc:
        logger.debug('[telegram] Handled Exception: %s', _suppressed_exc, exc_info=True)
    # 2. Topic-Aware Session State Frame & Topic Shift Detection
    state = get_session_state(chat_key)
    dom_reference = active_domain or state.get("active_domain", "")
    is_shift, shift_reason = detect_topic_shift(cmd, dom_reference, state.get("updated_at", 0.0))

    if is_shift:
        _safe_console_print(f"[SESSION_STATE] Topic shift detected ({shift_reason}) for {chat_key}. Flushing previous context.")
        current_goal = ""
        recent_steps = []
        BROWSER_CHAT_MEMORY[chat_key] = []
    else:
        current_goal = state.get("current_goal", "")
        recent_steps = state.get("recent_steps", [])

    context_lines = []
    dom_label = active_domain or state.get("active_domain", "")
    url_label = active_u or state.get("active_url", "")
    title_label = active_title or state.get("page_title", "")

    # Inject compact [SESSION CONTEXT] header (<300 tokens)
    if dom_label or current_goal or recent_steps:
        context_lines.append("[SESSION CONTEXT]")
        if dom_label:
            context_lines.append(f"Active Domain: {dom_label}")
        if url_label:
            context_lines.append(f"Active URL: {url_label}")
        if title_label:
            context_lines.append(f"Page Title: {title_label}")
        if current_goal:
            context_lines.append(f"Current Goal: {current_goal}")
        if recent_steps:
            context_lines.append("Recent Steps:")
            for s in recent_steps[-3:]:
                context_lines.append(f"  • {s}")
        context_lines.append("")

    if history_turns and not is_shift:
        context_lines.append("Recent Conversation History:")
        for turn in history_turns[-4:]:
            context_lines.append(f"  {turn['role']}: {turn['text']}")

    conv_context = "\n".join(context_lines).strip()

    acquired = BROWSER_TASK_LOCK.acquire(blocking=False)
    if not acquired:
        stop_markup = {
            "inline_keyboard": [
                [{"text": "🛑 Stop Active Task", "callback_data": "stop_browser_task"}]
            ]
        }
        _post_telegram_api(
            chat_id,
            "⚠️ <b>Browser is currently busy</b> executing another task.\nPlease wait for it to complete or stop it below.",
            parse_mode="HTML",
            reply_markup=stop_markup,
            bot_token=token,
        )
        return

    kill_event = threading.Event()
    ACTIVE_BROWSER_KILL_SWITCHES[str(chat_id)] = kill_event

    status_msg_id = send_telegram_status_message(
        chat_id, "🧠 <b>JAI Browser Agent</b> analyzing task...", bot_token=token
    )
    last_edit_time = 0.0

    def on_step(step_id: int, thought: str, action_desc: str):
        nonlocal last_edit_time
        now = time.time()
        if status_msg_id and (now - last_edit_time >= 1.5):
            th_clean = (thought or "").strip()
            # Truncate thought cleanly if long
            if len(th_clean) > 160:
                th_clean = th_clean[:157] + "..."
            short_thought = html.escape(th_clean)
            short_action = html.escape((action_desc or "")[:90])
            if short_thought and short_action:
                status_text = (
                    f"⏳ <b>Step {step_id}:</b> <code>{short_action}</code>\n\n"
                    f"💭 <i>{short_thought}</i>"
                )
            elif short_thought:
                status_text = f"⏳ <b>Step {step_id}</b>\n\n💭 <i>{short_thought}</i>"
            else:
                status_text = f"⏳ <b>Step {step_id}:</b> <code>{short_action}</code>"

            if edit_telegram_status_message(chat_id, status_msg_id, status_text, bot_token=token):
                last_edit_time = now

    try:
        from src.agent.loop import AgentLoop, BROWSER_AGENT_TOOLS
        loop = AgentLoop(
            max_steps=12,
            max_wall_time_sec=90.0,
            allowed_tools=BROWSER_AGENT_TOOLS,
            conversation_context=conv_context,
            kill_event=kill_event,
        )
        run_res = loop.run(task=cmd, context=conv_context, on_step=on_step)

        # Record conversational memory
        if chat_key not in BROWSER_CHAT_MEMORY:
            BROWSER_CHAT_MEMORY[chat_key] = []
        BROWSER_CHAT_MEMORY[chat_key].append({"role": "User", "text": cmd})
        reply_snip = (run_res.final_answer or run_res.message or "Task finished")[:250]
        BROWSER_CHAT_MEMORY[chat_key].append({"role": "Assistant", "text": reply_snip})
        if len(BROWSER_CHAT_MEMORY[chat_key]) > 12:
            BROWSER_CHAT_MEMORY[chat_key] = BROWSER_CHAT_MEMORY[chat_key][-12:]

        # Update persistent session state with last 3 turn outcomes
        last_3_steps = []
        if run_res.episodic_ledger:
            last_3_steps = [s.strip() for s in run_res.episodic_ledger[-3:]]
        elif run_res.steps:
            last_3_steps = [f"{s.action}: {str(s.observation)[:70]}" for s in run_res.steps[-3:]]

        save_session_state(
            chat_id=chat_key,
            active_domain=active_domain or dom_label,
            active_url=active_u or url_label,
            page_title=active_title or title_label,
            current_goal=cmd,
            recent_steps=last_3_steps,
            updated_at=time.time(),
        )

        # 0. Intercept Suspended Missing Fact
        if run_res.status == "SUSPENDED_MISSING_FACT":
            ACTIVE_MISSING_FACT_SESSIONS[str(chat_id)] = {
                "task_id": run_res.suspended_task_id,
                "pending_fields": getattr(run_res, "missing_fields", None) or [],
                "time": time.time(),
            }
            prompt_text = run_res.message or "Missing profile details needed to proceed."
            _post_telegram_api(chat_id, f"📋 <b>Missing Profile Details:</b>\n\n{html.escape(prompt_text)}\n\n<i>Reply directly to provide the missing details and resume.</i>", parse_mode="HTML", bot_token=token)
            return

        # 1. Paused for Rule 0 approval or human login/CAPTCHA
        is_paused = (
            run_res.status in ("REQUIRE_APPROVAL", "APPROVAL_REQUIRED", "approval_required", "suspended", "SUSPENDED_HUMAN_GATE", "AWAITING_APPROVAL")
            or bool(run_res.pending_action)
            or bool(run_res.gate_type)
        )
        if is_paused:
            pause_reason = run_res.message or run_res.error or f"Action requires approval: {run_res.pending_action}"
            pause_text = f"🔒 <b>Action Paused: Approval / Interaction Required</b>\n\n{html.escape(pause_reason)}"
            screenshot_path = capture_screenshot()
            approval_id = run_res.suspended_task_id or str(uuid.uuid4())[:8]
            try:
                from src.telegram_approval import DB_PATH
                with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS pending_approvals (
                            approval_id TEXT PRIMARY KEY,
                            action_name TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            status TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            expires_at REAL NOT NULL
                        );
                    """)
                    conn.execute(
                        "INSERT OR REPLACE INTO pending_approvals (approval_id, action_name, summary, status, created_at, expires_at) VALUES (?, ?, ?, 'PENDING', ?, ?)",
                        (approval_id, str(run_res.pending_action or "browser_action"), pause_reason[:200], time.time(), time.time() + 600)
                    )
                    conn.commit()
            except Exception as e:
                _safe_console_print(f"[APPROVAL DB ERROR] {e}")

            approval_markup = get_approval_keyboard(approval_id)
            sent = send_telegram_photo(chat_id, screenshot_path, caption=pause_text[:1024], reply_markup=approval_markup, bot_token=token)
            if not sent:
                _post_telegram_api(chat_id, pause_text, parse_mode="HTML", reply_markup=approval_markup, bot_token=token)
            return

        # 2. Success
        if run_res.success:
            ans = markdown_to_telegram_html(run_res.final_answer or "Task completed successfully.")
            # Flaw 5: Send screenshot on first view / task completion
            screenshot_path = None
            if hasattr(run_res, "first_view_screenshots") and run_res.first_view_screenshots:
                screenshot_path = list(run_res.first_view_screenshots.values())[0]
            if not screenshot_path:
                screenshot_path = capture_screenshot()
            sent = send_telegram_photo(chat_id, screenshot_path, caption=ans[:1024], reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
            if not sent:
                _post_telegram_api(chat_id, ans, parse_mode="HTML", reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
            return

        # 3. Failed / Timed out
        err_msg = run_res.error or "Agent task could not be completed."
        step_count = run_res.total_steps if (hasattr(run_res, "total_steps") and run_res.total_steps) else min(len(run_res.steps), 12)
        fail_caption = f"⚠️ <b>Task Incomplete ({step_count} steps):</b>\n{html.escape(err_msg)}"
        screenshot_path = capture_screenshot()
        sent = send_telegram_photo(chat_id, screenshot_path, caption=fail_caption[:1024], reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, fail_caption, parse_mode="HTML", reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)

    except Exception as ex:
        _safe_console_print(f"[BROWSER AGENT LOOP ERROR] {ex}")
        screenshot_path = capture_screenshot()
        err_cap = f"❌ <b>Agent Execution Error:</b> {html.escape(str(ex))}"
        sent = send_telegram_photo(chat_id, screenshot_path, caption=err_cap[:1024], reply_markup=BROWSER_INLINE_KEYBOARD, bot_token=token)
        if not sent:
            _post_telegram_api(chat_id, err_cap, parse_mode="HTML", bot_token=token)
    finally:
        ACTIVE_BROWSER_KILL_SWITCHES.pop(str(chat_id), None)
        try:
            BROWSER_TASK_LOCK.release()
        except RuntimeError as _suppressed_exc:
            logger.debug('[telegram] Handled RuntimeError: %s', _suppressed_exc, exc_info=True)
if __name__ == "__main__":
    main()
