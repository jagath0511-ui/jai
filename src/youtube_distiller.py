"""YouTube Knowledge Distiller & Zero-Disk-Damage Subtitle Summarizer (Priority P6).

Fetches video subtitles and metadata directly into memory buffer (0 video bytes downloaded).
Extracts the 3 core takeaways and technical insights using LLM router.
Appends structured note to Obsidian daily digest and SQLite Memory Vault.
Optionally transmits summary to Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("jai.youtube_distiller")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [YouTubeDistiller] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

DIGESTS_DIR = BASE_DIR / "vault" / "personal" / "daily_digests"
DIGESTS_DIR.mkdir(parents=True, exist_ok=True)


def extract_in_memory_subtitles(url: str, max_chars: int = 12000) -> Tuple[str, Dict[str, Any]]:
    """Extracts YouTube subtitles and metadata purely in RAM without downloading media.
    
    Returns:
        (transcript_text, metadata_dict)
    """
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt_dlp not installed")
        return "", {}

    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB"],
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "socket_timeout": 15,
    }

    metadata: Dict[str, Any] = {
        "title": "",
        "channel": "",
        "duration": 0,
        "id": "",
        "url": url,
        "description": "",
    }

    transcript_text = ""

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return "", metadata

            metadata["title"] = info.get("title") or "Unknown Video"
            metadata["channel"] = info.get("uploader") or info.get("channel") or "Unknown Creator"
            metadata["duration"] = info.get("duration") or 0
            metadata["id"] = info.get("id") or ""
            raw_desc = info.get("description") or ""
            metadata["description"] = raw_desc[:400].replace("\n", " ").strip() if raw_desc else ""

            # Check for manual or automatic subtitles
            subs = info.get("subtitles") or {}
            auto_subs = info.get("automatic_captions") or {}

            sub_formats = None
            for lang in ["en", "en-US", "en-GB"]:
                if lang in subs:
                    sub_formats = subs[lang]
                    break
                elif lang in auto_subs:
                    sub_formats = auto_subs[lang]
                    break

            if sub_formats:
                # Prefer json3 or vtt for lightweight text parsing
                target_fmt = None
                for fmt in sub_formats:
                    if fmt.get("ext") in ("json3", "vtt"):
                        target_fmt = fmt
                        break
                if not target_fmt and sub_formats:
                    target_fmt = sub_formats[0]

                if target_fmt and target_fmt.get("url"):
                    sub_url = target_fmt["url"]
                    req = urllib.request.Request(sub_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        raw_data = resp.read().decode("utf-8", errors="replace")

                        if target_fmt.get("ext") == "json3" or raw_data.strip().startswith("{"):
                            try:
                                sub_json = json.loads(raw_data)
                                events = sub_json.get("events", [])
                                pieces = []
                                for ev in events:
                                    segs = ev.get("segs", [])
                                    for seg in segs:
                                        t = seg.get("utf8", "").strip()
                                        if t and t != "\n":
                                            pieces.append(t)
                                transcript_text = " ".join(pieces)
                            except Exception:
                                pass
                        
                        if not transcript_text:
                            # Strip VTT timestamps and markup
                            lines = raw_data.splitlines()
                            cleaned_lines = []
                            for line in lines:
                                line = line.strip()
                                if not line or "-->" in line or line.isdigit() or line.startswith("WEBVTT"):
                                    continue
                                line_clean = re.sub(r"<[^>]+>", "", line).strip()
                                if line_clean and (not cleaned_lines or cleaned_lines[-1] != line_clean):
                                    cleaned_lines.append(line_clean)
                            transcript_text = " ".join(cleaned_lines)

    except Exception as e:
        logger.warning(f"In-memory subtitle extraction failed for {url}: {e}")

    # Fallback to description if no subtitles
    if not transcript_text.strip() and metadata.get("description"):
        transcript_text = f"Context from description: {metadata['description']}"

    return transcript_text[:max_chars], metadata


def distill_youtube_url(
    url: str,
    save_to_obsidian: bool = True,
    index_vault: bool = True,
    chat_id: Optional[Union[str, int]] = None,
) -> Dict[str, Any]:
    """Distill YouTube video content with zero disk bloat.
    
    1. Extracts transcript in RAM.
    2. Uses Gemini Flash / Qwen for 3 key takeaways.
    3. Saves structured note in Obsidian.
    4. Indexes into SQLite FTS5 Memory Vault.
    5. Optionally notifies Telegram.
    """
    logger.info(f"Distilling video: {url}")
    transcript, meta = extract_in_memory_subtitles(url)

    title = meta.get("title") or "YouTube Video"
    channel = meta.get("channel") or "Unknown Creator"

    if not transcript.strip():
        summary = f"• Video: {title} by {channel}\n• No spoken transcript was available to distill.\n• Direct link: {url}"
    else:
        # LLM Synthesis
        prompt = (
            f"You are JAI's Knowledge Distiller. Synthesize the following YouTube transcript into:\n"
            f"1. A 1-sentence executive summary.\n"
            f"2. Three concise bullet points highlighting the core technical/actionable takeaways.\n\n"
            f"Title: {title}\n"
            f"Creator: {channel}\n\n"
            f"Transcript:\n{transcript}\n\n"
            f"Format:\n"
            f"**Summary**: <1 sentence>\n"
            f"**Key Takeaways**:\n"
            f"• <Takeaway 1>\n"
            f"• <Takeaway 2>\n"
            f"• <Takeaway 3>"
        )

        try:
            import llm_router
            resp = llm_router.route_query(prompt, query_type="fast")
            summary = resp.text.strip() if hasattr(resp, "text") else str(resp).strip()
        except Exception as e:
            logger.warning(f"LLM summarization failed: {e}. Using extractive fallback.")
            summary = f"• **Overview**: {title} by {channel}\n• **Content Snippet**: {transcript[:200]}..."

    # 1. Index into SQLite Memory Vault
    if index_vault:
        try:
            import memory_vault
            vault_content = (
                f"[YouTube Distillation] {title}\n"
                f"Creator / Person: {channel}\n"
                f"Video URL: {url}\n"
                f"Watched: {datetime.now(timezone.utc).isoformat()}\n"
                f"Key Takeaways & Summary:\n{summary}\n"
            )
            memory_vault.insert(content=vault_content, source="browser_tab", vault_scope="personal")
        except Exception as e:
            logger.warning(f"Failed to index distillation into memory vault: {e}")

    # 2. Append to Obsidian Daily Digest
    obsidian_file = None
    if save_to_obsidian:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        obsidian_file = DIGESTS_DIR / f"{today_str}_browsing_digest.md"
        entry_md = (
            f"\n### 🎬 [[{title}]]\n"
            f"- **Creator / Person**: `{channel}`\n"
            f"- **URL**: [{url}]({url})\n"
            f"- **Distilled At**: `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`\n\n"
            f"{summary}\n"
            f"---\n"
        )
        try:
            with open(obsidian_file, "a", encoding="utf-8") as f:
                f.write(entry_md)
        except Exception as e:
            logger.warning(f"Failed to append to Obsidian digest: {e}")

    # 3. Transmit to Telegram if chat_id provided
    if chat_id:
        try:
            import telegram_bridge
            tg_msg = (
                f"🎬 <b>YouTube Distilled:</b> {title}\n"
                f"👤 <b>Creator:</b> {channel}\n\n"
                f"{summary}\n\n"
                f"🔗 <a href='{url}'>Watch Video</a>"
            )
            telegram_bridge.send_message(chat_id=chat_id, text=tg_msg)
        except Exception as e:
            logger.warning(f"Failed to send distillation to Telegram: {e}")

    return {
        "status": "success",
        "url": url,
        "title": title,
        "channel": channel,
        "summary": summary,
        "obsidian_file": str(obsidian_file) if obsidian_file else None,
        "has_transcript": bool(transcript.strip()),
    }
