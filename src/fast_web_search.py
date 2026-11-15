"""Fast, lightweight web search module for JAI ($0, no headless browser needed).

Features:
- Sub-2-second search using DuckDuckGo HTML / Lite endpoints via urllib.
- Zero local disk footprint, zero GPU memory usage.
- Clean URL unquoting and HTML tag stripping.
- Seamless integration with Qwen 2.5 14B and Gemini Flash for real-time grounded Q&A.
"""

from __future__ import annotations

from datetime import datetime
from html import unescape
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

logger = logging.getLogger("jai.fast_web_search")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fast_web_search(query: str, max_results: int = 5, timeout: float = 6.0) -> List[Dict[str, str]]:
    """Execute high-speed HTTP search against DuckDuckGo without browser overhead.

    Args:
        query: Search keywords.
        max_results: Maximum items to return.
        timeout: Socket timeout in seconds.

    Returns:
        List of dicts containing 'title', 'url', and 'snippet'.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return []

    # Strategy 1: DuckDuckGo HTML endpoint
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": clean_q}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://html.duckduckgo.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )

    results: List[Dict[str, str]] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Pattern matches result titles and snippets
        pattern = re.compile(
            r'<h2 class="result__title">\s*<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        matches = pattern.findall(html)
        for href, title, snippet in matches[:max_results]:
            c_title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
            c_snippet = unescape(re.sub(r"<[^>]+>", "", snippet)).strip()

            # Decode uddg redirect
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    href = urllib.parse.unquote(m.group(1))
            elif href.startswith("//"):
                href = "https:" + href

            if c_title and href:
                results.append({"title": c_title, "url": href, "snippet": c_snippet})

        if results:
            return results

    except Exception as exc:
        logger.debug(f"DuckDuckGo HTML search attempt failed: {exc}")

    # Strategy 2: Fallback to DuckDuckGo Lite endpoint
    try:
        lite_url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(clean_q)}"
        lite_req = urllib.request.Request(lite_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(lite_req, timeout=timeout) as resp:
            lite_html = resp.read().decode("utf-8", errors="replace")

        # Lite has <a class='result-link' href='...'>...</a> and <td class='result-snippet'>...</td>
        lite_matches = re.findall(
            r"<a class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>.*?"
            r"<td class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
            lite_html,
            re.DOTALL,
        )
        for href, title, snippet in lite_matches[:max_results]:
            c_title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
            c_snippet = unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    href = urllib.parse.unquote(m.group(1))
            results.append({"title": c_title, "url": href, "snippet": c_snippet})

        if results:
            return results

    except Exception as exc:
        logger.debug(f"DuckDuckGo Lite fallback failed: {exc}")

    # Strategy 3: Wikipedia OpenSearch & Summary API ($0, ultra-reliable fallback)
    try:
        wiki_search_url = (
            f"https://en.wikipedia.org/w/api.php?action=opensearch&search={urllib.parse.quote(clean_q)}"
            f"&limit={max_results}&namespace=0&format=json"
        )
        wiki_req = urllib.request.Request(wiki_search_url, headers={"User-Agent": "JAI-WebSearch/1.0"})
        with urllib.request.urlopen(wiki_req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if len(data) >= 4 and data[1]:
            titles = data[1]
            snippets = data[2]
            urls = data[3]
            for t, s, u in zip(titles[:max_results], snippets[:max_results], urls[:max_results]):
                # If snippet is empty, try quick page summary extract
                snippet_text = s
                if not snippet_text:
                    try:
                        sum_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(t.replace(' ', '_'))}"
                        sum_req = urllib.request.Request(sum_url, headers={"User-Agent": "JAI-WebSearch/1.0"})
                        with urllib.request.urlopen(sum_req, timeout=2.5) as s_resp:
                            sum_data = json.loads(s_resp.read().decode("utf-8"))
                            snippet_text = sum_data.get("extract", "")[:250]
                    except Exception:
                        snippet_text = f"Wikipedia entry for {t}."

                results.append({"title": t, "url": u, "snippet": snippet_text})

            if results:
                return results
    except Exception as exc:
        logger.debug(f"Wikipedia OpenSearch fallback failed: {exc}")

    return results


def format_search_results(results: List[Dict[str, str]], query: str) -> str:
    """Format search results into clean markdown for Telegram and console."""
    if not results:
        return f"🔍 No web results found for '{query}'."

    lines = [f"🌐 <b>Real-Time Search Results for:</b> <i>\"{query}\"</i>\n"]
    for idx, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        lines.append(f"{idx}. <b><a href=\"{url}\">{title}</a></b>")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   🔗 {url}\n")

    return "\n".join(lines).strip()


def synthesize_realtime_answer(
    query: str,
    max_results: int = 4,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch live web results and generate a grounded, factual answer using active LLM."""
    clean_q = query.strip()
    now_str = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
    results = fast_web_search(clean_q, max_results=max_results)

    snippets_text = ""
    if results:
        snippets_list = []
        for idx, r in enumerate(results, 1):
            snippets_list.append(f"[{idx}] {r['title']} - {r['snippet']} ({r['url']})")
        snippets_text = "\n".join(snippets_list)

    grounded_instruction = (
        f"You are JAI, a highly capable autonomous AI assistant with real-time web access.\n"
        f"Current Local Date & Time: {now_str}.\n"
        f"Answer the user's question directly, accurately, and concisely. "
        f"Base your answer on the live web search results below when applicable. "
        f"Cite source numbers [1], [2] where appropriate.\n\n"
        f"Live Web Search Results for '{clean_q}':\n"
        f"{snippets_text or 'No live search results available; rely on system clock or foundational knowledge.'}\n"
    )

    if system_prompt:
        grounded_instruction += f"\nAdditional Instructions: {system_prompt}"

    # Try Qwen 14B GPU first if available, else Gemini Flash
    reply_text = ""
    try:
        from src.qwen_agent_council import call_qwen_endpoint
        reply_text = call_qwen_endpoint(
            prompt=clean_q,
            system_prompt=grounded_instruction,
            temperature=0.5,
            max_tokens=800,
        )
    except Exception:
        pass

    if not reply_text:
        try:
            from llm_router import route_query
            res = route_query(
                prompt=f"{grounded_instruction}\n\nUser Question: {clean_q}",
                query_type="fast",
            )
            reply_text = res.text
        except Exception as exc:
            logger.warning(f"Synthesis LLM call failed: {exc}")
            # Fallback to direct formatted results
            reply_text = format_search_results(results, clean_q)

    # Format final message with synthesis and sources
    final_message = f"💡 <b>Answer:</b>\n{reply_text.strip()}\n\n"
    if results:
        final_message += "🌐 <b>Sources:</b>\n"
        for idx, r in enumerate(results[:3], 1):
            final_message += f"{idx}. <a href=\"{r['url']}\">{r['title'][:60]}</a>\n"

    return {
        "status": "handled",
        "query": clean_q,
        "answer": reply_text,
        "results": results,
        "message": final_message.strip(),
        "timestamp": now_str,
    }
