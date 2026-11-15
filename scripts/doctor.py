#!/usr/bin/env python3
"""scripts/doctor.py - Ultra-fast repository preflight health check.

Uses Python standard library and ctypes only (zero heavy imports).
Runs in under 2 seconds to diagnose developer & agent setup health.
"""

from __future__ import annotations

import ctypes
import glob
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent


def check_python_version() -> Tuple[str, str, Optional[str]]:
    """Check Python version compatibility."""
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"

    if ver < (3, 10):
        return (
            "FAIL",
            f"Python {ver_str} detected (Python >= 3.10 is strictly required).",
            "Install Python 3.10-3.12 (e.g. winget install Python.Python.3.11 or brew install python@3.11)",
        )
    if ver >= (3, 15):
        return (
            "WARN",
            f"Python {ver_str} detected (experimental; optional benchmark wheels may lack pre-built binaries).",
            "For running official benchmarks (BrowserGym/MiniWoB), Python 3.10-3.12 virtualenv is recommended.",
        )
    return (
        "PASS",
        f"Python {ver_str} detected (fully compatible).",
        None,
    )


def check_c_sandbox() -> Tuple[str, str, Optional[str]]:
    """Check C compiler presence and verify native C sandbox binary loading."""
    is_win = sys.platform == "win32"

    # 1. Check for compiler
    compilers = ["gcc", "clang", "cl", "zig"]
    found_compiler: Optional[str] = None
    for c in compilers:
        if shutil.which(c):
            found_compiler = c
            break

    if is_win and not found_compiler:
        win_compiler_paths = [
            r"C:\msys64\ucrt64\bin\gcc.exe",
            r"C:\msys64\mingw64\bin\gcc.exe",
            r"C:\Program Files\LLVM\bin\clang.exe",
            r"C:\Program Files (x86)\Dev-Cpp\MinGW64\bin\gcc.exe",
        ]
        for p in win_compiler_paths:
            if os.path.isfile(p):
                found_compiler = p
                break

    # 2. Check for compiled sandbox library
    candidates = []
    if is_win:
        candidates = [
            _REPO_ROOT / "src" / "security" / "jai_sandbox.dll",
            _REPO_ROOT / "native" / "jai_sandbox.dll",
        ]
    else:
        candidates = [
            _REPO_ROOT / "src" / "security" / "libjai_sandbox.so",
            _REPO_ROOT / "native" / "libjai_sandbox.so",
        ]

    valid_lib_path: Optional[Path] = None
    lib_error: Optional[str] = None

    for cand in candidates:
        if cand.is_file():
            try:
                loaded = ctypes.CDLL(str(cand))
                # Verify export existence
                spawn_fn = getattr(loaded, "jai_spawn_sandboxed", None)
                term_fn = getattr(loaded, "jai_terminate_sandbox", None)
                if spawn_fn is not None and term_fn is not None:
                    valid_lib_path = cand
                    break
            except Exception as e:
                lib_error = str(e)

    if valid_lib_path:
        compiler_note = f" (Compiler '{found_compiler}' ready)" if found_compiler else ""
        return (
            "PASS",
            f"Native sandbox library verified & loaded ({valid_lib_path.name}){compiler_note}.",
            None,
        )

    if found_compiler:
        build_cmd = ".\\native\\build.ps1" if is_win else "make build"
        return (
            "WARN",
            f"Native sandbox binary missing or unverified, but compiler '{found_compiler}' is available.",
            f"Build native sandbox: {build_cmd}",
        )

    build_fix = ".\\native\\build.ps1" if is_win else "make build"
    return (
        "FAIL",
        f"Native C sandbox library missing and no C compiler detected ({lib_error or 'binary not found'}).",
        f"Install MinGW/Clang and run {build_fix}",
    )


def check_browsers() -> Tuple[str, str, Optional[str]]:
    """Check Playwright package and local browser availability across Tier 1, Tier 2, and Tier 3."""
    is_win = sys.platform == "win32"
    detected: List[str] = []

    # Check if playwright Python library is installed
    import importlib.util
    playwright_installed = importlib.util.find_spec("playwright") is not None

    # Tier 1: Google Chrome
    if is_win:
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        if any(os.path.isfile(p) for p in chrome_paths):
            detected.append("Chrome (Tier 1)")
    else:
        for p in ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
            if os.path.isfile(p) or shutil.which("google-chrome"):
                detected.append("Chrome (Tier 1)")
                break

    # Tier 2: Microsoft Edge
    if is_win:
        edge_paths = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
        if any(os.path.isfile(p) for p in edge_paths):
            detected.append("Edge (Tier 2)")
    else:
        for p in ["/usr/bin/microsoft-edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]:
            if os.path.isfile(p) or shutil.which("microsoft-edge"):
                detected.append("Edge (Tier 2)")
                break

    # Tier 3: Playwright Chromium Cache
    playwright_found = False
    if is_win:
        pw_pattern = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win64\chrome.exe")
        pw_pattern_alt = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win\chrome.exe")
        if glob.glob(pw_pattern) or glob.glob(pw_pattern_alt):
            playwright_found = True
    else:
        pw_linux = os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")
        pw_mac = os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app")
        if glob.glob(pw_linux) or glob.glob(pw_mac):
            playwright_found = True

    if playwright_found:
        detected.append("Playwright Chromium (Tier 3)")

    if not playwright_installed:
        return (
            "FAIL",
            "Playwright python package is not installed.",
            "Install Playwright: pip install playwright && python -m playwright install chromium",
        )

    if detected:
        return (
            "PASS",
            f"Playwright installed; browsers ready: {', '.join(detected)}.",
            None,
        )

    return (
        "WARN",
        "Playwright installed, but no local Chrome/Edge or Playwright Chromium cache detected.",
        "Install Playwright browser: python -m playwright install chromium",
    )


def check_environment_and_keys() -> Tuple[str, str, Optional[str]]:
    """Check .env file presence and API key credentials ($0 free tier)."""
    env_file = _REPO_ROOT / ".env"
    env_keys: Dict[str, str] = {}

    if env_file.is_file():
        try:
            content = env_file.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_keys[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass

    gemini_key = os.environ.get("GEMINI_API_KEY") or env_keys.get("GEMINI_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY") or env_keys.get("GROQ_API_KEY", "")

    configured_providers = []
    if gemini_key:
        configured_providers.append("Gemini Flash")
    if groq_key:
        configured_providers.append("Groq Qwen")

    if configured_providers:
        env_note = f" (via {env_file.name})" if env_file.is_file() else " (via environment)"
        return (
            "PASS",
            f"Planning API key configured for {', '.join(configured_providers)}{env_note}.",
            None,
        )

    if env_file.is_file():
        return (
            "WARN",
            ".env exists but GEMINI_API_KEY / GROQ_API_KEY is unset.",
            "Add GEMINI_API_KEY=your_key to .env (Free key at https://aistudio.google.com/app/apikey)",
        )

    return (
        "WARN",
        ".env file not found and no API key in environment.",
        "Create .env with GEMINI_API_KEY=your_key (Free key at https://aistudio.google.com/app/apikey)",
    )


def main() -> int:
    """Run all preflight checks and format status checklist."""
    t0 = time.time()
    print("\n" + "=" * 78)
    print(" 🩺 JAI Preflight Doctor - Developer & Agent Setup Health")
    print("=" * 78)
    print(f" Repository Root: {_REPO_ROOT}\n")

    checks = [
        ("Python Runtime", check_python_version),
        ("C Sandbox Binary", check_c_sandbox),
        ("Browser 3-Tier", check_browsers),
        ("Planning Keys", check_environment_and_keys),
    ]

    pass_count = 0
    warn_count = 0
    fail_count = 0
    fixes: List[Tuple[str, str]] = []

    print(f" {'STATUS':<8} {'CATEGORY':<18} {'DETAILS'}")
    print(" " + "-" * 76)

    for category, check_fn in checks:
        status, details, fix = check_fn()
        if status == "PASS":
            pass_count += 1
            tag = "[PASS]"
        elif status == "WARN":
            warn_count += 1
            tag = "[WARN]"
            if fix:
                fixes.append((category, fix))
        else:
            fail_count += 1
            tag = "[FAIL]"
            if fix:
                fixes.append((category, fix))

        print(f" {tag:<8} {category:<18} {details}")

    print(" " + "-" * 76)
    elapsed = time.time() - t0
    print(f" Summary: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL in {elapsed:.3f}s")

    if fixes:
        print("\n Recommended Fixes:")
        for cat, fix in fixes:
            print(f"  • {cat}: {fix}")

    if fail_count > 0:
        print("\n ❌ One or more required components failed. Please resolve above issues.")
        print("=" * 78 + "\n")
        return 1

    print("\n ✅ Repository is verified and ready for development!")
    print("=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
