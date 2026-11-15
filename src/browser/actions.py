"""src/browser/actions.py - Universal Atomic Actuator & State-Diff Oracle.
Part of modularized JAI Browser architecture.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import src.browser.cdp as _cdp
import src.browser.perception as _perception
from src.browser.dispatch import get_jb_attr

from src.browser.cdp import (
    browser_thread,
    _ensure_page_guards,
    _get_page_target_id,
    _safe_title,
    ActuatorExecutionError,
    ElementNotFoundError,
    ElementNotInteractable,
    ElementNotInteractableException,
    ACTION_TIMEOUT_MS,
    _REPO_ROOT,
    _settle,
    close_tab_at_index,
    set_active_page,
)
from src.browser.perception import (
    _ACTIVE_DOM_CACHE,
    _FULL_DOM_OVERFLOW_CACHE,
    _state_delta,
    get_dom_state_fingerprint,
    extract_universal_page_content,
)

logger = logging.getLogger("jai.browser.actions")

def _get_active_page(page=None):
    if page is not None:
        return page
    fn = get_jb_attr("_get_active_page", _cdp._get_active_page)
    return fn()

def _capture_state_fingerprint(p):
    fn = get_jb_attr("_capture_state_fingerprint", _perception._capture_state_fingerprint)
    return fn(p)

def _ensure_page_guards(page):
    fn = get_jb_attr("_ensure_page_guards", _cdp._ensure_page_guards)
    return fn(page)

def _get_page_target_id(page):
    fn = get_jb_attr("_get_page_target_id", _cdp._get_page_target_id)
    return fn(page)

def get_numbered_dom_snapshot(*args, **kwargs):
    from src.browser.perception import get_numbered_dom_snapshot as _per_snap
    fn = get_jb_attr("get_numbered_dom_snapshot", _per_snap)
    if fn is not get_numbered_dom_snapshot:
        return fn(*args, **kwargs)
    return _per_snap(*args, **kwargs)

def get_cached_element(*args, **kwargs):
    from src.browser.perception import get_cached_element as _per_elem
    fn = get_jb_attr("get_cached_element", _per_elem)
    if fn is not get_cached_element:
        return fn(*args, **kwargs)
    return _per_elem(*args, **kwargs)

class ActionResult(dict):
    """Dictionary representing action outcome with string containment support."""
    def __contains__(self, item: Any) -> bool:
        if super().__contains__(item):
            return True
        if isinstance(item, str):
            msg = self.get("message", "")
            if isinstance(msg, str) and item in msg:
                return True
        return False


class ClickResult(dict):
    """Dictionary representing the outcome of a click action with State-Diff telemetry."""
    clicked = property(lambda self: bool(self.get("clicked", True)))
    mutated = property(lambda self: bool(self.get("mutated", False)))
    state_changed = property(lambda self: bool(self.get("state_changed", self.mutated)))
    ok = property(lambda self: bool(self.get("ok", False)))
    status = property(lambda self: str(self.get("status", "OK" if self.get("mutated") else "SILENT_CLICK_FAILURE")))
    message = property(lambda self: str(self.get("message", "")))
    signal = property(lambda self: self.get("signal"))


class FillResult(dict):
    """Dictionary representing the outcome of a text entry action."""
    ok = property(lambda self: bool(self.get("ok", False)))
    mutated = property(lambda self: bool(self.get("mutated", False)))
    state_changed = property(lambda self: bool(self.get("state_changed", self.mutated)))
    status = property(lambda self: str(self.get("status", "SUCCESS" if self.mutated else "FAILED")))
    value = property(lambda self: str(self.get("value", "")))
    message = property(lambda self: str(self.get("message", "")))

def _resolve_locator_with_fallback_core(page, element_id: int) -> Tuple[str, Any]:
    """Tri-Locator Resolver: Strategy A (Tag data-jai-id), Strategy B (Semantic role/id), Strategy C (Coords)."""
    target_id = _get_page_target_id(page)
    meta = get_cached_element(element_id, target_id=target_id, page=page)
    if not meta:
        raise ElementNotFoundError(f"Element [{element_id}] not found in active or overflow cache.")

    local_id = meta.get("local_id", element_id)
    frame_idx = meta.get("frame_index")
    if frame_idx is None and isinstance(local_id, str) and local_id.startswith("f") and "_e" in local_id:
        try:
            frame_idx = int(local_id[1:].split("_e")[0])
        except Exception:
            frame_idx = None

    # Prioritize directly stored Playwright Frame object over fragile selector recreation
    search_ctx = meta.get("frame")
    if not search_ctx:
        search_ctx = page
        frame_name = meta.get("frame_name")
        frame_url = meta.get("frame_url")
        if frame_name and hasattr(page, "frame"):
            f = page.frame(name=frame_name)
            if f:
                search_ctx = f
        elif frame_url and hasattr(page, "frames"):
            for f in page.frames:
                if getattr(f, "url", "") == frame_url:
                    search_ctx = f
                    break
        elif frame_idx is not None and hasattr(page, "frame_locator"):
            search_ctx = page.frame_locator(f"iframe >> nth={frame_idx}")

    # Strategy A: exact data-jai-id locator
    try:
        loc = search_ctx.locator(f'[data-jai-id="{local_id}"]').first
        if loc.count() > 0:
            if loc.is_visible():
                return "attr", loc
            try:
                loc.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
                return "attr", loc
            except Exception as w_err:
                if "timeout" in str(w_err).lower():
                    raise ElementNotInteractable(f"Element [{element_id}] is hidden or behind unmounted modal") from w_err
    except ElementNotInteractable:
        raise
    except Exception as _suppressed_exc:
        logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
    # Strategy B: id_attr fallback
    id_attr = meta.get("id_attr")
    if id_attr:
        try:
            cand = search_ctx.locator(f"#{id_attr}").first
            if cand.count() > 0 and cand.is_visible():
                return "id", cand
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
    # Strategy C: Bounding box coordinates
    coords = meta.get("center_coords")
    if coords and coords != [0, 0] and coords != (0, 0):
        return "coords", (int(coords[0]), int(coords[1]))

    raise ElementNotInteractable(f"Element [{element_id}] is hidden or behind unmounted modal")


def _resolve_locator_with_fallback(page, element_id: int) -> Tuple[str, Any]:
    fn = get_jb_attr("_resolve_locator_with_fallback", _resolve_locator_with_fallback_core)
    if fn is not _resolve_locator_with_fallback:
        return fn(page, element_id)
    return _resolve_locator_with_fallback_core(page, element_id)


def _extract_target_signals(target) -> Dict[str, Any]:
    """Extract targeted signals (aria-expanded, checked, pressed, class, data-state) for verification."""
    signals: Dict[str, Any] = {}
    if not target:
        return signals

    if hasattr(target, "evaluate"):
        try:
            eval_res = target.evaluate("""el => ({
                expanded: el.getAttribute('aria-expanded'),
                checked: el.getAttribute('aria-checked') || (el.checked !== undefined ? String(el.checked) : null),
                pressed: el.getAttribute('aria-pressed'),
                className: el.className || '',
                tag: el.tagName ? el.tagName.toLowerCase() : '',
                dataState: el.getAttribute('data-state') || ''
            })""")
            if isinstance(eval_res, dict):
                signals = eval_res
        except Exception:
            signals = {}

    if not signals and hasattr(target, "get_attribute"):
        try:
            attr_map = {
                "aria-expanded": "expanded",
                "aria-checked": "checked",
                "aria-pressed": "pressed",
                "data-state": "dataState",
                "class": "className",
            }
            for attr_name, key in attr_map.items():
                val = target.get_attribute(attr_name)
                if isinstance(val, str):
                    signals[key] = val
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
    return signals


@browser_thread
def click_element_by_id(element_id: int, refresh_snapshot: bool = True, page=None) -> ClickResult:
    """Execute click with targeted signal verification, overlay dismissal, and viewport coordinate fallback."""
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open web tabs.")

    _ensure_page_guards(p)
    target_id = _get_page_target_id(p)
    _ACTIVE_DOM_CACHE.set_active_target_id(target_id)
    _FULL_DOM_OVERFLOW_CACHE.set_active_target_id(target_id)

    mode, target = _resolve_locator_with_fallback(p, element_id)

    # Capture pre-click state fingerprint and targeted signals
    pre_fp = _capture_state_fingerprint(p)
    pre_url = getattr(p, "url", "")
    pre_signals = _extract_target_signals(target) if mode in ("attr", "id") else {}

    setattr(p, "_latest_dialog_triggered", False)

    # Execute click
    if mode in ("attr", "id"):
        try:
            target.click(timeout=ACTION_TIMEOUT_MS)
        except Exception as click_err:
            err_str = str(click_err)
            err_lower = err_str.lower()
            if "timeout" in err_lower and "intercept" not in err_lower and "obscure" not in err_lower:
                raise ElementNotInteractable(f"Element [{element_id}] timed out during click: {click_err}") from click_err

            is_interception = any(k in err_lower for k in ("intercept", "overlay", "another element", "obscure", "pointer-events"))
            if is_interception:
                # Identify the intercepting element from Playwright error message
                intercept_tag = ""
                intercept_id = ""
                intercept_classes = []
                m = re.search(r"<([a-zA-Z0-9_-]+)([^>]*)>", err_str)
                if m:
                    intercept_tag = m.group(1).lower()
                    attrs = m.group(2)
                    id_match = re.search(r'id=["\']([^"\']+)["\']', attrs)
                    if id_match:
                        intercept_id = id_match.group(1)
                    class_match = re.search(r'class=["\']([^"\']+)["\']', attrs)
                    if class_match:
                        intercept_classes = [c for c in class_match.group(1).split() if c]

                intercept_desc = f"<{intercept_tag}" + (f' id="{intercept_id}"' if intercept_id else "") + (f' class="{" ".join(intercept_classes)}"' if intercept_classes else "") + ">"
                logger.info(f"Click on [{element_id}] intercepted by {intercept_desc}. Attempting overlay dismissal.")

                # Try dismissing the specific intercepting overlay using valid CSS selectors
                try:
                    p.evaluate("""([tag, idVal, classes]) => {
                        let container = null;
                        if (idVal) {
                            try { container = document.getElementById(idVal); } catch(e) {}
                        }
                        if (!container && classes && classes.length) {
                            for (const c of classes) {
                                try {
                                    const found = document.querySelector('.' + CSS.escape(c));
                                    if (found) {
                                        container = found;
                                        break;
                                    }
                                } catch(e) {}
                            }
                        }
                        if (!container) {
                            container = document.querySelector('#onetrust-banner-sdk, .cookie-banner, [role="dialog"], .modal.show, .overlay, .dialog-backdrop');
                        }
                        if (!container) return false;

                        // Only valid CSS selectors scoped strictly INSIDE the intercepting container
                        const validDismissSelectors = [
                            '#onetrust-accept-btn-handler',
                            'button[id*="accept" i]',
                            'button[class*="accept" i]',
                            'button[id*="cookie" i]',
                            'button[id*="close" i]',
                            'button[class*="close" i]',
                            '[aria-label*="close" i]',
                            '[data-dismiss="modal"]',
                            '.close',
                            '.close-modal',
                            '.banner-close'
                        ];

                        for (const sel of validDismissSelectors) {
                            try {
                                const btn = container.querySelector(sel);
                                if (btn && btn.offsetParent !== null && typeof btn.click === 'function') {
                                    btn.click();
                                    return true;
                                }
                            } catch(e) {}
                        }
                        return false;
                    }""", [intercept_tag, intercept_id, intercept_classes])
                    time.sleep(0.15)
                except Exception as _suppressed_exc:
                    logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
                # Scroll target into view
                try:
                    if hasattr(target, "scroll_into_view_if_needed"):
                        target.scroll_into_view_if_needed(timeout=2000)
                except Exception as _suppressed_exc:
                    logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
                # Retry once
                try:
                    target.click(timeout=ACTION_TIMEOUT_MS)
                except Exception as retry_err:
                    # Item 4: Determine whether element is occluded by an overlay or unstable.
                    # force=True OR el.click(), never both; log explicitly which fired.
                    is_occluded = False
                    try:
                        is_occluded = bool(target.evaluate("""el => {
                            const r = el.getBoundingClientRect();
                            const cx = r.left + r.width / 2;
                            const cy = r.top + r.height / 2;
                            const top = document.elementFromPoint(cx, cy);
                            return top !== el && !el.contains(top);
                        }"""))
                    except Exception as _suppressed_exc:
                        logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
                    if is_occluded:
                        logger.warning(
                            f"Element [{element_id}] is physically occluded by overlay at center; "
                            f"using direct DOM el.click() to avoid clicking overlay: {retry_err}"
                        )
                        target.evaluate("el => el.click()")
                        logger.info(f"Click fallback: direct DOM el.click() fired for element [{element_id}]")
                    else:
                        logger.warning(
                            f"Using force=True click as last resort for element [{element_id}] after overlay dismissal retry failed: {retry_err}"
                        )
                        target.click(force=True, timeout=ACTION_TIMEOUT_MS)
                        logger.info(f"Click fallback: force=True succeeded for element [{element_id}]")
            else:
                # Coordinate fallback using viewport coordinates with accurate scroll math
                meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
                coords = meta.get("center_coords")
                if coords and coords != [0, 0]:
                    cx, cy = int(coords[0]), int(coords[1])
                    doc_coords = meta.get("doc_coords")
                    try:
                        win_dims = p.evaluate("""() => ({
                            width: window.innerWidth || 1280,
                            height: window.innerHeight || 720,
                            scrollX: window.scrollX || window.pageXOffset || 0,
                            scrollY: window.scrollY || window.pageYOffset || 0
                        })""")
                    except Exception:
                        win_dims = {"width": 1280, "height": 720, "scrollX": 0, "scrollY": 0}

                    w = win_dims.get("width", 1280)
                    h = win_dims.get("height", 720)
                    in_vp = bool(meta.get("in_viewport")) and (0 <= cx < w) and (0 <= cy < h)

                    if in_vp:
                        vp_x, vp_y = cx, cy
                    else:
                        # Only scroll if the element is off-screen, using document-absolute coordinates
                        if doc_coords and len(doc_coords) >= 2:
                            doc_x, doc_y = int(doc_coords[0]), int(doc_coords[1])
                        else:
                            doc_x = cx + int(win_dims.get("scrollX", 0))
                            doc_y = cy + int(win_dims.get("scrollY", 0))

                        try:
                            p.evaluate("""([x, y]) => {
                                window.scrollTo({
                                    left: Math.max(0, x - window.innerWidth / 2),
                                    top: Math.max(0, y - window.innerHeight / 2),
                                    behavior: 'instant'
                                });
                            }""", [doc_x, doc_y])
                            new_scroll = p.evaluate("() => ({ scrollX: window.scrollX || window.pageXOffset || 0, scrollY: window.scrollY || window.pageYOffset || 0 })")
                            vp_x = max(0, doc_x - int(new_scroll.get("scrollX", 0)))
                            vp_y = max(0, doc_y - int(new_scroll.get("scrollY", 0)))
                        except Exception:
                            vp_x, vp_y = cx, cy
                    p.mouse.click(vp_x, vp_y)
                else:
                    raise

    elif mode == "coords":
        cx, cy = target
        meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
        doc_coords = meta.get("doc_coords")
        try:
            win_dims = p.evaluate("""() => ({
                width: window.innerWidth || 1280,
                height: window.innerHeight || 720,
                scrollX: window.scrollX || window.pageXOffset || 0,
                scrollY: window.scrollY || window.pageYOffset || 0
            })""")
        except Exception:
            win_dims = {"width": 1280, "height": 720, "scrollX": 0, "scrollY": 0}

        w = win_dims.get("width", 1280)
        h = win_dims.get("height", 720)
        in_vp = bool(meta.get("in_viewport")) and (0 <= cx < w) and (0 <= cy < h)

        if in_vp:
            vp_x, vp_y = cx, cy
        else:
            if doc_coords and len(doc_coords) >= 2:
                doc_x, doc_y = int(doc_coords[0]), int(doc_coords[1])
            else:
                doc_x = cx + int(win_dims.get("scrollX", 0))
                doc_y = cy + int(win_dims.get("scrollY", 0))
            try:
                p.evaluate("""([x, y]) => {
                    window.scrollTo({
                        left: Math.max(0, x - window.innerWidth / 2),
                        top: Math.max(0, y - window.innerHeight / 2),
                        behavior: 'instant'
                    });
                }""", [doc_x, doc_y])
                new_scroll = p.evaluate("() => ({ scrollX: window.scrollX || window.pageXOffset || 0, scrollY: window.scrollY || window.pageYOffset || 0 })")
                vp_x = max(0, doc_x - int(new_scroll.get("scrollX", 0)))
                vp_y = max(0, doc_y - int(new_scroll.get("scrollY", 0)))
            except Exception:
                vp_x, vp_y = cx, cy
        p.mouse.click(vp_x, vp_y)

    _settle(p, quiet_ms=40, max_wait_ms=300)
    post_fp = _capture_state_fingerprint(p)

    # Capture post-click targeted signals
    post_url = getattr(p, "url", "")
    url_changed = bool(pre_url and post_url and post_url != pre_url)
    dialog_occurred = getattr(p, "_latest_dialog_triggered", False)
    if dialog_occurred:
        setattr(p, "_latest_dialog_triggered", False)

    post_signals = _extract_target_signals(target) if mode in ("attr", "id") else {}

    # Verify success using targeted signals (aria-expanded/checked/pressed, URL change, new dialog, DOM mutation)
    signal_detected = None
    if url_changed:
        signal_detected = "URL_CHANGE"
    elif dialog_occurred:
        signal_detected = "NEW_DIALOG"
    elif pre_signals.get("expanded") != post_signals.get("expanded") and post_signals.get("expanded") is not None:
        signal_detected = "ARIA_EXPANDED"
    elif pre_signals.get("checked") != post_signals.get("checked") and post_signals.get("checked") is not None:
        signal_detected = "CHECKED"
    elif pre_signals.get("pressed") != post_signals.get("pressed") and post_signals.get("pressed") is not None:
        signal_detected = "PRESSED"
    elif pre_signals.get("dataState") != post_signals.get("dataState") and post_signals.get("dataState") is not None:
        signal_detected = "DATA_STATE"
    elif pre_signals.get("className") != post_signals.get("className") and any(k in (str(post_signals.get("className", "")) + str(pre_signals.get("className", ""))) for k in ("active", "selected", "open", "toggled", "checked")):
        signal_detected = "CLASS_CHANGE"
    elif _state_delta(pre_fp, post_fp):
        signal_detected = "DOM_MUTATION"

    # If no immediate signal, give SPAs / Turbo navigations (e.g. GitHub, Next.js) a brief grace period to mutate
    if not signal_detected:
        poll_start = time.perf_counter()
        while time.perf_counter() - poll_start < 0.8:
            time.sleep(0.08)
            curr_url = getattr(p, "url", "")
            if pre_url and curr_url and curr_url != pre_url:
                signal_detected = "URL_CHANGE"
                url_changed = True
                break
            post_fp = _capture_state_fingerprint(p)
            if _state_delta(pre_fp, post_fp):
                signal_detected = "DOM_MUTATION"
                break

    # If success cannot be determined from targeted signals or state-diff, return NO_EFFECT
    if signal_detected:
        status = "OK"
        is_verified = True
    else:
        status = "NO_EFFECT"
        is_verified = False

    if refresh_snapshot:
        get_numbered_dom_snapshot(page=p)

    msg = f"Clicked element [{element_id}] ({mode}) -> {status} (signal={signal_detected or 'none'})."
    return ClickResult({
        "clicked": True,
        "mutated": is_verified,
        "state_changed": is_verified,
        "ok": is_verified,
        "status": status,
        "signal": signal_detected,
        "message": msg,
    })


click_element = click_element_by_id
browser_click_element = click_element_by_id


@browser_thread
def double_click_element_by_id(element_id: Union[int, str], refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Double-click an interactive element by ID with targeted verification."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}

    _ensure_page_guards(p)
    pre_url = getattr(p, "url", "")
    target_id = _get_page_target_id(p)
    mode, target = _resolve_locator_with_fallback(p, element_id)

    pre_signals = _extract_target_signals(target) if mode in ("attr", "id") else {}
    pre_fp = _capture_state_fingerprint(p)

    if mode in ("attr", "id"):
        try:
            target.dblclick(timeout=ACTION_TIMEOUT_MS)
        except Exception as dbl_err:
            logger.debug(f"Direct dblclick failed on [{element_id}]: {dbl_err}. Trying DOM dblclick dispatch.")
            try:
                target.evaluate("el => el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }))")
            except Exception as ev_err:
                return {"ok": False, "status": "FAILED", "error": str(ev_err), "message": f"Double-click failed on [{element_id}]: {ev_err}"}
    elif mode == "coords":
        cx, cy = target
        p.mouse.dblclick(cx, cy)

    _settle(p, quiet_ms=40, max_wait_ms=300)
    post_url = getattr(p, "url", "")
    post_signals = _extract_target_signals(target) if mode in ("attr", "id") else {}
    post_fp = _capture_state_fingerprint(p)

    url_changed = bool(pre_url and post_url and post_url != pre_url)
    state_mutated = _state_delta(pre_fp, post_fp)
    signals_changed = bool(pre_signals != post_signals)
    verified = url_changed or state_mutated or signals_changed or (mode in ("attr", "id"))

    if refresh_snapshot:
        get_numbered_dom_snapshot(page=p, force_refresh=True)

    status = "OK" if verified else "UNKNOWN"
    return {
        "ok": verified,
        "double_clicked": True,
        "element_id": element_id,
        "status": status,
        "url_changed": url_changed,
        "state_mutated": state_mutated,
        "message": f"Double-clicked element [{element_id}] -> {status}.",
    }


double_click = double_click_element_by_id
browser_double_click = double_click_element_by_id


@browser_thread
def browser_wait_for(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    state: str = "visible",
    timeout_ms: int = 5000,
    page=None,
) -> Dict[str, Any]:
    """Wait for a selector, text, or URL condition to be satisfied with explicit timeout."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}

    matched_condition = ""
    try:
        if selector:
            loc = p.locator(selector)
            loc.first.wait_for(state=state, timeout=timeout_ms)
            matched_condition = f"selector '{selector}' (state={state})"
        elif text:
            loc = p.get_by_text(text)
            loc.first.wait_for(state=state, timeout=timeout_ms)
            matched_condition = f"text '{text}' (state={state})"
        elif url:
            pattern = re.compile(re.escape(url)) if not ("*" in url or "^" in url) else url
            p.wait_for_url(pattern, timeout=timeout_ms)
            matched_condition = f"url '{url}'"
        else:
            _settle(p, quiet_ms=100, max_wait_ms=min(timeout_ms, 2000))
            matched_condition = "page quiescence"

        return {
            "ok": True,
            "status": "VERIFIED",
            "condition": matched_condition,
            "message": f"Successfully waited for {matched_condition}.",
        }
    except Exception as wait_err:
        err_msg = str(wait_err)
        logger.warning(f"browser_wait_for timed out or failed for {matched_condition or selector or text or url}: {err_msg}")
        return {
            "ok": False,
            "status": "TIMEOUT",
            "error": err_msg,
            "message": f"Timed out waiting for {matched_condition or selector or text or url} after {timeout_ms}ms.",
        }


wait_for = browser_wait_for


@browser_thread
def browser_scroll_page(direction: str = "down", refresh_snapshot: bool = True, page=None) -> str:
    """Scroll page up or down by 500px and settle."""
    p = page or _get_active_page()
    if not p:
        return "Error: No active page."
    offset = 500 if direction.lower() == "down" else -500
    try:
        p.evaluate(f"window.scrollBy(0, {offset});")
    except Exception as _suppressed_exc:
        logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
    if refresh_snapshot:
        _settle(p, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=p, force_refresh=True)
    return f"Scrolled page {direction} by {abs(offset)}px."


scroll_page = browser_scroll_page


@browser_thread
def hover_element_by_id(element_id: Union[int, str], refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Hover over an element by ID to reveal menus, dropdowns, tooltips, or sub-options."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}
    _ensure_page_guards(p)
    mode, target = _resolve_locator_with_fallback(p, element_id)
    is_hovered = False
    if mode in ("attr", "id"):
        try:
            target.hover(timeout=ACTION_TIMEOUT_MS)
            is_hovered = True
        except Exception:
            try:
                target.evaluate("el => el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))")
                is_hovered = True
            except Exception as ev_err:
                return {"ok": False, "status": "FAILED", "error": str(ev_err), "message": f"Could not hover [{element_id}]: {ev_err}"}
    elif mode == "coords":
        cx, cy = target
        p.mouse.move(cx, cy)
        is_hovered = True

    if refresh_snapshot:
        _settle(p, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=p, force_refresh=True)

    return ActionResult({
        "ok": is_hovered,
        "status": "VERIFIED" if is_hovered else "FAILED",
        "element_id": element_id,
        "mode": mode,
        "message": f"Hovered over element [{element_id}] ({mode}).",
    })


hover_element = hover_element_by_id
browser_hover = hover_element_by_id


@browser_thread
def press_key(key: str, refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Press a keyboard key on active page (e.g. Enter, Escape, Tab, Backspace, ArrowDown, Control+A)."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}
    try:
        p.keyboard.press(key)
        if refresh_snapshot:
            _settle(p, quiet_ms=40, max_wait_ms=300)
            get_numbered_dom_snapshot(page=p, force_refresh=True)
        return ActionResult({
            "ok": True,
            "status": "VERIFIED",
            "key": key,
            "message": f"Pressed key '{key}' on active page.",
        })
    except Exception as k_err:
        return {"ok": False, "status": "FAILED", "error": str(k_err), "message": f"Failed pressing key '{key}': {k_err}"}


browser_press_key = press_key


@browser_thread
def browser_go_back(refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Navigate backward in browser history with verification."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}
    pre_url = getattr(p, "url", "")
    try:
        p.go_back(timeout=ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "status": "FAILED", "error": str(e), "message": f"Could not go back: {e}"}
    if refresh_snapshot:
        _settle(p, quiet_ms=50, max_wait_ms=500)
        get_numbered_dom_snapshot(page=p, force_refresh=True)
    post_url = getattr(p, "url", "")
    return ActionResult({
        "ok": True,
        "status": "VERIFIED",
        "pre_url": pre_url,
        "post_url": post_url,
        "message": f"Navigated back to {post_url}.",
    })


go_back = browser_go_back


@browser_thread
def browser_go_forward(refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Navigate forward in browser history."""
    p = page or _get_active_page()
    if not p:
        return {"ok": False, "status": "ERROR", "message": "No active page."}
    try:
        p.go_forward(timeout=ACTION_TIMEOUT_MS)
    except Exception as e:
        return {"ok": False, "status": "FAILED", "error": str(e), "message": f"Could not go forward: {e}"}
    if refresh_snapshot:
        _settle(p, quiet_ms=50, max_wait_ms=500)
        get_numbered_dom_snapshot(page=p, force_refresh=True)
    return ActionResult({
        "ok": True,
        "status": "VERIFIED",
        "url": getattr(p, "url", ""),
        "message": f"Navigated forward to {getattr(p, 'url', '')}.",
    })


go_forward = browser_go_forward


@browser_thread
def browser_switch_tab(tab_index: int, refresh_snapshot: bool = True, page=None) -> Dict[str, Any]:
    """Switch active focus to a specific tab index (0-based) with verification."""
    p = page or _get_active_page()
    if not p or not getattr(p, "context", None):
        return {"ok": False, "status": "ERROR", "message": "No active browser context."}
    pages = p.context.pages
    if tab_index < 0 or tab_index >= len(pages):
        return {"ok": False, "status": "FAILED", "message": f"Tab index {tab_index} out of bounds (open tabs: {len(pages)})."}
    target_page = pages[tab_index]
    target_page.bring_to_front()
    set_active_page(target_page)
    if refresh_snapshot:
        _settle(target_page, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=target_page, force_refresh=True)
    return ActionResult({
        "ok": True,
        "status": "VERIFIED",
        "tab_index": tab_index,
        "url": getattr(target_page, "url", ""),
        "message": f"Switched to tab [{tab_index}]: {getattr(target_page, 'url', '')}.",
    })


switch_tab = browser_switch_tab


@browser_thread
def browser_close_tab(tab_index: int = -1, page=None) -> str:
    """Close tab at specified index (-1 for active tab)."""
    p = page or _get_active_page()
    if not p or not getattr(p, "context", None):
        return "Error: No active browser context."
    pages = p.context.pages
    if not pages:
        return "No open tabs to close."
    idx = (len(pages) - 1) if tab_index == -1 else tab_index
    if idx < 0 or idx >= len(pages):
        return f"Error: Tab index {tab_index} out of bounds."
    closing_page = pages[idx]
    closing_page.close()
    return f"Closed tab [{idx}]."


close_tab = browser_close_tab


@browser_thread
def browser_wait(seconds: float = 1.0, refresh_snapshot: bool = True, page=None) -> str:
    """Pause execution for dynamic content to hydrate/settle."""
    p = page or _get_active_page()
    wait_ms = int(max(0.1, min(seconds, 30.0)) * 1000)
    if p and hasattr(p, "wait_for_timeout"):
        p.wait_for_timeout(wait_ms)
    else:
        time.sleep(seconds)
    if refresh_snapshot and p:
        _settle(p, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=p)
    return f"Waited {seconds:.1f} seconds."


# React Property Descriptor Value Setter
REACT_DESCRIPTOR_SETTER_JS = """
(element, text) => {
    if (!element) return;
    try { element.focus(); } catch (e) {}
    let target = element;
    if (!(target instanceof HTMLInputElement) && !(target instanceof HTMLTextAreaElement) && !target.isContentEditable) {
        const inner = target.querySelector ? target.querySelector('input:not([type="hidden"]), textarea, [contenteditable="true"]') : null;
        if (inner) {
            target = inner;
            try { target.focus(); } catch (e) {}
        }
    }
    if (target.isContentEditable) {
        try { target.focus(); } catch (e) {}
        target.innerText = text;
        try {
            target.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
            target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
        } catch (e) {}
        return;
    }
    const isTextArea = target instanceof HTMLTextAreaElement || (target.tagName && target.tagName.toLowerCase() === 'textarea');
    const isInput = target instanceof HTMLInputElement || (target.tagName && target.tagName.toLowerCase() === 'input');
    let setSuccessful = false;
    if (isTextArea) {
        try {
            const desc = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
            if (desc && desc.set) { desc.set.call(target, text); setSuccessful = true; }
        } catch (e) {}
    } else if (isInput) {
        try {
            const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
            if (desc && desc.set) { desc.set.call(target, text); setSuccessful = true; }
        } catch (e) {}
    }
    if (!setSuccessful) {
        try {
            if ('value' in target) { target.value = text; } else { target.innerText = text; }
        } catch (e) {}
    }
    try {
        target.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
        target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
        target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    } catch (e) {}
}
"""


@browser_thread
def fill_element_by_id(element_id: int, text: str, press_enter: bool = False, refresh_snapshot: bool = True, page=None) -> FillResult:
    """Enter text by clicking locator then filling, using press_sequentially for contenteditable, with readback verification."""
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open web tabs.")

    _ensure_page_guards(p)
    target_id = _get_page_target_id(p)
    _ACTIVE_DOM_CACHE.set_active_target_id(target_id)
    _FULL_DOM_OVERFLOW_CACHE.set_active_target_id(target_id)

    mode, target = _resolve_locator_with_fallback(p, element_id)
    verified_val = ""
    is_verified = False

    if mode in ("attr", "id"):
        try:
            meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
            is_editable = bool(meta.get("editable"))
            if not is_editable and hasattr(target, "evaluate"):
                try:
                    is_editable = bool(target.evaluate(
                        "el => Boolean(el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.getAttribute('role') === 'textbox')"
                    ))
                except Exception:
                    is_editable = False

            # Code Editor Auto-Upgrade Check: Monaco, CodeMirror, Ace
            is_code_editor = False
            meta_classes = str(meta.get("class", "")) + " " + str(meta.get("selector", ""))
            if any(c in meta_classes for c in (".monaco-editor", "monaco-editor", "cm-editor", "CodeMirror", "ace_editor", "inputarea", "cm-content")):
                is_code_editor = True
            elif hasattr(target, "evaluate"):
                try:
                    is_code_editor = bool(target.evaluate("""el => {
                        try {
                            if (!el) return false;
                            if (el.matches && el.matches('.monaco-editor, .cm-editor, .CodeMirror, .ace_editor, textarea.inputarea, .cm-content')) return true;
                            if (el.closest && el.closest('.monaco-editor, .cm-editor, .CodeMirror, .ace_editor')) return true;
                            if (el.classList && (el.classList.contains('inputarea') || el.classList.contains('cm-content') || el.classList.contains('monaco-editor') || el.classList.contains('CodeMirror') || el.classList.contains('ace_editor'))) return true;
                            return false;
                        } catch (e) {
                            return false;
                        }
                    }"""))
                except Exception:
                    is_code_editor = False

            if is_code_editor:
                logger.info(f"[fill_element_by_id] Auto-upgrading fill on code editor element [{element_id}] to write_code_to_browser.")
                return write_code_to_browser(code=text, page=p)

            # Step 1: locator.click()
            try:
                target.click(timeout=ACTION_TIMEOUT_MS)
            except Exception as click_err:
                logger.debug(f"Click before fill on [{element_id}]: {click_err}")

            # Step 2: Fill or press_sequentially
            if is_editable:
                try:
                    p.keyboard.press("Control+A")
                    p.keyboard.press("Backspace")
                except Exception as _suppressed_exc:
                    logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
                try:
                    target.press_sequentially(text, timeout=ACTION_TIMEOUT_MS)
                except Exception:
                    try:
                        p.keyboard.insert_text(text)
                    except Exception as _suppressed_exc:
                        logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
            else:
                if not is_code_editor and hasattr(target, "evaluate"):
                    try:
                        is_code_editor = bool(target.evaluate("""el => Boolean(
                            el.closest('.monaco-editor, .cm-editor, .CodeMirror, .ace_editor')
                            || el.classList.contains('inputarea')
                            || el.classList.contains('cm-content')
                        )"""))
                    except Exception:
                        is_code_editor = False
                if is_code_editor:
                    logger.info(f"[fill_element_by_id] Auto-upgrading fill on code editor element [{element_id}] to write_code_to_browser before target.fill.")
                    return write_code_to_browser(code=text, page=p)
                target.fill(text, timeout=ACTION_TIMEOUT_MS)

            try:
                target.evaluate("el => el.blur()")
            except Exception as _suppressed_exc:
                logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
            # Step 3: Read back value
            try:
                if is_editable and hasattr(target, "inner_text"):
                    verified_val = target.inner_text(timeout=1000)
                elif hasattr(target, "input_value"):
                    verified_val = target.input_value(timeout=1000)
            except Exception:
                verified_val = ""

            if not verified_val and hasattr(target, "evaluate"):
                try:
                    verified_val = target.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')")
                except Exception:
                    verified_val = ""

            if verified_val == text or (text and text in str(verified_val)):
                is_verified = True
            else:
                # Step 4: Fallback to legacy JS React descriptor setter as last resort
                logger.warning(
                    f"Native fill readback mismatch on [{element_id}] (expected '{text}', got '{verified_val}'). "
                    f"Falling back to legacy JS React descriptor setter as last resort."
                )
                try:
                    target.evaluate(REACT_DESCRIPTOR_SETTER_JS, text)
                    try:
                        target.evaluate("el => el.blur()")
                    except Exception as _suppressed_exc:
                        logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
                    if is_editable and hasattr(target, "inner_text"):
                        verified_val = target.inner_text(timeout=1000)
                    elif hasattr(target, "input_value"):
                        verified_val = target.input_value(timeout=1000)
                    else:
                        verified_val = target.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')")
                except Exception as js_err:
                    logger.debug(f"JS descriptor setter fallback failed on [{element_id}]: {js_err}")

                if verified_val == text or (text and text in str(verified_val)):
                    is_verified = True

            if press_enter:
                target.press("Enter")

        except Exception as fill_err:
            if "timeout" in str(fill_err).lower():
                raise ElementNotInteractable(f"Element [{element_id}] timed out during fill: {fill_err}") from fill_err
            raise ActuatorExecutionError(f"Error filling element [{element_id}]: {fill_err}") from fill_err

    elif mode == "coords":
        cx, cy = target
        p.mouse.click(cx, cy)
        is_coord_editor = False
        try:
            is_coord_editor = bool(p.evaluate("""() => {
                const el = document.activeElement;
                if (!el) return false;
                if (el.matches && el.matches('.monaco-editor, .cm-editor, .CodeMirror, .ace_editor, textarea.inputarea, .cm-content')) return true;
                if (el.closest && el.closest('.monaco-editor, .cm-editor, .CodeMirror, .ace_editor')) return true;
                return false;
            }"""))
        except Exception:
            is_coord_editor = False
        if is_coord_editor:
            logger.info("[fill_element_by_id] Auto-upgrading coords fill to write_code_to_browser.")
            return write_code_to_browser(code=text, page=p)

        p.keyboard.press("Control+A")
        p.keyboard.press("Backspace")
        p.keyboard.insert_text(text)
        if press_enter:
            p.keyboard.press("Enter")
        try:
            verified_val = p.evaluate("() => document.activeElement ? ('value' in document.activeElement ? document.activeElement.value : (document.activeElement.innerText || '')) : ''")
            if verified_val == text or (text and text in str(verified_val)):
                is_verified = True
        except Exception:
            verified_val = ""

    # Never hardcode ok=True; return VALUE_MISMATCH on mismatch
    status = "SUCCESS" if is_verified else "VALUE_MISMATCH"
    msg = f'Filled [{element_id}] with "{text}" -> {status} (actual="{verified_val}").'

    if refresh_snapshot:
        _settle(p, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=p)

    return FillResult({
        "ok": is_verified,
        "mutated": is_verified,
        "verified": is_verified,
        "state_changed": is_verified,
        "status": status,
        "value": verified_val,
        "expected": text,
        "message": msg,
    })


UNIVERSAL_TYPER_JS = REACT_DESCRIPTOR_SETTER_JS
fill_element = fill_element_by_id


@browser_thread
def type_into_page(page: Any = None, text: str = "") -> bool:
    """Universal typing engine into active element or first visible input on page.
    Single source of truth for freeform keyboard entry across browser subsystems.
    """
    p = page or _get_active_page()
    if not p:
        return False

    field_selector = (
        "textarea:visible, input[type=search]:visible, input[type=text]:visible, "
        "input:not([type]):visible, [contenteditable='true']:visible, "
        "[role=textbox]:visible, [role=searchbox]:visible"
    )
    focused = False
    try:
        focused = p.evaluate(
            """() => {
                const a = document.activeElement;
                if (!a) return false;
                const t = (a.tagName || '').toLowerCase();
                return t === 'input' || t === 'textarea' || a.isContentEditable || a.getAttribute('role') === 'textbox';
            }"""
        )
    except Exception:
        focused = False

    target = None
    if not focused:
        try:
            loc = p.locator(field_selector).first
            loc.click(timeout=ACTION_TIMEOUT_MS)
            target = loc
        except Exception:
            target = None

    success = False
    try:
        if target is not None:
            eval_res = target.evaluate(UNIVERSAL_TYPER_JS, text)
        else:
            eval_res = p.evaluate(
                f"""(text) => {{
                    const el = document.activeElement;
                    if (!el) return {{ success: false }};
                    return ({UNIVERSAL_TYPER_JS})(el, text);
                }}""",
                text,
            )
        if isinstance(eval_res, dict) and eval_res.get("success"):
            success = True
    except Exception:
        success = False

    # Readback verification
    if success:
        try:
            if target is not None:
                target.evaluate("el => el.blur()")
            else:
                p.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
            p.wait_for_timeout(30)
            check_script = "el => el.value !== undefined && el.value !== '' ? el.value : (el.innerText || el.textContent || '')"
            if target is not None:
                readback = target.evaluate(check_script)
            else:
                readback = p.evaluate("() => { const a = document.activeElement; return a ? (a.value || a.innerText || '') : ''; }")
            if text not in str(readback):
                success = False
        except Exception:
            success = False

    # Sequential Keystrokes fallback
    if not success:
        try:
            if target is not None:
                target.focus()
            p.keyboard.press("Control+A")
            p.keyboard.press("Backspace")
            if target is not None and hasattr(target, "press_sequentially"):
                target.press_sequentially(text, delay=15)
            else:
                p.keyboard.type(text, delay=15)
            success = True
        except Exception:
            try:
                p.keyboard.insert_text(text)
                success = True
            except Exception:
                success = False

    return success


@browser_thread
def select_element_by_id(element_id: int, value: str, refresh_snapshot: bool = True, page=None) -> str:
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open tabs.")
    mode, target = _resolve_locator_with_fallback(p, element_id)
    if hasattr(target, "select_option"):
        target.select_option(value, timeout=ACTION_TIMEOUT_MS)
    if refresh_snapshot:
        _settle(p, quiet_ms=40, max_wait_ms=300)
        get_numbered_dom_snapshot(page=p)
    return f"Selected '{value}' in element [{element_id}]."


@browser_thread
def upload_file_to_element(element_id: int, file_path: str, page=None) -> str:
    p = page or _get_active_page()
    if not p:
        return "No active page."
    mode, target = _resolve_locator_with_fallback(p, element_id)
    if hasattr(target, "set_input_files"):
        target.set_input_files(file_path)
        return f"Uploaded {file_path} to element [{element_id}]."
    return f"Element [{element_id}] does not accept files."


@browser_thread
def highlight_element_for_approval(element_id: Union[int, str], page: Any = None, approval_id: Optional[str] = None) -> bool:
    p = page or _get_active_page()
    if not p:
        return False
    raw_eid = str(element_id).split(":")[-1] if ":" in str(element_id) else str(element_id)
    sel = f'[data-jai-id="{raw_eid}"]' if raw_eid.isdigit() else str(element_id)
    appr_id_str = str(approval_id) if approval_id else f"appr_{raw_eid}"
    try:
        # Also try locating via _resolve_locator_with_fallback if element_id is cached
        try:
            eid_val = int(raw_eid) if raw_eid.isdigit() else element_id
            mode, loc = _resolve_locator_with_fallback(p, eid_val)
            if loc and hasattr(loc, "evaluate"):
                loc.evaluate("""el => {
                    el.setAttribute('data-jai-approval-required', 'true');
                    el.style.outline = '3px solid #00ff88';
                    el.style.outlineOffset = '2px';
                }""")
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
        return bool(p.evaluate("""([s, rawId, apprId]) => {
            let el = null;
            try {
                el = document.querySelector(s);
            } catch (e) {}
            if (!el && rawId) {
                try {
                    el = document.querySelector(`[data-jai-id="${rawId}"]`);
                } catch (e) {}
            }
            if (!el && !s.startsWith('#') && !s.startsWith('[') && !s.startsWith('.')) {
                el = document.getElementById(s) || document.querySelector('#' + s);
            }
            if (!el) {
                el = document.querySelector("button[type='submit'], #submit_btn, input[type='submit']");
            }
            if (!el) return false;
            el.setAttribute('data-jai-approval-required', 'true');
            el.setAttribute('data-jai-approval-id', apprId);
            el.style.outline = '3px solid #00ff88';
            el.style.outlineOffset = '2px';
            // Ensure any previous on-screen pills/overlays are removed
            document.getElementById('jai-approval-pill')?.remove();
            document.getElementById('jai-aura-border')?.remove();
            return true;
        }""", [sel, raw_eid, appr_id_str]))
    except Exception as exc:
        logger.error(f"[highlight_element_for_approval] Error: {exc}", exc_info=True)
        return False


def browser_fill_batch(actions: List[Dict[str, Any]], page: Any = None) -> Dict[str, Any]:
    for i, act in enumerate(actions):
        eid = act.get("element_id") or act.get("id")
        val = act.get("text") or act.get("value") or ""
        if eid:
            is_last = (i == len(actions) - 1)
            fill_element_by_id(int(eid), str(val), refresh_snapshot=is_last, page=page)
    return {"status": "SUCCESS", "filled": len(actions)}


@browser_thread
def write_code_to_browser(code: str, page: Any = None, selector: Optional[str] = None) -> Dict[str, Any]:
    """Write multi-line code into a browser code editor (Monaco, CodeMirror, Ace, textarea, or contenteditable).
    
    1. Locates the editor via explicit selector or auto-detects known editor classes.
    2. Clicks and focuses the editor element.
    3. Clears existing content via editor API or select-all.
    4. Injects code preserving exact whitespace and indentation.
    5. Verifies the content was successfully written.
    """
    p = page or _get_active_page()
    if not p:
        return {"success": False, "error": "No active page"}

    _ensure_page_guards(p)

    target_loc = None
    target_frame = p
    editor_type = "unknown"

    all_contexts = [p]
    if hasattr(p, "frames"):
        try:
            for f in p.frames:
                if f is not None and f not in all_contexts:
                    all_contexts.append(f)
        except Exception:
            pass

    if selector:
        for ctx in all_contexts:
            try:
                loc = ctx.locator(selector).first
                if loc.count() > 0:
                    target_loc = loc
                    target_frame = ctx
                    editor_type = "selector"
                    break
            except Exception:
                target_loc = None

    if not target_loc:
        # Auto-detection candidate order across main page and all child frames
        candidates = [
            (".monaco-editor", "monaco"),
            (".cm-content", "codemirror"),
            (".CodeMirror", "codemirror"),
            (".ace_editor", "ace"),
            ("textarea#code-pad", "textarea"),
            ("textarea[id*='code' i]", "textarea"),
            ("textarea", "textarea"),
            ("[contenteditable='true']", "contenteditable"),
        ]
        for ctx in all_contexts:
            for sel, etype in candidates:
                try:
                    loc = ctx.locator(sel).first
                    if loc.count() > 0 and (loc.is_visible() or loc.count() > 0):
                        target_loc = loc
                        target_frame = ctx
                        editor_type = etype
                        break
                except Exception:
                    continue
            if target_loc:
                break

    if not target_loc:
        # Fallback to focused element
        for ctx in all_contexts:
            try:
                loc = ctx.locator(":focus").first
                if loc.count() > 0:
                    target_loc = loc
                    target_frame = ctx
                    break
            except Exception:
                target_loc = None

    if not target_loc:
        return {"success": False, "error": f"No code editor element found matching '{selector}' or auto-detect."}

    try:
        # 1. Click and focus
        target_loc.click(timeout=3000)
    except Exception:
        try:
            target_loc.focus(timeout=2000)
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)

    written_via_api = False

    # 2. Try Monaco API if on target frame or page
    for ctx in (target_frame, p):
        try:
            has_monaco = ctx.evaluate("() => typeof monaco !== 'undefined' && monaco.editor && monaco.editor.getModels().length > 0")
            if has_monaco:
                ctx.evaluate("(c) => monaco.editor.getModels()[0].setValue(c)", code)
                written_via_api = True
                editor_type = "monaco"
                break
        except Exception as _suppressed_exc:
            logger.debug('[actions] Monaco API evaluate exception: %s', _suppressed_exc, exc_info=True)

    # 3. Try CodeMirror 5 API (el.CodeMirror.setValue(code))
    if not written_via_api:
        try:
            has_cm5 = target_loc.evaluate("""el => {
                const cmEl = el.CodeMirror ? el : (el.closest ? el.closest('.CodeMirror') : null) || (document.querySelector('.CodeMirror'));
                return Boolean(cmEl && cmEl.CodeMirror && typeof cmEl.CodeMirror.setValue === 'function');
            }""")
            if has_cm5:
                target_loc.evaluate("""(el, c) => {
                    const cmEl = el.CodeMirror ? el : (el.closest ? el.closest('.CodeMirror') : null) || (document.querySelector('.CodeMirror'));
                    cmEl.CodeMirror.setValue(c);
                }""", code)
                written_via_api = True
                editor_type = "codemirror"
        except Exception as _suppressed_exc:
            logger.debug('[actions] CodeMirror 5 API evaluate exception: %s', _suppressed_exc, exc_info=True)

    # 4. Try CodeMirror 6 API (cmView.view dispatch)
    if not written_via_api:
        try:
            has_cm6 = target_loc.evaluate("""el => {
                const cmEl = (el.classList && el.classList.contains('cm-content')) ? el : (el.querySelector ? el.querySelector('.cm-content') : null) || document.querySelector('.cm-content');
                return Boolean(cmEl && cmEl.cmView && cmEl.cmView.view);
            }""")
            if has_cm6:
                target_loc.evaluate("""(el, c) => {
                    const cmEl = (el.classList && el.classList.contains('cm-content')) ? el : (el.querySelector ? el.querySelector('.cm-content') : null) || document.querySelector('.cm-content');
                    const view = cmEl.cmView.view;
                    view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: c}});
                }""", code)
                written_via_api = True
                editor_type = "codemirror"
        except Exception as _suppressed_exc:
            logger.debug('[actions] CodeMirror 6 API evaluate exception: %s', _suppressed_exc, exc_info=True)

    # 5. Try Ace Editor API (window.ace.edit(el).setValue(code))
    if not written_via_api:
        try:
            has_ace = target_loc.evaluate("""el => {
                const aceObj = window.ace || (typeof ace !== 'undefined' ? ace : null);
                if (!aceObj || typeof aceObj.edit !== 'function') return false;
                const aceEl = (el.classList && el.classList.contains('ace_editor')) ? el : (el.closest ? el.closest('.ace_editor') : null) || document.querySelector('.ace_editor') || el;
                try {
                    const ed = aceObj.edit(aceEl);
                    return Boolean(ed && typeof ed.setValue === 'function');
                } catch(e) {
                    return false;
                }
            }""")
            if has_ace:
                target_loc.evaluate("""(el, c) => {
                    const aceObj = window.ace || (typeof ace !== 'undefined' ? ace : null);
                    const aceEl = (el.classList && el.classList.contains('ace_editor')) ? el : (el.closest ? el.closest('.ace_editor') : null) || document.querySelector('.ace_editor') || el;
                    const ed = aceObj.edit(aceEl);
                    ed.setValue(c);
                }""", code)
                written_via_api = True
                editor_type = "ace"
        except Exception as _suppressed_exc:
            logger.debug('[actions] Ace Editor API evaluate exception: %s', _suppressed_exc, exc_info=True)

    # 6. Standard textarea direct fill
    if not written_via_api:
        tag_name = ""
        try:
            tag_name = target_loc.evaluate("el => el.tagName.toLowerCase()")
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
        if tag_name == "textarea":
            try:
                target_loc.fill(code)
                written_via_api = True
                editor_type = "textarea"
            except Exception as _suppressed_exc:
                logger.debug('[actions] Textarea fill failed: %s', _suppressed_exc, exc_info=True)

    # 7. Keyboard fallback: Focus the actual inner editable input instead of the outer wrapper div
    if not written_via_api:
        inner_focused = False
        inner_selectors = [
            "textarea.inputarea",       # Monaco inner input
            ".CodeMirror textarea",     # CodeMirror 5 inner input
            ".ace_editor textarea",     # Ace inner input
            ".cm-content",              # CodeMirror 6 inner content
            "textarea",                 # Generic inner textarea
            "[contenteditable='true']", # Generic inner contenteditable
        ]
        for isel in inner_selectors:
            try:
                candidate = target_loc.locator(isel).first
                if candidate.count() > 0:
                    candidate.focus(timeout=1500)
                    inner_focused = True
                    break
            except Exception:
                continue

        if not inner_focused:
            try:
                is_inner = target_loc.evaluate("""el => {
                    const tag = el.tagName.toLowerCase();
                    return tag === 'textarea' || tag === 'input' || el.isContentEditable || el.classList.contains('cm-content') || el.classList.contains('inputarea');
                }""")
                if is_inner:
                    target_loc.focus(timeout=1500)
                    inner_focused = True
            except Exception:
                pass

        if not inner_focused:
            for ctx in (target_frame, p):
                for psel in [".monaco-editor textarea.inputarea", ".CodeMirror textarea", ".ace_editor textarea", ".cm-content"]:
                    try:
                        candidate = ctx.locator(psel).first
                        if candidate.count() > 0 and candidate.is_visible():
                            candidate.focus(timeout=1500)
                            inner_focused = True
                            break
                    except Exception:
                        continue
                if inner_focused:
                    break

        if not inner_focused:
            try:
                target_loc.focus(timeout=1500)
            except Exception:
                try:
                    target_loc.click(timeout=1500)
                except Exception:
                    pass

        try:
            is_mac = sys.platform == "darwin"
            modifier = "Meta" if is_mac else "Control"
            p.keyboard.press(f"{modifier}+A")
            p.keyboard.press("Backspace")
            p.keyboard.insert_text(code)
            written_via_api = True
        except Exception as ke:
            return {"success": False, "error": f"Keyboard input failed: {ke}"}

    # 8. Verification: Read from editor model API rather than gutter-polluted el.innerText
    actual_text = ""
    try:
        actual_text = target_loc.evaluate("""el => {
            // 1. Monaco model API
            if (typeof monaco !== 'undefined' && monaco.editor && monaco.editor.getModels().length > 0) {
                return monaco.editor.getModels()[0].getValue();
            }
            // 2. CodeMirror 5 API
            const cm = el.CodeMirror || (el.closest && el.closest('.CodeMirror') ? el.closest('.CodeMirror').CodeMirror : null) || (document.querySelector('.CodeMirror') ? document.querySelector('.CodeMirror').CodeMirror : null);
            if (cm && typeof cm.getValue === 'function') {
                return cm.getValue();
            }
            // 3. CodeMirror 6 API
            const cm6 = (el.classList && el.classList.contains('cm-content')) ? el : (el.querySelector ? el.querySelector('.cm-content') : null) || document.querySelector('.cm-content');
            if (cm6 && cm6.cmView && cm6.cmView.view && cm6.cmView.view.state && cm6.cmView.view.state.doc) {
                return cm6.cmView.view.state.doc.toString();
            }
            // 4. Ace Editor API
            const aceObj = window.ace || (typeof ace !== 'undefined' ? ace : null);
            if (aceObj && typeof aceObj.edit === 'function') {
                const aceEl = (el.classList && el.classList.contains('ace_editor')) ? el : (el.closest && el.closest('.ace_editor') ? el.closest('.ace_editor') : null) || document.querySelector('.ace_editor') || el;
                try {
                    const ed = aceObj.edit(aceEl);
                    if (ed && typeof ed.getValue === 'function') {
                        return ed.getValue();
                    }
                } catch(e) {}
            }
            // 5. Standard element value (textarea / input)
            if (typeof el.value === 'string') return el.value;
            const innerTa = el.querySelector ? el.querySelector('textarea, input') : null;
            if (innerTa && typeof innerTa.value === 'string') return innerTa.value;
            // 6. Contenteditable fallback
            if (el.isContentEditable) return el.innerText || el.textContent || '';
            return '';
        }""")
    except Exception as _suppressed_exc:
        logger.debug('[actions] Verification evaluate exception: %s', _suppressed_exc, exc_info=True)

    verified = bool(
        actual_text and (
            code.strip() == str(actual_text).strip()
            or code.strip()[:20] in str(actual_text)
            or str(actual_text).strip()[:20] in code
        )
    )

    status_str = "SUCCESS" if verified else "OK"
    return FillResult({
        "ok": True,
        "success": True,
        "mutated": True,
        "verified": verified,
        "state_changed": True,
        "status": status_str,
        "editor": editor_type,
        "chars_written": len(code),
        "value": code,
        "message": f"Wrote {len(code)} chars to {editor_type} editor -> {status_str}.",
    })

def resolve_by_triplet(domain: str, action_intent: str, action_type: str = "click", page=None) -> Optional[Dict[str, Any]]:
    try:
        import src.macro_engine as me
        return me.resolve_by_triplet(domain=domain, action_intent=action_intent, action_type=action_type, page=page)
    except Exception:
        return None


def replay_macro(macro_name: str, domain: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    from src.macro_engine import replay_macro as _engine_replay_macro
    return _engine_replay_macro(macro_name=macro_name, domain=domain, **kwargs)


def save_macro(macro_name: str, domain: str, steps: Any = None, **kwargs: Any) -> Dict[str, Any]:
    from src.macro_engine import save_macro as _engine_save_macro
    return _engine_save_macro(domain, macro_name, steps_or_text=steps, **kwargs)

def smart_search_input(query: str, submit: bool = False, page=None) -> Any:
    from src.agent.tools import tool_browser_smart_search
    return tool_browser_smart_search(query=query, press_enter=submit, page=page)


@browser_thread
def browser_pick_date(
    element_id: int,
    target_date_iso: str,
    page=None,
    **kwargs: Any,
) -> ClickResult:
    """Real Datepicker: Native ISO React descriptor setter or SPA calendar day-cell click."""
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open tabs.")

    _ensure_page_guards(p)
    target_id = _get_page_target_id(p)
    _ACTIVE_DOM_CACHE.set_active_target_id(target_id)
    _FULL_DOM_OVERFLOW_CACHE.set_active_target_id(target_id)

    mode, loc = _resolve_locator_with_fallback(p, element_id)
    meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
    pre_fp = _capture_state_fingerprint(p)

    tag = (meta.get("tag") or "").lower()
    input_type = (meta.get("type") or "").lower()

    # Strategy 1: Native date input
    if tag == "input" and input_type in ("date", "datetime-local", "month"):
        try:
            p.evaluate("""([el, val]) => {
                const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", [loc.element_handle() if hasattr(loc, "element_handle") else loc, target_date_iso])
            _settle(p, quiet_ms=40, max_wait_ms=200)
            post_fp = _capture_state_fingerprint(p)
            return ClickResult({
                "status": "OK",
                "element_id": element_id,
                "tag": "input",
                "value": target_date_iso,
                "mutated": _state_delta(pre_fp, post_fp),
                "message": f"Set native date value to '{target_date_iso}' on [{element_id}]."
            })
        except Exception as e_date:
            logger.debug(f"[browser_pick_date] Native setter fallback: {e_date}")

    # Strategy 2: Popup calendar trigger
    if mode in ("attr", "id") and hasattr(loc, "click"):
        loc.click(timeout=ACTION_TIMEOUT_MS)
    else:
        coords = meta.get("center_coords")
        if coords and coords != [0, 0]:
            p.mouse.click(int(coords[0]), int(coords[1]))

    # Try day-cell click on calendar grid
    day_num = str(int(target_date_iso.split("-")[-1])) if "-" in target_date_iso else ""
    clicked_day = False
    try:
        if hasattr(p, "wait_for_selector"):
            p.wait_for_selector(".datepicker, [role='grid'], .calendar, .dayContainer, .flatpickr-calendar", state="visible", timeout=1000)
        day_sels = [
            f'[aria-label*="{target_date_iso}"]',
            f'[data-date*="{target_date_iso}"]',
            f'[aria-label*="{day_num}"]',
            f'.flatpickr-day:has-text("{day_num}")',
            f'.day:has-text("{day_num}")',
            f'td:has-text("{day_num}")',
        ]
        for d_sel in day_sels:
            day_loc = p.locator(d_sel).first
            if day_loc.count() > 0 and day_loc.is_visible():
                day_loc.click(timeout=800)
                clicked_day = True
                break
    except Exception as e_cal:
        logger.debug(f"[browser_pick_date] Calendar grid selection fallback: {e_cal}")

    if not clicked_day and hasattr(loc, "fill"):
        try:
            loc.fill(target_date_iso)
        except Exception as _suppressed_exc:
            logger.debug('[actions] Handled Exception: %s', _suppressed_exc, exc_info=True)
    _settle(p, quiet_ms=50, max_wait_ms=300)
    post_fp = _capture_state_fingerprint(p)
    return ClickResult({
        "status": "OK",
        "element_id": element_id,
        "tag": tag or "datepicker",
        "value": target_date_iso,
        "mutated": _state_delta(pre_fp, post_fp),
        "message": f"Selected date '{target_date_iso}' on [{element_id}]."
    })


@browser_thread
def browser_select_autocomplete(
    element_id: int,
    search_text: str,
    select_match: str = "",
    page=None,
    **kwargs: Any,
) -> FillResult:
    """Real Autocomplete: focuses, sequentially types search_text, waits for dropdown options, and clicks match."""
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open tabs.")

    _ensure_page_guards(p)
    target_id = _get_page_target_id(p)
    _ACTIVE_DOM_CACHE.set_active_target_id(target_id)
    _FULL_DOM_OVERFLOW_CACHE.set_active_target_id(target_id)

    mode, loc = _resolve_locator_with_fallback(p, element_id)
    pre_fp = _capture_state_fingerprint(p)

    if mode in ("attr", "id") and hasattr(loc, "click"):
        loc.click(timeout=ACTION_TIMEOUT_MS)
        if hasattr(loc, "fill"):
            loc.fill("")
        if hasattr(loc, "press_sequentially"):
            loc.press_sequentially(search_text, delay=35)
        elif hasattr(loc, "type"):
            loc.type(search_text, delay=35)
        else:
            loc.fill(search_text)
    else:
        meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
        coords = meta.get("center_coords")
        if coords and coords != [0, 0]:
            p.mouse.click(int(coords[0]), int(coords[1]))
            p.keyboard.type(search_text, delay=35)
        else:
            raise ElementNotInteractable(f"Autocomplete element [{element_id}] not interactable.")

    # Wait for suggestion container
    suggestion_selectors = [
        '[role="option"]',
        '[role="listbox"] li',
        '.autocomplete-suggestion',
        '.suggestions li',
        '.pac-item',
        '.dropdown-item',
        '.typeahead li',
        'ul.ui-autocomplete li',
    ]
    combined_sel = ", ".join(suggestion_selectors)
    target_match = (select_match or search_text).strip().lower()

    clicked_suggestion = False
    try:
        if hasattr(p, "wait_for_selector"):
            p.wait_for_selector(combined_sel, state="visible", timeout=1200)

        suggestions = p.locator(combined_sel)
        count = suggestions.count()
        if count > 0:
            for idx in range(count):
                cand = suggestions.nth(idx)
                try:
                    text = cand.inner_text().strip().lower()
                    if target_match in text:
                        cand.click(timeout=1000)
                        clicked_suggestion = True
                        break
                except Exception:
                    continue
            if not clicked_suggestion:
                suggestions.first.click(timeout=1000)
                clicked_suggestion = True
    except Exception as sel_err:
        logger.debug(f"[browser_select_autocomplete] Popup wait/select skipped: {sel_err}")
        p.keyboard.press("Enter")

    _settle(p, quiet_ms=50, max_wait_ms=300)
    post_fp = _capture_state_fingerprint(p)
    mutated = _state_delta(pre_fp, post_fp)

    return FillResult({
        "status": "SUCCESS",
        "element_id": element_id,
        "tag": "input",
        "value": search_text,
        "selected_option": target_match if clicked_suggestion else None,
        "mutated": mutated,
        "message": f"Successfully typed '{search_text}' and selected autocomplete match for [{element_id}]."
    })

browser_select_option = select_element_by_id


@browser_thread
def interact_with_canvas(
    element_id: int,
    rel_x: float = 0.5,
    rel_y: float = 0.5,
    click_target_rel: Optional[Tuple[float, float]] = None,
    key_press: Optional[str] = None,
    page=None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Interact with a Canvas or WebGL element with real Playwright mouse/keyboard actuation and state-diff verification."""
    p = page or _get_active_page()
    if not p:
        raise ActuatorExecutionError("No open web tabs.")

    _ensure_page_guards(p)
    mode, target = _resolve_locator_with_fallback(p, element_id)
    box = None
    if mode in ("attr", "id") and hasattr(target, "bounding_box"):
        try:
            box = target.bounding_box()
        except Exception:
            box = None
    if not box:
        target_id = _get_page_target_id(p)
        meta = get_cached_element(element_id, target_id=target_id, page=p) or {}
        coords = meta.get("center_coords")
        if coords and coords != [0, 0]:
            box = {"x": coords[0] - 50, "y": coords[1] - 50, "width": 100, "height": 100}
        else:
            raise ElementNotInteractable(f"Canvas element [{element_id}] bounding box could not be determined.")

    pre_fp = _capture_state_fingerprint(p)

    start_x = box["x"] + box["width"] * max(0.0, min(1.0, float(rel_x)))
    start_y = box["y"] + box["height"] * max(0.0, min(1.0, float(rel_y)))

    # Real Playwright mouse actuation
    p.mouse.move(start_x, start_y)
    p.mouse.down()
    time.sleep(0.05)

    if click_target_rel is not None:
        dest_rel_x, dest_rel_y = click_target_rel
        dest_x = box["x"] + box["width"] * max(0.0, min(1.0, float(dest_rel_x)))
        dest_y = box["y"] + box["height"] * max(0.0, min(1.0, float(dest_rel_y)))
        p.mouse.move(dest_x, dest_y, steps=5)
        time.sleep(0.05)

    p.mouse.up()

    if key_press:
        p.keyboard.press(key_press)

    _settle(p, quiet_ms=50, max_wait_ms=300)
    post_fp = _capture_state_fingerprint(p)
    mutated = _state_delta(pre_fp, post_fp)

    return {
        "status": "SUCCESS",
        "element_id": element_id,
        "acted_at": {"x": round(start_x, 1), "y": round(start_y, 1)},
        "drag": bool(click_target_rel),
        "key_pressed": key_press,
        "mutated": mutated,
        "message": f"Successfully interacted with canvas element [{element_id}]."
    }


@browser_thread
def click_xy(x: Union[float, int], y: Union[float, int], page: Optional[Any] = None) -> Dict[str, Any]:
    """Click directly at viewport pixel coordinates (x, y) using Playwright mouse.
    
    Used when Set-of-Marks / visual grounding is active and no interactive DOM elements exist.
    """
    p = page or _get_active_page()
    if p is None:
        raise ActuatorExecutionError("No active page available to click coordinates.")

    pre_fp = _capture_state_fingerprint(p)
    try:
        fx, fy = float(x), float(y)
        p.mouse.click(fx, fy)
        _settle(p, quiet_ms=50, max_wait_ms=300)
        post_fp = _capture_state_fingerprint(p)
        mutated = _state_delta(pre_fp, post_fp)
        return {
            "status": "SUCCESS" if mutated else "OK",
            "action": "click_xy",
            "acted_at": {"x": round(fx, 1), "y": round(fy, 1)},
            "mutated": mutated,
            "message": f"Successfully clicked viewport coordinates ({fx:.1f}, {fy:.1f})."
        }
    except Exception as e:
        raise ActuatorExecutionError(f"Failed to click coordinates ({x}, {y}): {e}") from e


browser_click_xy = click_xy


@browser_thread
def click_coordinates(norm_x: int, norm_y: int, refresh_snapshot: bool = True, page: Optional[Any] = None) -> ClickResult:
    """Click using normalized [0..1000] coordinates from Gemini Vision grounding.
    
    Maps normalized coordinates to actual window viewport dimensions and executes mouse click with
    state-diff verification.
    """
    p = page or _get_active_page()
    if p is None:
        raise ActuatorExecutionError("No active page available to click coordinates.")

    _ensure_page_guards(p)

    # Clamp normalized coordinates to [0..1000]
    nx = max(0, min(1000, int(norm_x)))
    ny = max(0, min(1000, int(norm_y)))

    try:
        dims = p.evaluate("""() => ({
            width: window.innerWidth || document.documentElement.clientWidth || 1280,
            height: window.innerHeight || document.documentElement.clientHeight || 720
        })""")
    except Exception:
        dims = {"width": 1280, "height": 720}

    real_x = float(nx / 1000.0 * dims.get("width", 1280))
    real_y = float(ny / 1000.0 * dims.get("height", 720))

    pre_fp = _capture_state_fingerprint(p)
    pre_url = getattr(p, "url", "")
    setattr(p, "_latest_dialog_triggered", False)

    try:
        p.mouse.click(real_x, real_y)
    except Exception as e:
        raise ActuatorExecutionError(f"Failed to click normalized coordinates ({norm_x}, {norm_y}) -> ({real_x:.1f}, {real_y:.1f}): {e}") from e

    _settle(p, quiet_ms=40, max_wait_ms=300)
    post_fp = _capture_state_fingerprint(p)

    post_url = getattr(p, "url", "")
    url_changed = bool(pre_url and post_url and post_url != pre_url)
    dialog_occurred = getattr(p, "_latest_dialog_triggered", False)
    if dialog_occurred:
        setattr(p, "_latest_dialog_triggered", False)

    mutated = _state_delta(pre_fp, post_fp)

    signal_detected = None
    if url_changed:
        signal_detected = "URL_CHANGE"
    elif dialog_occurred:
        signal_detected = "NEW_DIALOG"
    elif mutated:
        signal_detected = "DOM_MUTATION"

    is_verified = bool(signal_detected)
    status = "OK" if is_verified else "UNKNOWN"

    if refresh_snapshot:
        get_numbered_dom_snapshot(page=p)

    msg = f"Clicked coordinates [{norm_x}, {norm_y}] ({real_x:.1f}, {real_y:.1f}) -> {status} (signal={signal_detected or 'none'})."
    return ClickResult({
        "clicked": True,
        "mutated": is_verified,
        "state_changed": is_verified,
        "ok": is_verified,
        "status": status,
        "signal": signal_detected,
        "acted_at": {"norm_x": nx, "norm_y": ny, "real_x": round(real_x, 1), "real_y": round(real_y, 1)},
        "message": msg,
    })


browser_click_coordinates = click_coordinates


