"""JAI Browser commands: read / scroll / type / press / click / back / close tab / list tabs / @tab targeting.

Works on the ACTIVE tab of JAI Browser (Chrome on port 9222), or on the tab specified by @<selector>.
Used by src/local_actuator.py via handle_browser_command(text).
"""
from __future__ import annotations

import logging
logger = logging.getLogger("jai.browser_commands")

import re
from typing import Callable, Optional, Tuple

from src.action_gateway import (
    ActionApprovalRequiredError,
    ActionSecurityError,
    action_gateway,
)

from src.jai_browser import (
    CDP_URL,
    NOT_RUNNING,
    _run_on_browser_thread,
    _get_cdp_page,
    _safe_title as _title,
    _settle,
    is_running,
    resolve_active_page as _active_page,
    _BROWSER_EXECUTOR,
    _is_on_browser_worker_thread,
    _ensure_page_guards,
    UNIVERSAL_TYPER_JS,
    _ACTIVE_DOM_CACHE,
    _FULL_DOM_OVERFLOW_CACHE,
    _get_page_target_id,
    get_numbered_dom_snapshot,
    type_into_page,
)

KEY_MAP = {
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": " ", "backspace": "Backspace", "delete": "Delete",
    "down": "ArrowDown", "up": "ArrowUp", "left": "ArrowLeft", "right": "ArrowRight",
    "pagedown": "PageDown", "pageup": "PageUp", "home": "Home", "end": "End",
}

FIELD_SELECTOR = (
    "textarea:visible, input[type=search]:visible, input[type=text]:visible, "
    "input:not([type]):visible, [contenteditable='true']:visible, "
    "[role=textbox]:visible, [role=searchbox]:visible"
)


def _run(action: Callable) -> str:
    def _act():
        if not is_running():
            return NOT_RUNNING
        try:
            page = _get_cdp_page()
            if page is None:
                return "No web page is open in JAI Browser. Try: open google.com"
            _ensure_page_guards(page)
            return action(page)
        except Exception as e:
            return f"JAI Browser error: {e}"

    try:
        return _run_on_browser_thread(_act)
    except Exception as e:
        return f"JAI Browser error: {e}"


# --- actions ---------------------------------------------------------------

def read_page(max_chars: int = 3500) -> str:
    def act(page):
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()[:max_chars]
        return f"📄 {_title(page)}\n{page.url}\n\n{text or '(no readable text yet)'}"
    return _run(act)


def scroll(direction: str) -> str:
    sign = -1 if direction == "up" else 1

    def act(page):
        page.evaluate(f"window.scrollBy(0, {sign} * window.innerHeight * 0.8)")
        return f"Scrolled {direction} on: {_title(page)}"
    return _run(act)


def _refresh_dom_cache_safe(page) -> None:
    """Invalidate and refresh DOM cache for the active tab without wiping other open tabs."""
    try:
        target_id = _get_page_target_id(page)
        if target_id:
            _ACTIVE_DOM_CACHE.get_tab_cache(target_id).clear()
            _FULL_DOM_OVERFLOW_CACHE.get_tab_cache(target_id).clear()
        get_numbered_dom_snapshot(page=page, max_preview_chars=600)
    except Exception as _suppressed_exc:
        logger.debug('[browser_commands] Handled Exception: %s', _suppressed_exc, exc_info=True)


def _type_into_page(page, text: str) -> bool:
    """Delegates directly to canonical typing engine in src.jai_browser."""
    return type_into_page(page=page, text=text)


def type_text(text: str) -> str:
    try:
        action_gateway.enforce("browser.fill", {"text": text})
    except (ActionApprovalRequiredError, ActionSecurityError) as e:
        return f"🔒 Security Block / Approval Required: {e}"

    def act(page):
        _type_into_page(page, text)
        return f'⌨️ Typed: "{text}"\nSay "press enter" to submit.'
    return _run(act)


def press_key(name: str) -> str:
    try:
        action_gateway.enforce("browser.press_key", {"key": name, "text": name})
    except (ActionApprovalRequiredError, ActionSecurityError) as e:
        return f"🔒 Security Block / Approval Required: {e}"

    key = KEY_MAP.get(name.lower(), name)

    def act(page):
        page.keyboard.press(key)
        _settle(page)
        return f"Pressed {name}. Now on: {_title(page)}"
    return _run(act)


def click_text(label: str) -> str:
    try:
        action_gateway.enforce("browser.click", {"text": label, "label": label})
    except (ActionApprovalRequiredError, ActionSecurityError) as e:
        return f"🔒 Security Block / Approval Required: {e}"

    def act(page):
        try:
            target_loc = None
            # 1. Prefer exact visible matching first
            for cand in (
                page.get_by_role("button", name=label, exact=True).filter(visible=True),
                page.get_by_role("link", name=label, exact=True).filter(visible=True),
                page.get_by_text(label, exact=True).filter(visible=True),
            ):
                try:
                    if cand.count() > 0:
                        target_loc = cand.first
                        break
                except Exception:
                    pass

            # 2. Fall back to substring matching with visibility filter
            if target_loc is None:
                for cand in (
                    page.get_by_role("button", name=label).filter(visible=True),
                    page.get_by_role("link", name=label).filter(visible=True),
                    page.get_by_text(label).filter(visible=True),
                ):
                    try:
                        if cand.count() > 0:
                            target_loc = cand.first
                            break
                    except Exception:
                        pass

            # 3. Final fallback without visible filter if not matched
            if target_loc is None:
                loc = page.get_by_role("button", name=label).or_(
                    page.get_by_role("link", name=label)
                ).or_(
                    page.get_by_text(label)
                )
                if loc.count() > 0:
                    target_loc = loc.first

            if target_loc is None:
                return f'Could not find anything to click that matches "{label}".'

            target_loc.click(timeout=3000)
            _settle(page)
            _refresh_dom_cache_safe(page)
            return f'🖱️ Clicked "{label}". Now on: {_title(page)}'
        except Exception:
            return f'Could not find anything to click that matches "{label}".'
    return _run(act)


def go_back() -> str:
    def act(page):
        try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            _settle(page)
            _refresh_dom_cache_safe(page)
        except Exception as _suppressed_exc:
            logger.debug('[browser_commands] Handled Exception: %s', _suppressed_exc, exc_info=True)
        return f"Went back. Now on: {_title(page)}"
    return _run(act)


def close_tab() -> str:
    def act(page):
        name = _title(page)
        page.close()
        return f"Closed tab: {name}"
    return _run(act)


def list_open_tabs() -> str:
    from src.jai_browser import get_open_tabs_info
    tabs = get_open_tabs_info()
    if isinstance(tabs, str):
        return tabs
    if not tabs:
        return "No web pages are currently open in JAI Browser."
    lines = [f"📑 Open Tabs in JAI Browser ({len(tabs)}):"]
    for t in tabs:
        lines.append(f"{t['index']}. {t['title']} - {t['url']}")
    return "\n".join(lines)


def parse_tab_selector(text: str) -> Tuple[Optional[str], str]:
    """Extract @<selector> from the start of text, e.g. '@youtube scroll down' -> ('youtube', 'scroll down')."""
    t = (text or "").strip()
    m = re.match(r"^@([a-zA-Z0-9_-]+)(?:\s+(.*))?$", t, flags=re.DOTALL)
    if m:
        sel = m.group(1).strip()
        remainder = (m.group(2) or "").strip()
        return sel, remainder
    return None, t


# --- command parser --------------------------------------------------------

def handle_browser_command(text: str) -> Optional[dict]:
    """Return a reply dict if the message is a browser command, else None."""
    t = (text or "").strip()
    low = t.lower()
    result: Optional[str] = None

    # Check for tab switch command: e.g. "/tab 2", "tab 2", "switch to tab 2", "/tab youtube"
    m_tab = re.match(r"^(?:/?tab|switch\s+to\s+tab)\s+(\S+)$", low)
    if m_tab:
        tab_target = m_tab.group(1).strip()
        from src.jai_browser import switch_to_tab
        res = switch_to_tab(tab_target)
        return {"reply": res, "step_note": f"JAI Browser (Tab {tab_target})"}

    # Check for background open command
    m_bg = re.match(r"^(?:open\s+(\S+)\s+in\s+background|background\s+open\s+(\S+))$", t, flags=re.IGNORECASE)
    if m_bg:
        target = m_bg.group(1) or m_bg.group(2)
        from src.local_actuator import parse_open_target
        url = parse_open_target(f"open {target}") or target
        from src.jai_browser import open_url_background
        result = open_url_background(url)
        return {"reply": result, "step_note": "JAI Browser (Background)"}

    # Check for @tab selector
    sel, remainder = parse_tab_selector(t)
    if sel is not None:
        rem_low = remainder.lower()
        from src.jai_browser import switch_to_tab, read_tab_by_selector, run_action_on_tab

        # 1. Switch / Focus (no remainder or 'switch'/'focus'/'show')
        if not rem_low or rem_low in ("switch", "focus", "show"):
            res = switch_to_tab(sel)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # 2. Targeted Read
        if rem_low in ("read", "read page", "read tab", "read this page"):
            info, body, err = read_tab_by_selector(sel)
            if err:
                return {"reply": err, "step_note": f"JAI Browser (@{sel})"}
            body_clean = re.sub(r"\n{3,}", "\n\n", body).strip()
            reply = f"📄 {info['title']}\n{info['url']}\n\n{body_clean or '(no readable text yet)'}"
            return {"reply": reply, "step_note": f"JAI Browser (@{sel})"}

        # 3. Targeted Scroll
        if rem_low in ("scroll down", "scroll up"):
            direction = rem_low.split()[1]
            sign = -1 if direction == "up" else 1

            def _act_scroll(page, info):
                page.evaluate(f"window.scrollBy(0, {sign} * window.innerHeight * 0.8)")
                return f"Scrolled {direction} on Tab {info['index']}: {info['title']}"

            res = run_action_on_tab(sel, _act_scroll)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # 4. Targeted Close
        if rem_low in ("close", "close tab", "close this tab"):
            def _act_close(page, info):
                name = info['title']
                page.close()
                return f"Closed Tab {info['index']}: {name}"

            res = run_action_on_tab(sel, _act_close)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # 5. Targeted Type
        m_type = re.match(r"^(?:type|write)\s+(.+)$", remainder, flags=re.IGNORECASE | re.DOTALL)
        if m_type:
            typed_val = m_type.group(1).strip()
            try:
                action_gateway.enforce("browser.type", {"text": typed_val})
            except (ActionApprovalRequiredError, ActionSecurityError) as e:
                return {"reply": f"🔒 Security Block / Approval Required: {e}", "step_note": f"JAI Browser (@{sel})"}

            def _act_type(page, info):
                _type_into_page(page, typed_val)
                return f'⌨️ Typed on Tab {info["index"]} ({info["title"]}): "{typed_val}"\nSay "press enter" to submit.'

            res = run_action_on_tab(sel, _act_type)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # 6. Targeted Press
        m_press = re.match(r"^press\s+(\w+)$", remainder, flags=re.IGNORECASE)
        if m_press:
            key_name = m_press.group(1)
            key_code = KEY_MAP.get(key_name.lower(), key_name)
            try:
                action_gateway.enforce("browser.press_key", {"key": key_name})
            except (ActionApprovalRequiredError, ActionSecurityError) as e:
                return {"reply": f"🔒 Security Block / Approval Required: {e}", "step_note": f"JAI Browser (@{sel})"}

            def _act_press(page, info):
                page.keyboard.press(key_code)
                _settle(page)
                _refresh_dom_cache_safe(page)
                return f"Pressed {key_name} on Tab {info['index']}. Now on: {_title(page)}"

            res = run_action_on_tab(sel, _act_press)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # 7. Targeted Click
        m_click = re.match(r"^click\s+(?:on\s+)?(.+)$", remainder, flags=re.IGNORECASE)
        if m_click:
            click_label = m_click.group(1).strip().strip("\"'")
            try:
                action_gateway.enforce("browser.click", {"text": click_label, "label": click_label})
            except (ActionApprovalRequiredError, ActionSecurityError) as e:
                return {"reply": f"🔒 Security Block / Approval Required: {e}", "step_note": f"JAI Browser (@{sel})"}

            def _act_click(page, info):
                try:
                    loc = page.get_by_role("button", name=click_label).or_(
                        page.get_by_role("link", name=click_label)
                    ).or_(
                        page.get_by_text(click_label)
                    )
                    loc.first.click(timeout=3000)
                    _settle(page)
                    _refresh_dom_cache_safe(page)
                    return f'🖱️ Clicked "{click_label}" on Tab {info["index"]}. Now on: {_title(page)}'
                except Exception:
                    return f'Could not find anything to click matching "{click_label}" on Tab {info["index"]}.'

            res = run_action_on_tab(sel, _act_click)
            return {"reply": res, "step_note": f"JAI Browser (@{sel})"}

        # If remainder is a question or query (e.g. '@youtube what is this video about?'),
        # return None so local_actuator's execute_targeted_tab_query handles it.
        return None

    # Global active tab commands
    if low in ("read page", "read the page", "read tab", "read this page", "read"):
        result = read_page()
    elif low in ("scroll down", "scroll up"):
        result = scroll(low.split()[1])
    elif low in ("back", "go back"):
        result = go_back()
    elif low in ("close tab", "close this tab"):
        result = close_tab()
    elif low in ("tabs", "list tabs", "show tabs", "open tabs", "get tabs"):
        result = list_open_tabs()
    elif low in ("tidy tabs", "tidy up tabs", "clean tabs", "cleanup tabs"):
        from src.jai_browser import tidy_tabs_smart
        result = tidy_tabs_smart()
    elif low in ("snapshot", "snap", "inspect", "elements", "page elements"):
        from src.jai_browser import get_numbered_dom_snapshot
        snapshot_text, _ = get_numbered_dom_snapshot()
        result = snapshot_text
    elif re.match(r"^click\s+(?:element\s+|#|\[)?(\d+)\]?$", low):
        m_num = re.match(r"^click\s+(?:element\s+|#|\[)?(\d+)\]?$", low)
        from src.agent.tools import tool_browser_click_element
        result = tool_browser_click_element(int(m_num.group(1)))
    elif re.match(r"^(?:fill|type\s+in)\s+(?:element\s+|#|\[)?(\d+)\]?\s+(?:with\s+)?(.+)$", t, flags=re.IGNORECASE):
        m_fill = re.match(r"^(?:fill|type\s+in)\s+(?:element\s+|#|\[)?(\d+)\]?\s+(?:with\s+)?(.+)$", t, flags=re.IGNORECASE)
        el_id = int(m_fill.group(1))
        fill_txt = m_fill.group(2).strip()
        from src.agent.tools import tool_browser_fill_element
        result = tool_browser_fill_element(el_id, fill_txt)
    elif low in ("close duplicate tabs", "close duplicates"):
        from src.jai_browser import close_duplicate_tabs
        result = close_duplicate_tabs()
    elif low in ("group tabs", "grouped tabs", "tab groups", "tabs by domain"):
        from src.jai_browser import group_tabs_by_domain
        result = group_tabs_by_domain()
    elif low in ("close other tabs", "close others", "keep only this tab", "keep this tab"):
        from src.jai_browser import close_other_tabs
        result = close_other_tabs("active")
    elif re.match(r"^(?:keep\s+tab\s+(\S+)|close\s+other\s+tabs\s+except\s+(\S+))$", low):
        m_keep = re.match(r"^(?:keep\s+tab\s+(\S+)|close\s+other\s+tabs\s+except\s+(\S+))$", low)
        keep_target = m_keep.group(1) or m_keep.group(2)
        from src.jai_browser import close_other_tabs
        result = close_other_tabs(keep_target)
    elif re.match(r"^close\s+tab\s+(\d+)$", low):
        m = re.match(r"^close\s+tab\s+(\d+)$", low)
        from src.jai_browser import close_tab_at_index
        result = close_tab_at_index(int(m.group(1)))
    elif re.match(r"^close\s+(?:all\s+)?(\S+)\s+tabs$", low):
        m_close_all = re.match(r"^close\s+(?:all\s+)?(\S+)\s+tabs$", low)
        pat = m_close_all.group(1)
        if pat not in ("other", "duplicate", "all"):
            from src.jai_browser import close_tabs_matching
            result = close_tabs_matching(pat)
    else:
        m_type = re.match(r"^(?:type|write)\s+(.+)$", t, flags=re.IGNORECASE | re.DOTALL)
        if m_type:
            result = type_text(m_type.group(1).strip())
        elif not any(conj in low for conj in (" and ", " then ", " after ", " search for ")):
            m = re.match(r"^press\s+(\w+)$", t, flags=re.IGNORECASE)
            if m:
                result = press_key(m.group(1))
            else:
                m = re.match(r"^click\s+(?:on\s+)?(.+)$", t, flags=re.IGNORECASE)
                if m:
                    result = click_text(m.group(1).strip().strip("\"'"))

    if result is None:
        return None
    return {"reply": result, "step_note": "JAI Browser (CDP)"}
