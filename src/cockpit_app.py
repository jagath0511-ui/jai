# --- EMBEDDED LOCAL HTTP TELEMETRY BRIDGE SERVER (PORT 8765) ---
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "data" / "jai_memory.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# In-memory caches for backwards compatibility
PAGE_REGISTRY = {}
LATEST_PAGE_TEXT = ""
LATEST_SNAPSHOT = {}
LATEST_ELEMENTS = ""

import secrets
from typing import Optional, Tuple

def get_localhost_token() -> str:
    """Retrieves bridge token from data/bridge_token.txt or environment."""
    token_file = ROOT / "data" / "bridge_token.txt"
    if token_file.is_file():
        try:
            tok = token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except Exception:
            pass
    env_tok = os.environ.get("JAI_BRIDGE_TOKEN", "").strip()
    if env_tok:
        return env_tok
    return ""

BROWSER_PREFIXES = ("/browser ", "@browser ", "browser: ", "/browse ", "/web ")

def is_browser_intent(prompt: str, mode: str = "auto", file_path: str = None):
    clean_p = prompt.strip()
    low = clean_p.lower()
    
    if mode == "browser":
        return True, clean_p
        
    browser_triggers = [
        "open", "go to", "visit", "navigate to", "launch",
        "search", "google", "find online",
        "what is on my screen", "read tab", "summarize", "page", "what am i looking at",
        "youtube", "gmail", "github", "leetcode"
    ]
    
    # Natural trigger match
    if any(low.startswith(w) or f" {w} " in f" {low} " for w in browser_triggers):
        return True, clean_p
        
    if re.search(r"https?://\S+", clean_p) or re.search(r"[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", clean_p):
        return True, clean_p
        
    return False, clean_p
class CleanChatHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):

        pass



    def do_GET(self):

        if self.path == "/" or self.path.startswith("/index"):

            self.send_response(200)

            self.send_header("Content-Type", "text/html; charset=utf-8")

            self.end_headers()

            token = get_localhost_token()
            html_out = HTML_CLEAN.replace("<head>", f"<head><script>window.JAI_TOKEN = {json.dumps(token)};</script>")
            self.wfile.write(html_out.encode("utf-8"))

        elif self.path.startswith("/audio/"):

            fname = self.path.split("?")[0].replace("/audio/", "")

            fpath = AUDIO_DIR / fname

            if fpath.exists():

                self.send_response(200)

                self.send_header("Content-Type", "audio/mpeg")

                self.end_headers()

                self.wfile.write(fpath.read_bytes())

            else:

                self.send_error(404)

        else:

            self.send_error(404)



    def do_POST(self):
        # 1. Host Validation (Must be 127.0.0.1 or localhost)
        host = self.headers.get("Host", "").strip().lower()
        host_name = host.split(":")[0] if ":" in host else host
        if host_name not in ("127.0.0.1", "localhost"):
            self.send_error(403, f"Forbidden: Invalid Host header '{host}'")
            return

        # 2. Origin Validation (If present, must be loopback)
        origin = self.headers.get("Origin", "").strip()
        if origin:
            parsed_origin = urlparse(origin)
            origin_host = (parsed_origin.hostname or "").lower()
            if origin_host not in ("127.0.0.1", "localhost"):
                self.send_error(403, f"Forbidden: Untrusted Origin '{origin}'")
                return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        # 3. Localhost Token Validation
        expected_token = get_localhost_token()
        if expected_token:
            auth_header = self.headers.get("Authorization", "").strip()
            bearer_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            custom_token = self.headers.get("X-JAI-Token", "").strip()
            body_token = body.get("token", "").strip() if isinstance(body, dict) else ""
            provided_token = bearer_token or custom_token or body_token

            if not provided_token or not secrets.compare_digest(provided_token, expected_token):
                self.send_error(401, "Unauthorized: Invalid or missing localhost token")
                return

        if self.path == "/api/tts":

            text = body.get("text", "")[:260]

            audio_url = None

            try:

                mp3_file = AUDIO_DIR / "latest_reply.mp3"

                ao.synthesize_turn_sync(speaker="ayanokoji", text=text, out_path=str(mp3_file))

                if mp3_file.exists():

                    audio_url = "/audio/latest_reply.mp3"

            except Exception:

                pass

            self.send_response(200)

            self.send_header("Content-Type", "application/json")

            self.end_headers()

            self.wfile.write(json.dumps({"audio_url": audio_url}).encode("utf-8"))

            return



        if self.path == "/api/chat":
            prompt = body.get("prompt", "").strip()
            project = body.get("project", "ALPHA")
            mode = body.get("mode", "auto")
            file_path = body.get("file_path")

            is_browser, task_prompt = is_browser_intent(prompt, mode=mode, file_path=file_path)

            if is_browser:
                try:
                    import asyncio
                    import urllib.parse
                    from ddgs import DDGS
                    from browser_agent import inspect_current_tab, open_and_operate
                    
                    low = task_prompt.lower()
                    step_note = "Local Actuation Engine (Port 9222)"
                    
                    # 1. Live Web Search (ddgs)
                    if any(low.startswith(w) for w in ["search for", "search", "find online for", "latest news on"]):
                        q = re.sub(r"^(?:search\s+for|search|find online for|latest news on)\s+", "", task_prompt, flags=re.I).strip()
                        step_note = f"Live Web Search: {q}"
                        with DDGS() as ddgs:
                            is_news = any(w in q.lower() for w in ["news", "latest", "today", "updates"])
                            results = list(ddgs.news(q, max_results=4)) if is_news else list(ddgs.text(q, max_results=4))
                            if not results and is_news:
                                results = list(ddgs.text(q, max_results=4))
                        snippets = "\n\n".join([f"Source: {r.get('title')}\nURL: {r.get('url') or r.get('href')}\nSnippet: {r.get('body')}" for r in results])
                        prompt_synth = f"User Query: {q}\n\nSearch Context:\n{snippets}\n\nProvide a direct, factual synthesis citing key sources. Zero fluff."
                        llm_res = route_query(prompt_synth)
                        reply = llm_res.text if hasattr(llm_res, 'text') else str(llm_res)
                    
                    # 2. Inspect active screen
                    elif any(w in low for w in ["what is on my screen", "read tab", "summarize", "page", "what am i looking at"]):
                        step_note = "Chrome Active Tab Inspection"
                        data = asyncio.run(inspect_current_tab())
                        if data.get("status") == "ok":
                            source_url = data.get("url")
                            prompt_dom = f"Webpage Title: {data['title']}\nURL: {data['url']}\nSnapshot:\n{data['snapshot'][:4500]}\nSummarize this tab cleanly for the user:"
                            llm_res = route_query(prompt_dom)
                            reply = llm_res.text if hasattr(llm_res, 'text') else str(llm_res)
                        else:
                            reply = f"Browser Error: {data.get('error')} (Ensure Chrome is open on port 9222)"
                    
                    # 3. In-site Search or Direct URL Navigation
                    else:
                        step_note = "Playwright Navigation"
                        target_url = "https://www.google.com"
                        
                        yt_match = re.search(r"(?:open\s+youtube\s+and\s+search\s+for|search\s+youtube\s+for|youtube\s+search\s+for)\s+(.+)", low)
                        if yt_match:
                            q_clean = urllib.parse.quote_plus(yt_match.group(1).strip())
                            target_url = f"https://www.youtube.com/results?search_query={q_clean}"
                        elif "youtube" in low:
                            target_url = "https://www.youtube.com"
                        elif "gmail" in low:
                            target_url = "https://mail.google.com"
                        elif "github" in low:
                            target_url = "https://www.github.com"
                        elif "leetcode" in low:
                            target_url = "https://www.leetcode.com"
                        elif re.search(r"https?://\S+", task_prompt):
                            target_url = re.search(r"https?://\S+", task_prompt).group(0)
                        elif re.search(r"[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", task_prompt):
                            domain = re.search(r"[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", task_prompt).group(0)
                            target_url = f"https://{domain}"
                        else:
                            clean_q = re.sub(r"^(?:open|go\s+to|visit|launch)\s+", "", task_prompt, flags=re.I).strip()
                            target_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(clean_q)}"

                        res = asyncio.run(open_and_operate(url=target_url))
                        source_url = target_url
                        if res.get("status") == "ok":
                            reply = f"✅ Opened **{res.get('title', target_url)}** in your Chrome browser.\nURL: {target_url}"
                        else:
                            reply = f"⚠️ Navigation failed: {res.get('error')}"
                except Exception as b_err:
                    reply, step_note = f"Browser Error: {b_err}", "Browser Action Error"
                    source_url, extracted_data = None, None
            else:
                try:
                    from src.local_actuator import execute_or_route
                    res = execute_or_route(prompt, project=project, mode=mode)
                    reply, step_note = res["reply"], res["step_note"]
                    source_url = res.get("source_url")
                    extracted_data = res.get("extracted_data")
                except Exception as e:
                    reply, step_note = f"Error: {e}", "Error"
                    source_url = None
                    extracted_data = None

            clean_speech = " ".join(reply.replace("*", "").replace("`", "").replace("#", "").split())[:250]



            self.send_response(200)

            self.send_header("Content-Type", "application/json")

            self.end_headers()

            self.wfile.write(json.dumps({

                "reply": reply,

                "step_note": step_note,

                "speech_text": clean_speech

            }).encode("utf-8"))



def main():

    server = ThreadingHTTPServer(("127.0.0.1", PORT), CleanChatHandler)

    url = f"http://127.0.0.1:{PORT}"

    print(f"JAI Clean Chat UI running at {url}", flush=True)

    try:

        subprocess.Popen(["cmd", "/c", "start", "msedge", f"--app={url}"])

    except Exception:

        import webbrowser

        webbrowser.open(url)

    server.serve_forever()



if __name__ == "__main__":

    main()
