"""src/browser/extraction.py - Structural Card Extraction & Pagination Engine.
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

from src.browser.cdp import (
    browser_thread,
    _get_active_page,
    _ensure_page_guards,
    _get_page_target_id,
    _safe_title,
    ActuatorExecutionError,
    ElementNotFoundError,
    ACTION_TIMEOUT_MS,
    _REPO_ROOT,
    _settle,
)
from src.browser.perception import (
    _capture_state_fingerprint,
    _state_delta,
    extract_universal_page_content,
    get_numbered_dom_snapshot,
)
from src.browser.dispatch import get_jb_attr
from src.browser.actions import (
    click_element_by_id,
    scroll_page,
)

logger = logging.getLogger("jai.browser.extraction")

class CollectionResult(str):
    @property
    def items(self) -> list:
        return []


def format_collection_to_markdown(items: list[dict]) -> str:
    if not items:
        return "No items."
    headers = list(items[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for it in items:
        lines.append("| " + " | ".join(str(it.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def browser_extract_collection(page: Any = None, max_pages: int = 5, **kwargs) -> CollectionResult:
    items = extract_paginated_cards(max_pages=max_pages, page=page, **kwargs)
    max_items = kwargs.get("max_items")
    if max_items is not None and isinstance(max_items, int) and max_items > 0:
        items = items[:max_items]
    md = format_collection_to_markdown(items)

    class _CollectionResult(CollectionResult):
        def __new__(cls, val, itms=None):
            obj = super().__new__(cls, val)
            obj._items = list(itms or [])
            return obj

        @property
        def items(self) -> list:
            return getattr(self, "_items", [])

        def get(self, key: str, default: Any = None) -> Any:
            if key == "items":
                return self.items
            if key == "total_items_extracted":
                return len(self.items)
            return default

        def __getitem__(self, key: Any) -> Any:
            if key == "items":
                return self.items
            if key == "total_items_extracted":
                return len(self.items)
            return super().__getitem__(key)

    return _CollectionResult(md, itms=items)


def extract_structured_cards(page: Any = None, max_cards: int = 50, auto_scroll: bool = True, **kwargs) -> dict:
    cards = extract_paginated_cards(max_pages=1, page=page)
    if not cards:
        res = extract_universal_page_content(page=page)
        cards = [
            {"title": item.get("name") or item.get("text", ""), "role": item.get("role", ""), "is_remaining": True}
            for item in res.get("items", [])
            if (item.get("name") or item.get("text"))
        ]
    cards = cards[:max_cards]
    for c in cards:
        if "is_remaining" not in c:
            txt_l = str(c.get("text", "") + " " + c.get("status", "")).lower()
            c["is_remaining"] = not any(w in txt_l for w in ("closed", "ended", "expired", "past"))
    rem_cnt = sum(1 for c in cards if c.get("is_remaining", True))
    payload = {
        "cards": cards,
        "total_cards_extracted": len(cards),
        "remaining_active_count": rem_cnt,
        "ended_past_count": len(cards) - rem_cnt,
        "active_filter_tab": "All",
    }
    payload["untrusted_dom_data"] = f"<untrusted_dom_data>\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</untrusted_dom_data>"
    return payload


_extract_structured_cards_core = extract_structured_cards


@browser_thread
def extract_paginated_cards(card_selector: Optional[str] = None, next_button_selector: Optional[str] = None, max_pages: int = 5, page: Any = None, **kwargs: Any) -> list[dict]:
    """Bounded multi-page card harvester supporting DOM Next buttons, arrow controls, and pagination indicators."""
    if hasattr(card_selector, "goto") or hasattr(card_selector, "evaluate"):
        p = card_selector
        card_selector = None
    else:
        p = page or _get_active_page()
    if not p:
        return []
    aggregated: list[dict] = []
    seen = set()

    for page_num in range(1, max_pages + 1):
        raw_eval = p.evaluate("""(customSel) => {
            const root = document.querySelector('main, #main-content, .main-content, #content') || document.body;
            let els = customSel ? Array.from(root.querySelectorAll(customSel)) : [];
            if (!els.length) {
                for (const s of ['article', '.card', '[class*="card"]', 'tr', 'li', '[class*="item"]']) {
                    const found = Array.from(root.querySelectorAll(s)).filter(el => !el.closest('aside, nav, header, footer'));
                    if (found.length >= 2) { els = found; break; }
                }
            }
            const cards = els.map(el => {
                const h = el.querySelector('h1, h2, h3, h4, h5, .title, [class*="title"], a');
                return {
                    id: el.getAttribute('data-id') || el.id || '',
                    title: h ? (h.innerText || '').trim() : (el.innerText || '').split('\\n')[0].trim(),
                    text: (el.innerText || '').trim()
                };
            }).filter(c => c.title.length > 0);

            const curPageEl = document.querySelector('[aria-current="page"], .pagination .active, [class*="pagination"] .active, .current-page, [class*="current-page"], .page-indicator');
            const pageInd = curPageEl ? (curPageEl.innerText || curPageEl.getAttribute('aria-label') || '').trim() : '';

            return { cards, pageInd };
        }""", card_selector)

        if isinstance(raw_eval, dict):
            raw_cards = raw_eval.get("cards", [])
            current_page_ind = raw_eval.get("pageInd", "")
        elif isinstance(raw_eval, list):
            raw_cards = raw_eval
            current_page_ind = ""
        else:
            raw_cards = []
            current_page_ind = ""

        if isinstance(raw_cards, list):
            for c in raw_cards:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "").strip()
                if cid:
                    key = f"id:{cid}"
                else:
                    title = str(c.get("title") or "").strip()
                    text = str(c.get("text") or "").strip()
                    text_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
                    key = f"title_hash:{title}:{text_hash}"

                if key not in seen:
                    seen.add(key)
                    aggregated.append(c)

        if page_num >= max_pages:
            break

        clicked = False
        if next_button_selector:
            try:
                loc = p.locator(next_button_selector).first
                if loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=2000)
                    clicked = True
            except Exception as _suppressed_exc:
                logger.debug('[extraction] Handled Exception: %s', _suppressed_exc, exc_info=True)
        if not clicked:
            next_res = p.evaluate("""() => {
                function isClickable(el) {
                    if (!el) return false;
                    if (el.disabled) return false;
                    if (el.getAttribute('aria-disabled') === 'true') return false;
                    if (el.classList.contains('disabled')) return false;
                    try {
                        const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
                        if (style && (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none')) {
                            return false;
                        }
                    } catch (_) {}
                    return true;
                }

                // 1. Direct attribute selectors: rel="next" and aria-label variants
                const directSelectors = [
                    '[rel="next"]',
                    'button[aria-label*="next" i]',
                    'a[aria-label*="next" i]',
                    '[role="button"][aria-label*="next" i]',
                    'button[aria-label*="forward" i]',
                    'a[aria-label*="forward" i]',
                    '[role="button"][aria-label*="forward" i]',
                    'button[aria-label*="right" i]',
                    'a[aria-label*="right" i]',
                    '[role="button"][aria-label*="right" i]',
                    '.pagination-next',
                    '#next-page-btn',
                    '[class*="pagination-next"]',
                    '[class*="pagination__next"]',
                    '[class*="next-page"]',
                    '[class*="next_page"]'
                ];
                for (const sel of directSelectors) {
                    const el = document.querySelector(sel);
                    if (isClickable(el)) { el.click(); return true; }
                }

                // 2. Text matches
                for (const b of Array.from(document.querySelectorAll('button, a, [role="button"]'))) {
                    const txt = (b.innerText || '').trim();
                    if (/^next\\b/i.test(txt) || txt === '>' || txt === '»' || txt === '→' || /^next page/i.test(txt)) {
                        if (isClickable(b)) { b.click(); return true; }
                    }
                }

                // 3. Arrow buttons / svg / icon inside a pagination control
                const paginationRoots = Array.from(document.querySelectorAll(
                    'nav[aria-label*="pagination" i], .pagination, [class*="pagination"], [role="navigation"], .pager, [class*="pager"]'
                ));
                for (const root of paginationRoots) {
                    const controls = Array.from(root.querySelectorAll('button, a, [role="button"]'));
                    for (const ctrl of controls) {
                        if (!isClickable(ctrl)) continue;
                        const icon = ctrl.querySelector('svg, i, [class*="icon"], [class*="arrow"], [class*="chevron"]');
                        if (icon) {
                            const combined = (ctrl.className || '') + ' ' + (ctrl.getAttribute('aria-label') || '') + ' ' + (icon.outerHTML || '');
                            if (/next|right|forward|chevron-right|arrow-right/i.test(combined)) {
                                ctrl.click();
                                return true;
                            }
                        }
                    }
                    if (controls.length >= 2) {
                        const lastCtrl = controls[controls.length - 1];
                        if (isClickable(lastCtrl)) {
                            const icon = lastCtrl.querySelector('svg, i, [class*="icon"], [class*="arrow"], [class*="chevron"]');
                            const lastTxt = (lastCtrl.innerText || '').trim();
                            if (icon || lastTxt === '' || />|»|→/.test(lastTxt)) {
                                lastCtrl.click();
                                return true;
                            }
                        }
                    }
                }

                // 4. Any button or link with an arrow svg/icon
                for (const b of Array.from(document.querySelectorAll('button, a, [role="button"]'))) {
                    if (!isClickable(b)) continue;
                    const aria = b.getAttribute('aria-label') || '';
                    if (/next|right|forward/i.test(aria)) {
                        b.click();
                        return true;
                    }
                    const svg = b.querySelector('svg, [class*="arrow-right"], [class*="chevron-right"]');
                    if (svg) {
                        const svgHtml = (svg.outerHTML || '') + ' ' + (b.className || '');
                        if (/right|next|chevron-right|arrow-right/i.test(svgHtml)) {
                            b.click();
                            return true;
                        }
                    }
                }

                return false;
            }""")
            clicked = bool(next_res)

        if not clicked:
            break

        prev_sig = [
            (str(c.get("id") or "").strip() or f"{str(c.get('title') or '').strip()}::{str(c.get('text') or '').strip()}")
            for c in raw_cards
            if isinstance(c, dict)
        ]
        prev_page_ind = current_page_ind

        start_wait = time.time()
        while time.time() - start_wait < 8.0:
            _settle(p, quiet_ms=80, max_wait_ms=800)
            check_eval = p.evaluate("""(customSel) => {
                const root = document.querySelector('main, #main-content, .main-content, #content') || document.body;
                let els = customSel ? Array.from(root.querySelectorAll(customSel)) : [];
                if (!els.length) {
                    for (const s of ['article', '.card', '[class*="card"]', 'tr', 'li', '[class*="item"]']) {
                        const found = Array.from(root.querySelectorAll(s)).filter(el => !el.closest('aside, nav, header, footer'));
                        if (found.length >= 2) { els = found; break; }
                    }
                }
                const cards = els.map(el => {
                    const h = el.querySelector('h1, h2, h3, h4, h5, .title, [class*="title"], a');
                    return {
                        id: el.getAttribute('data-id') || el.id || '',
                        title: h ? (h.innerText || '').trim() : (el.innerText || '').split('\\n')[0].trim(),
                        text: (el.innerText || '').trim()
                    };
                }).filter(c => c.title.length > 0);

                const curPageEl = document.querySelector('[aria-current="page"], .pagination .active, [class*="pagination"] .active, .current-page, [class*="current-page"], .page-indicator');
                const pageInd = curPageEl ? (curPageEl.innerText || curPageEl.getAttribute('aria-label') || '').trim() : '';

                return { cards, pageInd };
            }""", card_selector)

            if isinstance(check_eval, dict):
                check_cards = check_eval.get("cards", [])
                check_page_ind = check_eval.get("pageInd", "")
            elif isinstance(check_eval, list):
                check_cards = check_eval
                check_page_ind = ""
            else:
                check_cards = []
                check_page_ind = ""

            check_sig = [
                (str(c.get("id") or "").strip() or f"{str(c.get('title') or '').strip()}::{str(c.get('text') or '').strip()}")
                for c in check_cards
                if isinstance(c, dict)
            ]

            if (check_sig and check_sig != prev_sig) or (check_page_ind and check_page_ind != prev_page_ind):
                break
            time.sleep(0.2)

    return aggregated


browser_extract_paginated_cards = extract_paginated_cards
_extract_paginated_cards_core = extract_paginated_cards
universal_page_content = extract_universal_page_content


def format_cards_to_markdown(cards: list[dict]) -> str:
    """Deterministically formats cards to markdown by delegating to src.agent.loop (SSoT)."""
    from src.agent.loop import format_cards_to_markdown as _format_loop
    return _format_loop(cards)
