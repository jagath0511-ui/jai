"""
src/action_gateway.py - Declarative Action Gateway & Security Policy Engine.
========================================================================
Canonical implementation of ActionGateway, RiskLevel, Decision, PolicyRule,
and audit logging. Inspired by OpenBot's Common Expression Language (CEL)
Action Gateway and Meta Muse AI's 6-Layer Security Architecture.

Enforces:
1. Deny-by-default on unclassified destructive operations.
2. Hard approval gates for irreversible actions (submitting assignments, checkout, deleting files).
3. Automatic passthrough for safe, read-only perception (screen OCR, DOM reading, drafting).
4. Prompt-injection defense: blocks rogue attempts to execute system-level destructive commands.
5. Audit trail: appends all evaluated actions to `data/action_gateway_audit.jsonl`.

Guarantees single source of truth across import styles:
    import action_gateway is import src.action_gateway
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("jai.action_gateway")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [ActionGateway] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Ensure repository root is on sys.path so sibling imports (config, data, etc.) resolve
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Validated runtime approval tokens for resumed actions
_VALIDATED_APPROVAL_TOKENS: set[str] = set()


def register_system_approval_token(token: str) -> None:
    """Register a cryptographically random or system approval token when human explicitly approves."""
    if token:
        _VALIDATED_APPROVAL_TOKENS.add(token)


def create_system_approval_token() -> str:
    """Create and register a secure single-use approval token."""
    token = str(uuid.uuid4())
    _VALIDATED_APPROVAL_TOKENS.add(token)
    return token


try:
    from src import config
    DATA_DIR: Path = getattr(config, "DATA_DIR", _REPO_ROOT / "data")
except Exception:
    try:
        import config
        DATA_DIR = getattr(config, "DATA_DIR", _REPO_ROOT / "data")
    except Exception:
        DATA_DIR = _REPO_ROOT / "data"

AUDIT_LOG_PATH = DATA_DIR / "action_gateway_audit.jsonl"
DB_PATH = DATA_DIR / "jai_memory.db"
BASE_DIR = _REPO_ROOT

import sqlite3
import time


try:
    from src.telegram_approval import init_approval_db, resolve_approval
except ImportError:
    from telegram_approval import init_approval_db, resolve_approval


RING_0_SAFE_TOOLS: set[str] = {
    "browser_get_snapshot",
    "browser_read_active_tab",
    "browser_get_text_by_role",
    "browser_read_page",
    "browser_screenshot",
    "file_read",
    "web_search",
    "calculate",
    "tab_read_partition",
    "browser_find_elements",
    "browser_read_network_json",
    "browser_extract_task_context",
    "browser.extract_task_context",
    "browser_extract_cards",
    "browser.extract_cards",
    "browser_extract_paginated_cards",
    "browser.extract_paginated_cards",
    "harvest_paginated_data",
    "harvest_all_unstop_pages",
    "tool_harvest_all_unstop_pages",
    "browser_extract_collection",
    "browser_extract_generic_collection",
    "universal_page_content",
    "extract_universal_page_content",
    "browser_extract_universal_content",
    "tool_browser_extract_collection",
    "browser_hover",
    "tool_browser_hover",
    "browser_go_back",
    "tool_browser_go_back",
    "browser_go_forward",
    "tool_browser_go_forward",
    "browser_switch_tab",
    "tool_browser_switch_tab",
    "browser_close_tab",
    "tool_browser_close_tab",
    "browser_wait",
    "tool_browser_wait",
    "browser_wait_for",
    "open_shadow_tab",
    "tool_open_shadow_tab",
    "get_axtree_snapshot",
    "tool_get_axtree_snapshot",
    "read_all_tabs",
    "tool_read_all_tabs",
    "tool_browser_wait_for",
    "browser.wait_for",
    "wait_for",
}

RING_1_FORM_SUBMITS: set[str] = {
    "submit",
    "browser_submit",
    "submit_form",
    "browser_submit_form",
    "form_submit",
    "browser_form_submit",
}

RING_1_PAYMENTS: set[str] = {
    "pay",
    "authorize_payment",
    "checkout",
    "buy",
    "transfer_money",
    "purchase",
    "place_order",
}

RING_1_DELETIONS: set[str] = {
    "delete_account",
    "close_account",
    "cancel_account",
    "delete",
    "file_delete",
    "remove_account",
    "purge",
    "truncate",
    "drop",
}

RING_1_FILE_UPLOADS: set[str] = {
    "upload_file",
    "file_upload",
    "browser_upload_file",
    "upload",
}

RING_1_EMAIL_DISPATCH: set[str] = {
    "gmail_send_message",
    "send_email",
    "send_mail",
    "email_send",
    "dispatch_email",
    "send_message",
}


class RiskLevel(str, Enum):
    LOW = "low"          # Read-only perception, navigation, non-destructive drafting
    MEDIUM = "medium"    # Temporary local state, non-permanent clicks
    HIGH = "high"        # Irreversible: form submits, payments, deletes, file writes outside vault
    FATAL = "fatal"      # System destructive: formatting, credential exfiltration, terminal forkbombs


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class EvaluationResult(tuple):
    def __new__(cls, decision: Decision, risk: RiskLevel, reason: str):
        return super().__new__(cls, (decision, risk, reason))

    @property
    def decision(self) -> Decision:
        return self[0]

    @property
    def risk(self) -> RiskLevel:
        return self[1]

    @property
    def risk_level(self) -> RiskLevel:
        return self[1]

    @property
    def reason(self) -> str:
        return self[2]

    def __eq__(self, other):
        if isinstance(other, Decision):
            return self[0] == other
        if isinstance(other, str):
            return self[0].value == other or self[0] == other
        return super().__eq__(other)

    def __hash__(self):
        return super().__hash__()

    def __repr__(self):
        return f"EvaluationResult(decision={self[0]}, risk={self[1]}, reason='{self[2]}')"



class ActionSecurityError(PermissionError):
    """Raised when an automated agent action is explicitly denied by policy."""
    pass


class ActionApprovalRequiredError(PermissionError):
    """Raised when an automated agent action requires explicit human confirmation."""
    def __init__(self, action_name: str, reason: str, details: Optional[Dict[str, Any]] = None, approval_id: Optional[str] = None):
        super().__init__(f"Action '{action_name}' halted: {reason}")
        self.action_name = action_name
        self.reason = reason
        self.details = details or {}
        self.approval_id = approval_id or (self.details.get("approval_id") if isinstance(self.details, dict) else None) or str(uuid.uuid4())[:8]


@dataclass
class PolicyRule:
    """Declarative security rule matching an action pattern."""
    action_pattern: str
    decision: Decision
    risk_level: RiskLevel
    reason: str
    condition_fn: Optional[Callable[[Dict[str, Any]], bool]] = None
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# JAI Browser Rules (docs/JAI_BROWSER_RULES.md)
# ---------------------------------------------------------------------------

FINAL_STEP_KEYWORDS: List[str] = [
    # Submit, apply, register, agree, book, confirm, complete
    "send", "submit", "apply", "register", "book", "confirm", "accept", "agree", "turn in", "complete",
    # Financial / Transactions
    "pay", "pay now", "proceed to pay", "continue to payment", "confirm booking", "buy", "place order", "place an order",
    "transfer money", "subscribe", "checkout", "submit application", "donate",
    # Social / Communications
    "post", "publish", "share", "comment", "reply", "tweet",
    # Deletion / Cancellation / Account
    "delete", "remove", "cancel", "unsubscribe", "close account", "close an account",
]

FINAL_STEP_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FINAL_STEP_KEYWORDS) + r")\b",
    re.IGNORECASE
)

WIZARD_NAV_KEYWORDS: List[str] = [
    "next", "continue", "save & continue", "save and continue", "save & next", "save and next",
    "proceed", "proceed to passenger details", "select seat", "choose seat", "search",
    "step 1", "step 2", "step 3", "verify", "back", "previous",
    # Authentication & Google SSO navigation (Authorized by user)
    "sign in", "signin", "log in", "login",
    "continue with google", "log in with google", "sign in with google", "choose an account"
]

WIZARD_NAV_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in WIZARD_NAV_KEYWORDS) + r")\b",
    re.IGNORECASE
)

SENSITIVE_FIELD_REGEX = re.compile(
    r"(password|passcode|\botp\b|verification[_\s]*code|card[_\s]*number|\bcvv\b|\bcvc\b|"
    r"bank[_\s]*details|iban|routing[_\s]*number|\bssn\b|aadhar|aadhaar|pan[_\s]*card|"
    r"govt[_\s]*id|government[_\s]*id|\bpin\b)",
    re.IGNORECASE
)



SENSITIVE_DOMAINS: Tuple[str, ...] = (
    "mail.google.com",
    "drive.google.com",
    "github.com",
    "accounts.google.com",
    "chase.com",
    "wellsfargo.com",
    "fidelity.com",
)


def get_hostname(url_or_domain: str) -> str:
    """Extract normalized lowercase hostname from a URL or domain string."""
    if not url_or_domain:
        return ""
    s = str(url_or_domain).strip().lower()
    if "://" not in s:
        s = "http://" + s
    try:
        parsed = urllib.parse.urlparse(s)
        return (parsed.hostname or "").lower()
    except Exception:
        return s.split("/")[0].split("?")[0].split(":")[0].lower()


NON_BANK_WORDS = ("data", "power", "food", "blood", "sperm", "seed", "toy", "memory", "cloud", "river", "west")

def is_sensitive_origin(url_or_domain: str) -> bool:
    """Check if a URL or domain belongs to sensitive personal services.
    
    Fail-closed policy: Returns True on any parse error or malformed URL.
    """
    if not url_or_domain:
        return False
    s = str(url_or_domain).strip()
    if not s:
        return False

    try:
        to_parse = s if "://" in s else "http://" + s
        parsed = urllib.parse.urlsplit(to_parse)
        # Accessing parsed.port forces validation of the port range (raises ValueError if out of range)
        _ = parsed.port
        raw_host = parsed.hostname
        if not raw_host:
            # If an explicit URL or domain was provided but cannot produce a host, fail closed
            if not s.startswith("/") and not s.startswith("#"):
                return True
            return False
        host = raw_host.rstrip(".").lower()
    except Exception:
        # Fail closed on URL parse errors
        return True

    for sd in SENSITIVE_DOMAINS:
        if host == sd or host.endswith("." + sd):
            return True

    # Filter out false-positive non-banking words ending in 'bank'
    for nb in NON_BANK_WORDS:
        if re.search(rf"(^|\.){nb}bank\.[a-z]+$", host):
            return False

    # Match exact bank domains or subdomains, NOT substrings like databank.org or powerbank.com
    if (
        host.startswith("bank.")
        or host.startswith("bank-")
        or host.startswith("bankof")
        or host.endswith(".bank")
        or bool(re.search(r"(^|\.)[a-z0-9-]+bank\.[a-z]+$", host))
    ):
        return True
    return False


SAFE_NAV_REGEX = re.compile(
    r"\b(login|sign\s*in|signin|my\s*activity|registrations?|my\s*registrations?|profile|search|filter|browse|explore|view|details|activity|run|test|compile|console|next|next\s*page|prev|previous|prev\s*page)\b",
    re.IGNORECASE
)

FINANCIAL_MUTATION_REGEX = re.compile(
    r"\b(pay|pay\s+now|proceed\s+to\s+pay|continue\s+to\s+payment|buy|checkout|purchase|transfer|subscribe|donate|place\s+order|place\s+an\s+order)\b",
    re.IGNORECASE
)

DESTRUCTIVE_MUTATION_REGEX = re.compile(
    r"\b(delete|remove|revoke|cancel\s+registration|close\s+account)\b",
    re.IGNORECASE
)

FINAL_SUBMISSION_REGEX = re.compile(
    r"\b(submit|apply\s+now|confirm\s+order|confirm|complete|book|register|send|publish|tweet|post|reply)\b",
    re.IGNORECASE
)


def is_browser_final_step(args: Dict[str, Any]) -> bool:
    """Recognises whether a browser action constitutes an irreversible final mutation requiring human approval.
    
    Checks:
    1. Distinguishes safe navigation, search, reading, and wizard progression from irreversible final mutations.
    2. Enforces REQUIRE_APPROVAL strictly on Financial, Destructive, or Final Submit mutations.
    3. Handles submit-type buttons inside <form>.
    """
    text_to_check = " ".join(
        str(v) for k, v in args.items()
        if k in {"text", "label", "button_text", "aria_label", "name", "action", "key", "target"}
    )
    if not text_to_check and args:
        text_to_check = " ".join(str(v) for v in args.values())

    tag = str(args.get("tag", "")).lower()
    role = str(args.get("role", "")).lower()
    is_anchor = (tag == "a") or (role == "link")
    href = str(args.get("href", "") or args.get("url", "") or args.get("target", "")).lower()

    # Benign navigation & reading labels: always ALLOW unless explicitly flagged financial/destructive
    BENIGN_NAV_PHRASES = (
        "order history", "past orders", "my orders", "view orders",
        "download pdf", "download csv", "download report", "download file",
        "apply filter", "apply filters", "filter by",
        "blog post", "view post", "read post", "post details", "forum post",
        "run code", "test code", "run tests", "run test", "compile", "console",
    )
    lower_text = text_to_check.lower()
    if any(phrase in lower_text for phrase in BENIGN_NAV_PHRASES):
        if not bool(args.get("is_financial")) and not bool(args.get("is_destructive")) and not bool(FINAL_SUBMISSION_REGEX.search(text_to_check)):
            return False

    is_safe_nav = bool(SAFE_NAV_REGEX.search(text_to_check))
    is_wizard_nav = bool(WIZARD_NAV_REGEX.search(text_to_check)) or is_safe_nav

    # 1. Financial / Checkout mutations
    if bool(args.get("is_financial")) or bool(FINANCIAL_MUTATION_REGEX.search(text_to_check)):
        return True

    # 2. Destructive mutations
    if bool(DESTRUCTIVE_MUTATION_REGEX.search(text_to_check)) or (is_anchor and any(d in href for d in ("/delete", "/remove", "/cancel", "/checkout"))):
        return True

    # Navigation links (<a> or role="link") are NOT final submit steps unless destructive/financial
    if is_anchor:
        return False

    # a) Cookie consent buttons (accept cookies, accept all, i agree outside a checkout/application form)
    COOKIE_CONSENT_PHRASES = (
        "accept cookies", "accept all cookies", "accept all", "accept essential",
        "allow cookies", "allow all cookies", "allow all",
        "i agree", "agree & continue", "agree and continue", "agree & proceed", "agree and proceed",
        "cookie consent", "dismiss cookie",
    )
    is_cookie_consent = any(phrase in lower_text for phrase in COOKIE_CONSENT_PHRASES)
    if is_cookie_consent and not bool(args.get("in_form")) and not any(kw in lower_text for kw in ("checkout", "pay", "order", "application")):
        return False

    # b) Filter tabs (role="tab", role="menuitem", role="option") are auto-allowed even if text says "Completed" or "Registered"
    if role in ("tab", "menuitem", "option") and not bool(args.get("is_financial")) and not bool(args.get("is_destructive")):
        return False

    in_form = bool(args.get("in_form") or args.get("form") or args.get("form_id") or "form" in str(args.get("parent", "")).lower())
    elem_t = str(args.get("element_type", "") or args.get("type", "")).lower()
    is_submit_flag = (elem_t == "submit") or bool(args.get("is_submit"))

    EXPLICIT_FINAL_PHRASES_REGEX = re.compile(
        r"\b("
        r"submit\s+application|confirm\s+booking|complete\s+registration|confirm\s+and\s+pay|"
        r"confirm\s+order|pay\s+now|proceed\s+to\s+pay|continue\s+to\s+payment|place\s+order|"
        r"place\s+an\s+order|delete\s+account|remove\s+account|cancel\s+subscription|"
        r"transfer\s+money|checkout|buy\s+now|submit\s+form|turn\s+in"
        r")\b",
        re.IGNORECASE,
    )
    if bool(EXPLICIT_FINAL_PHRASES_REGEX.search(text_to_check)):
        return True

    # Benign tab switchers and navigation buttons outside forms (e.g. "Register / Sign Up", "Book Flights")
    NAV_BUTTON_PHRASES = (
        "register / sign up", "register/sign up", "sign up", "book flights", "book flight",
        "book tickets", "book ticket", "book hotel", "book hotels", "book a demo",
    )
    if any(phrase in lower_text for phrase in NAV_BUTTON_PHRASES) and not in_form and not is_submit_flag:
        return False

    # Single-word "Register" or "Book" buttons outside a <form> (in_form == False) that open a page/modal are allowed
    clean_label = re.sub(r"[^\w\s]", "", lower_text).strip()
    if clean_label in ("register", "book") and args.get("in_form") is False and not is_submit_flag:
        return False
    if clean_label in ("run", "test", "compile", "console") and not is_submit_flag:
        return False

    # 3. Final Submissions & keyword mutations
    if bool(FINAL_SUBMISSION_REGEX.search(text_to_check)) or bool(FINAL_STEP_REGEX.search(text_to_check)):
        return True

    # 4. Structural submit-type buttons inside a <form>
    is_btn = (tag == "button") or (role == "button")
    is_submit_btn = (is_btn and elem_t not in ("button", "reset")) or is_submit_flag
    if in_form and is_submit_btn and not is_wizard_nav:
        return True

    key = str(args.get("key", "")).lower()
    if key in {"enter", "return"}:
        is_search = bool(args.get("is_search")) or "search" in str(args.get("context", "")).lower() or is_safe_nav
        if not is_search and not is_wizard_nav:
            return True

    return False


def is_sensitive_file_upload(args: Dict[str, Any]) -> bool:
    """Returns True if the file path or target points to sensitive secrets, tokens, or private keys."""
    file_path = str(args.get("file_path", "")).lower()
    sensitive_markers = (".env", "id_rsa", "id_ed25519", ".pem", ".key", "credentials", "secret", "token_store")
    return any(marker in file_path for marker in sensitive_markers)


def sanitize_sensitive_args(args: Dict[str, Any], is_sensitive: bool = False) -> Dict[str, Any]:
    """Scrub sensitive credentials (passwords, OTPs, CVVs, tokens, secret values) from action arguments."""
    sanitized: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        k_str = str(k).lower()
        if re.search(r"(key|secret|password|pwd|token|otp|mpin|cvv|card|pin|auth|credential)", k_str):
            sanitized[k] = "[REDACTED_SENSITIVE_INPUT]"
        elif is_sensitive and k_str in ("text", "value", "content", "input_text", "query", "string"):
            sanitized[k] = "[REDACTED_SENSITIVE_INPUT]"
        else:
            sanitized[k] = v
    return sanitized


# Default ruleset mapping common tool calls to declarative security policies
DEFAULT_POLICIES: List[PolicyRule] = [
    # 1. Fatal / Malicious / System Destructive Commands
    PolicyRule(
        action_pattern=r"^(system|os|terminal)\.(format|rmdir_system|wipe|reboot)$",
        decision=Decision.DENY,
        risk_level=RiskLevel.FATAL,
        reason="System destructive command prohibited by non-negotiable security invariant.",
        name="System Destructive Block",
    ),
    PolicyRule(
        action_pattern=r"^.*$",
        decision=Decision.DENY,
        risk_level=RiskLevel.FATAL,
        reason="Detected prompt injection targeting credential exfiltration or token theft.",
        condition_fn=lambda args: any(
            re.search(r"(password|token|api_key|secret_key|id_rsa|master_key|auth_token|cookie|secret|credential)", str(v).lower())
            and any(ex in str(v).lower() for ex in ("exfiltrat", "webhook", "pipedream", "pastebin", "ngrok", "requestbin", "curl http", "curl https", "evil.com", "steal"))
            for v in args.values()
        ),
        name="Credential Exfiltration Block",
    ),

    # 2. High Risk: Submit / Turn In / Checkout / Financial / Permanent Deletion (Final Step Gate)
    PolicyRule(
        action_pattern=r"^(browser|gui|computer)[._](type|fill|fill_element|select_option|input)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Sensitive field pause: JAI never types passwords, OTPs, or financial credentials. Please type directly into your browser and reply continue.",
        condition_fn=lambda args: bool(args.get("is_sensitive")) or any(
            bool(SENSITIVE_FIELD_REGEX.search(str(k) + " " + str(v)))
            for k, v in args.items()
        ),
        name="Sensitive Field / OTP Gate",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer|tool)[._](click|click_element|submit|press_enter|press_key|.*click.*)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Action is a final step (submit/pay/delete/publish) requiring human confirmation.",
        condition_fn=is_browser_final_step,
        name="Browser Final Step Gate",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer)[._](handle_dialog|dialog)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Autonomous native JavaScript dialog handling (alert/confirm dismiss).",
        name="Browser Handle Dialog",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer|file)[._](upload|upload_file|browser_upload_file|file_upload)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Uploading sensitive credential or secret files to the browser requires explicit user confirmation.",
        condition_fn=is_sensitive_file_upload,
        name="Browser Sensitive File Upload Gate",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer|file)[._](upload|upload_file|browser_upload_file|file_upload)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Autonomous form file upload for non-sensitive documents (Rule 0 detail entry).",
        name="Browser Safe File Upload Entry",
    ),
    PolicyRule(
        action_pattern=r"^(file|os|database)\.(delete|drop|purge|truncate)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Permanent deletion requires explicit two-step user confirmation.",
        name="Permanent Deletion Gate",
    ),
    PolicyRule(
        action_pattern=r"^(database)[._](mutate|insert|update|create|alter)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Database mutations require explicit user confirmation.",
        name="Database Mutation Gate",
    ),
    PolicyRule(
        action_pattern=r"^(email|gmail|email_dispatch)\.(send|dispatch)?$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Sending external communications requires explicit user sign-off.",
        name="Email Dispatch Gate",
    ),
    PolicyRule(
        action_pattern=r"^(calendar)\.(create_event|delete_event)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Modifying external calendar schedule requires explicit user sign-off.",
        name="Calendar Modification Gate",
    ),
    PolicyRule(
        action_pattern=r"^(email|gmail|calendar|drive)\.(fetch|list|read|search)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Read-only workspace perception is inherently safe.",
        name="Workspace Perception Gate",
    ),
    PolicyRule(
        action_pattern=r"^(file|vault)\.(read|view|cat|stat|exists|list)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Read-only file inspection within permitted boundaries.",
        name="Safe File Read",
    ),
    PolicyRule(
        action_pattern=r"^(web|google|search)\.(search|query|grounding|search_grounding)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Read-only web search and informational grounding.",
        name="Safe Web Search",
    ),
    PolicyRule(
        action_pattern=r"^(hyperbridge)\.(read_tab|read|inspect|observe|get_url|get_dom)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Ring 0 auto-read of browser tab content via HyperBridge.",
        name="HyperBridge Ring 0 Read",
    ),
    PolicyRule(
        action_pattern=r"^(hyperbridge)\.(mutate_tab|click|input|mutate)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="HyperBridge Ring 1 state mutation requires user confirmation.",
        name="HyperBridge Ring 1 Mutation Gate",
    ),

    # 3. Low Risk: Read-Only Perception, Safe Math & Draft Typing
    PolicyRule(
        action_pattern=r"^(tool|math|calc)\.(calculate|echo)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Safe mathematical computation or local utility tool.",
        name="Safe Utility Computation",
    ),
    PolicyRule(
        action_pattern=r"^(browser|screen|gui|perception)\.(observe|scrape|screenshot|read|read_active_tab|get_text_by_role|ocr|inspect|get_text|get_url|get_title|get_snapshot|snapshot)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Read-only environmental perception is inherently safe.",
        name="Read-Only Perception",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer)[._](navigate|open_url|open_shadow_tab|goto)$",
        decision=Decision.REQUIRE_APPROVAL,
        risk_level=RiskLevel.HIGH,
        reason="Navigation to sensitive personal or financial origin requires human authorization.",
        condition_fn=lambda args: bool(
            args.get("url") and is_sensitive_origin(str(args.get("url"))) and not (
                # Google SSO & Workspace navigation is pre-authorized by user attorney directive
                get_hostname(str(args.get("url"))) in ("mail.google.com", "drive.google.com", "accounts.google.com")
            )
        ),
        name="Sensitive Origin Navigation Gate",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer)[._](navigate|scroll|scroll_page|hover|mouse_move|open_url|open_shadow_tab)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Non-state-changing cursor positioning and page navigation.",
        name="Non-Mutating Navigation",
    ),
    PolicyRule(
        action_pattern=r"^(open_shadow_tab|open_url|read_all_tabs|switch_to_tab|close_tab|tidy_tabs_smart|group_tabs_by_domain|read_tab_by_selector|web_search|calculate|vault_note_append)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Safe autonomous read, tab management, or informational utility tool.",
        name="Safe Core Utility & Tab Tools",
    ),
    PolicyRule(
        action_pattern=r"^(pdf_read_text|tab_read_partition|vault_query|vault_commit|verify_in_sandbox|verify_solution_in_sandbox|google_search_grounding|hyperbridge_read_all_tabs|browser_find_elements|file_read|extract_ast_slice|run_pytest|browser_reload_page|browser_write_code|browser_read_network_json|browser_query_network_api|execute_doctor|browser_interact_canvas|browser_select_autocomplete|browser_pick_date|browser_fill_batch|browser_select_option|browser_handle_dialog|query_hybrid_rrf|browser_search_history|browser_read_active_tab|browser_get_text_by_role)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.LOW,
        reason="Autonomous frontier research, evaluation, and epistemic vault verification tool.",
        name="Frontier Research & Evaluation Gate",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer)[._](type|fill|fill_element|select_option)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.MEDIUM,
        reason="Autonomous drafting and form filling for non-sensitive fields.",
        condition_fn=lambda args: not any(
            bool(SENSITIVE_FIELD_REGEX.search(str(k) + " " + str(v)))
            for k, v in args.items()
        ),
        name="Draft Text Entry",
    ),
    PolicyRule(
        action_pattern=r"^(browser|gui|computer|tool)[._](click|click_element|mouse_click|.*click.*)$",
        decision=Decision.ALLOW,
        risk_level=RiskLevel.MEDIUM,
        reason="Safe navigation or UI element interaction (links, menus, filters, expanders, next buttons).",
        name="Safe UI Click",
    ),
]


def compute_approval_digest(
    secret: str,
    current_url: str,
    element_fingerprint: str,
    action_name: str,
    canonical_args_json: str,
    expires_at: Union[float, int, str],
) -> str:
    """Compute HMAC-SHA256 digest binding action parameters, URL, element fingerprint, and expiration."""
    exp_str = f"{float(expires_at):.6f}" if isinstance(expires_at, (int, float)) else str(expires_at)
    payload = f"{str(current_url or '')}|{str(element_fingerprint or '')}|{str(action_name or '')}|{str(canonical_args_json or '{}')}|{exp_str}"
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def get_element_fingerprint(page: Any = None, action_args: Optional[Dict[str, Any]] = None) -> str:
    """Compute a deterministic fingerprint of the target element.
    If the element mutates (attributes, text, tag change), the fingerprint changes.
    """
    args = action_args or {}
    eid = args.get("element_id") or args.get("id")
    sel = args.get("selector")

    # 1. Inspect live page via DOM evaluation if available
    if page is not None and hasattr(page, "evaluate"):
        try:
            script = """([target_id, target_sel]) => {
                let el = null;
                if (target_id !== null && target_id !== undefined && target_id !== '') {
                    el = document.querySelector(`[data-jai-id="${target_id}"]`);
                }
                if (!el && target_sel) {
                    try { el = document.querySelector(target_sel); } catch(e) {}
                }
                if (!el && (target_id !== null && target_id !== undefined && target_id !== '')) {
                    try { el = document.getElementById(String(target_id)); } catch(e) {}
                }
                if (!el) return "";
                const tag = (el.tagName || "").toLowerCase();
                const idAttr = el.id || "";
                const name = el.getAttribute("name") || "";
                const role = el.getAttribute("role") || "";
                const type = el.getAttribute("type") || "";
                const val = (el.value || "").slice(0, 100);
                const text = (el.innerText || el.textContent || "").trim().slice(0, 100);
                return `${tag}|${idAttr}|${name}|${role}|${type}|${val}|${text}`;
            }"""
            sig = page.evaluate(script, [eid, sel])
            if sig and isinstance(sig, str):
                return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        except Exception:
            pass

    # 2. Inspect active DOM cache if available (skipped in benchmark mode)
    if not (args.get("_benchmark") or os.environ.get("JAI_BENCHMARK_MODE")):
        try:
            import src.jai_browser as jb
            if hasattr(jb, "_ACTIVE_DOM_CACHE") and eid is not None:
                cache = jb._ACTIVE_DOM_CACHE.get_active_tab_cache()
                meta = cache.get(int(eid)) if str(eid).isdigit() else None
                if isinstance(meta, dict):
                    sig = f"{meta.get('tag')}|{meta.get('id_attr')}|{meta.get('role')}|{meta.get('text')}"
                    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        except Exception:
            pass

    # 3. Fallback to args-based fingerprint
    fallback = f"eid={eid}|sel={sel}|text={args.get('text', '')}"
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:16]


class ActionGateway:
    """The central security controller verifying all autonomous agent actions."""

    def __init__(self, policies: Optional[List[PolicyRule]] = None, strict_mode: bool = True):
        self.policies = policies or list(DEFAULT_POLICIES)
        self.strict_mode = strict_mode
        self._approval_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None
        self._session_hmac_secret: str = secrets.token_hex(32)
        self._ensure_audit_log_dir()
        self._thread_local = threading.local()

    def get_session_hmac_secret(self) -> str:
        """Return the ephemeral per-session HMAC secret, initializing if missing."""
        if not getattr(self, "_session_hmac_secret", None):
            self._session_hmac_secret = secrets.token_hex(32)
        return self._session_hmac_secret

    def reset_session_hmac_secret(self) -> str:
        """Generate a new ephemeral per-session HMAC secret."""
        self._session_hmac_secret = secrets.token_hex(32)
        return self._session_hmac_secret

    @property
    def is_tainted_by_sensitive_origin(self) -> bool:
        return getattr(self._thread_local, "is_tainted_by_sensitive_origin", False)

    @is_tainted_by_sensitive_origin.setter
    def is_tainted_by_sensitive_origin(self, val: bool) -> None:
        self._thread_local.is_tainted_by_sensitive_origin = bool(val)

    @property
    def tainted_origins(self) -> set[str]:
        if not hasattr(self._thread_local, "tainted_origins"):
            self._thread_local.tainted_origins = set()
        return self._thread_local.tainted_origins

    @tainted_origins.setter
    def tainted_origins(self, val: set[str]) -> None:
        self._thread_local.tainted_origins = set(val)

    @property
    def tainted_domains(self) -> set[str]:
        if not hasattr(self._thread_local, "tainted_domains"):
            self._thread_local.tainted_domains = set()
        return self._thread_local.tainted_domains

    @tainted_domains.setter
    def tainted_domains(self, val: set[str]) -> None:
        self._thread_local.tainted_domains = set(val)

    @property
    def current_url(self) -> str:
        return getattr(self._thread_local, "current_url", "")

    @current_url.setter
    def current_url(self, val: str) -> None:
        self._thread_local.current_url = str(val or "")

    def mark_tainted(self, origin: str = "") -> None:
        """Mark session tainted by access to a sensitive origin or domain."""
        self.is_tainted_by_sensitive_origin = True
        if origin:
            host = get_hostname(origin) or str(origin).strip().lower()
            if host:
                self.tainted_origins.add(host)
                self.tainted_domains.add(host)

    def mark_domain_tainted(self, domain: str) -> None:
        """Mark a specific domain as tainted."""
        d = get_hostname(domain) or str(domain).strip().lower()
        if d:
            self.tainted_domains.add(d)
            self.tainted_origins.add(d)
            self.is_tainted_by_sensitive_origin = True

    def reset_taint(self) -> None:
        """Reset sensitive taint state (e.g. for new session)."""
        self.is_tainted_by_sensitive_origin = False
        self.tainted_origins.clear()
        self.tainted_domains.clear()
        self.current_url = ""


    @property
    def approval_callback(self) -> Optional[Callable[[str, Dict[str, Any]], bool]]:
        """Return the current approval callback."""
        return self._approval_callback

    @approval_callback.setter
    def approval_callback(self, callback: Optional[Callable[[str, Dict[str, Any]], bool]]) -> None:
        """Register the approval callback."""
        self._approval_callback = callback

    def register_policy(self, rule: PolicyRule, priority: bool = False) -> None:
        """Register a new policy rule. If priority=True, insert at the beginning."""
        if priority:
            self.policies.insert(0, rule)
        else:
            self.policies.append(rule)

    def _ensure_audit_log_dir(self):
        try:
            AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create audit log directory {AUDIT_LOG_PATH.parent}: {e}")

    def register_approval_callback(self, callback: Callable[[str, Dict[str, Any]], bool]):
        """Sets a human-in-the-loop callback (e.g. GUI modal, Telegram message, or CLI prompt)."""
        self._approval_callback = callback

    def evaluate(self, action_name: str, arguments: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None) -> EvaluationResult:
        """Evaluates an action and its arguments against declarative policies."""
        args = dict(arguments or {})
        ctx = dict(context or {})
        if "url" in ctx and "url" not in args:
            args["url"] = ctx["url"]
        if "page_url" in ctx and "page_url" not in args:
            args["page_url"] = ctx["page_url"]
        is_benchmark = bool(ctx.get("benchmark") or os.environ.get("JAI_BENCHMARK_MODE"))

        clean = (action_name or "").lower().strip()
        while clean.startswith("tool_") or clean.startswith("tool."):
            clean = clean[5:]
        norm = clean.replace(".", "_")

        names_to_check = [action_name, clean, norm]
        if "_" in action_name:
            names_to_check.append(action_name.replace("_", ".", 1))

        # 1. System destructive commands prohibited by non-negotiable security invariant
        if any(re.match(r"^(system|os|terminal)\.(format|rmdir_system|wipe|reboot)$", n, re.IGNORECASE) for n in names_to_check):
            return EvaluationResult(Decision.DENY, RiskLevel.FATAL, "System destructive command prohibited.")

        # 2. Prompt injection targeting credential exfiltration
        non_url_args = {k: v for k, v in args.items() if not (k in ("url", "target_url") and any(re.search(r"(navigate|goto|open_url|open_tab|open_shadow_tab)", n, re.IGNORECASE) for n in names_to_check))}
        if any(
            re.search(r"(password|token|api_key|secret_key|id_rsa|master_key|auth_token|cookie|secret|credential)", str(v).lower())
            and any(ex in str(v).lower() for ex in ("exfiltrat", "webhook", "pipedream", "pastebin", "ngrok", "requestbin", "curl http", "curl https", "evil.com", "steal"))
            for v in non_url_args.values()
        ):
            return EvaluationResult(Decision.DENY, RiskLevel.FATAL, "Detected prompt injection targeting credential exfiltration.")

        # 3. Protected credential file check
        path_arg = str(args.get("path", "") or args.get("target_file", "") or args.get("file_path", "")).lower()
        if any(secret in path_arg for secret in [".env", "config.py", "id_rsa", "client_secret"]):
            return EvaluationResult(Decision.DENY, RiskLevel.FATAL, f"Blocked access to protected credential file: {path_arg}")

        # 3a. Verified runtime approval token check (resumed coroutine continuation only)
        # Model-supplied pre_approved keys are strictly ignored and stripped
        if "_system_approval_token" in args:
            token = str(args.pop("_system_approval_token"))
            if token in _VALIDATED_APPROVAL_TOKENS:
                _VALIDATED_APPROVAL_TOKENS.discard(token)
                return EvaluationResult(Decision.ALLOW, RiskLevel.MEDIUM, f"Action '{action_name}' verified by registered approval token.")
            return EvaluationResult(Decision.DENY, RiskLevel.HIGH, "Invalid or expired system approval token.")

        # 3b. Gate browser_press_key: Enter key in form context or sensitive origin requires approval
        is_press_key = any(n in ("browser_press_key", "tool_browser_press_key", "browser.press_key", "press_key") for n in names_to_check)
        if is_press_key:
            key_name = str(args.get("key") or args.get("key_name") or "").strip().lower()
            if key_name in ("enter", "return", "numpadenter"):
                page_url = str(args.get("page_url") or args.get("url") or "")
                if not page_url and not is_benchmark:
                    try:
                        from src.jai_browser import _get_active_page
                        p = _get_active_page()
                        if p and hasattr(p, "url"):
                            page_url = p.url or ""
                    except Exception as e:
                        logger.debug("Active page lookup in Enter check suppressed: %s", e)
                is_sensitive_page = is_sensitive_origin(page_url) if page_url else False
                active_tag = str(args.get("active_tag") or args.get("tag") or "").lower()
                in_form = bool(args.get("in_form") or args.get("form") or active_tag in ("input", "form", "button", "select") or is_sensitive_page)

                if in_form or is_sensitive_page:
                    return EvaluationResult(Decision.REQUIRE_APPROVAL, RiskLevel.HIGH, f"Pressing Enter in a form/sensitive context ({page_url or 'form element'}) triggers submission and requires approval.")
            return EvaluationResult(Decision.ALLOW, RiskLevel.LOW, f"Key press '{key_name}' permitted.")

        # Hydrate element metadata from _ACTIVE_DOM_CACHE and _FULL_DOM_OVERFLOW_CACHE (browser actions only)
        eid = args.get("element_id") or args.get("id")
        is_browser_act = any(action_name.startswith(p) for p in ("browser", "tab", "cdp", "page"))
        if eid is not None and is_browser_act:
            try:
                from src.jai_browser import _ACTIVE_DOM_CACHE, _FULL_DOM_OVERFLOW_CACHE
                target_p = args.get("page")
                target_tid = None
                if target_p:
                    try:
                        from src.browser.cdp import _get_page_target_id
                        target_tid = _get_page_target_id(target_p)
                    except Exception:
                        pass
                meta = None
                for c in (_ACTIVE_DOM_CACHE, _FULL_DOM_OVERFLOW_CACHE):
                    cache_dict = c.get_tab_cache(target_tid) if hasattr(c, "get_tab_cache") else c
                    if eid in cache_dict:
                        meta = cache_dict[eid]
                        break
                    try:
                        ieid = int(eid)
                        if ieid in cache_dict:
                            meta = cache_dict[ieid]
                            break
                    except (ValueError, TypeError) as e:
                        logger.debug("Element ID int conversion suppressed: %s", e)
                if meta and isinstance(meta, dict):
                    # Prevent stale button cache entry from polluting a text fill action
                    is_fill_act = "fill" in action_name.lower() or "type" in action_name.lower()
                    if is_fill_act and meta.get("tag") == "button":
                        meta = None
                    if meta:
                        for k in ("label", "text", "name", "role", "tag", "id_attr", "aria_label", "title", "type", "is_submit", "is_financial", "is_sensitive", "in_form"):
                            if k in meta and k not in args:
                                args[k] = meta[k]
            except Exception as e:
                logger.debug(f"Could not hydrate element metadata for {eid}: {e}")

        # ----------------------------------------------------------------------
        # Rule 0 Safety Gate & Cross-Domain Taint Firewall (Mandatory Deterministic)
        # ----------------------------------------------------------------------

        # A. Cross-Domain Taint Gate
        # 1. Navigation outside user-selected tabs
        if bool(args.get("outside_user_tabs")) or bool(args.get("outside_tabs")) or (context and (context.get("outside_user_tabs") or context.get("outside_tabs"))):
            return EvaluationResult(
                Decision.REQUIRE_APPROVAL,
                RiskLevel.HIGH,
                "Navigation outside user-selected tabs requires explicit user approval.",
            )

        # 2. Text read from Domain A dispatched/typed into Domain B (Domain A != Domain B)
        source_domain = args.get("source_domain") or (context and context.get("source_domain")) or args.get("read_from_domain")
        target_domain = args.get("target_domain") or (context and context.get("target_domain")) or args.get("write_to_domain")
        if not target_domain:
            target_cand = args.get("url") or args.get("target_url") or args.get("current_url") or self.current_url
            if target_cand:
                target_domain = get_hostname(str(target_cand))

        if source_domain and target_domain:
            src_h = get_hostname(str(source_domain)) or str(source_domain).strip().lower()
            tgt_h = get_hostname(str(target_domain)) or str(target_domain).strip().lower()
            if src_h and tgt_h and src_h != tgt_h:
                return EvaluationResult(
                    Decision.REQUIRE_APPROVAL,
                    RiskLevel.HIGH,
                    f"Cross-Domain Taint Firewall: Text read from '{src_h}' cannot be dispatched or typed into '{tgt_h}' without user approval.",
                )

        if self.tainted_domains and target_domain:
            tgt_h = get_hostname(str(target_domain)) or str(target_domain).strip().lower()
            if tgt_h and any(td != tgt_h for td in self.tainted_domains):
                return EvaluationResult(
                    Decision.REQUIRE_APPROVAL,
                    RiskLevel.HIGH,
                    f"Cross-Domain Taint Firewall: Data from tainted domain dispatched to '{tgt_h}' requires user approval.",
                )

        # Structural check: Navigation to sensitive domains
        is_nav_act = any(
            re.search(r"^(browser|gui|computer|page)?[._]?(navigate|open_url|goto|open_shadow_tab|open_tab)$", n, re.IGNORECASE)
            for n in names_to_check
        )
        nav_cand = args.get("url") or args.get("target_url") or args.get("href") or args.get("link")
        if is_nav_act and nav_cand:
            if is_sensitive_origin(str(nav_cand)):
                h = get_hostname(str(nav_cand))
                if h not in ("mail.google.com", "drive.google.com", "accounts.google.com"):
                    return EvaluationResult(
                        Decision.REQUIRE_APPROVAL,
                        RiskLevel.HIGH,
                        f"Structural gate: Navigation to sensitive domain '{h or nav_cand}' requires human authorization.",
                    )

        # B. Interacting with password/payment field or typing sensitive credentials
        elem_type = str(args.get("type", "") or args.get("element_type", "")).strip().lower()
        has_sensitive_credential = (
            elem_type in ("password", "cvv", "cvc", "card", "payment", "creditcard")
            or any(k in elem_type for k in ("password", "cvv", "card_number", "payment"))
            or any(k in str(args.get("autocomplete", "")).lower() for k in ("cc-number", "cc-csc", "cc-exp", "current-password", "new-password"))
            or any(k in str(args.get("name", "")).lower() for k in ("password", "passwd", "pwd", "cvv", "cardnumber", "card_number", "payment"))
            or bool(args.get("is_sensitive"))
            or any(bool(SENSITIVE_FIELD_REGEX.search(f"{k} {v}")) for k, v in args.items())
        )
        if has_sensitive_credential:
            return EvaluationResult(
                Decision.REQUIRE_APPROVAL,
                RiskLevel.HIGH,
                "Sensitive field pause: JAI never types passwords, OTPs, or financial credentials. Please type directly into your browser and reply continue.",
            )

        # Structural check: Any action on elements whose label is non-ASCII or empty on a sensitive page
        page_cand = (
            args.get("page_url")
            or args.get("current_url")
            or args.get("url")
            or self.current_url
            or (context.get("current_url") if context else "")
        )
        is_on_sensitive_page = (
            self.is_tainted_by_sensitive_origin
            or bool(page_cand and is_sensitive_origin(str(page_cand)))
            or any(is_sensitive_origin(d) for d in self.tainted_domains)
        )
        if is_on_sensitive_page:
            raw_label_val = (
                args.get("label")
                if args.get("label") is not None
                else args.get("text")
                if args.get("text") is not None
                else args.get("button_text")
                if args.get("button_text") is not None
                else args.get("aria_label")
                if args.get("aria_label") is not None
                else args.get("aria-label")
                if args.get("aria-label") is not None
                else args.get("title")
                if args.get("title") is not None
                else args.get("name")
            )
            raw_label_str = str(raw_label_val if raw_label_val is not None else "").strip()
            is_empty_label = not raw_label_str
            is_non_ascii_label = any(ord(ch) > 127 for ch in raw_label_str)
            elem_tag_for_sens = str(args.get("tag", "")).lower()
            is_interactive = (
                eid is not None
                or elem_tag_for_sens in ("button", "a", "input", "select", "textarea")
                or any(act_kw in action_name.lower() for act_kw in ("click", "press", "fill", "type", "submit", "select"))
            )
            if is_interactive and (is_empty_label or is_non_ascii_label):
                desc = "empty (icon-only)" if is_empty_label else "non-ASCII"
                return EvaluationResult(
                    Decision.REQUIRE_APPROVAL,
                    RiskLevel.HIGH,
                    f"Structural gate: Action on element with {desc} label on sensitive page requires human confirmation.",
                )

        # C. Interacting with file field (type="file") or file upload
        if elem_type == "file" or ("upload" in norm and ("browser" in norm or "file" in norm)) or any(n in ("browser.upload_file", "browser_upload_file") for n in names_to_check):
            return EvaluationResult(
                Decision.REQUIRE_APPROVAL,
                RiskLevel.HIGH,
                "Rule 0 Gate: Interacting with file fields or uploading filesystem files requires user approval.",
            )

        # D. Submitting a form via the Enter key (exempt search inputs & search engines)
        page_cand = str(args.get("page_url") or args.get("url") or getattr(self, "current_url", "") or "").lower()
        if not page_cand and not is_benchmark:
            try:
                from src.jai_browser import _get_active_page
                p = _get_active_page()
                if p and hasattr(p, "url"):
                    page_cand = str(p.url or "").lower()
            except Exception:
                pass

        is_search_engine_page = any(se in page_cand for se in ("google.", "youtube.", "bing.", "duckduckgo.", "wikipedia.", "yahoo."))
        elem_role = str(args.get("role", "")).lower()
        elem_name = str(args.get("name", "")).lower()
        elem_tag = str(args.get("tag", "")).lower()

        is_search = (
            is_search_engine_page
            or elem_type in ("search",)
            or elem_role in ("searchbox", "combobox")
            or elem_name in ("q", "query", "search", "search_query", "search_bar", "search_term", "k", "keyword")
            or any(kw in str(args.get(k, "")).lower() for k in ("placeholder", "label", "aria_label", "aria-label", "title", "id_attr", "name") for kw in ("search", "find", "query", "lookup", "filter"))
        )
        has_press_enter = bool(args.get("press_enter") is True or (str(args.get("key", "")).strip().lower() == "enter" and (args.get("in_form") or "form" in str(args))))

        # Submitting via Enter requires approval ONLY if in a transactional/mutation form and not a search
        is_transactional_form = bool(args.get("in_form")) and (
            is_browser_final_step(args)
            or bool(args.get("is_submit"))
            or bool(args.get("is_financial"))
            or bool(args.get("is_destructive"))
        )

        if has_press_enter and not is_search and (is_transactional_form or (bool(args.get("in_form")) and not is_search_engine_page and elem_tag != "textarea")):
            return EvaluationResult(
                Decision.REQUIRE_APPROVAL,
                RiskLevel.HIGH,
                "Rule 0 Gate: Submitting a form via the Enter key requires user approval.",
            )

        # E. Clicking or submitting any element whose text, aria-label, title, name, or id matches:
        # submit/destructive keywords or has type="submit" / is_submit=True
        elem_tag = str(args.get("tag", "")).lower()
        elem_role = str(args.get("role", "")).lower()
        is_anchor = (elem_tag == "a") or (elem_role == "link")
        href_val = str(args.get("href", "") or args.get("url", "") or args.get("target", "")).lower()

        fields_to_scan = [
            args.get("text"),
            args.get("label"),
            args.get("aria_label"),
            args.get("aria-label"),
            args.get("title"),
            args.get("name"),
            args.get("id"),
            args.get("id_attr"),
            args.get("value"),
        ]
        label_text = " ".join(str(v) for v in fields_to_scan if v).strip().lower()

        BENIGN_NAV_PHRASES = (
            "order history", "past orders", "my orders", "view orders",
            "download pdf", "download csv", "download report", "download file",
            "apply filter", "apply filters", "filter by",
            "blog post", "view post", "read post", "post details", "forum post",
        )
        is_benign_nav = any(phrase in label_text for phrase in BENIGN_NAV_PHRASES)

        # Links (<a> or role="link") are read-only navigation unless pointing to destructive routes
        if is_anchor and not any(d in href_val for d in ("/delete", "/remove", "/cancel", "/checkout")):
            is_benign_nav = True

        # a) Cookie consent buttons (accept cookies, accept all, i agree outside checkout/application form)
        COOKIE_CONSENT_PHRASES = (
            "accept cookies", "accept all cookies", "accept all", "accept essential",
            "allow cookies", "allow all cookies", "allow all",
            "i agree", "agree & continue", "agree and continue", "agree & proceed", "agree and proceed",
            "cookie consent", "dismiss cookie",
        )
        if any(phrase in label_text for phrase in COOKIE_CONSENT_PHRASES) and not bool(args.get("in_form")) and not any(kw in label_text for kw in ("checkout", "pay", "order", "application")):
            is_benign_nav = True

        # b) Filter tabs (role="tab", role="menuitem", role="option") are auto-allowed even if text says "Completed" or "Registered"
        if elem_role in ("tab", "menuitem", "option") and not bool(args.get("is_financial")) and not bool(args.get("is_destructive")):
            is_benign_nav = True

        # c) Navigation buttons (e.g. "Register / Sign Up", "Book Flights") and single-word "Register" / "Book" outside a <form>
        NAV_BUTTON_PHRASES = (
            "register / sign up", "register/sign up", "sign up", "book flights", "book flight",
            "book tickets", "book ticket", "book hotel", "book hotels", "book a demo",
        )
        elem_is_submit = (elem_type == "submit") or bool(args.get("is_submit"))
        if any(phrase in label_text for phrase in NAV_BUTTON_PHRASES) and not bool(args.get("in_form")) and not elem_is_submit:
            is_benign_nav = True

        clean_label = re.sub(r"[^\w\s]", "", label_text).strip()
        if clean_label in ("register", "book") and args.get("in_form") is False and not elem_is_submit:
            is_benign_nav = True

        # Refined submit/destructive regex - includes extended mutation keywords
        RULE0_SUBMIT_REGEX = re.compile(
            r"\b(pay|pay\s+now|proceed\s+to\s+pay|continue\s+to\s+payment|buy|checkout|place\s+order|place\s+an\s+order|donate|transfer\s+money|delete\s+account|delete|remove\s+account|cancel\s+subscription|purge|truncate|drop\s+table|submit|submit\s+application|apply\s+now|confirm\s+and\s+pay|confirm\s+order|confirm|complete|book|register|publish\s+post|post|reply|tweet|send\s+email|send\s+message)\b",
            re.IGNORECASE,
        )
        NON_SUBMIT_NAV_REGEX = re.compile(
            r"\b(solve|run|test|execute|console|search|filter|prev|back|expand|details|practice|explore|login|sign in|google)\b",
            re.IGNORECASE,
        )
        is_safe_nav = is_benign_nav or bool(NON_SUBMIT_NAV_REGEX.search(label_text))

        EXPLICIT_FINAL_PHRASES_REGEX = re.compile(
            r"\b("
            r"submit\s+application|confirm\s+booking|complete\s+registration|confirm\s+and\s+pay|"
            r"confirm\s+order|pay\s+now|proceed\s+to\s+pay|continue\s+to\s+payment|place\s+order|"
            r"place\s+an\s+order|delete\s+account|remove\s+account|cancel\s+subscription|"
            r"transfer\s+money|checkout|buy\s+now|submit\s+form|turn\s+in"
            r")\b",
            re.IGNORECASE,
        )
        if bool(EXPLICIT_FINAL_PHRASES_REGEX.search(label_text)) or bool(args.get("is_financial")) or bool(args.get("is_destructive")):
            is_safe_nav = False
        has_submit_keyword = not is_safe_nav and any(
            v and (
                RULE0_SUBMIT_REGEX.search(str(v))
                or RULE0_SUBMIT_REGEX.search(re.sub(r"[\W_]+", " ", str(v)))
            )
            for v in fields_to_scan
        )
        is_submit_flag = (elem_type == "submit") or bool(args.get("is_submit"))

        # Structural check: submit-type buttons inside a <form> (HTML buttons inside form default to submit)
        in_form = bool(args.get("in_form") or args.get("form") or args.get("form_id") or "form" in str(args.get("parent", "")).lower())
        elem_is_button = (elem_tag == "button") or (elem_role == "button")
        is_structural_submit = in_form and (
            (elem_is_button and elem_type not in ("button", "reset"))
            or is_submit_flag
            or (elem_tag == "input" and elem_type in ("submit", "image"))
        )

        if (has_submit_keyword and not is_safe_nav) or (is_submit_flag and not is_safe_nav) or (is_structural_submit and not is_safe_nav):
            return EvaluationResult(
                Decision.REQUIRE_APPROVAL,
                RiskLevel.HIGH,
                f"Rule 0 Gate: Submit/destructive keyword, submit type, or form submit button detected in '{action_name}'.",
            )

        # Track taint when reading or inspecting sensitive origins (runs before Ring-0 bypass)
        for k in ("url", "target_url", "current_url", "page_url", "origin", "href", "link"):
            val = args.get(k)
            if val and is_sensitive_origin(str(val)):
                self.mark_tainted(str(val))
                self.current_url = str(val)
                break

        # Custom registered policies (beyond DEFAULT_POLICIES) take precedence
        custom_rules = [r for r in self.policies if r not in DEFAULT_POLICIES]
        for rule in custom_rules:
            if any(re.match(rule.action_pattern, name, re.IGNORECASE) for name in names_to_check):
                if rule.condition_fn is None or rule.condition_fn(args):
                    return EvaluationResult(rule.decision, rule.risk_level, rule.reason)

        # Ring 0 (Auto-Allowed after safety, taint, and submit checks):
        if norm in RING_0_SAFE_TOOLS or clean in RING_0_SAFE_TOOLS or action_name in RING_0_SAFE_TOOLS:
            return EvaluationResult(Decision.ALLOW, RiskLevel.LOW, "Ring-0 Safe")

        if clean in RING_0_EXTENDED_TOOLS or clean.replace(".", "_") in RING_0_EXTENDED_TOOLS or norm in RING_0_EXTENDED_TOOLS or action_name in RING_0_EXTENDED_TOOLS:
            return EvaluationResult(Decision.ALLOW, RiskLevel.LOW, f"Ring-0 Autonomous Safe Tool ({action_name}) auto-approved.")

        # Safe navigation, search, reading, and login-navigation words
        text_to_check = " ".join(str(v) for v in fields_to_scan if v)
        if SAFE_NAV_REGEX.search(text_to_check):
            return EvaluationResult(
                Decision.ALLOW,
                RiskLevel.LOW,
                f"Autonomous navigation/search/reading/login word permitted: '{action_name}'.",
            )

        # Detail entry autonomy for non-sensitive form fields
        is_fill_or_type = any(re.search(r"(type|fill|select_option)", n, re.IGNORECASE) for n in names_to_check)
        if is_fill_or_type:
            return EvaluationResult(
                Decision.ALLOW,
                RiskLevel.MEDIUM,
                f"Autonomous detail entry permitted for '{action_name}'.",
            )

        # Check default policies
        for rule in self.policies:
            matched_pattern = any(re.match(rule.action_pattern, name, re.IGNORECASE) for name in names_to_check)
            if matched_pattern:
                if rule.condition_fn is None or rule.condition_fn(args):
                    return EvaluationResult(rule.decision, rule.risk_level, rule.reason)

        # Any unrecognized action defaults to REQUIRE_APPROVAL
        return EvaluationResult(
            Decision.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
            f"Action '{action_name}' is unclassified; strict policy requires explicit user confirmation.",
        )

    def enforce(self, action_name: str, arguments: Optional[Dict[str, Any]] = None, auto_prompt: bool = True) -> bool:
        """Enforces policy. Returns True if execution may proceed.

        Raises:
            ActionSecurityError: If action is DENIED.
            ActionApprovalRequiredError: If action REQUIRES_APPROVAL and human rejected or callback absent.
        """
        raw_args = dict(arguments or {})
        decision, risk, reason = self.evaluate(action_name, raw_args)

        is_sensitive = bool(raw_args.get("is_sensitive")) or any(
            bool(SENSITIVE_FIELD_REGEX.search(str(k) + " " + str(v)))
            for k, v in raw_args.items()
        )
        # Block secret values from reaching any logs, callbacks, databases, or exceptions
        sanitized_args = sanitize_sensitive_args(raw_args, is_sensitive=is_sensitive)
        self._log_audit(action_name, sanitized_args, decision, risk, reason)

        if decision == Decision.DENY:
            logger.error(f"🛑 ACTION BLOCKED [DENY]: '{action_name}' - {reason}")
            raise ActionSecurityError(f"Action '{action_name}' blocked by security gateway: {reason}")

        if decision == Decision.REQUIRE_APPROVAL:
            logger.warning(f"🔒 ACTION GATE [APPROVAL NEEDED]: '{action_name}' - {reason}")

            if raw_args.get("pre_approved") or raw_args.get("is_pre_approved") or raw_args.get("approved_by_user"):
                logger.info(f"✅ ACTION PRE-APPROVED BY USER: '{action_name}'")
                return True

            if auto_prompt and self.approval_callback is not None:
                try:
                    approved = self.approval_callback(action_name, sanitized_args)
                    if approved:
                        logger.info(f"✅ ACTION APPROVED BY REGISTERED CALLBACK: '{action_name}'")
                        return True
                except Exception as cb_err:
                    logger.warning(f"Error in approval callback: {cb_err}")

            appr_id = raw_args.get("approval_id") or str(uuid.uuid4())[:8]
            raise ActionApprovalRequiredError(action_name, reason, sanitized_args, approval_id=appr_id)

        logger.info(f"🟢 ACTION PERMITTED [ALLOW]: '{action_name}' ({risk.value})")
        return True

    async def check_action_async(
        self,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        auto_prompt: bool = True,
        timeout: float = 120.0,
    ) -> bool:
        """Asynchronous action gateway evaluation with browser_server approval integration."""
        raw_args = arguments or {}
        decision, risk, reason = self.evaluate(action_name, raw_args, context)
        is_sensitive = any(
            bool(SENSITIVE_FIELD_REGEX.search(str(k) + " " + str(v)))
            for k, v in raw_args.items()
        )
        sanitized_args = sanitize_sensitive_args(raw_args, is_sensitive=is_sensitive)
        self._log_audit(action_name, sanitized_args, decision, risk, reason)

        if decision == Decision.DENY:
            logger.error(f"🛑 ACTION BLOCKED [DENY]: '{action_name}' - {reason}")
            raise ActionSecurityError(f"Action '{action_name}' blocked by security gateway: {reason}")

        if decision == Decision.REQUIRE_APPROVAL:
            logger.warning(f"🔒 ACTION GATE [APPROVAL NEEDED]: '{action_name}' - {reason}")
            if raw_args.get("pre_approved") or raw_args.get("is_pre_approved") or raw_args.get("approved_by_user"):
                logger.info(f"✅ ACTION PRE-APPROVED BY USER: '{action_name}'")
                return True

            if auto_prompt:
                try:
                    from src.browser_server import request_approval as bs_req
                    approved = await bs_req({
                        "action": action_name,
                        "arguments": sanitized_args,
                        "reason": reason,
                        "risk_level": risk.value,
                    }, timeout=timeout)
                    if approved:
                        logger.info(f"✅ ACTION APPROVED BY USER via Browser Server: '{action_name}'")
                        return True
                    else:
                        logger.warning(f"❌ ACTION REJECTED BY USER via Browser Server: '{action_name}'")
                        raise ActionApprovalRequiredError(action_name, "User explicitly declined action approval.", sanitized_args)
                except Exception as e:
                    if isinstance(e, ActionApprovalRequiredError):
                        raise
                    logger.warning(f"Browser server approval bridge unavailable: {e}")

            raise ActionApprovalRequiredError(action_name, reason, sanitized_args)

        logger.info(f"🟢 ACTION PERMITTED [ALLOW]: '{action_name}' ({risk.value})")
        return True

    def _log_audit(self, action_name: str, args: Dict[str, Any], decision: Decision, risk: RiskLevel, reason: str):
        """Appends a tamper-evident audit record."""
        # Sanitize sensitive keys and values from arguments before writing to disk
        sanitized_args = sanitize_sensitive_args(args)
        clean_args = {}
        for k, v in sanitized_args.items():
            clean_args[k] = str(v)[:200]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action_name,
            "decision": decision.value,
            "risk_level": risk.value,
            "reason": reason,
            "arguments": clean_args,
        }

        try:
            from src.sqlite_writer import enqueue_audit_log
            enqueue_audit_log(entry, AUDIT_LOG_PATH)
        except Exception as e:
            logger.debug(f"Async audit log write failed: {e}")
            try:
                with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e2:
                logger.error(f"Audit log write failed: {e2}")


# Global default instance
action_gateway = ActionGateway()


def normalize_tool_name(name: str) -> str:
    clean = name.strip()
    while clean.startswith("tool_"):
        clean = clean[5:]
    return clean


def evaluate(action: str, arguments: Optional[Dict[str, Any]] = None, step: int = 0) -> Decision:
    """Module-level evaluate returning Decision enum directly."""
    if step >= 25:
        return Decision.REQUIRE_APPROVAL
    clean_action = normalize_tool_name(action)
    res = action_gateway.evaluate(clean_action, arguments)
    return res.decision


async def request_approval(action_dict: dict, timeout: float = 120.0) -> bool:
    from src.browser_server import request_approval as bs_request_approval
    return await bs_request_approval(action_dict, timeout=timeout)


__all__ = [
    "AUDIT_LOG_PATH",
    "ActionApprovalRequiredError",
    "ActionGateway",
    "ActionSecurityError",
    "BASE_DIR",
    "DATA_DIR",
    "DB_PATH",
    "DEFAULT_POLICIES",
    "Decision",
    "FINAL_STEP_KEYWORDS",
    "FINAL_STEP_REGEX",
    "is_browser_final_step",
    "PolicyRule",
    "RiskLevel",
    "SENSITIVE_DOMAINS",
    "get_hostname",
    "is_sensitive_origin",
    "SENSITIVE_FIELD_REGEX",
    "action_gateway",
    "evaluate",
    "request_approval",
    "init_approval_db",
    "resolve_approval",
    "logger",
    "normalize_tool_name",
    "RING_0_SAFE_TOOLS",
    "compute_approval_digest",
    "get_element_fingerprint",
]


def main() -> None:
    """CLI entry point for action_gateway smoke test / evaluation."""
    import argparse
    parser = argparse.ArgumentParser(description="JAI Action Gateway Security Evaluator")
    parser.add_argument("--action", type=str, default="system.format", help="Action name to evaluate")
    parser.add_argument("--args", type=str, default='{"drive": "C:"}', help="JSON arguments dictionary")
    parsed_args = parser.parse_args()

    try:
        call_args = json.loads(parsed_args.args)
    except Exception:
        call_args = {}

    dec, risk, reason = action_gateway.evaluate(parsed_args.action, call_args)
    print(f"Action: {parsed_args.action}")
    print(f"Decision: {dec.value}")
    print(f"Risk: {risk.value}")
    print(f"Reason: {reason}")


if __name__ == "__main__":
    main()


# --- JAI TWO-RING AUTONOMOUS LOOP (CLAUDE CODE / COWORK RING-0 ALLOWLIST) ---
RING_0_EXTENDED_TOOLS = {
    # File reads
    "file.read", "tool_file_read",
    # Web search
    "web.search", "tool_web_search",
    # Calculation
    "tool_calculate",
    # Browser inspection
    "browser.read_active_tab", "tool_browser_read_active_tab", "read_active_tab",
    "browser.read_all_tabs", "tool_browser_read_all_tabs", "browser_read_all_tabs", "read_all_tabs",
    "browser.read_page", "tool_browser_read_page",
    "browser.screenshot", "tool_browser_screenshot",
    "browser.get_snapshot", "tool_browser_get_snapshot",
    "browser.extract_cards", "tool_browser_extract_cards", "browser_extract_cards",
    "browser.extract_task_context", "tool_browser_extract_task_context", "browser_extract_task_context",
    "browser.extract_paginated_cards", "tool_browser_extract_paginated_cards", "browser_extract_paginated_cards", "harvest_paginated_data", "harvest_all_unstop_pages", "tool_harvest_all_unstop_pages", "browser_extract_collection", "browser_extract_generic_collection", "tool_browser_extract_collection",
    "browser.scroll", "tool_browser_scroll_page", "browser_scroll_page",
    "browser.hover", "tool_browser_hover", "browser_hover",
    "browser.go_back", "tool_browser_go_back", "browser_go_back",
    "browser.go_forward", "tool_browser_go_forward", "browser_go_forward",
    "browser.switch_tab", "tool_browser_switch_tab", "browser_switch_tab",
    "browser.close_tab", "tool_browser_close_tab", "browser_close_tab",
    "browser.wait", "tool_browser_wait", "browser_wait",
    "hyperbridge.read_tab", "tool_hyperbridge_read_tab", "hyperbridge_read_tab",
    "hyperbridge.read_all_tabs", "tool_hyperbridge_read_all_tabs", "hyperbridge_read_all_tabs",
    # Office docs & artifacts
    "office.generate_docx", "tool_generate_word_report", "generate_word_report",
    "office.generate_xlsx", "tool_generate_excel_sheet", "generate_excel_sheet",
    "cowork.render_artifact", "tool_render_live_artifact", "render_live_artifact",
    # Workspace inspection
    "gmail.search", "tool_gmail_search", "gmail_search",
    "calendar.list", "tool_calendar_list", "calendar_list",
    "analyze_image", "tool_analyze_image", "screen_vision", "tool_screen_vision",
    "google_search_grounding", "tool_google_search_grounding",
    "cloud_code_execution", "tool_cloud_code_execution",
    "url_context", "tool_url_context",
    "paper_trending_harvester", "tool_paper_trending_harvester",
    "render_retina_slide", "tool_render_retina_slide",
    "render_dual_voice_podcast", "tool_render_dual_voice_podcast",
}

# Monkey patching eliminated: ActionGateway.evaluate natively enforces RING_0_EXTENDED_TOOLS and safety invariants.

