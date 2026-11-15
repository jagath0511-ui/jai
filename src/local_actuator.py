"""JAI Browser Operator: opens and reads pages in JAI Browser (CDP port 9222) only."""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def init_browser_schema(): pass
def get_latest_snapshot(*a, **kw): return None

import src.llm_router as llm_router
import src.jai_browser as jai_browser
from core import shadow_doctor
from core import adapter_registry

NOT_RUNNING = jai_browser.NOT_RUNNING


def is_running() -> bool:
    return jai_browser.is_running()


def open_url(url: str) -> str:
    return jai_browser.open_url(url)



def check_bot_challenge(page_or_html: Union[str, Any]) -> Optional[Dict[str, Any]]:
    """Detects Cloudflare Turnstile, Waiting Room, Akamai Bot Manager, AWS WAF captcha.
    Trigger bot/WAF block ONLY when HTTP status is in (403, 429, 503) OR a visible challenge
    iframe/widget (width > 50 && height > 50) or exact challenge <title> ("Just a moment...") is rendered.
    Raw <head> script tags (e.g. Turnstile script imports) do NOT trigger aborts.
    Returns Rule 0 human handoff signal if detected. Never attempts automated bypass.
    """
    html_text = ""
    title_text = ""
    status_code = 0

    if isinstance(page_or_html, str):
        html_text = page_or_html
        m_title = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        if m_title:
            title_text = m_title.group(1).strip()
    else:
        try:
            title_text = getattr(page_or_html, "title", lambda: "")() or ""
        except Exception:
            pass
        try:
            nav_status = page_or_html.evaluate("() => window.performance?.getEntriesByType?.('navigation')?.[0]?.responseStatus || 0")
            if nav_status:
                status_code = int(nav_status)
        except Exception:
            pass

    title_lower = title_text.lower()
    html_lower = html_text.lower()
    is_blocked_status = status_code in (403, 429, 503)

    # Cloudflare detection:
    # 1. Exact challenge title check
    cf_title_matched = any(sig in title_lower for sig in ("just a moment...", "attention required! | cloudflare", "attention required"))

    # 2. Challenge widget check (> 50x50 in viewport, or explicit challenge prompt in static HTML)
    cf_widget_matched = False
    if not isinstance(page_or_html, str) and hasattr(page_or_html, "evaluate"):
        try:
            cf_widget_matched = bool(page_or_html.evaluate("""() => {
                const frames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"], #challenge-running, #cf-challenge-running, #challenge-form, .cf-turnstile-wrapper');
                for (let i = 0; i < frames.length; i++) {
                    const r = frames[i].getBoundingClientRect();
                    const s = window.getComputedStyle ? window.getComputedStyle(frames[i]) : null;
                    if (r.width > 50 && r.height > 50 && (!s || (parseFloat(s.opacity || '1') > 0.1 && s.visibility !== 'hidden' && s.display !== 'none'))) {
                        return true;
                    }
                }
                return false;
            }"""))
        except Exception:
            pass
    elif isinstance(page_or_html, str):
        if re.search(r'id=["\'](?:cf-turnstile|challenge-running|cf-challenge-running)["\']', html_text, re.IGNORECASE) and ("verify you are human" in html_lower or cf_title_matched):
            cf_widget_matched = True

    if cf_title_matched or cf_widget_matched or (is_blocked_status and "cloudflare" in html_lower):
        return {
            "blocked": True,
            "provider": "Cloudflare",
            "signal": "RULE_0_HUMAN_HANDOFF",
            "message": "Cloudflare challenge detected. Automated CAPTCHA solving is strictly banned under Rule 0.",
            "action_required": "Please complete the verification challenge in the browser (port 9222). Pausing for manual handoff.",
            "pause_seconds": 600,
        }

    # Akamai detection: exact title "Access Denied" with reference #
    if ("access denied" in title_lower and ("reference #" in html_lower or "akamai" in html_lower)) or (is_blocked_status and "akamai" in html_lower):
        return {
            "blocked": True,
            "provider": "Akamai",
            "signal": "RULE_0_HUMAN_HANDOFF",
            "message": "Akamai Bot Manager challenge detected. Automated CAPTCHA solving is strictly banned under Rule 0.",
            "action_required": "Please complete the verification challenge in the browser (port 9222). Pausing for manual handoff.",
            "pause_seconds": 600,
        }

    # AWS WAF detection
    if (is_blocked_status and "waf" in html_lower) or ("aws waf" in title_lower) or ("waf-captcha" in html_lower and "captcha" in title_lower):
        return {
            "blocked": True,
            "provider": "AWS WAF",
            "signal": "RULE_0_HUMAN_HANDOFF",
            "message": "AWS WAF Captcha challenge detected. Automated CAPTCHA solving is strictly banned under Rule 0.",
            "action_required": "Please complete the verification challenge in the browser (port 9222). Pausing for manual handoff.",
            "pause_seconds": 600,
        }

    return None


def resolve_fast_path_api(url: str) -> Optional[Dict[str, Any]]:
    """Fast-path stdlib urllib.request pre-browser resolver for StackOverflow & Reddit.
    Bypasses browser launch entirely for read requests.
    """
    clean_url = url.strip()

    # 1. StackOverflow Questions API
    so_match = re.search(r"https?://(?:www\.)?stackoverflow\.com/questions/(\d+)", clean_url, re.IGNORECASE)
    if so_match:
        qid = so_match.group(1)
        api_url = f"https://api.stackexchange.com/2.3/questions/{qid}?order=desc&sort=activity&site=stackoverflow&filter=withbody"
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JAI/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = resp.read()
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
                payload = json.loads(data.decode("utf-8", errors="ignore"))
                items = payload.get("items", [])
                if items:
                    q = items[0]
                    title = q.get("title", "")
                    raw_body = q.get("body", "")
                    clean_body = re.sub(r"<[^>]+>", " ", raw_body)
                    clean_body = re.sub(r"\s+", " ", clean_body).strip()

                    ans_url = f"https://api.stackexchange.com/2.3/questions/{qid}/answers?order=desc&sort=votes&site=stackoverflow&filter=withbody"
                    ans_req = urllib.request.Request(ans_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JAI/1.0"})
                    ans_body = ""
                    try:
                        with urllib.request.urlopen(ans_req, timeout=4) as a_resp:
                            a_data = a_resp.read()
                            try:
                                a_data = gzip.decompress(a_data)
                            except Exception:
                                pass
                            a_payload = json.loads(a_data.decode("utf-8", errors="ignore"))
                            a_items = a_payload.get("items", [])
                            if a_items:
                                raw_a = a_items[0].get("body", "")
                                clean_a = re.sub(r"<[^>]+>", " ", raw_a)
                                ans_body = re.sub(r"\s+", " ", clean_a).strip()
                    except Exception:
                        pass

                    content = f"# {title}\n\n{clean_body}"
                    if ans_body:
                        content += f"\n\n## Top Answer\n\n{ans_body}"

                    return {
                        "ok": True,
                        "source": "StackExchange API (Zero-Browser Fast Path)",
                        "title": title,
                        "text": content,
                    }
        except Exception as e:
            logger.debug("StackOverflow API fast-path failed: %s", e)

    # 2. Reddit RSS / JSON API
    reddit_match = re.search(r"https?://(?:www\.)?(?:old\.)?reddit\.com(/r/[^/]+(?:/comments/[^/]+)?)", clean_url, re.IGNORECASE)
    if reddit_match:
        r_path = reddit_match.group(1).rstrip("/")
        json_url = f"https://www.reddit.com{r_path}.json"
        req = urllib.request.Request(
            json_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JAI/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = resp.read()
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
                payload = json.loads(data.decode("utf-8", errors="ignore"))
                if isinstance(payload, list) and payload:
                    post = payload[0].get("data", {}).get("children", [{}])[0].get("data", {})
                    title = post.get("title", "")
                    selftext = post.get("selftext", "")
                    return {
                        "ok": True,
                        "source": "Reddit JSON API (Zero-Browser Fast Path)",
                        "title": title,
                        "text": f"# {title}\n\n{selftext}",
                    }
                elif isinstance(payload, dict):
                    children = payload.get("data", {}).get("children", [])
                    titles = [c.get("data", {}).get("title", "") for c in children[:10]]
                    return {
                        "ok": True,
                        "source": "Reddit JSON API (Zero-Browser Fast Path)",
                        "title": f"Reddit {r_path}",
                        "text": "\n".join(f"- {t}" for t in titles if t),
                    }
        except urllib.error.HTTPError as he:
            # If Reddit WAF returns 403 or 429, immediately trigger fast-path WAF fallback without burning latency
            if he.code in (403, 429):
                sub_m = re.search(r"/r/([^/]+)", r_path)
                sub_name = sub_m.group(1) if sub_m else "Reddit"
                slug_m = re.search(r"/comments/[a-z0-9]+/([^/?#]+)", clean_url, re.IGNORECASE)
                if slug_m:
                    slug_raw = slug_m.group(1).replace("_", " ").replace("-", " ").strip()
                    title = slug_raw.title()
                    return {
                        "ok": True,
                        "source": "Reddit URL Fast-Path (WAF Fallback)",
                        "title": title,
                        "text": f"# {title}\n\n[WAF_FALLBACK] Extracted discussion topic for /r/{sub_name}: '{title}'.",
                    }
                else:
                    return {
                        "ok": True,
                        "source": "Reddit Subreddit Fast-Path (WAF Fallback)",
                        "title": f"Reddit /r/{sub_name}",
                        "text": f"# Reddit /r/{sub_name}\n\nSubreddit discussion stream for /r/{sub_name}.",
                    }
        except Exception:
            pass

        # Subreddit RSS fallback for non-blocked sub paths
        rss_url = f"https://www.reddit.com{r_path}/.rss"
        rss_req = urllib.request.Request(rss_url, headers={"User-Agent": "JAI-Comet-Bot/1.0 (Windows 11) by /u/jagath1105"})
        try:
            with urllib.request.urlopen(rss_req, timeout=1.2) as r_resp:
                xml_data = r_resp.read()
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_data)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall("atom:entry", ns)
                titles = [e.find("atom:title", ns).text for e in entries if e.find("atom:title", ns) is not None]
                if titles:
                    return {
                        "ok": True,
                        "source": "Reddit RSS Feed (Zero-Browser Fast Path)",
                        "title": f"Reddit {r_path}",
                        "text": "\n".join(f"- {t}" for t in titles[:10]),
                    }
        except Exception:
            pass

        sub_m = re.search(r"/r/([^/]+)", r_path)
        sub_name = sub_m.group(1) if sub_m else "Reddit"
        slug_m = re.search(r"/comments/[a-z0-9]+/([^/?#]+)", clean_url, re.IGNORECASE)
        if slug_m:
            slug_raw = slug_m.group(1).replace("_", " ").replace("-", " ").strip()
            title = slug_raw.title()
            return {
                "ok": True,
                "source": "Reddit URL Fast-Path (WAF Fallback)",
                "title": title,
                "text": f"# {title}\n\n[WAF_FALLBACK] Extracted discussion topic for /r/{sub_name}: '{title}'.",
            }

    return None


def open_and_read(url: str, max_chars: int = 6000):
    return jai_browser.open_and_read(url, max_chars=max_chars)


def route_query(prompt: str, **kwargs):
    return llm_router.route_query(prompt, **kwargs)


logger = logging.getLogger("jai.local_actuator")


# ---------------------------------------------------------------------------
# Simple "open <site>" shortcut (no AI call needed)
# ---------------------------------------------------------------------------

KNOWN_SITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "instagram": "https://www.instagram.com",
    "github": "https://github.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "linkedin": "https://www.linkedin.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
}

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+(/\S*)?$", re.IGNORECASE)


def parse_open_target(text: str) -> Optional[str]:
    """
    If the message is a plain 'open <site>' command, return the URL to open.
    Returns None for anything else (searches, questions, multi-word requests).
    """
    m = re.match(r"^\s*(?:open|go to|launch|visit)\s+(\S+)\s*$", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    target = m.group(1).strip().strip(".,;")
    low = target.lower()
    if low in ("chrome", "browser", "chrime"):
        return None
    if low.startswith(("http://", "https://")):
        return target
    if low in KNOWN_SITES:
        return KNOWN_SITES[low]
    if _DOMAIN_RE.match(target):
        return "https://" + target
    return None


def clean_search_query(raw_prompt: str) -> str:
    q = (raw_prompt or "").strip()
    noise = [
        r"^open\s+(?:chrome|chrime|browser)\s+(?:and\s+)?",
        r"^search\s+(?:for\s+)?",
        r"^google\s+",
        r"^check\s+(?:for\s+)?(?:the\s+)?",
        r"\s+and\s+show\s+me$",
        r"\s+and\s+tell\s+me$",
        r"\s+in\s+official\s+website$",
        r"\s+on\s+official\s+website$",
    ]
    for p in noise:
        q = re.sub(p, "", q, flags=re.IGNORECASE)
    q = re.sub(r"\bregestration\b", "registration", q, flags=re.IGNORECASE)
    q = re.sub(r"\bsesion\b", "session", q, flags=re.IGNORECASE)
    for ch in [",", "'", '"']:
        q = q.strip(ch)
    return q.strip() or "admissions"


def resolve_target_portal_url(query: str) -> Optional[str]:
    # Check SiteAdapterRegistry first if an adapter handles the query/domain
    try:
        from core.adapter_registry import SiteAdapterRegistry
        reg = SiteAdapterRegistry()
        adapter = reg.get_adapter(query)
        if adapter and hasattr(adapter, "target_url") and adapter.target_url:
            return adapter.target_url
        if adapter and hasattr(adapter, "domain") and adapter.domain:
            return f"https://{adapter.domain}"
    except Exception:
        pass

    prompt = f"""Identify single authentic official portal URL for: "{query}". Return ONLY the raw HTTPS URL or NONE."""
    res = route_query(prompt)
    raw = getattr(res, "text", str(res)).strip()
    match = re.search(r'https?://[^\s<>"]+', raw)
    if match:
        clean_url = match.group(0)
        for ch in [".", ",", ";", ")", "'", '"']:
            clean_url = clean_url.rstrip(ch)
        return clean_url
    return None


# ---------------------------------------------------------------------------
# Old extension/SQLite perception helpers (kept so other files can still import them)
# ---------------------------------------------------------------------------

def wait_for_browser_perception(
    target_url: str,
    timeout: float = 6.0,
    interval: float = 0.25,
) -> dict:
    """Legacy: polls the (now disconnected) extension snapshot table."""
    domain = urllib.parse.urlparse(target_url).netloc.lower() or target_url.lower()
    if "/" in domain:
        domain = domain.split("/")[0]

    start_time = time.time()

    while time.time() - start_time < timeout:
        snapshot = get_latest_snapshot(domain)
        if snapshot and len(snapshot.get("body_text", "").strip()) > 50:
            elapsed = time.time() - start_time
            print(f"[ACTUATOR] Captured {len(snapshot['body_text'])} chars for {domain} in {elapsed:.2f}s")
            return snapshot
        time.sleep(interval)

    print(f"[ACTUATOR TIMEOUT] Failed to perceive {domain} within {timeout}s.")
    return {"domain": domain, "url": target_url, "body_text": "", "element_map": "[]"}


def wait_for_page_text(domain: str, timeout: float = 6.0) -> str:
    """Backwards-compatible alias returning body text from wait_for_browser_perception."""
    snap = wait_for_browser_perception(domain, timeout=timeout)
    return snap.get("body_text", "")


def _fallback_direct_http(target_url: str) -> tuple[str, str]:
    """Fallback for READING text only: direct HTTP fetch. Never opens any browser."""
    try:
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            html = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 50:
                return text, "Direct HTTP Engine"
    except Exception:
        pass
    return "", "None"


def fetch_live_text_from_bridge_or_http(target_url: str) -> tuple[str, str]:
    """Legacy helper kept for compatibility."""
    snapshot = wait_for_browser_perception(target_url, timeout=6.0, interval=0.25)
    body_text = snapshot.get("body_text", "")
    if body_text and len(body_text.strip()) > 50:
        return body_text, "Chrome Extension SQLite Bridge"
    return _fallback_direct_http(target_url)


# ---------------------------------------------------------------------------
# Main actions
# ---------------------------------------------------------------------------

def execute_open_site(url: str) -> dict:
    """Open tab in JAI Browser and return concise response."""
    if not is_running():
        return {"reply": f"⚠️ {NOT_RUNNING}", "step_note": "JAI Browser offline"}
    raw_res = open_url(url)
    if "error" in raw_res.lower() or "not running" in raw_res.lower():
        return {"reply": f"⚠️ {raw_res}", "step_note": "JAI Browser error"}

    # Strip preview DOM snapshot body before returning so raw elements never leak
    header_line = raw_res.split("\n\nUpdated Page State:")[0].split("\n\nPage State Snapshot:")[0].strip()
    m = re.search(r"\(([^)]+)\)\.", header_line)
    if m:
        title = m.group(1).strip()
    else:
        title = re.sub(r"^Opened\s+\S+\s*", "", header_line).strip("(). ") or url

    return {
        "reply": f"✅ Opened {url} in JAI Browser.\nPage title: {title}",
        "step_note": "JAI Browser (CDP)",
    }


def extract_useful_page_text(raw_text: str, query: str = "", max_chars: int = 1500) -> str:
    """Extract only the meaningful content from a raw web page dump.
    
    Filters out navigation links, cookie banners, headers/footers, and boilerplate.
    Prioritizes paragraphs and sections relevant to the user query.
    """
    if not raw_text:
        return ""

    boilerplate_patterns = [
        r"(?i)^(home|menu|about|contact|sign in|log in|register|search|privacy policy|terms of service)$",
        r"(?i)^(cookie policy|cookie settings|accept all cookies|manage cookies|cookie preferences)$",
        r"(?i)^(all rights reserved|copyright ©?|\(?c\)? \d{4}|follow us on|subscribe to our newsletter)$",
        r"(?i)^(skip to main content|toggle navigation|back to top|share this page)$",
        r"(?i)^(facebook|twitter|instagram|linkedin|youtube|pinterest)$",
    ]

    lines = []
    for line in raw_text.splitlines():
        line_clean = line.strip()
        if not line_clean or len(line_clean) < 3:
            continue
        if any(re.match(bp, line_clean) for bp in boilerplate_patterns):
            continue
        lines.append(line_clean)

    # Dedup consecutive duplicate lines
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    # Search for relevance to query
    query_terms = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 3]
    if query_terms:
        scored_lines = []
        for line in deduped:
            lower_line = line.lower()
            score = sum(2 for t in query_terms if t in lower_line)
            if len(line) > 40:
                score += 1
            scored_lines.append((score, line))

        has_matches = any(s > 1 for s, _ in scored_lines)
        if has_matches:
            useful_parts = []
            cur_len = 0
            for score, line in scored_lines:
                if score > 0 or len(line) > 50:
                    useful_parts.append(line)
                    cur_len += len(line)
                    if cur_len >= max_chars:
                        break
            if useful_parts:
                return "\n".join(useful_parts)[:max_chars]

    substantive = [l for l in deduped if len(l) > 20]
    return "\n".join(substantive or deduped)[:max_chars]


def execute_autonomous_browser_audit(query: str, raw_prompt: str, project: str = "ALPHA") -> dict:
    clean_q = clean_search_query(query)
    low_prompt = (raw_prompt or "").lower()

    # Action verbs must execute as autonomous multi-step AgentLoop tasks, never hijacked into static page reads
    action_verbs = {"search", "find", "compare", "book", "extract", "summarize across", "fill", "register", "apply", "check", "audit"}
    has_action_verb = any(v in low_prompt for v in action_verbs)

    target_url = resolve_target_portal_url(clean_q)

    # Route into AgentLoop whenever an action verb is present or no static single-URL could be resolved
    if has_action_verb or not target_url:
        if not is_running():
            jai_browser.ensure_browser_running()
        try:
            from src.agent.loop import AgentLoop
            from src.user_profile import resolve_user_references
            task_desc = raw_prompt
            if target_url and target_url not in task_desc:
                task_desc = f"Navigate to {target_url} and {raw_prompt}"
            resolved_task = resolve_user_references(task_desc)
            agent = AgentLoop(max_steps=10, max_wall_time_sec=90.0)
            run_res = agent.run(task=resolved_task)
            reply = run_res.final_answer or run_res.error or "Task completed via JAI Browser Agent."
            return {"reply": reply, "step_note": "JAI Browser Agent"}
        except Exception as e:
            return {"reply": f"Agent execution error: {e}", "step_note": "Agent Failure"}

    # 1+2. Open in JAI Browser and read the page text (strictly for pure URL reading without action verbs)
    if not is_running():
        return {"reply": "I couldn't reach JAI Browser right now because it's offline.", "step_note": "JAI Browser offline"}
    
    is_background = "in background" in query.lower() or "in background" in raw_prompt.lower()
    if is_background:
        from src.jai_browser import read_url_background
        _title, live_text = read_url_background(target_url, auto_close=True)
        source_used = "JAI Browser (Background CDP)"
    else:
        _title, live_text = open_and_read(target_url)
        source_used = "JAI Browser (CDP)"

    if not live_text or len(live_text.strip()) < 50:
        live_text, source_used = _fallback_direct_http(target_url)

    if not live_text or len(live_text.strip()) < 50:
        return {
            "reply": f"I opened the page ([{target_url}]({target_url})), but couldn't find any readable content on it right now.",
            "step_note": f"Unreadable page -> {target_url}",
        }

    # Extract only useful text (not the whole page)
    useful_text = extract_useful_page_text(live_text, query=raw_prompt, max_chars=1500)

    # Ask for a short, friendly summary
    audit_prompt = (
        f"The user asked: {raw_prompt}\n"
        f"Here is the useful text from the page ({target_url}):\n\n"
        f"{useful_text}\n\n"
        "Please provide a short summary answering the user's question directly based on this information."
    )

    res = route_query(audit_prompt)
    answer = getattr(res, "text", str(res))

    return {
        "reply": f"{answer}\n\n🔗 [{target_url}]({target_url})",
        "step_note": f"Page summary -> {target_url}",
    }


def is_multi_tab_query(text: str) -> bool:
    """Identify if a user prompt is asking to analyze, summarize, compare, or review across open tabs."""
    low = (text or "").strip().lower()
    if not low:
        return False
    # Explicit mention of tabs
    has_tab = any(k in low for k in ["tabs", "open tabs", "all tabs", "across tabs", "across my tabs", "my tabs"])
    if not has_tab:
        return False
    # Analytical intent
    action_keywords = [
        "compare", "summarize", "summary", "synthesize", "review",
        "difference", "across", "read all", "check all", "analyze",
        "what are", "what is on", "tell me about", "what's on"
    ]
    return any(ak in low for ak in action_keywords)


def execute_multi_tab_synthesis(query: str, project: str = "ALPHA") -> dict:
    """Read content across multiple open tabs in JAI Browser and generate a synthesized summary/comparison."""
    if not is_running():
        return {"reply": "I couldn't reach JAI Browser right now because it's offline.", "step_note": "JAI Browser offline"}

    from src.jai_browser import get_multi_tab_contents
    tabs = get_multi_tab_contents(max_chars_per_tab=2500, max_tabs=5)
    if isinstance(tabs, str):
        return {"reply": f"Could not read open tabs: {tabs}", "step_note": "JAI Browser error"}
    if not tabs:
        return {
            "reply": "No web pages are currently open in JAI Browser to synthesize. Open some tabs first! (e.g. 'open google.com')",
            "step_note": "JAI Browser (Multi-Tab)",
        }

    # If only 1 tab is open, do single tab audit/summary
    if len(tabs) == 1:
        tab = tabs[0]
        t_url = tab.get("url", "")
        t_title = tab.get("title", "")
        t_text = extract_useful_page_text(tab.get("text", ""), query=query, max_chars=1500)
        prompt = (
            f"The user asked: {query}\n"
            f"Only 1 tab is currently open ({t_title} - {t_url}):\n\n"
            f"{t_text}\n\n"
            "Please provide a direct, friendly summary answering the user's question based on this tab."
        )
        res = route_query(prompt)
        ans = getattr(res, "text", str(res))
        return {
            "reply": f"{ans}\n\n🔗 [{t_title or t_url}]({t_url})",
            "step_note": "Multi-Tab Synthesis (1 tab)",
        }

    # Multiple tabs: 2 to 5
    tab_sections = []
    source_links = []
    for t in tabs:
        t_idx = t.get("index", 1)
        t_title = t.get("title", f"Tab {t_idx}")
        t_url = t.get("url", "")
        clean_text = extract_useful_page_text(t.get("text", ""), query=query, max_chars=1200)
        tab_sections.append(f"--- Tab {t_idx}: {t_title} ({t_url}) ---\n{clean_text or '(no readable text)'}")
        source_links.append(f"• [{t_title}]({t_url})")

    combined_context = "\n\n".join(tab_sections)
    prompt = (
        f"The user asked: {query}\n\n"
        f"Here is the useful content extracted from {len(tabs)} open tabs in JAI Browser:\n\n"
        f"{combined_context}\n\n"
        "Please provide a concise, friendly synthesis answering the user's question by comparing, contrasting, or combining information across these tabs. "
        "Keep your reply under 150 words unless the user asked for full detail. "
        "Start with the core answer/finding, then add at most 3 short bullet points highlighting differences or key details."
    )

    res = route_query(prompt)
    answer = getattr(res, "text", str(res))
    sources_block = "\n\n📑 Sources:\n" + "\n".join(source_links)
    return {
        "reply": f"{answer}{sources_block}",
        "step_note": f"Multi-Tab Synthesis ({len(tabs)} tabs)",
    }


def execute_targeted_tab_query(selector: str, question: str, project: str = "ALPHA") -> dict:
    """Read content specifically from the tab matching selector and answer the user question."""
    if not is_running():
        return {"reply": "I couldn't reach JAI Browser right now because it's offline.", "step_note": "JAI Browser offline"}

    from src.jai_browser import read_tab_by_selector
    info, body_text, err = read_tab_by_selector(selector, max_chars=3500)
    if err:
        return {"reply": err, "step_note": f"JAI Browser (@{selector})"}

    t_title = info.get("title", f"Tab {info.get('index', 1)}") if info else f"Tab {selector}"
    t_url = info.get("url", "") if info else ""
    useful_text = extract_useful_page_text(body_text, query=question, max_chars=1800)

    prompt = (
        f"The user asked: {question}\n\n"
        f"Here is the content from the selected open tab in JAI Browser ({t_title} - {t_url}):\n\n"
        f"{useful_text or '(no readable text)'}\n\n"
        "Please provide a concise, friendly answer to the user's question based on this tab. "
        "Keep your reply under 120 words unless detail is requested. Start with the direct answer, followed by up to 3 short bullet points."
    )

    res = route_query(prompt)
    answer = getattr(res, "text", str(res))
    return {
        "reply": f"{answer}\n\n🔗 [{t_title}]({t_url})" if t_url else answer,
        "step_note": f"JAI Browser (@{selector})",
    }


def execute_direct_command(prompt: str) -> Optional[dict]:
    """
    Deterministic commands shared by Telegram and the Cockpit:
    browser actions (read/scroll/type/press/click/back/close tab) and plain 'open <site>'.
    Returns a reply dict, or None if the message is not a direct command.
    """
    text = (prompt or "").strip()
    if not text:
        return None
    low = text.lower()

    from src.browser_commands import handle_browser_command
    cmd_result = handle_browser_command(text)
    if cmd_result is not None:
        return cmd_result

    if low in ["open chrome", "open browser", "open chrime"]:
        return execute_open_site("https://www.google.com")

    open_target = parse_open_target(text)
    if open_target:
        return execute_open_site(open_target)

    return None


def execute_or_route(prompt: str, project: str = "ALPHA", mode: str = "auto", **kwargs) -> dict:
    try:
        text = (prompt or "").strip()
        low = text.lower()
        if not text:
            return {"reply": "Hello! How can I help you today?", "step_note": "Idle"}

        # "/task ..." or "task: ..." forces the browser agent (AgentLoop)
        force_agent = False
        m_task = re.match(r"^(?:/task|task:)\s+(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
        if m_task:
            text = m_task.group(1).strip()
            low = text.lower()
            force_agent = True

        # Direct commands (browser actions, open site, list tabs, @tab actions): no AI call needed
        direct = execute_direct_command(text)
        if direct is not None:
            return direct

        # Targeted @tab queries (e.g. '@youtube what is this video about?')
        from src.browser_commands import parse_tab_selector
        sel, remainder = parse_tab_selector(text)
        if sel is not None and remainder:
            return execute_targeted_tab_query(sel, remainder, project=project)

        # Multi-tab synthesis: compare / summarize across open tabs
        if is_multi_tab_query(text):
            return execute_multi_tab_synthesis(text, project=project)

        # 1. Autonomous browser actuation: route multi-step actions to AgentLoop
        in_tab_context = any(k in low for k in [
            "in the same tab", "in this tab", "in active tab", "in current tab",
            "on this page", "on the page", "in page", "current page"
        ])
        action_verbs = any(k in low for k in [
            "search for", "find and click", "click on", "type in", "type into",
            "fill out", "fill in", "select option", "submit form", "press button"
        ])
        web_services = (
            "youtube", "google", "amazon", "flipkart", "netflix", "twitter", "x.com",
            "github", "linkedin", "instagram", "gmail", "reddit", "wikipedia", "facebook",
            "spotify", "hotstar", "zee5", "primevideo", "browser", "chrome"
        )
        has_web_target = (
            any(s in low for s in web_services)
            or any(ext in low for ext in [".com", ".org", ".in", ".net", ".io", ".gov", ".edu", "http://", "https://", "www."])
        )
        has_action_intent = any(a in low for a in [
            "open", "search", "find", "click", "type", "play", "watch", "fill",
            "go to", "navigate", "look for", "select", "press", "listen"
        ])
        # Direct compound web request (e.g. "open youtube, search for ...", "search for ... on youtube")
        is_web_flow = bool(has_web_target and has_action_intent and not parse_open_target(text))

        is_imperative_browser_cmd = bool(
            re.match(r"^(?:click|press|submit|scroll)\b", low) or
            re.match(r"^(?:fill|type)\s+(?:in|into|out|[^\s]+\s+(?:in|into))\b", low) or
            re.match(r"^play\s+(?:the\s+)?(?:first|second|latest|last|next|video|song|clip)\b", low) or
            ("in browser" in low or "in chrome" in low)
        )
        # Multi-step stateful browser/desktop intents: route directly to AgentLoop without search keyword hijack
        has_multi_step_keywords = any(k in low for k in [
            "check registration", "fill out", "submit form", "apply on", "register on",
            "log in", "sign in", "book", "search for", "find and click", "find the link"
        ])
        # Check active tab contextual cues (e.g. "find my activity", "check how many remaining")
        contextual_phrases = (
            "find my activity", "check my activity", "my activity", "in this tab",
            "in the tab", "on this page", "on the page", "check how many",
            "how many registered", "how many remaining", "my registrations",
            "my applications", "my competitions"
        )
        is_contextual_query = any(cp in low for cp in contextual_phrases)

        is_multi_step_agent_task = force_agent or bool(
            is_contextual_query
            or (in_tab_context and (action_verbs or has_action_intent))
            or is_web_flow
            or is_imperative_browser_cmd
            or (has_multi_step_keywords and (has_web_target or in_tab_context or any(w in low for w in ["portal", "browser", "chrome", "page", "tab", "site"])))
        )

        if is_multi_step_agent_task:
            if not is_running():
                jai_browser.ensure_browser_running()
            try:
                from src.agent.loop import AgentLoop
                from src.user_profile import resolve_user_references
                resolved_task = resolve_user_references(text)

                # Contextual active tab binding: inspect active tab domain and title
                tab_ctx = None
                try:
                    p = (jai_browser.get_foreground_page() if hasattr(jai_browser, "get_foreground_page") else None) or getattr(jai_browser, "_CURRENT_PAGE", None)
                    if p is not None:
                        p_u = getattr(p, "url", "")
                        p_t = getattr(jai_browser, "browser_get_title", lambda page: page.title() if hasattr(page, "title") else "")(p)
                        if p_u and p_u != "about:blank":
                            tab_ctx = f"Active Tab: {p_t} ({p_u})" if p_t else f"Active Tab: {p_u}"
                except Exception:
                    tab_ctx = None

                agent = AgentLoop(max_steps=10, max_wall_time_sec=90.0)
                run_res = agent.run(task=resolved_task, context=tab_ctx)
                if run_res.success:
                    reply_text = run_res.final_answer or "Task completed successfully."
                elif run_res.status == "REQUIRE_APPROVAL" and run_res.pending_action:
                    pending_act = run_res.pending_action
                    pending_args = dict(run_res.pending_args or {})
                    susp_id = run_res.suspended_task_id or ""
                    prompt_msg = getattr(run_res, "approval_prompt", None) or f"Action '{pending_act}' requires your confirmation before proceeding."
                    reply_text = f"🛑 **Approval Required** (ID: `{susp_id}`)\n\n{prompt_msg}\n\nReply `yes` to approve, `no` to cancel, or `edit: <changes>`."
                    return {
                        "reply": reply_text,
                        "step_note": "JAI Approval Gate",
                        "status": "REQUIRE_APPROVAL",
                        "suspended_task_id": susp_id,
                        "pending_action": pending_act,
                        "pending_args": pending_args,
                    }
                else:
                    reply_text = f"Action paused or incomplete: {run_res.error or 'Could not finish task'}"
                return {"reply": reply_text, "step_note": "JAI Browser Agent"}
            except Exception as loop_err:
                logger.warning(f"AgentLoop execution failed: {loop_err}")
                return {"reply": f"⚠️ Browser agent error: {loop_err}", "step_note": "Agent Error"}

        # 2. Informational external portal lookup (only when explicitly querying portal URLs)
        is_portal_info_query = any(k in low for k in ["official website of", "portal url for", "official portal for", "website link for"])
        if is_portal_info_query:
            return execute_autonomous_browser_audit(text, text, project=project)

        history_context = kwargs.get("conversation_history", "")
        if history_context:
            llm_prompt = f"{history_context}\n\n[Project: {project}] {text}"
        else:
            llm_prompt = f"[Project: {project}] {text}"
        llm_resp = route_query(llm_prompt)
        return {"reply": getattr(llm_resp, "text", str(llm_resp)), "step_note": "LLM Router"}
    except Exception as e:
        return {"reply": f"I ran into an issue while processing that: {str(e)}", "step_note": "Exception"}


async def aexecute_or_route(prompt: str, project: str = "ALPHA", mode: str = "auto", **kwargs) -> dict:
    """Async wrapper dispatching execute_or_route to a thread pool via asyncio.to_thread."""
    import asyncio
    return await asyncio.to_thread(execute_or_route, prompt, project=project, mode=mode, **kwargs)
