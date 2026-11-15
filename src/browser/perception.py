"""src/browser/perception.py - Universal Perception Engine.
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
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import src.browser.cdp as _cdp
from src.browser.dispatch import get_jb_attr

from src.browser.cdp import (
    browser_thread,
    _get_page_target_id,
    _safe_title,
    ActuatorExecutionError,
    ElementNotFoundError,
    ACTION_TIMEOUT_MS,
    _REPO_ROOT,
    _ensure_page_guards,
    _settle,
)

logger = logging.getLogger("jai.browser.perception")

def _get_active_page(page=None):
    if page is not None:
        return page
    fn = get_jb_attr("_get_active_page", _cdp._get_active_page)
    return fn()

# ==============================================================================
# SECTION B: Universal Perception Engine (~400 lines)
# ==============================================================================

class UniversalContentResult(dict):
    """Dictionary holding extracted markdown content and interactive element metadata."""
    markdown = property(lambda self: str(self.get("markdown", self.get("content", ""))))
    content = property(lambda self: str(self.get("content", "")))
    items = property(lambda self: list(self.get("items", [])))


class DomSnapshotResult(tuple):
    """Tuple (snapshot_text, cache_dict) representing a numbered DOM state."""
    pass


def _parse_axtree_snapshot(raw_snap: str) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse Chromium Accessibility Tree (aria_snapshot) into Tier 1 (inputs), Tier 2 (buttons/links), Tier 3 (context)."""
    lines = raw_snap.split("\n")
    skip_indent: Optional[int] = None
    interactives: List[Dict[str, Any]] = []
    contents: List[Dict[str, Any]] = []
    md_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())

        if skip_indent is not None:
            if indent > skip_indent:
                continue
            else:
                skip_indent = None

        lower = stripped.lower()
        if any(lower.startswith(prefix) for prefix in [
            "- navigation", "- banner", "- complementary", "- contentinfo",
            "navigation:", "banner:", "complementary:", "contentinfo:"
        ]) or any(ad_kw in lower for ad_kw in ["sponsor", "advertisement", "promo", "google_ad", "ad-banner"]):
            skip_indent = indent
            continue

        # 1. Inputs (Tier 1)
        m_txt = re.match(r'^-?\s*textbox\s+"([^"]*)"(?::\s*(.*))?', stripped)
        if m_txt:
            name = m_txt.group(1).strip() or "Input"
            val = m_txt.group(2).strip() if m_txt.group(2) else ""
            md_lines.append(f"- [Input: {name}{' (value: ' + val + ')' if val else ''}]")
            interactives.append({"role": "textbox", "name": name, "value": val, "tier": 1})
            continue

        m_combo = re.match(r'^-?\s*combobox\s+"([^"]*)"(?::\s*(.*))?', stripped)
        if m_combo:
            name = m_combo.group(1).strip() or "Select"
            md_lines.append(f"- [Select: {name}]")
            interactives.append({"role": "combobox", "name": name, "tier": 1})
            continue

        # 2. Buttons & Links (Tier 2)
        m_btn = re.match(r'^-?\s*button\s+"([^"]+)"', stripped)
        if m_btn:
            name = m_btn.group(1).strip()
            md_lines.append(f"- [Button: {name}]")
            interactives.append({"role": "button", "name": name, "tier": 2})
            continue

        m_link = re.match(r'^-?\s*link\s+"([^"]+)":?', stripped)
        if m_link:
            name = m_link.group(1).strip()
            md_lines.append(f"- [Link: {name}]")
            interactives.append({"role": "link", "name": name, "tier": 2})
            continue

        # 3. Content Context (Tier 3)
        m_head = re.match(r'^-?\s*heading\s+"([^"]+)"(?:\s*\[level=(\d+)\])?', stripped)
        if m_head:
            title = m_head.group(1).strip()
            level = int(m_head.group(2) or 2)
            md_lines.append(f'{"#" * level} {title}')
            contents.append({"role": "heading", "text": title, "level": level, "tier": 3})
            continue

        m_li = re.match(r'^-?\s*listitem(?::\s*(.*))?', stripped)
        if m_li:
            txt = m_li.group(1).strip() if m_li.group(1) else ""
            if txt:
                md_lines.append(f"- {txt}")
                contents.append({"role": "listitem", "text": txt, "tier": 3})
            continue

        m_cell = re.match(r'^-?\s*cell\s*"([^"]*)":?(.*)', stripped)
        if m_cell:
            c_txt = m_cell.group(1).strip() or m_cell.group(2).strip()
            if c_txt:
                md_lines.append(f"| {c_txt} |")
                contents.append({"role": "cell", "text": c_txt, "tier": 3})
            continue

        m_p = re.match(r'^-?\s*paragraph:\s*(.+)', stripped)
        if m_p:
            p_txt = m_p.group(1).strip()
            md_lines.append(p_txt)
            contents.append({"role": "paragraph", "text": p_txt, "tier": 3})
            continue

        # 4. Text, Strong, Emphasis, and Inline Leaf Nodes (Tier 3)
        m_inline = re.match(r'^-?\s*(?:text|strong|emphasis|code|span):\s*(?:"([^"]*)"|(.+))', stripped)
        if m_inline:
            inline_txt = (m_inline.group(1) or m_inline.group(2) or "").strip()
            if inline_txt:
                md_lines.append(inline_txt)
                contents.append({"role": "text", "text": inline_txt, "tier": 3})
                continue

    # Cap interactive nodes at 60
    interactives = interactives[:60]
    md_content = "\n\n".join(md_lines)
    return md_content, interactives, contents


def _extract_readability_content(p: Any) -> Dict[str, Any]:
    """In-memory readability cleaner that strips noise and returns a clean markdown tree."""
    js = r"""() => {
        const root = document.querySelector('article, main, [role="main"], #content, body') || document.body;
        if (!root) return { title: document.title || '', url: window.location.href || '', content: '', interactive_elements: [] };
        const clone = root.cloneNode(true);
        ['script', 'style', 'svg', 'noscript', 'iframe', 'canvas', 'nav', 'header', 'footer', 'aside', '.ad', '[class*="banner"]'].forEach(s => {
            clone.querySelectorAll(s).forEach(e => e.remove());
        });
        const interactives = [];
        clone.querySelectorAll('button, a[href], input:not([type="hidden"]), select, textarea').forEach(el => {
            const tag = el.tagName.toLowerCase();
            const name = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.value || '').trim();
            interactives.push({ role: tag, name: name });
        });
        return {
            title: document.title || '',
            url: window.location.href || '',
            content: (clone.innerText || '').slice(0, 6000),
            interactive_elements: interactives.slice(0, 60)
        };
    }"""
    try:
        res = p.evaluate(js)
        if isinstance(res, dict):
            return res
    except Exception as _suppressed_exc:
        logger.debug('[perception] Handled Exception: %s', _suppressed_exc, exc_info=True)
    return {"title": "", "url": "", "content": "", "interactive_elements": []}


@browser_thread
def extract_universal_page_content(
    page: Any = None,
    max_tokens: int = 1500,
    force_fallback: bool = False,
    **kwargs: Any,
) -> UniversalContentResult:
    """Unified Universal Perception Engine: Primary AXTree with Readability fallback, capped at 1500 tokens."""
    p = page or _get_active_page()
    if not p:
        return UniversalContentResult({"error": "No active page.", "content": "No active page.", "markdown": "No active page."})

    title = getattr(p, "title", lambda: "")() if callable(getattr(p, "title", None)) else ""
    url = getattr(p, "url", "")
    content_str = ""
    interactives: List[Dict[str, Any]] = []
    contents: List[Dict[str, Any]] = []
    mode = "readability"

    if not force_fallback:
        try:
            loc = p.locator(":root")
            if hasattr(loc, "aria_snapshot"):
                raw_snap = loc.aria_snapshot()
                if raw_snap and len(raw_snap.strip()) > 30:
                    md_text, ax_interactives, ax_contents = _parse_axtree_snapshot(raw_snap)
                    if len(md_text.strip()) > 20:
                        content_str = md_text.strip()
                        interactives = ax_interactives
                        contents = ax_contents
                        mode = "axtree"
        except Exception as _suppressed_exc:
            logger.debug('[perception] Handled Exception: %s', _suppressed_exc, exc_info=True)
    if not content_str or force_fallback:
        read_res = _extract_readability_content(p)
        content_str = read_res.get("content", "").strip()
        interactives = read_res.get("interactive_elements", [])
        mode = "readability"

    # Enforce Token Ceiling (<= 1,500 tokens ~ 6,000 chars)
    char_limit = max_tokens * 4
    if len(content_str) > char_limit:
        content_str = content_str[:char_limit - 80] + f"\n\n... [Content truncated at {max_tokens} token ceiling]"

    return UniversalContentResult({
        "mode": mode,
        "title": title,
        "url": url,
        "content": content_str,
        "text": content_str,
        "markdown": content_str,
        "char_count": len(content_str),
        "token_estimate": max(1, len(content_str) // 4),
        "interactive_elements": interactives,
        "content_nodes": contents,
        "items": interactives + contents,
    })


def get_page_reading_snapshot(page=None) -> str:
    res = extract_universal_page_content(page=page)
    return res.markdown


@browser_thread
def get_cdp_axtree_snapshot(page: Any = None, target_id: Optional[str] = None) -> Any:
    p = page or _get_active_page()
    if not p:
        return DomSnapshotResult(("", {}))
    res = extract_universal_page_content(page=p)
    return DomSnapshotResult((res.markdown, {i: item for i, item in enumerate(res.items, 1)}))


def get_cdp_ax_tree(page: Any = None) -> List[Dict[str, Any]]:
    p = page or _get_active_page()
    if not p:
        return []
    res = extract_universal_page_content(page=p)
    return res.get("interactive_elements", [])


# Tab-Scoped Element Caches
class TabScopedCache(dict):
    """Dictionary keyed by CDP targetId maintaining isolated element caches per tab with LRU cap."""
    def __init__(self, max_tabs: int = 5, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_tabs = max_tabs
        self._tabs: collections.OrderedDict[str, dict[int, dict[str, Any]]] = collections.OrderedDict()
        self._active_target_id = "default"
        self._lock = threading.RLock()

    def set_active_target_id(self, target_id: str) -> None:
        if target_id:
            with self._lock:
                tid = str(target_id)
                self._active_target_id = tid
                if tid in self._tabs:
                    self._tabs.move_to_end(tid)

    def get_active_target_id(self) -> str:
        with self._lock:
            return self._active_target_id

    def get_active_tab_cache(self) -> dict[int, dict[str, Any]]:
        return self.get_tab_cache()

    def get_tab_cache(self, target_id: Optional[str] = None) -> dict[int, dict[str, Any]]:
        with self._lock:
            tid = str(target_id) if target_id else self._active_target_id
            if tid not in self._tabs:
                while len(self._tabs) >= self.max_tabs:
                    self._tabs.popitem(last=False)
                self._tabs[tid] = {}
            else:
                self._tabs.move_to_end(tid)
            return self._tabs[tid]

    def __getitem__(self, key: Any) -> Any:
        with self._lock:
            if isinstance(key, str) and key in self._tabs:
                return self.get_tab_cache(key)
            active = self.get_tab_cache()
            if key in active:
                return active[key]
            try:
                el_id = int(key)
                if el_id in active:
                    return active[el_id]
            except (ValueError, TypeError) as _suppressed_exc:
                logger.debug('[perception] Handled (ValueError, TypeError): %s', _suppressed_exc, exc_info=True)
            if isinstance(key, str):
                return self.get_tab_cache(key)
            raise KeyError(key)

    def clear(self) -> None:
        with self._lock:
            super().clear()
            self._tabs.clear()
            self._active_target_id = "default"

    def __setitem__(self, key: Any, value: Any) -> None:
        with self._lock:
            if isinstance(key, str) and isinstance(value, dict):
                self._tabs[key] = value
            else:
                self.get_tab_cache()[key] = value

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            if isinstance(key, str) and key in self._tabs:
                return True
            active = self.get_tab_cache()
            if key in active:
                return True
            try:
                el_id = int(key)
                return el_id in active
            except (ValueError, TypeError):
                return False

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, TypeError):
            return default

    def __len__(self) -> int:
        with self._lock:
            return len(self.get_tab_cache())

    def items(self):
        with self._lock:
            return list(self.get_tab_cache().items())

    def values(self):
        with self._lock:
            return list(self.get_tab_cache().values())

    def keys(self):
        with self._lock:
            return list(self.get_tab_cache().keys())


_ACTIVE_DOM_CACHE = TabScopedCache(max_tabs=5)
_FULL_DOM_OVERFLOW_CACHE = TabScopedCache(max_tabs=5)


def get_cached_element(element_id: Any, target_id: Optional[str] = None, page: Any = None) -> Optional[Dict[str, Any]]:
    tid = target_id or (page and _get_page_target_id(page)) or _ACTIVE_DOM_CACHE.get_active_target_id()
    ac = _ACTIVE_DOM_CACHE.get_tab_cache(tid)
    res = ac.get(element_id)
    if res is None and isinstance(element_id, str) and element_id.isdigit():
        res = ac.get(int(element_id))
    return res or _FULL_DOM_OVERFLOW_CACHE.get_tab_cache(tid).get(element_id)


def find_elements_in_overflow_cache(query: str, limit: int = 15) -> str:
    """Search _FULL_DOM_OVERFLOW_CACHE and _ACTIVE_DOM_CACHE by label, role, tag, id_attr."""
    q = (query or "").strip().lower()
    if not q:
        return "No search query provided."
    combined = dict(_FULL_DOM_OVERFLOW_CACHE.items())
    combined.update(dict(_ACTIVE_DOM_CACHE.items()))
    matches = []
    for eid in sorted(combined.keys(), key=lambda k: (0, k) if isinstance(k, int) else (1, str(k))):
        meta = combined[eid]
        search_blob = str(meta.get("search_blob") or "").lower()
        if not search_blob:
            search_blob = f"{meta.get('tag', '')} {meta.get('role', '')} {meta.get('id_attr', '')} {meta.get('name', '')} {meta.get('label', '')}".lower()
        if q in search_blob or all(tok in search_blob for tok in q.split()):
            lbl = meta.get("label") or meta.get("name") or meta.get("id_attr") or meta.get("tag")
            matches.append(f"[{eid}] <{meta.get('tag', 'element')} id=\"{meta.get('id_attr', '')}\"> {lbl}")
            if len(matches) >= limit:
                break
    if not matches:
        return f"No elements matching '{query}' found in overflow cache."
    return f"Found {len(matches)} matching elements in overflow cache:\n" + "\n".join(matches)


browser_find_elements = find_elements_in_overflow_cache
find_elements_in_overflow = find_elements_in_overflow_cache


DOM_WALKER_JS = r"""
(framePrefix) => {
    function clearIds(root) {
        if (!root) return;
        if (root.querySelectorAll) {
            root.querySelectorAll('[data-jai-id]').forEach(el => el.removeAttribute('data-jai-id'));
            root.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) clearIds(el.shadowRoot);
            });
        }
    }
    clearIds(document);

    function findActiveModal(root) {
        if (!root) return null;
        const totalArea = window.innerWidth * window.innerHeight;
        if (root.querySelectorAll) {
            const dialogs = root.querySelectorAll('dialog[open], [role="dialog"], [aria-modal="true"]');
            for (let i = 0; i < dialogs.length; i++) {
                const d = dialogs[i];
                try {
                    const rect = d.getBoundingClientRect();
                    const s = window.getComputedStyle(d);
                    if (s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0') {
                        const area = rect.width * rect.height;
                        if (area > 0.15 * totalArea) {
                            return d;
                        }
                    }
                } catch (e) {}
            }
            const all = root.querySelectorAll('*');
            for (let i = 0; i < all.length; i++) {
                if (all[i].shadowRoot) {
                    const found = findActiveModal(all[i].shadowRoot);
                    if (found) return found;
                }
            }
        }
        return null;
    }

    const activeModal = findActiveModal(document);
    const searchRoot = activeModal || document;

    const selector = 'button, a[href], input, textarea, select, [role="button"], [role="link"], [role="textbox"], [role="searchbox"], [role="combobox"], [contenteditable="true"], [role="tab"], [role="menuitem"], [role="option"], [role="checkbox"], [role="radio"], [role="switch"], [role="treeitem"], summary, [tabindex]:not([tabindex="-1"]), [onclick]';
    function collect(root, acc = []) {
        if (!root) return acc;
        if (root.querySelectorAll) {
            root.querySelectorAll(selector).forEach(el => {
                if (el.type === 'hidden') return;
                acc.push(el);
            });

            // Generic elements with explicit interactive attributes
            try {
                root.querySelectorAll('div[onclick], div[jsaction], div[data-action], div[tabindex]:not([tabindex="-1"]), span[onclick], span[jsaction], span[data-action], span[tabindex]:not([tabindex="-1"]), li[onclick], li[jsaction], li[data-action], li[tabindex]:not([tabindex="-1"])').forEach(el => {
                    if (el.type === 'hidden') return;
                    if (!acc.includes(el)) {
                        acc.push(el);
                    }
                });
            } catch (e) {}

            root.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) collect(el.shadowRoot, acc);
            });
        }
        return acc;
    }

    const allElements = collect(searchRoot);

    function isElementOccluded(el, cx, cy) {
        if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight) {
            cx = Math.max(0, Math.min(window.innerWidth - 1, cx));
            cy = Math.max(0, Math.min(window.innerHeight - 1, cy));
        }
        try {
            let top = document.elementFromPoint(cx, cy);
            while (top && top.shadowRoot) {
                const nested = top.shadowRoot.elementFromPoint(cx, cy);
                if (!nested || nested === top) break;
                top = nested;
            }
            if (!top) return false;
            if (top === el || el.contains(top) || top.contains(el)) return false;
            let curr = top;
            while (curr) {
                if (curr === el) return false;
                curr = curr.parentElement || (curr.getRootNode ? curr.getRootNode().host : null);
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    const tier1Viewport = [];
    const tier1Offscreen = [];
    const tier2Viewport = [];
    const tier3Viewport = [];
    const tierOtherOffscreen = [];

    const vH = window.innerHeight * 1.15;
    const vW = window.innerWidth;

    for (let i = 0; i < allElements.length; i++) {
        const el = allElements[i];
        let b = { x: 0, y: 0, width: 0, height: 0, top: 0, bottom: 0, left: 0, right: 0 };
        let cx = 0, cy = 0;
        let is_visible = false;
        let in_viewport = false;
        let occluded = false;

        try {
            const rect = el.getBoundingClientRect();
            b = {
                x: rect.x,
                y: rect.y,
                left: rect.left,
                top: rect.top,
                width: rect.width,
                height: rect.height,
                right: rect.right,
                bottom: rect.bottom
            };
            cx = Math.round(b.x + b.width / 2);
            cy = Math.round(b.y + b.height / 2);
            const s = window.getComputedStyle(el);
            const is_displayed = (b.width > 0 || b.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
            in_viewport = (b.bottom > 0 && b.top < vH && b.right > 0 && b.left < vW);

            if (is_displayed && in_viewport) {
                occluded = isElementOccluded(el, cx, cy);
                is_visible = !occluded;
            } else if (is_displayed) {
                is_visible = true;
            }
        } catch (e) {}

        const is_occluded = Boolean(in_viewport && occluded);

        const tag = (el.tagName || '').toLowerCase();
        const role = (el.getAttribute('role') || tag).toLowerCase();
        const type = (el.type || '').toLowerCase();
        const isContentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';

        // Check if text input:
        const isTextInput = (tag === 'textarea') || (tag === 'select') || isContentEditable ||
            role === 'searchbox' || role === 'textbox' ||
            (tag === 'input' && !['hidden', 'button', 'submit', 'reset', 'checkbox', 'radio'].includes(type));

        let is_submit = false;
        if (tag === 'a') {
            const href = el.getAttribute('href') || '';
            is_submit = /\/(delete|remove|cancel|checkout)/i.test(href);
        } else {
            const btnLabel = (el.getAttribute('aria-label') || el.innerText || el.value || '').trim();
            const hasSubmitKeyword = /(submit|apply|register|turn in|confirm order|place order|pay|checkout|buy|transfer money|publish|tweet|delete|remove)/i.test(btnLabel);
            const isSafeNav = /(solve|run|test|execute|console|search|filter|view|open|details|practice|login|sign in|google)/i.test(btnLabel);
            is_submit = !isSafeNav && (hasSubmitKeyword || (type === 'submit' && hasSubmitKeyword));
        }

        const isTier1 = isTextInput || is_submit;
        const isTier2 = !isTier1 && Boolean(el.closest && el.closest('main, article, [role="main"], form'));
        const isTier3 = !isTier1 && !isTier2;

        const itemInfo = {
            el: el,
            b: b,
            cx: cx,
            cy: cy,
            is_visible: is_visible,
            in_viewport: in_viewport,
            tier: isTier1 ? 1 : (isTier2 ? 2 : 3),
            is_submit: is_submit,
        };

        if (in_viewport && !is_occluded) {
            if (isTier1) {
                tier1Viewport.push(itemInfo);
            } else if (isTier2) {
                tier2Viewport.push(itemInfo);
            } else {
                tier3Viewport.push(itemInfo);
            }
        } else {
            const is_displayed = (b.width > 0 || b.height > 0);
            if (isTier1 && is_displayed) {
                tier1Offscreen.push(itemInfo);
            } else {
                tierOtherOffscreen.push(itemInfo);
            }
        }
    }

    const orderedItems = [
        ...tier1Viewport,
        ...tier1Offscreen,
        ...tier2Viewport,
        ...tier3Viewport,
        ...tierOtherOffscreen
    ];

    const results = [];
    let nextId = 1;
    const maxItems = Math.min(orderedItems.length, 500);

    for (let i = 0; i < maxItems; i++) {
        const info = orderedItems[i];
        const el = info.el;
        const b = info.b;
        const cx = info.cx;
        const cy = info.cy;
        const is_visible = info.is_visible;
        const in_viewport = info.in_viewport;

        const strId = (framePrefix && String(framePrefix).trim() !== '') ? `${framePrefix}_e${nextId}` : String(nextId);
        try {
            el.setAttribute('data-jai-id', strId);
        } catch (e) {}
        const rect = { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) };

        const tag = (el.tagName || '').toLowerCase();
        const role = (el.getAttribute('role') || tag).toLowerCase();
        const id_attr = el.id || '';
        const raw_name = el.name || '';
        const type = (el.type || '').toLowerCase();
        const placeholder = el.getAttribute('placeholder') || '';
        const aria_label = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const isContentEditable = Boolean(el.isContentEditable || el.getAttribute('contenteditable') === 'true');

        // Explicit <label for="..."> and parent label resolution
        let for_label = '';
        if (id_attr) {
            try {
                const escapedId = (window.CSS && CSS.escape) ? CSS.escape(id_attr) : id_attr;
                const lbl = document.querySelector(`label[for="${escapedId}"]`);
                if (lbl) {
                    for_label = (lbl.innerText || lbl.textContent || '').trim();
                }
            } catch (e) {}
        }
        if (!for_label && el.closest) {
            try {
                const parentLbl = el.closest('label');
                if (parentLbl) {
                    for_label = (parentLbl.innerText || parentLbl.textContent || '').trim();
                }
            } catch (e) {}
        }

        const name = for_label || raw_name || aria_label || '';
        const label = (aria_label || for_label || el.innerText || placeholder || el.value || '').trim().slice(0, 100);
        const in_form = Boolean(el.closest && el.closest('form'));
        const combined_text = (tag + ' ' + role + ' ' + id_attr + ' ' + raw_name + ' ' + name + ' ' + label + ' ' + placeholder + ' ' + title).toLowerCase();

        const is_financial = /\b(pay|pay now|proceed to pay|checkout|buy|purchase|transfer money)\b/i.test(label);
        const is_sensitive = (type === 'password') || /\b(password|passcode|otp|cvv|cvc|pin|verification code|card number|aadhaar|aadhar|ssn)\b/i.test(combined_text);

        let curVal = '';
        if (el.value !== undefined && el.value !== null && type !== 'password') {
            curVal = String(el.value);
        }
        let ariaChecked = el.getAttribute('aria-checked');
        if (!ariaChecked && el.checked !== undefined && (type === 'checkbox' || type === 'radio')) {
            ariaChecked = String(el.checked);
        }
        let ariaExpanded = el.getAttribute('aria-expanded');
        let ariaSelected = el.getAttribute('aria-selected');
        let dataState = el.getAttribute('data-state');

        results.push({
            id: nextId,
            local_id: strId,
            frame_index: (framePrefix || null),
            tier: info.tier,
            tag: tag,
            role: role,
            id_attr: id_attr,
            name: name,
            label: label,
            text: label,
            title: title,
            placeholder: placeholder,
            aria_label: aria_label,
            type: type,
            value: curVal,
            checked: ariaChecked || '',
            expanded: ariaExpanded || '',
            selected: ariaSelected || '',
            data_state: dataState || '',
            editable: isContentEditable,
            in_form: in_form,
            is_submit: info.is_submit,
            is_financial: is_financial,
            is_sensitive: is_sensitive,
            rect: rect,
            center_coords: [cx, cy],
            doc_coords: [Math.round(cx + (window.scrollX || window.pageXOffset || 0)), Math.round(cy + (window.scrollY || window.pageYOffset || 0))],
            is_visible: is_visible,
            in_viewport: in_viewport,
            search_blob: combined_text
        });
        nextId++;
    }

    const textSnippet = (document.body ? document.body.innerText : '').slice(0, 3000);
    return { elements: results, title: document.title || '', url: window.location.href || '', text_snippet: textSnippet };
}
"""


_TAB_SEEN_ELEMENTS: Dict[str, Set[str]] = {}


def _element_rank_score(it: Dict[str, Any]) -> int:
    """Ranks interactive DOM elements prioritizing viewport visibility, interactivity, and informative labels."""
    score = 0
    in_vp = bool(it.get("in_viewport", False))
    is_vis = bool(it.get("is_visible", True))
    tag = (it.get("tag") or "").lower()
    role = (it.get("role") or "").lower()
    lbl = (it.get("label") or it.get("aria_label") or it.get("name") or it.get("placeholder") or "").strip()
    is_interactive = (
        tag in ("input", "textarea", "select", "button")
        or it.get("tier") == 1
        or role in ("button", "link", "textbox", "checkbox", "radio", "combobox", "searchbox", "switch", "tab", "menuitem")
        or bool(it.get("editable"))
    )
    has_label = bool(lbl)

    # 1. Viewport-visible gets primary priority
    if in_vp and is_vis:
        score += 1000
    elif is_vis:
        score += 300

    # 2. Interactive elements (forms, buttons, inputs)
    if is_interactive:
        score += 500
        if tag in ("input", "textarea", "select", "button"):
            score += 200

    # 3. Labeled elements
    if has_label:
        score += 300

    # 4. Critical submit/financial actions
    if it.get("is_submit") or it.get("is_financial") or it.get("is_sensitive"):
        score += 400

    # 4b. Critical pagination / navigation controls
    lbl_lower = lbl.lower()
    if any(p_term == lbl_lower or f" {p_term}" in lbl_lower or f"{p_term} " in lbl_lower for p_term in ("next", "more", "previous", "prev")):
        score += 450

    # 5. Penalize noisy unlabeled generic elements
    if tag in ("a", "div", "span") and not has_label:
        score -= 200

    return score


_DOM_SNAPSHOT_CACHE_FP: Dict[str, Dict[str, Any]] = {}
_DOM_SNAPSHOT_CACHE_DATA: Dict[str, Tuple[str, dict]] = {}


@browser_thread
def get_numbered_dom_snapshot(page=None, max_preview_chars: int = 900, force_refresh: bool = False) -> Tuple[str, dict]:
    """Indexes interactive elements per-frame into _ACTIVE_DOM_CACHE and _FULL_DOM_OVERFLOW_CACHE with a token budget.

    COORDINATE SPACE CONVENTION:
    All coordinates in returned element metadata (`rect`, `center_coords`) are VIEWPORT-RELATIVE
    pixels relative to the top-level page viewport at snapshot capture time.
    For child iframe elements, the iframe's bounding box offset (frame_offset_x, frame_offset_y)
    is added to the local frame coordinates so top-level physical clicks resolve accurately.
    """
    p = page or _get_active_page()
    if not p:
        return "No active page.", {}

    target_id_fn = get_jb_attr("_get_page_target_id", _cdp._get_page_target_id)
    target_id = target_id_fn(p)
    fp_fn = get_jb_attr("_capture_state_fingerprint", _capture_state_fingerprint)
    current_fp = fp_fn(p)
    if not force_refresh:
        last_fp = _DOM_SNAPSHOT_CACHE_FP.get(target_id)
        if last_fp is not None and not _state_delta(last_fp, current_fp):
            cached = _DOM_SNAPSHOT_CACHE_DATA.get(target_id)
            if cached is not None:
                return cached

    _ACTIVE_DOM_CACHE.set_active_target_id(target_id)
    _FULL_DOM_OVERFLOW_CACHE.set_active_target_id(target_id)

    frames = getattr(p, "frames", [])
    if not frames:
        frames = [p]

    main_frame = getattr(p, "main_frame", frames[0])
    main_title = ""
    main_url = getattr(p, "url", "")
    main_text = ""
    all_elements: List[Dict[str, Any]] = []

    for f_idx, frame in enumerate(frames):
        try:
            frame_url = getattr(frame, "url", "") or ""
            if frame_url.startswith("chrome:") or frame_url.startswith("devtools:"):
                continue

            is_main = (frame == main_frame or frame == p or f_idx == 0)
            frame_name = getattr(frame, "name", "") or ""
            frame_identity = "main" if is_main else (frame_name or f"frame_{f_idx}")
            prefix = "" if is_main else frame_identity

            # Calculate frame offset in top-level viewport coordinates
            frame_offset_x = 0.0
            frame_offset_y = 0.0
            if not is_main and hasattr(frame, "frame_element"):
                try:
                    frame_el = frame.frame_element()
                    if frame_el:
                        bbox = frame_el.bounding_box()
                        if bbox:
                            frame_offset_x = float(bbox.get("x", 0.0))
                            frame_offset_y = float(bbox.get("y", 0.0))
                except Exception as frame_box_err:
                    logger.debug(f"[get_numbered_dom_snapshot] Could not compute frame offset for {frame_identity}: {frame_box_err}")

            try:
                frame_data = frame.evaluate(DOM_WALKER_JS, prefix)
            except Exception:
                try:
                    frame_data = frame.evaluate(DOM_WALKER_JS)
                except Exception as fallback_err:
                    logger.debug(f"[get_numbered_dom_snapshot] Frame {frame_identity} evaluation skipped: {fallback_err}")
                    continue

            if not frame_data or not isinstance(frame_data, dict):
                continue

            if is_main:
                main_title = frame_data.get("title", "")
                if not main_title and callable(getattr(p, "title", None)):
                    try:
                        main_title = p.title()
                    except Exception as _suppressed_exc:
                        logger.debug('[perception] Handled Exception: %s', _suppressed_exc, exc_info=True)
                main_url = frame_data.get("url", "") or main_url
                main_text = frame_data.get("text_snippet", "")

            frame_elements = frame_data.get("elements", [])
            for f_item in frame_elements:
                f_item["frame"] = frame  # Store the actual Playwright Frame object!
                f_item["frame_identity"] = frame_identity
                f_item["frame_name"] = frame_name
                f_item["frame_url"] = frame_url
                f_item["is_iframe"] = not is_main
                local_id = str(f_item.get("local_id") or f_item.get("id"))
                f_item["local_id"] = local_id
                f_item["frame_key"] = (frame_identity, local_id)

                # Add frame offset to local coordinates to map to top-level viewport coordinate space
                if frame_offset_x or frame_offset_y:
                    rect = f_item.get("rect")
                    if isinstance(rect, dict):
                        f_item["rect"] = {
                            "x": float(rect.get("x", 0.0)) + frame_offset_x,
                            "y": float(rect.get("y", 0.0)) + frame_offset_y,
                            "width": float(rect.get("width", 0.0)),
                            "height": float(rect.get("height", 0.0)),
                        }
                    center = f_item.get("center_coords")
                    if isinstance(center, list) and len(center) >= 2:
                        f_item["center_coords"] = [
                            float(center[0]) + frame_offset_x,
                            float(center[1]) + frame_offset_y,
                        ]
                    doc_c = f_item.get("doc_coords")
                    if isinstance(doc_c, list) and len(doc_c) >= 2:
                        f_item["doc_coords"] = [
                            float(doc_c[0]) + frame_offset_x,
                            float(doc_c[1]) + frame_offset_y,
                        ]
                f_item["frame_offset"] = [frame_offset_x, frame_offset_y]
                all_elements.append(f_item)
        except Exception as frame_err:
            logger.debug(f"[get_numbered_dom_snapshot] Frame loop error: {frame_err}")

    active_cache = _ACTIVE_DOM_CACHE.get_tab_cache(target_id)
    overflow_cache = _FULL_DOM_OVERFLOW_CACHE.get_tab_cache(target_id)
    active_cache.clear()
    overflow_cache.clear()

    # Prioritize higher-ranking interactive elements (inputs, forms, visible) within token budget
    all_elements.sort(key=lambda it: _element_rank_score(it), reverse=True)

    seen_set = _TAB_SEEN_ELEMENTS.get(target_id)
    current_seen: Set[str] = set()

    # Index all elements in overflow_cache preserving their original DOM data-jai-id
    for idx, item in enumerate(all_elements, 1):
        eid = item.get("id") if item.get("id") is not None else idx
        item["id"] = eid
        local_id = item.get("local_id", str(eid))
        frame_key = item.get("frame_key")

        tag = item.get("tag", "")
        id_attr_val = item.get("id_attr", "")
        lbl = item.get("label", "")

        # Index in overflow_cache
        overflow_cache[eid] = item
        overflow_cache[str(eid)] = item
        if item.get("is_iframe") and local_id:
            overflow_cache[local_id] = item
        if id_attr_val:
            overflow_cache[id_attr_val] = item
        if frame_key:
            overflow_cache[frame_key] = item
            overflow_cache[f"{frame_key[0]}:{frame_key[1]}"] = item
        rect = item.get("rect", {})
        rx = rect.get("x", 0)
        ry = rect.get("y", 0)
        sig = f"{tag}:{id_attr_val}:{lbl}:{rx}:{ry}"
        current_seen.add(sig)

    # Format text preview
    if main_text:
        main_text = main_text[:max_preview_chars].strip()
        body_text = f"\n\nPage Text Preview:\n{main_text}"
    else:
        body_text = ""

    header = f"URL: {main_url} | Title: {main_title}\nInteractive Elements ({len(all_elements)} total):\n"
    MAX_SNAPSHOT_CHARS = 3400
    current_chars = len(header) + len(body_text)

    snap_lines = []
    budgeted_items: List[Dict[str, Any]] = []

    for item in all_elements:
        eid = item["id"]
        local_id = item.get("local_id", str(eid))
        tag = item.get("tag", "")
        id_attr_val = item.get("id_attr", "")
        lbl = item.get("label", "")
        rect = item.get("rect", {})
        rx = rect.get("x", 0)
        ry = rect.get("y", 0)
        sig = f"{tag}:{id_attr_val}:{lbl}:{rx}:{ry}"
        is_new = bool(seen_set is not None and sig not in seen_set)

        id_attr = f' id="{id_attr_val}"' if id_attr_val else ""
        iframe_tag = " [iframe]" if item.get("is_iframe") else ""

        state_parts = []
        cur_val = item.get("value")
        if cur_val:
            state_parts.append(f'value="{cur_val}"')
        if item.get("checked"):
            state_parts.append(f'checked={item["checked"]}')
        if item.get("expanded"):
            state_parts.append(f'expanded={item["expanded"]}')
        if item.get("selected"):
            state_parts.append(f'selected={item["selected"]}')
        if item.get("data_state"):
            state_parts.append(f'state={item["data_state"]}')

        state_str = (" " + " ".join(state_parts)) if state_parts else ""
        prefix = "[NEW] " if is_new else ""
        line = f'{prefix}[{eid}]{iframe_tag} <{tag}{id_attr}{state_str}> {lbl}'

        # Token budget constraint: don't exceed snapshot character limit or 50 visible elements
        if (len(budgeted_items) >= 50 or current_chars + len(line) + 1 > MAX_SNAPSHOT_CHARS) and len(budgeted_items) >= 20:
            break

        budgeted_items.append(item)
        snap_lines.append(line)
        current_chars += len(line) + 1

        # Index in active_cache strictly by element id
        active_cache[eid] = item

    _TAB_SEEN_ELEMENTS[target_id] = current_seen

    overflow_count = len(all_elements) - len(budgeted_items)
    overflow_hint = f"\n...[{overflow_count} lower-priority elements in overflow cache; use find_elements_in_overflow to search]" if overflow_count > 0 else ""

    snapshot_text = header + "\n".join(snap_lines) + overflow_hint + body_text
    if len(snapshot_text) >= 3480:
        snapshot_text = snapshot_text[:3440] + "\n...[TRUNCATED]"

    res = (snapshot_text, active_cache)
    _DOM_SNAPSHOT_CACHE_FP[target_id] = current_fp
    _DOM_SNAPSHOT_CACHE_DATA[target_id] = res
    return res


_get_numbered_dom_snapshot_core = get_numbered_dom_snapshot


def normalize_google_url(url: str, authuser: str = "") -> str:
    """Normalize Google URLs by delegating to src.agent.browser_operator (Single Source of Truth)."""
    from src.agent.browser_operator import normalize_google_url as _norm
    return _norm(url, authuser=authuser)


@browser_thread
def harvest_paginated_data(max_pages: int = 5, page=None) -> List[Dict[str, Any]]:
    """Crawls across paginated cards on the active page up to max_pages."""
    p = page or _get_active_page()
    if not p:
        return []
    harvested = []
    seen_ids = set()

    for _ in range(max_pages):
        _settle(p, quiet_ms=50, max_wait_ms=500)
        cards = p.evaluate("""() => {
            const items = [];
            const cardEls = document.querySelectorAll('.card, [data-id], article, .item');
            for (const el of cardEls) {
                const id = el.getAttribute('data-id') || el.id || '';
                const title = (el.querySelector('h1, h2, h3, h4, .title')?.innerText || el.innerText || '').slice(0, 100).trim();
                const text = (el.innerText || '').slice(0, 200).trim();
                if (id || title) {
                    items.push({ id, title, text });
                }
            }
            return items;
        }""")
        for c in (cards or []):
            cid = c.get("id") or c.get("title")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                harvested.append(c)

        has_next = p.evaluate("""() => {
            const nextBtn = document.querySelector('#next-btn, .pagination-next, button[aria-label="Next"], a[rel="next"]');
            if (nextBtn && !nextBtn.disabled && !nextBtn.classList.contains('disabled')) {
                nextBtn.click();
                return true;
            }
            return false;
        }""")
        if not has_next:
            break
        time.sleep(0.3)

    return harvested


def classify_page_archetype(page=None) -> Dict[str, Any]:
    p = page or _get_active_page()
    if not p:
        return {"archetype": "UNKNOWN"}
    has_input = False
    try:
        has_input = bool(p.locator('input:not([type="hidden"]), textarea').count() > 0)
    except Exception as _suppressed_exc:
        logger.debug('[perception] Handled Exception: %s', _suppressed_exc, exc_info=True)
    return {"archetype": "FORM_READY" if has_input else "LINK_CATALOG"}


def extract_task_context(page=None) -> Dict[str, Any]:
    p = page or _get_active_page()
    res = extract_universal_page_content(page=p)
    return {"title": res.get("title", ""), "url": res.get("url", ""), "content": res.get("content", "")}

def _capture_state_fingerprint(page) -> Dict[str, Any]:
    """Capture (url, dom_node_count, input_values_hash, modal_count, text_hash) for State-Diff verification using fast scoped signals."""
    try:
        state = page.evaluate("""() => {
            const forms = document.forms ? document.forms.length : 0;
            const links = document.links ? document.links.length : 0;
            const images = document.images ? document.images.length : 0;
            const dialogs = document.querySelectorAll('dialog[open], .modal.show, [role="dialog"], [aria-modal="true"]').length;
            const scopedCount = forms * 50 + links + images + dialogs * 10;

            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, select')).map(
                el => (el.id || el.name || '') + ':' + (el.type === 'checkbox' || el.type === 'radio' ? el.checked : (el.value || ''))
            ).join('|');
            const customState = Array.from(document.querySelectorAll('[aria-expanded], [aria-checked], [aria-selected], [data-state]')).map(
                el => (el.id || el.getAttribute('role') || el.tagName) + ':' + (el.getAttribute('aria-expanded') || '') + ':' + (el.getAttribute('aria-checked') || '') + ':' + (el.getAttribute('aria-selected') || '') + ':' + (el.getAttribute('data-state') || '')
            ).join('|');
            const textSample = (document.body ? document.body.innerText : '').slice(0, 5000);
            const docTitle = document.title || '';
            return {
                url: window.location.href || '',
                title: docTitle,
                dom_node_count: scopedCount,
                inputs: inputs + '||' + customState + '||' + docTitle,
                modal_count: dialogs,
                text_sample: textSample
            };
        }""")
        input_hash = hashlib.md5(state.get("inputs", "").encode("utf-8", errors="replace")).hexdigest()
        text_hash = hashlib.md5(state.get("text_sample", "").encode("utf-8", errors="replace")).hexdigest()
        dom_node_count = int(state.get("dom_node_count", 0))
        modal_count = int(state.get("modal_count", 0))
        return {
            "url": state.get("url", ""),
            "title": state.get("title", ""),
            "dom_node_count": dom_node_count,
            "input_values_hash": input_hash,
            "text_hash": text_hash,
            "modal_count": modal_count,
            "dom_hash": str(dom_node_count),
            "input_hash": input_hash,
            "dialog_count": modal_count,
            "feedback_count": modal_count,
        }
    except Exception:
        return {
            "url": "",
            "dom_node_count": 0,
            "input_values_hash": "",
            "text_hash": "",
            "modal_count": 0,
            "dom_hash": "",
            "input_hash": "",
            "dialog_count": 0,
            "feedback_count": 0,
        }


def _state_delta(fp1: Dict[str, Any], fp2: Dict[str, Any]) -> bool:
    """Return True if any of url, dom_node_count, input_values_hash, text_hash, or modal_count changed."""
    return (
        fp1.get("url") != fp2.get("url")
        or fp1.get("dom_node_count") != fp2.get("dom_node_count")
        or fp1.get("input_values_hash") != fp2.get("input_values_hash")
        or fp1.get("modal_count") != fp2.get("modal_count")
        or (
            bool(fp1.get("text_hash"))
            and bool(fp2.get("text_hash"))
            and fp1.get("text_hash") != fp2.get("text_hash")
        )
    )


def get_dom_state_fingerprint(page=None) -> Dict[str, Any]:
    p = page or _get_active_page()
    return _capture_state_fingerprint(p) if p else {}
