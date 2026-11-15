"""src/browser/dispatch.py - Dynamic dispatch helper for test monkeypatch compatibility."""
from __future__ import annotations

import sys
from typing import Any

def get_jb_attr(name: str, fallback: Any) -> Any:
    """Return attribute from src.jai_browser if present and different from facade fallback, else fallback."""
    jb = sys.modules.get("src.jai_browser")
    if jb is not None and hasattr(jb, name):
        val = getattr(jb, name)
        if val is not None:
            return val
    return fallback
