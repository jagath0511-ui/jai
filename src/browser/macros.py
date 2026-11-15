"""src/browser/macros.py - Phase 4 Structural Muscle Memory & Macro Resolution Engine.

Enforces:
1. Strict soft-delete architecture: Zero hard DELETE statements (deleted_at IS NULL).
2. Two-stage macro resolution:
   - Stage 1 (Exact Match): Query (domain, aria_role, normalized_text) where deleted_at IS NULL.
   - Stage 2 (Fuzzy Fallback): If exact match misses, query active macros matching (domain, aria_role)
     and compute token-set Jaccard similarity (or prefix containment) combined with parent_container_role.
     If similarity >= 0.65, resolve macro and update last_verified_at = CURRENT_TIMESTAMP.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

logger = logging.getLogger("jai.browser.macros")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "jai_memory.db"


def _normalize_token(text: Optional[str]) -> str:
    """Normalize semantic tokens for deterministic macro storage and matching."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", str(text).strip().lower())
    return cleaned[:120]


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase alphanumeric words."""
    if not text:
        return set()
    return set(re.findall(r"\w+", text.lower()))


def get_db_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Obtain SQLite connection with WAL mode and row factory."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_macro_schema(db_path: Optional[Union[str, Path]] = None) -> None:
    """Initialize schema for browser_macros_v2 and ensure all required columns exist."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_macros_v2 (
                    macro_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    aria_role TEXT NOT NULL DEFAULT '',
                    parent_container_role TEXT DEFAULT '',
                    normalized_text TEXT NOT NULL DEFAULT '',
                    action_type TEXT DEFAULT 'click',
                    action_payload TEXT DEFAULT '{}',
                    last_verified_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP DEFAULT NULL
                );
                """
            )
            # Ensure columns exist if table was previously created with older schema
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(browser_macros_v2);")
            existing_cols = {r[1] for r in cur.fetchall()}
            for col_def, col_name in [
                ("aria_role TEXT DEFAULT ''", "aria_role"),
                ("parent_container_role TEXT DEFAULT ''", "parent_container_role"),
                ("normalized_text TEXT DEFAULT ''", "normalized_text"),
                ("action_type TEXT DEFAULT 'click'", "action_type"),
                ("last_verified_at TIMESTAMP", "last_verified_at"),
                ("macro_id TEXT", "macro_id"),
                ("macro_name TEXT DEFAULT ''", "macro_name"),
                ("step_order INTEGER DEFAULT 0", "step_order"),
                ("action_payload TEXT DEFAULT '{}'", "action_payload"),
            ]:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE browser_macros_v2 ADD COLUMN {col_def};")

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_browser_macros_exact
                ON browser_macros_v2 (domain, aria_role, normalized_text, deleted_at);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_browser_macros_fuzzy
                ON browser_macros_v2 (domain, aria_role, deleted_at);
                """
            )
    finally:
        conn.close()


def save_macro(
    domain: str,
    aria_role: str,
    normalized_text: Optional[str] = None,
    parent_container_role: Optional[str] = None,
    action_type: str = "click",
    action_payload: Optional[Dict[str, Any]] = None,
    db_path: Optional[Union[str, Path]] = None,
    *,
    text: Optional[str] = None,
    css_selector: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Save or revive a macro triplet in browser_macros_v2."""
    init_macro_schema(db_path)
    if normalized_text is None:
        normalized_text = text or ""
    d_clean = _normalize_token(domain)
    r_clean = _normalize_token(aria_role) or "element"
    t_clean = _normalize_token(normalized_text)
    p_clean = _normalize_token(parent_container_role)
    payload_dict = dict(action_payload or {})
    if css_selector:
        payload_dict["css_selector"] = css_selector
        payload_dict["selector"] = css_selector
    for k, v in kwargs.items():
        if k not in payload_dict:
            payload_dict[k] = v
    payload_str = json.dumps(payload_dict, separators=(",", ":"))
    macro_id = f"{d_clean}_{r_clean}_{t_clean}_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection(db_path)
    try:
        with conn:
            # Check for existing soft-deleted entry to resurrect
            row = conn.execute(
                """
                SELECT macro_id FROM browser_macros_v2
                WHERE domain = ? AND aria_role = ? AND normalized_text = ? AND deleted_at IS NOT NULL
                LIMIT 1
                """,
                (d_clean, r_clean, t_clean),
            ).fetchone()

            if row:
                m_id = row["macro_id"]
                conn.execute(
                    """
                    UPDATE browser_macros_v2
                    SET deleted_at = NULL, last_verified_at = ?, action_type = ?,
                        parent_container_role = ?, action_payload = ?
                    WHERE macro_id = ?
                    """,
                    (now_iso, action_type, p_clean, payload_str, m_id),
                )
                return {"macro_id": m_id, "status": "RESURRECTED"}

            # Check existing columns to satisfy any legacy NOT NULL constraints
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(browser_macros_v2);")
            col_names = {r[1] for r in cur.fetchall()}

            cols = ["macro_id", "domain", "aria_role", "parent_container_role", "normalized_text", "action_type", "action_payload", "last_verified_at"]
            vals = [macro_id, d_clean, r_clean, p_clean, t_clean, action_type, payload_str, now_iso]
            if "action_intent" in col_names:
                cols.append("action_intent")
                vals.append(str(kwargs.get("action_intent") or f"{action_type} {t_clean}".strip() or "action"))
            if "structural_triplet" in col_names:
                cols.append("structural_triplet")
                vals.append(f"{r_clean} {t_clean}".strip() or "element")

            placeholders = ", ".join(["?"] * len(cols))
            cols_joined = ", ".join(cols)
            conn.execute(
                f"INSERT INTO browser_macros_v2 ({cols_joined}) VALUES ({placeholders})",
                vals,
            )
            return {"macro_id": macro_id, "status": "SAVED"}
    finally:
        conn.close()


def soft_delete_macro(
    domain: str,
    aria_role: Optional[str] = None,
    normalized_text: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """Soft delete macro records by updating deleted_at (Strictly NO hard deletes)."""
    init_macro_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        with conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            query = "UPDATE browser_macros_v2 SET deleted_at = ? WHERE domain = ? AND deleted_at IS NULL"
            params: List[Any] = [now_iso, _normalize_token(domain)]
            if aria_role:
                query += " AND aria_role = ?"
                params.append(_normalize_token(aria_role))
            if normalized_text:
                query += " AND normalized_text = ?"
                params.append(_normalize_token(normalized_text))
            cur = conn.execute(query, params)
            return cur.rowcount
    finally:
        conn.close()


def resolve_macro(
    domain: str,
    aria_role: str,
    text: str,
    parent_container_role: Optional[str] = None,
    threshold: float = 0.65,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """Two-Stage Macro Resolution with Exact Match & Fuzzy Token-Set Fallback:

    Stage 1 (Exact Match):
      Query (domain, aria_role, normalized_text) where deleted_at IS NULL.
      If matched, updates last_verified_at and returns immediately.

    Stage 2 (Fuzzy Fallback):
      If exact match misses, queries active macros matching (domain, aria_role) where deleted_at IS NULL.
      Computes token-set Jaccard similarity or prefix containment combined with parent_container_role.
      If similarity >= threshold (default 0.65), updates last_verified_at and returns the macro.
    """
    init_macro_schema(db_path)
    d_clean = _normalize_token(domain)
    r_clean = _normalize_token(aria_role)
    t_clean = _normalize_token(text)
    p_clean = _normalize_token(parent_container_role)
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection(db_path)
    try:
        # --- Stage 1: Exact Match ---
        row = conn.execute(
            """
            SELECT * FROM browser_macros_v2
            WHERE domain = ? AND aria_role = ? AND normalized_text = ? AND deleted_at IS NULL
            LIMIT 1
            """,
            (d_clean, r_clean, t_clean),
        ).fetchone()

        if row:
            macro_data = dict(row)
            with conn:
                conn.execute(
                    "UPDATE browser_macros_v2 SET last_verified_at = ? WHERE macro_id = ?",
                    (now_iso, macro_data["macro_id"]),
                )
            macro_data["match_type"] = "EXACT"
            macro_data["similarity"] = 1.0
            macro_data["last_verified_at"] = now_iso
            payload_raw = macro_data.get("action_payload")
            if payload_raw:
                try:
                    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                    if isinstance(payload, dict):
                        if "css_selector" in payload and "css_selector" not in macro_data:
                            macro_data["css_selector"] = payload["css_selector"]
                        elif "selector" in payload and "css_selector" not in macro_data:
                            macro_data["css_selector"] = payload["selector"]
                except Exception:
                    pass
            return macro_data

        # --- Stage 2: Fuzzy Token-Set Fallback ---
        candidates = conn.execute(
            """
            SELECT * FROM browser_macros_v2
            WHERE domain = ? AND aria_role = ? AND deleted_at IS NULL
            """,
            (d_clean, r_clean),
        ).fetchall()

        if not candidates:
            return None

        query_tokens = _tokenize(t_clean)
        best_candidate: Optional[Dict[str, Any]] = None
        best_similarity = 0.0

        for cand in candidates:
            c_text = cand["normalized_text"]
            cand_tokens = _tokenize(c_text)

            # Jaccard token similarity
            intersection = query_tokens & cand_tokens
            union = query_tokens | cand_tokens
            jaccard = len(intersection) / len(union) if union else 0.0

            # Prefix or substring containment boost
            is_contained = (t_clean in c_text) or (c_text in t_clean)
            if is_contained:
                base_sim = max(jaccard, 0.70)
            else:
                base_sim = jaccard

            # Container role alignment boost
            c_parent = cand["parent_container_role"] or ""
            if p_clean and c_parent:
                if p_clean == c_parent:
                    base_sim = min(1.0, base_sim + 0.15)
                else:
                    base_sim = max(0.0, base_sim - 0.10)

            if base_sim > best_similarity:
                best_similarity = base_sim
                best_candidate = dict(cand)

        if best_candidate and best_similarity >= threshold:
            with conn:
                conn.execute(
                    "UPDATE browser_macros_v2 SET last_verified_at = ? WHERE macro_id = ?",
                    (now_iso, best_candidate["macro_id"]),
                )
            best_candidate["match_type"] = "FUZZY"
            best_candidate["similarity"] = best_similarity
            best_candidate["last_verified_at"] = now_iso
            payload_raw = best_candidate.get("action_payload")
            if payload_raw:
                try:
                    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                    if isinstance(payload, dict):
                        if "css_selector" in payload and "css_selector" not in best_candidate:
                            best_candidate["css_selector"] = payload["css_selector"]
                        elif "selector" in payload and "css_selector" not in best_candidate:
                            best_candidate["css_selector"] = payload["selector"]
                except Exception:
                    pass
            return best_candidate

        return None
    finally:
        conn.close()


def execute_macro(macro: Dict[str, Any], page: Any = None) -> bool:
    """Execute action payload of a resolved macro on the Playwright page."""
    if not page or not macro:
        return False
    try:
        action_type = str(macro.get("action_type", "click")).lower()
        payload_raw = macro.get("action_payload", "{}")
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {})
        selector = payload.get("selector")
        aria_role = macro.get("aria_role")
        text = macro.get("normalized_text")

        # 1. Try selector if available
        if selector and hasattr(page, "locator"):
            loc = page.locator(selector)
            if action_type == "click":
                loc.click(timeout=5000)
                return True
            elif action_type == "fill":
                loc.fill(str(payload.get("value", "")), timeout=5000)
                return True

        # 2. Try get_by_role
        if aria_role and text and hasattr(page, "get_by_role"):
            loc = page.get_by_role(aria_role, name=text)
            if action_type == "click":
                loc.click(timeout=5000)
                return True
            elif action_type == "fill":
                loc.fill(str(payload.get("value", "")), timeout=5000)
                return True

        # 3. Try in-browser evaluate
        script = """(data) => {
            const role = data.role;
            const text = data.text;
            const action = data.action;
            const val = data.value || '';
            const candidates = Array.from(document.querySelectorAll(`[role="${role}"], button, input, a`));
            const el = candidates.find(e => (e.innerText || e.value || e.placeholder || '').toLowerCase().includes(text.toLowerCase()));
            if (el) {
                if (action === 'click') { el.click(); return true; }
                if (action === 'fill') { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); return true; }
            }
            return false;
        }"""
        return bool(page.evaluate(script, {
            "role": aria_role or "",
            "text": text or "",
            "action": action_type,
            "value": payload.get("value", "")
        }))
    except Exception as e:
        logger.debug(f"Failed to execute macro: {e}")
        return False


def execute_resolved_macro(
    domain: str,
    aria_role: str,
    text: str,
    page: Any,
    parent_container_role: Optional[str] = None,
    threshold: float = 0.65,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve macro via exact or fuzzy token-set lookup, execute on page, and update last_verified_at."""
    macro = resolve_macro(
        domain=domain,
        aria_role=aria_role,
        text=text,
        parent_container_role=parent_container_role,
        threshold=threshold,
        db_path=db_path,
    )
    if not macro:
        return None

    executed = execute_macro(macro, page=page)
    macro["executed"] = executed
    return macro


__all__ = [
    "DEFAULT_DB_PATH",
    "init_macro_schema",
    "save_macro",
    "soft_delete_macro",
    "resolve_macro",
    "execute_macro",
    "execute_resolved_macro",
]

