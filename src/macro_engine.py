"""src/macro_engine.py - Phase 4 Structural Triplet Muscle Memory Engine.

Provides an ultra-fast (<2ms) local SQLite perception cache for recurring web elements.
Caches verified structural locators: (domain, parent_role, element_role, accessible_name)
and executes soft-deletes upon selector drift to trigger self-healing ReAct fallbacks.

ENFORCES ZERO HARD DELETES: Only soft-deletes via `deleted_at = CURRENT_TIMESTAMP`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("MacroEngine")

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "jai_memory.db"


def _normalize_token(text: Optional[str]) -> str:
    """Normalize semantic tokens for deterministic triplet hashing and lookups."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", str(text).strip().lower())
    return cleaned[:100]


def get_db_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Obtain a SQLite connection with WAL mode enabled and auto-created parent directories."""
    try:
        from src.sqlite_writer import get_configured_db_connection
        return get_configured_db_connection(db_path)
    except Exception:
        path = Path(db_path) if db_path else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.debug("PRAGMA journal_mode=WAL failed: %s", e)
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA wal_autocheckpoint=0;")
        return conn


try:
    from src.sqlite_writer import init_triplet_schema
    from src.browser.macros import (
        init_macro_schema as bm_init_macro_schema,
        save_macro as bm_save_macro,
        soft_delete_macro as bm_soft_delete,
        resolve_macro as bm_resolve_macro,
        execute_macro as bm_execute_macro,
        execute_resolved_macro as bm_execute_resolved_macro,
    )
    init_macro_schema = bm_init_macro_schema
    resolve_macro = bm_resolve_macro
    execute_macro = bm_execute_macro
    execute_resolved_macro = bm_execute_resolved_macro
except ImportError:
    pass


def build_triplet_key(
    domain: str,
    parent_role: str,
    element_role: str,
    accessible_name: str,
    action_type: str = "click",
) -> str:
    """Construct a canonical, deterministic primary key for a structural triplet."""
    d = _normalize_token(domain)
    pr = _normalize_token(parent_role) or "none"
    er = _normalize_token(element_role) or "element"
    an = _normalize_token(accessible_name)
    act = _normalize_token(action_type) or "click"
    return f"{d}|{act}|{pr}|{er}|{an}"


def record_triplet(
    domain: str,
    parent_role: str = "none",
    element_role: str = "element",
    accessible_name: str = "",
    action_type: str = "click",
    db_path: Optional[Union[str, Path]] = None,
    action_intent: Optional[str] = None,
    container_role: Optional[str] = None,
    action_payload: Optional[str] = None,
    form_action_slug: Optional[str] = None,
    tag: Optional[str] = None,
    input_type: Optional[str] = None,
    name_attr: Optional[str] = None,
    id_attr: Optional[str] = None,
    aria_role: Optional[str] = None,
    relative_index: int = 0,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Record or revive a verified structural triplet upon successful mutation.

    Uses non-blocking background queue (< 1ms foreground latency) with soft-resurrection:
    if a previously soft-deleted triplet succeeds again, resets `deleted_at = NULL`.
    """
    key = build_triplet_key(domain, parent_role, element_role, accessible_name, action_type)
    d = _normalize_token(domain)
    pr = _normalize_token(parent_role) or _normalize_token(container_role) or "none"
    er = _normalize_token(element_role) or "element"
    an = _normalize_token(accessible_name)
    act = _normalize_token(action_type) or "click"
    cr = _normalize_token(container_role) or pr
    intent = action_intent or kwargs.get("action_intent") or ""
    payload = action_payload or kwargs.get("action_payload") or ""

    extra = {
        "action_intent": intent,
        "container_role": cr,
        "action_payload": payload,
        "form_action_slug": form_action_slug or kwargs.get("form_action_slug"),
        "tag": tag or kwargs.get("tag"),
        "input_type": input_type or kwargs.get("input_type"),
        "name_attr": name_attr or kwargs.get("name_attr"),
        "id_attr": id_attr or kwargs.get("id_attr"),
        "aria_role": aria_role or kwargs.get("aria_role"),
        "relative_index": relative_index or kwargs.get("relative_index", 0),
    }

    try:
        from src.sqlite_writer import enqueue_triplet_write
        enqueue_triplet_write(
            triplet_key=key,
            domain=d,
            parent_role=pr,
            element_role=er,
            accessible_name=an,
            action_type=act,
            db_path=db_path,
            extra_fields=extra,
        )
    except Exception as e:
        logger.warning("Failed to enqueue triplet write: %s; falling back to direct write", e)
        init_triplet_schema(db_path)
        conn = get_db_connection(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO browser_triplets (
                        triplet_key, domain, action_intent, container_role, parent_role, element_role, accessible_name, action_type, action_payload,
                        form_action_slug, tag, input_type, name_attr, id_attr, aria_role, relative_index, success_count, miss_count,
                        created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
                    ON CONFLICT(triplet_key) DO UPDATE SET
                        deleted_at = NULL,
                        miss_count = 0,
                        success_count = browser_triplets.success_count + 1,
                        updated_at = CURRENT_TIMESTAMP,
                        action_intent = COALESCE(excluded.action_intent, browser_triplets.action_intent),
                        container_role = COALESCE(excluded.container_role, browser_triplets.container_role),
                        action_payload = COALESCE(excluded.action_payload, browser_triplets.action_payload),
                        form_action_slug = COALESCE(excluded.form_action_slug, browser_triplets.form_action_slug),
                        tag = COALESCE(excluded.tag, browser_triplets.tag),
                        input_type = COALESCE(excluded.input_type, browser_triplets.input_type),
                        name_attr = COALESCE(excluded.name_attr, browser_triplets.name_attr),
                        id_attr = COALESCE(excluded.id_attr, browser_triplets.id_attr),
                        aria_role = COALESCE(excluded.aria_role, browser_triplets.aria_role),
                        relative_index = COALESCE(excluded.relative_index, browser_triplets.relative_index)
                    """,
                    (key, d, intent, cr, pr, er, an, act, payload,
                     extra.get("form_action_slug"), extra.get("tag"), extra.get("input_type"),
                     extra.get("name_attr"), extra.get("id_attr"), extra.get("aria_role"), extra.get("relative_index", 0)),
                )
        finally:
            conn.close()

    logger.debug("Enqueued structural triplet write: %s", key)
    return {
        "triplet_key": key,
        "domain": d,
        "action_intent": intent,
        "container_role": cr,
        "parent_role": pr,
        "element_role": er,
        "accessible_name": an,
        "action_type": act,
        "action_payload": payload,
        "status": "RECORDED",
        **extra,
    }


def soft_delete_triplet(triplet_key: str, db_path: Optional[Union[str, Path]] = None) -> bool:
    """Soft-delete a stale or drifted triplet. ZERO hard deletions permitted."""
    try:
        from src.sqlite_writer import enqueue_soft_delete
        enqueue_soft_delete(triplet_key, db_path=db_path)
        logger.debug("Enqueued soft-delete for stale triplet: %s", triplet_key)
        return True
    except Exception as e:
        logger.debug("enqueue_soft_delete fallback: %s", e)

    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                """
                UPDATE browser_triplets
                SET deleted_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    miss_count = CASE WHEN miss_count < 2 THEN 2 ELSE miss_count END
                WHERE triplet_key = ? AND deleted_at IS NULL
                """,
                (triplet_key,),
            )
            updated = cur.rowcount > 0
            if updated:
                logger.warning("Soft-deleted stale triplet (selector drift): %s", triplet_key)
            return True
    finally:
        conn.close()


def record_triplet_miss(triplet_key: str, miss_count: int = 1, db_path: Optional[Union[str, Path]] = None) -> None:
    """Record an incremental miss count on a triplet."""
    try:
        from src.sqlite_writer import enqueue_triplet_miss
        enqueue_triplet_miss(triplet_key, miss_count, db_path=db_path)
        return
    except Exception as e:
        logger.debug("enqueue_triplet_miss fallback: %s", e)

    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE browser_triplets SET miss_count = ?, updated_at = CURRENT_TIMESTAMP WHERE triplet_key = ?",
                (miss_count, triplet_key),
            )
    finally:
        conn.close()


def record_miss(triplet_identifier: Union[int, str], db_path: Optional[Union[str, Path]] = None) -> int:
    """Record a miss on a triplet by its integer id or canonical triplet_key.
    Increments miss_count. If miss_count >= 2, soft-deletes the triplet.
    """
    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        with conn:
            is_numeric = isinstance(triplet_identifier, int) or (
                isinstance(triplet_identifier, str) and triplet_identifier.isdigit()
            )
            if is_numeric:
                row = conn.execute(
                    "SELECT id, triplet_key, miss_count FROM browser_triplets WHERE id = ?",
                    (int(triplet_identifier),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, triplet_key, miss_count FROM browser_triplets WHERE triplet_key = ?",
                    (str(triplet_identifier),),
                ).fetchone()

            if not row:
                return 0

            t_id = row["id"]
            t_key = row["triplet_key"] or ""
            new_miss = int(row["miss_count"] or 0) + 1

            if new_miss >= 2:
                conn.execute(
                    """
                    UPDATE browser_triplets
                    SET miss_count = ?, deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_miss, t_id),
                )
                if t_key:
                    try:
                        from src.sqlite_writer import enqueue_soft_delete
                        enqueue_soft_delete(t_key, db_path=db_path)
                    except Exception as e:
                        logger.debug("enqueue_soft_delete on strike 2 fallback: %s", e)
            else:
                conn.execute(
                    """
                    UPDATE browser_triplets
                    SET miss_count = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_miss, t_id),
                )
                if t_key:
                    try:
                        from src.sqlite_writer import enqueue_triplet_miss
                        enqueue_triplet_miss(t_key, new_miss, db_path=db_path)
                    except Exception as e:
                        logger.debug("enqueue_triplet_miss on miss fallback: %s", e)

            return new_miss
    finally:
        conn.close()


def record_triplet_hit(triplet_key: str, db_path: Optional[Union[str, Path]] = None) -> None:
    """Reset miss count to 0 and increment success_count upon a verified successful match."""
    try:
        from src.sqlite_writer import enqueue_triplet_hit
        enqueue_triplet_hit(triplet_key, db_path=db_path)
        return
    except Exception as e:
        logger.debug("enqueue_triplet_hit fallback: %s", e)

    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE browser_triplets
                SET miss_count = 0,
                    success_count = success_count + 1,
                    updated_at = CURRENT_TIMESTAMP,
                    last_verified_at = CURRENT_TIMESTAMP
                WHERE triplet_key = ?
                """,
                (triplet_key,),
            )
    finally:
        conn.close()


def get_triplet(triplet_key: str, db_path: Optional[Union[str, Path]] = None) -> Optional[Dict[str, Any]]:
    """Retrieve a triplet record by its canonical key, including soft-deleted status."""
    try:
        from src.sqlite_writer import get_pending_cached_triplet
        pending = get_pending_cached_triplet(triplet_key)
        if pending is not None:
            return pending
    except Exception as e:
        logger.debug("get_pending_cached_triplet check failed: %s", e)

    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT *
            FROM browser_triplets
            WHERE triplet_key = ?
            """,
            (triplet_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_active_triplets(
    domain: Optional[str] = None,
    action_type: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Query all active (non-soft-deleted) triplets for a domain/action."""
    init_triplet_schema(db_path)
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM browser_triplets WHERE deleted_at IS NULL"
        params: List[Any] = []
        if domain:
            query += " AND domain = ?"
            params.append(_normalize_token(domain))
        if action_type:
            query += " AND action_type = ?"
            params.append(_normalize_token(action_type))
        query += " ORDER BY created_at DESC"

        rows = conn.execute(query, params).fetchall()
        results = [dict(r) for r in rows]

        # Check pending cache for uncommitted entries
        try:
            from src.sqlite_writer import _PENDING_TRIPLETS_CACHE, _CACHE_LOCK
            with _CACHE_LOCK:
                # Filter out any keys that have been marked deleted in the in-memory cache
                results = [
                    r for r in results
                    if _PENDING_TRIPLETS_CACHE.get(r.get("triplet_key", ""), {}).get("deleted_at") is None
                ]
                existing_keys = {r["triplet_key"] for r in results}
                for k, v in _PENDING_TRIPLETS_CACHE.items():
                    if v.get("deleted_at") is None and k not in existing_keys:
                        if domain and _normalize_token(domain) != v.get("domain"):
                            continue
                        if action_type and _normalize_token(action_type) != v.get("action_type"):
                            continue
                        results.append(dict(v))
        except Exception as e:
            logger.debug("Pending triplets cache lookup failed: %s", e)

        return results
    finally:
        conn.close()


def resolve_by_triplet(
    domain: str,
    action_intent: str,
    action_type: str = "click",
    active_cache: Optional[Dict[int, Dict[str, Any]]] = None,
    page: Any = None,
    db_path: Optional[Union[str, Path]] = None,
    miss_threshold: Optional[int] = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Locate an element via structural muscle memory using weighted multi-anchor matching.

    Scores candidates by structural anchors:
      tag (+0.25) + input_type (+0.20) + name_attr (+0.30) + form_action_slug (+0.15) + aria_role (+0.10)
    Resolves strictly against visible, non-occluded nodes (never hidden inputs).
    Tolerates label/placeholder drift with score >= 0.85.
    Requires 2 consecutive misses (miss_count >= 2) before soft-deleting.
    """
    norm_domain = _normalize_token(domain)
    norm_intent = _normalize_token(action_intent)
    norm_action = _normalize_token(action_type)

    triplets = list_active_triplets(domain=norm_domain, action_type=norm_action, db_path=db_path)
    if not triplets:
        return None

    # Filter candidate triplets matching action_intent or domain
    candidate_triplets = []
    intent_tokens = set(re.findall(r"\w+", norm_intent))
    for trip in triplets:
        name = trip.get("accessible_name", "")
        role = trip.get("element_role", "")
        name_attr = trip.get("name_attr", "")
        name_tokens = set(re.findall(r"\w+", name))
        jaccard = len(intent_tokens & name_tokens) / len(intent_tokens | name_tokens) if (intent_tokens | name_tokens) else 0.0
        if (
            norm_intent in name
            or name in norm_intent
            or jaccard >= 0.5
            or norm_intent == role
            or norm_intent == name_attr
            or not norm_intent
        ):
            candidate_triplets.append(trip)

    if not candidate_triplets:
        for trip in triplets:
            if any(tok in trip.get("accessible_name", "") for tok in norm_intent.split()):
                candidate_triplets.append(trip)

    if not candidate_triplets:
        candidate_triplets = triplets

    # Retrieve active_cache
    cache = active_cache
    if cache is None:
        try:
            import src.jai_browser as jb
            target_page = page or (jb.get_foreground_page() if hasattr(jb, "get_foreground_page") else None)
            target_id = jb._get_page_target_id(target_page) if (target_page and hasattr(jb, "_get_page_target_id")) else None
            dom_cache = getattr(jb, "_ACTIVE_DOM_CACHE", None)
            if dom_cache is not None:
                if hasattr(dom_cache, "get_tab_cache") and target_id:
                    cache = dom_cache.get_tab_cache(target_id)
                elif hasattr(dom_cache, "get_tab_cache"):
                    cache = dom_cache.get_tab_cache()
                else:
                    cache = dom_cache
            else:
                cache = {}
        except Exception as e:
            logger.debug("Failed resolving foreground page cache in resolve_by_triplet: %s", e)
            cache = {}
    if cache is None:
        cache = {}

    for trip in candidate_triplets:
        t_key = trip.get("triplet_key", "")
        t_name = _normalize_token(trip.get("accessible_name", ""))
        t_tag = _normalize_token(trip.get("tag", ""))
        t_input_type = _normalize_token(trip.get("input_type", ""))
        t_name_attr = _normalize_token(trip.get("name_attr", ""))
        t_form_slug = _normalize_token(trip.get("form_action_slug", ""))
        t_aria_role = _normalize_token(trip.get("aria_role", "") or trip.get("element_role", ""))

        best_score = 0.0
        best_id: Optional[int] = None

        # Scan active cache for matching element
        for eid, meta in cache.items():
            # 1. STRICT VISIBILITY & NON-OCCLUSION GUARD
            if meta.get("occluded_by_modal") or meta.get("occludedByModal"):
                continue
            if meta.get("disabled"):
                continue
            m_type = _normalize_token(meta.get("type", ""))
            if m_type == "hidden":
                continue
            rect = meta.get("rect", {})
            w = rect.get("w", rect.get("width", meta.get("w", 1)))
            h = rect.get("h", rect.get("height", meta.get("h", 1)))
            if w <= 0 or h <= 0:
                continue

            # 2. WEIGHTED MULTI-ANCHOR SCORING
            score = 0.0
            m_tag = _normalize_token(meta.get("tag", ""))
            m_name_attr = _normalize_token(meta.get("name") or meta.get("name_attr", ""))
            m_form_slug = _normalize_token(
                meta.get("form_action_slug", "") or meta.get("form_action", "") or meta.get("formAction", "")
            )
            m_aria_role = _normalize_token(meta.get("role", ""))
            m_acc_name = _normalize_token(meta.get("label") or meta.get("accessible_name") or meta.get("name", ""))

            # Tag match (+0.25)
            if t_tag and m_tag == t_tag:
                score += 0.25
            elif not t_tag and m_tag in ("input", "button", "a", "select", "textarea"):
                score += 0.15

            # Input type match (+0.20)
            if t_input_type and m_type == t_input_type:
                score += 0.20

            # Name attribute match (+0.30)
            if t_name_attr and m_name_attr == t_name_attr:
                score += 0.30

            # Form action slug match (+0.15)
            if t_form_slug and (
                t_form_slug in m_form_slug
                or t_form_slug in _normalize_token(meta.get("selector", ""))
                or t_form_slug in _normalize_token(meta.get("search_blob", ""))
                or ("search" in t_form_slug and "search" in m_form_slug)
            ):
                score += 0.15

            # Aria role / Element role match (+0.20)
            t_elem_role = _normalize_token(trip.get("element_role", ""))
            if t_aria_role and m_aria_role == t_aria_role:
                score += 0.20
            elif t_elem_role and (m_tag == t_elem_role or m_aria_role == t_elem_role):
                score += 0.20

            # Parent role match (+0.20)
            t_parent_role = _normalize_token(trip.get("parent_role", ""))
            m_parent_role = _normalize_token(meta.get("parent_role", ""))
            if t_parent_role and m_parent_role == t_parent_role:
                score += 0.20

            # Accessible name match (+0.40 exact, +0.35 fuzzy)
            if t_name:
                t_tokens = set(re.findall(r"\w+", t_name))
                m_tokens = set(re.findall(r"\w+", m_acc_name))
                jaccard_m = len(t_tokens & m_tokens) / len(t_tokens | m_tokens) if (t_tokens | m_tokens) else 0.0
                if t_name == m_acc_name:
                    score += 0.40
                elif t_name in m_acc_name or m_acc_name in t_name or jaccard_m >= 0.5:
                    score += 0.35
                elif any(tok in m_acc_name for tok in t_tokens if len(tok) > 2):
                    score += 0.20

            # Id attribute match (+0.15)
            t_id_attr = _normalize_token(trip.get("id_attr", ""))
            m_id_attr = _normalize_token(meta.get("id_attr") or meta.get("id") or "")
            if t_id_attr and m_id_attr == t_id_attr:
                score += 0.15

            if score > best_score:
                best_score = score
                best_id = eid

        # Acceptance threshold: 0.85 with drift tolerance
        if best_id is not None and best_score >= 0.85:
            logger.info("Fast-path resolved [%s] via multi-anchor triplet %s (score=%.2f)", best_id, t_key, best_score)
            record_triplet_hit(t_key, db_path=db_path)

            return {
                "ok": True,
                "element_id": best_id,
                "triplet_key": t_key,
                "triplet": trip,
                "cached_meta": cache[best_id],
                "score": round(best_score, 2),
                "confidence": round(best_score, 2),
                "fast_path": True,
            }

        # 3. SELF-HEALING SOFT-DELETE ON MISS
        # Critical form submissions with form_action_slug require 2 consecutive misses before soft-deleting.
        # Generic unanchored triplets undergo immediate self-healing soft-delete on selector drift.
        threshold = miss_threshold or kwargs.get("miss_threshold")
        if threshold is None:
            threshold = 2 if trip.get("form_action_slug") else 1

        curr_miss = int(trip.get("miss_count", 0) or 0) + 1
        if curr_miss >= threshold:
            logger.warning(
                "Selector drift confirmed (miss_count=%d >= %d) for triplet %s; self-healing soft-delete triggered.",
                curr_miss, threshold, t_key,
            )
            soft_delete_triplet(t_key, db_path=db_path)
        else:
            logger.info(
                "Miss recorded (miss_count=%d < %d) for triplet %s; keeping active.",
                curr_miss, threshold, t_key,
            )
            record_triplet_miss(t_key, curr_miss, db_path=db_path)

    return None


def invalidate_on_no_effect(triplet_key: str, db_path: Optional[Union[str, Path]] = None) -> bool:
    """Invalidate a triplet that resolved but produced NO_EFFECT upon actuation."""
    return soft_delete_triplet(triplet_key, db_path=db_path)


# ==============================================================================
# Macro Caching & Fast-Path Replay Runner (Zero Hard-Deletes)
# ==============================================================================

def _init_macros_v2_schema(con: sqlite3.Connection) -> None:
    """Initialize schema for browser_macros_v2 without hard deletions."""
    with con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS browser_macros_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                macro_name TEXT NOT NULL,
                step_order INTEGER NOT NULL,
                parent_role TEXT DEFAULT '',
                aria_role TEXT DEFAULT '',
                normalized_text TEXT DEFAULT '',
                relative_index INTEGER DEFAULT 0,
                action TEXT DEFAULT 'click',
                value TEXT DEFAULT '',
                text TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP DEFAULT NULL
            );
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_bm_v2_domain_macro
            ON browser_macros_v2 (domain, macro_name, deleted_at, step_order);
            """
        )


def connect(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Connect to SQLite database with WAL mode enabled and initialize macro tables."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    _init_macros_v2_schema(conn)
    return conn


def save_macro(
    domain_or_name: Optional[str] = None,
    name_or_domain: Optional[str] = None,
    steps_or_text: Optional[Union[List[Dict[str, Any]], str]] = None,
    db_path: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Any:
    if domain_or_name is None:
        domain_or_name = kwargs.pop("macro_name_or_domain", None) or kwargs.pop("domain", None) or kwargs.pop("macro_name", "localhost")
    if name_or_domain is None:
        name_or_domain = kwargs.pop("domain_or_aria_role", None) or kwargs.pop("macro_name", None) or kwargs.pop("aria_role", "macro")
    """Store or update a cached multi-step macro or structural element macro. ZERO hard deletions permitted."""
    steps = steps_or_text if steps_or_text is not None else kwargs.get("steps")
    is_workflow = isinstance(steps, list) or (
        isinstance(steps, str) and steps.strip().startswith("[")
    )

    if is_workflow and steps is not None:
        if isinstance(steps, str):
            try:
                parsed_steps = json.loads(steps)
            except Exception:
                parsed_steps = []
        else:
            parsed_steps = list(steps)

        a = str(domain_or_name).strip()
        b = str(name_or_domain).strip()
        if "." in a or "/" in a:
            domain = a.lower()
            macro_name = b.lower()
        elif "." in b or "/" in b:
            domain = b.lower()
            macro_name = a.lower()
        else:
            domain = a.lower()
            macro_name = b.lower()

        con = connect(db_path)
        with con:
            con.execute(
                """
                UPDATE browser_macros_v2
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE domain = ? AND macro_name = ? AND deleted_at IS NULL
                """,
                (domain, macro_name),
            )
            for idx, s in enumerate(parsed_steps):
                action = str(s.get("action", "click")).strip().lower()
                val = str(s.get("value", ""))
                parent_role = str(s.get("parent_role", "")).strip().lower()
                aria_role = str(s.get("aria_role", "")).strip().lower()
                text_raw = str(s.get("text", ""))
                norm_text = str(s.get("normalized_text") or s.get("text", "")).strip().lower()
                rel_idx = int(s.get("relative_index", idx))
                con.execute(
                    """
                    INSERT INTO browser_macros_v2 (
                        domain, macro_name, step_order, parent_role, aria_role,
                        normalized_text, relative_index, action, value, text,
                        created_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                    """,
                    (domain, macro_name, idx, parent_role, aria_role, norm_text, rel_idx, action, val, text_raw),
                )
        return {"domain": domain, "macro_name": macro_name, "steps": len(parsed_steps), "status": "SAVED"}

    # Triplet fallback when steps is not a list
    from src.browser.macros import save_macro as bm_save_macro
    return bm_save_macro(
        domain_or_name,
        name_or_domain,
        normalized_text=str(steps_or_text) if steps_or_text is not None else None,
        db_path=db_path,
        **kwargs,
    )


def find_macro(
    domain: str,
    query: str,
    db_path: Optional[Union[str, Path]] = None,
) -> List[sqlite3.Row]:
    """Retrieve all active step records for a macro by exact or fuzzy token match."""
    con = connect(db_path)
    d = str(domain).strip().lower()
    q = str(query).strip().lower()
    q_words = set(re.findall(r"\w+", q))

    # 1. Exact match on macro_name
    rows = con.execute(
        """
        SELECT * FROM browser_macros_v2
        WHERE domain = ? AND macro_name = ? AND deleted_at IS NULL
        ORDER BY step_order ASC
        """,
        (d, q),
    ).fetchall()
    if rows:
        return rows

    # 2. Fuzzy / token-containment search across active macros for this domain
    macro_names = [
        r[0] for r in con.execute(
            "SELECT DISTINCT macro_name FROM browser_macros_v2 WHERE domain = ? AND deleted_at IS NULL",
            (d,),
        ).fetchall()
    ]
    best_macro = None
    best_score = 0.0

    for m_name in macro_names:
        m_words = set(re.findall(r"\w+", m_name.lower()))
        if q_words and q_words.issubset(m_words):
            best_macro = m_name
            break
        intersection = len(q_words & m_words)
        union = len(q_words | m_words)
        score = intersection / union if union > 0 else 0
        if score > best_score and score >= 0.5:
            best_score = score
            best_macro = m_name

    if best_macro:
        return con.execute(
            """
            SELECT * FROM browser_macros_v2
            WHERE domain = ? AND macro_name = ? AND deleted_at IS NULL
            ORDER BY step_order ASC
            """,
            (d, best_macro),
        ).fetchall()

    return []


def get_macro(
    macro_name: str,
    domain: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve an active cached macro by name."""
    con = connect(db_path)
    clean_name = _normalize_token(macro_name)
    query = "SELECT * FROM browser_macros_v2 WHERE macro_name = ? AND deleted_at IS NULL"
    params = [clean_name]
    if domain:
        query += " AND domain = ?"
        params.append(_normalize_token(domain))
    row = con.execute(query, params).fetchone()
    return dict(row) if row else None


def soft_delete_macro(
    domain_or_name: str,
    macro_name: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> int:
    """Soft-delete a cached macro upon redesign or failure. ZERO hard deletions permitted."""
    if "db_path" in kwargs and db_path is None:
        db_path = kwargs.pop("db_path")
    con = connect(db_path)
    a = str(domain_or_name).strip().lower()
    if macro_name:
        b = str(macro_name).strip().lower()
        if "." in a:
            domain, name = a, b
        elif "." in b:
            domain, name = b, a
        else:
            domain, name = a, b
        with con:
            cur = con.execute(
                """
                UPDATE browser_macros_v2
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE domain = ? AND macro_name = ? AND deleted_at IS NULL
                """,
                (domain, name),
            )
            return cur.rowcount
    else:
        with con:
            cur = con.execute(
                """
                UPDATE browser_macros_v2
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE (domain = ? OR macro_name = ?) AND deleted_at IS NULL
                """,
                (a, a),
            )
            return cur.rowcount


def replay_macro(
    domain_or_name: str,
    macro_name_or_domain: Optional[str] = None,
    executor: Optional[Callable[[Dict[str, Any]], bool]] = None,
    gateway: Optional[Any] = None,
    db_path: Optional[Union[str, Path]] = None,
    page: Optional[Any] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Replay a cached macro via the fast-path runner with Step and Rule 0 checks."""
    t0 = time.perf_counter()
    if "db_path" in kwargs and db_path is None:
        db_path = kwargs.pop("db_path")
    if "executor" in kwargs and executor is None:
        executor = kwargs.pop("executor")

    a = str(domain_or_name).strip()
    b = str(macro_name_or_domain).strip() if macro_name_or_domain else ""
    if "." in a or "/" in a:
        domain = a.lower()
        m_name = b.lower()
    elif "." in b or "/" in b:
        domain = b.lower()
        m_name = a.lower()
    else:
        domain = a.lower()
        m_name = b.lower()

    steps = find_macro(domain, m_name, db_path=db_path)
    if not steps:
        ms = (time.perf_counter() - t0) * 1000
        return {
            "hit": False,
            "total": 0,
            "ms": ms,
            "completed": False,
            "ran": 0,
            "failed_step": None,
            "ok": False,
        }

    total = len(steps)
    ran = 0
    failed_step = None
    completed = True

    for s in steps:
        step_dict = dict(s)
        if executor is not None:
            try:
                res = executor(step_dict)
                if not res:
                    failed_step = step_dict.get("step_order", ran)
                    completed = False
                    break
            except Exception:
                failed_step = step_dict.get("step_order", ran)
                completed = False
                break
        ran += 1

    ms = (time.perf_counter() - t0) * 1000
    return {
        "hit": True,
        "total": total,
        "ms": ms,
        "completed": completed,
        "ran": ran,
        "failed_step": failed_step,
        "ok": completed,
    }


def handle_learn_command(
    cmd: str,
    domain: str,
    steps: List[Dict[str, Any]],
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[str]:
    """Parse a /learn or save routine command and persist steps as a named macro."""
    c = (cmd or "").strip()
    c_lower = c.lower()
    routine_name = ""
    if c_lower.startswith("/learn"):
        routine_name = c[6:].strip()
    elif c_lower.startswith("save routine"):
        routine_name = c[12:].strip()
    else:
        return None

    if not steps:
        return f"No recent steps recorded to save routine '{routine_name}'."

    save_macro(domain, routine_name, steps, db_path=db_path)
    return f"Saved routine '{routine_name}' with {len(steps)} steps."


__all__ = [
    "build_triplet_key",
    "record_triplet",
    "soft_delete_triplet",
    "record_triplet_miss",
    "record_miss",
    "record_triplet_hit",
    "get_triplet",
    "list_active_triplets",
    "resolve_by_triplet",
    "invalidate_on_no_effect",
    "connect",
    "save_macro",
    "get_macro",
    "find_macro",
    "soft_delete_macro",
    "replay_macro",
    "handle_learn_command",
    "resolve_macro",
    "execute_macro",
    "execute_resolved_macro",
    "init_macro_schema",
]



# Re-export two-stage macro resolution functions from src.browser.macros
try:
    from src.browser.macros import (
        init_macro_schema,
        resolve_macro,
    )
except ImportError as e:
    logger.debug("src.browser.macros unavailable: %s", e)
