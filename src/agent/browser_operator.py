"""JAI Browser operator: controls JAI Browser (port 9222) only.

Production-grade, thread-safe operator coordinating browser actions across
single-worker Playwright CDP threads without socket collisions or race conditions.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from src.jai_browser import (
    CDP_URL,
    _cdp_session,
    _run_on_browser_thread,
    _safe_title,
    capture_screenshot,
    is_privacy_restricted_url,
    is_running,
    open_url,
    open_url_background,
)

logger = logging.getLogger("jai.browser_operator")

LOCAL_BLOCKED_DOMAINS = {
    "malware.com",
    "phishing.com",
    "evil.com",
    "attacker.local",
    "darkweb.onion",
}

# Shared thread pool executor for non-blocking DNS timeouts
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jai_dns")


def resolve_domain_dns_with_timeout(host: str, timeout: float = 2.0) -> List[Tuple[Any, ...]]:
    """Resolve DNS for host with explicit timeout using a shared thread worker (no with-block wait)."""
    future = _DNS_EXECUTOR.submit(socket.getaddrinfo, host, None)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        logger.warning("DNS resolution timed out after %.1fs for host '%s'", timeout, host)
        raise TimeoutError(f"DNS resolution timed out for host '{host}'")


def is_network_safety_blocked(url: str) -> Tuple[bool, str]:
    """Fast, non-blocking SSRF and protocol check for the network route guard.
    
    Blocks:
    - Non-HTTP/HTTPS schemes (file:, javascript:, data:, etc.)
    - CDP port 9222
    - Known malware/phishing domains in LOCAL_BLOCKED_DOMAINS
    - Explicit private/loopback/cloud metadata IP literals (127.0.0.1, 169.254.169.254, 10.0.0.0/8, etc.)
    
    Does NOT block sensitive origins (e.g. github.com, google.com) which belong to human gateway approval.
    Does NOT execute blocking DNS resolution (which stalls the browser event loop).
    """
    if not url:
        return False, ""
    clean = url.strip()

    # Allow safe internal browser targets (tab initialization, about:blank, blank/srcdoc)
    if clean.lower() in ("about:blank", "about:srcdoc", "chrome://newtab/", "chrome://newtab", "edge://newtab/"):
        return False, ""

    try:
        parsed = urllib.parse.urlsplit(clean)
        scheme = (parsed.scheme or "").lower()

        if scheme not in ("http", "https"):
            return True, f"URI scheme is not permitted. Only HTTP and HTTPS are allowed (got '{scheme}:')."

        try:
            if parsed.port == 9222:
                return True, "Port 9222 is blocked to protect Chrome DevTools Protocol."
        except ValueError as port_err:
            logger.warning("Invalid port in URL '%s': %s", clean, port_err)
            return True, f"Blocked: invalid port specification: {port_err}"

        raw_host = parsed.hostname
        if not raw_host:
            return True, "Blocked: HTTP/HTTPS URL missing valid hostname."

        host = raw_host.rstrip(".").lower()

        # Check domain blacklist
        for blocked in LOCAL_BLOCKED_DOMAINS:
            if host == blocked or host.endswith("." + blocked):
                return True, f"Domain is blacklisted: {host}"

        # Allow local test servers only if explicitly authorized via environment variable
        is_localhost_test = os.environ.get("JAI_ALLOW_LOCALHOST_TEST") == "1"
        if is_localhost_test and host in ("localhost", "127.0.0.1", "0.0.0.0"):
            return False, ""

        # Check localhost & local/private hostnames
        if host in ("localhost", "0.0.0.0") or host.endswith(".localhost") or host.endswith(".local"):
            return True, f"Local host '{host}' is blocked for security."

        # Check IP address literals
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                return True, f"Private or local IP address '{host}' is blocked for security."
        except ValueError as _suppressed_exc:
            logger.debug('[browser_operator] Handled ValueError: %s', _suppressed_exc, exc_info=True)
        return False, ""
    except Exception as e:
        logger.warning("Error checking network safety for '%s': %s", clean, e)
        return True, f"Blocked: error validating destination URL: {e}"


def is_domain_blocked(url: str) -> Tuple[bool, str]:
    """Validate destination URL against network safety rules and DNS rebinding protections."""
    blocked, reason = is_network_safety_blocked(url)
    if blocked:
        return True, reason

    clean = (url or "").strip()
    try:
        # Sensitive origins are tracked by ActionGateway taint, not hard-blocked at navigation
        parsed = urllib.parse.urlsplit(clean)
        raw_host = parsed.hostname
        if not raw_host:
            return True, "Blocked: missing valid hostname."
        host = raw_host.rstrip(".").lower()

        is_localhost_test = os.environ.get("JAI_ALLOW_LOCALHOST_TEST") == "1"
        if is_localhost_test and host in ("localhost", "127.0.0.1", "0.0.0.0"):
            return False, ""

        try:
            ipaddress.ip_address(host)
            # Already verified in is_network_safety_blocked
        except ValueError:
            # Host is a domain name -> DNS Rebinding Shield with timeout
            try:
                addr_info = resolve_domain_dns_with_timeout(host, timeout=2.0)
                for res in addr_info:
                    ip_str = res[4][0]
                    resolved_ip = ipaddress.ip_address(ip_str)
                    if (
                        resolved_ip.is_loopback
                        or resolved_ip.is_private
                        or resolved_ip.is_link_local
                        or resolved_ip.is_reserved
                        or resolved_ip.is_unspecified
                    ):
                        return True, f"DNS resolution for '{host}' resolved to forbidden local/private IP '{ip_str}'."
            except (TimeoutError, FutureTimeoutError):
                return True, f"Blocked: DNS resolution timed out for host '{host}'."
            except (socket.gaierror, OSError) as dns_err:
                return True, f"Blocked: DNS resolution failed for host '{host}': {dns_err}"

    except Exception as e:
        logger.warning("Error validating domain in is_domain_blocked for '%s': %s", clean, e)
        return True, f"Blocked: error validating destination URL: {e}"

    return False, ""


def normalize_google_url(url: str, authuser: Optional[str] = None) -> str:
    """Normalize and clean URL for browser navigation with proper authuser query placement."""
    u = (url or "").strip()
    u_lower = u.lower()
    # If forbidden or pseudo-schemes are present, preserve scheme as-is so is_domain_blocked can reject it
    if any(u_lower.startswith(p) for p in ("about:", "chrome:", "javascript:", "data:", "file:", "vbscript:")):
        return u
    if not any(u_lower.startswith(p) for p in ("http://", "https://")):
        u = "https://" + u
    if not authuser:
        return u
    parsed = urllib.parse.urlsplit(u)
    q = parsed.query
    if q:
        query_parts = [p for p in q.split("&") if not p.startswith("authuser=")]
        query_parts.append(f"authuser={authuser}")
        new_query = "&".join(query_parts)
    else:
        new_query = f"authuser={authuser}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


def _inspect_pages_on_browser_thread() -> List[Dict[str, str]]:
    """Inspect open browser pages directly via Playwright context to prevent socket collisions."""
    if not is_running():
        return []
    try:
        with _cdp_session() as browser:
            pages = []
            for ctx in browser.contexts:
                for p in ctx.pages:
                    try:
                        if not p.is_closed():
                            snippet = ""
                            try:
                                raw_text = p.evaluate("() => (document.body ? document.body.innerText : '')") or ""
                                snippet = " ".join(raw_text.split())[:600]
                            except Exception as _suppressed_exc:
                                logger.debug('[browser_operator] Handled Exception: %s', _suppressed_exc, exc_info=True)
                            pages.append({
                                "title": _safe_title(p),
                                "url": p.url or "",
                                "snippet": snippet,
                            })
                    except Exception as _suppressed_exc:
                        logger.debug('[browser_operator] Handled Exception: %s', _suppressed_exc, exc_info=True)
            return pages
    except Exception as e:
        logger.debug("Error inspecting open tabs via Playwright context: %s", e)
        return []


class JaiBrowserOperator:
    """Thread-safe, coordinated operator controlling the JAI Chrome instance."""

    def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate active tab to url through dedicated browser thread."""
        norm_url = normalize_google_url(url)
        blocked, reason = self.is_domain_blocked(norm_url)
        if blocked:
            return {"status": "blocked", "url": norm_url, "reason": reason}

        res = _run_on_browser_thread(open_url, norm_url)
        res_s = str(res)
        if "not_running" in res_s.lower() or res_s.startswith("Error:"):
            return {"status": "error", "url": norm_url, "error": res_s}
        return {"status": "opened_in_jai_browser", "url": norm_url, "details": res}

    def read_all_tabs(self) -> Dict[str, Any]:
        """Inspect all open pages safely via Playwright context with privacy checks against sensitive URLs."""
        try:
            tabs = _run_on_browser_thread(_inspect_pages_on_browser_thread)
            safe_tabs = [
                t for t in (tabs or [])
                if not is_privacy_restricted_url(str(t.get("url", "")))
            ]
            return {
                "status": "ok",
                "count": len(safe_tabs),
                "tabs": safe_tabs,
            }
        except Exception as e:
            logger.warning("read_all_tabs error: %s", e)
            return {"status": "error", "count": 0, "tabs": [], "error": str(e)}

    def open_shadow_tab(self, url: str) -> Dict[str, Any]:
        """Open a background shadow tab without stealing focus."""
        norm_url = normalize_google_url(url)
        blocked, reason = self.is_domain_blocked(norm_url)
        if blocked:
            return {"status": "blocked", "url": norm_url, "reason": reason}

        res = _run_on_browser_thread(open_url_background, norm_url)
        return {"status": "opened_background", "url": norm_url, "details": res}

    def screenshot(self, prefix: str = "snapshot") -> Path:
        """Capture a screenshot with prefix and timestamp routed through browser thread.
        
        Raises RuntimeError if capture failed or output file is empty.
        """
        out_path = _run_on_browser_thread(capture_screenshot, prefix=prefix)
        if not out_path or not str(out_path).strip():
            raise RuntimeError("Screenshot capture failed: empty output path returned.")
        p = Path(out_path)
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError(f"Screenshot capture failed: output file '{out_path}' does not exist or is empty.")
        return p

    def is_domain_blocked(self, url: str) -> Tuple[bool, str]:
        """Validate destination against network safety rules and DNS rebinding protections."""
        return is_domain_blocked(url)


# Backwards compatibility alias
DummyBrowserOperator = JaiBrowserOperator


def get_browser_operator() -> JaiBrowserOperator:
    """Return the global operator instance."""
    return JaiBrowserOperator()


__all__ = [
    "JaiBrowserOperator",
    "DummyBrowserOperator",
    "get_browser_operator",
    "is_network_safety_blocked",
    "is_domain_blocked",
    "normalize_google_url",
]
