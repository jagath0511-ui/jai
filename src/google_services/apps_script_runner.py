"""Google Apps Script Serverless Cloud Runner for JAI.

Dispatches serverless cloud document and spreadsheet generation to a free
Google Apps Script Web App without requiring local CPU/RAM overhead.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

logger = logging.getLogger("jai.apps_script")


class AppsScriptRunner:
    """Dispatches tasks to Google Apps Script Web App endpoint with local fallback."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> None:
        self.webhook_url = (webhook_url or os.getenv("APPS_SCRIPT_WEBHOOK_URL", "")).strip()
        self.secret = (secret or os.getenv("APPS_SCRIPT_SECRET", "")).strip()

    def is_configured(self) -> bool:
        """Return True if Apps Script webhook URL is set."""
        return bool(self.webhook_url and self.webhook_url.startswith("https://script.google.com"))

    def _call_webhook(self, payload: Dict[str, Any], timeout: float = 25.0) -> Dict[str, Any]:
        """POST payload to Google Apps Script Web App."""
        if not self.is_configured():
            raise ValueError("APPS_SCRIPT_WEBHOOK_URL is not configured in .env")

        if self.secret:
            payload["secret"] = self.secret

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data_bytes,
            headers={"Content-Type": "application/json", "User-Agent": "JAI-CloudRunner/1.0"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)

    def ping(self) -> bool:
        """Health-check the Apps Script webhook."""
        if not self.is_configured():
            return False
        try:
            res = self._call_webhook({"action": "ping"}, timeout=10.0)
            return res.get("status") == "success"
        except Exception as e:
            logger.debug(f"Apps Script ping failed: {e}")
            return False

    def create_google_doc(
        self,
        title: str,
        blocks: Optional[List[Dict[str, str]]] = None,
        text_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Google Doc on Google Drive via Apps Script cloud runner."""
        if not self.is_configured():
            # Graceful local fallback via python-docx
            return self._create_local_docx_fallback(title, blocks, text_content)

        payload: Dict[str, Any] = {
            "action": "create_doc",
            "title": title,
            "blocks": blocks or [],
        }
        if text_content:
            payload["text"] = text_content

        try:
            res = self._call_webhook(payload)
            if res.get("status") == "success":
                logger.info(f"Created cloud Google Doc: {res.get('url')}")
            return res
        except Exception as exc:
            logger.warning(f"Cloud doc generation failed ({exc}), falling back to local docx.")
            return self._create_local_docx_fallback(title, blocks, text_content)

    def append_to_sheet(
        self,
        rows: List[List[Any]],
        sheet_name: str = "JAI_Data",
        spreadsheet_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append rows to a Google Sheet via Apps Script cloud runner."""
        if not self.is_configured():
            return {
                "status": "not_configured",
                "message": "Apps Script Webhook is not configured. Configure APPS_SCRIPT_WEBHOOK_URL to enable cloud sheets.",
            }

        payload: Dict[str, Any] = {
            "action": "append_sheet",
            "sheet_name": sheet_name,
            "rows": rows,
        }
        if spreadsheet_id:
            payload["spreadsheet_id"] = spreadsheet_id

        return self._call_webhook(payload)

    def _create_local_docx_fallback(
        self,
        title: str,
        blocks: Optional[List[Dict[str, str]]] = None,
        text_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fallback: create local .docx document using python-docx."""
        try:
            import docx  # type: ignore
            doc = docx.Document()
            doc.add_heading(title, level=0)

            if blocks:
                for b in blocks:
                    b_type = b.get("type", "paragraph").lower()
                    b_text = b.get("content", "")
                    if b_type in ("h1", "heading1"):
                        doc.add_heading(b_text, level=1)
                    elif b_type in ("h2", "heading2"):
                        doc.add_heading(b_text, level=2)
                    elif b_type == "bullet":
                        doc.add_paragraph(b_text, style="List Bullet")
                    else:
                        doc.add_paragraph(b_text)
            elif text_content:
                for para in text_content.split("\n\n"):
                    if para.strip():
                        doc.add_paragraph(para.strip())

            out_dir = Path.home() / "OneDrive" / "Documents"
            if not out_dir.is_dir():
                out_dir = Path.cwd() / "data" / "docs"
            out_dir.mkdir(parents=True, exist_ok=True)

            safe_name = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip() or "JAI_Document"
            file_path = out_dir / f"{safe_name}.docx"
            doc.save(str(file_path))

            return {
                "status": "success",
                "type": "local_fallback",
                "path": str(file_path),
                "title": title,
                "message": f"Saved locally to {file_path.name} (Configure APPS_SCRIPT_WEBHOOK_URL to generate directly in Google Drive).",
            }
        except Exception as e:
            return {"status": "error", "error": f"Local docx creation failed: {e}"}
