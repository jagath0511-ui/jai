"""src/cli.py - JAI Runtime Interactive Command Line Interface.

Provides zero-setup first-run onboarding, 3-tier browser diagnostic checks,
benchmark execution, and autonomous web browsing.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
import time
from typing import Optional

# Setup root path resolution
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

logger = logging.getLogger("jai.cli")


def prompt_first_run_api_key() -> Optional[str]:
    """Interactive first-run onboarding if no API key is detected."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    if gemini_key or groq_key:
        return gemini_key or groq_key

    # Only prompt if running in an interactive terminal
    if not sys.stdin.isatty():
        return None

    print("\n" + "=" * 65)
    print(" 🚀 Welcome to Project JAI Browser (Zero-Setup Onboarding)")
    print("=" * 65)
    print("No GEMINI_API_KEY or GROQ_API_KEY was found in your environment.")
    print("JAI uses the free tier to reason and plan web actions with $0 budget.")
    print("👉 Get a free Gemini key in 10 seconds: https://aistudio.google.com/app/apikey\n")

    try:
        user_key = input("Enter your GEMINI_API_KEY (or press Enter to run offline mock): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSkipped API key entry.")
        return None

    if user_key:
        env_file = _REPO_ROOT / ".env"
        try:
            # Append or create .env file
            existing_content = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
            if "GEMINI_API_KEY=" in existing_content:
                lines = [
                    f"GEMINI_API_KEY={user_key}" if l.startswith("GEMINI_API_KEY=") else l
                    for l in existing_content.splitlines()
                ]
                env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                prefix = "\n" if existing_content and not existing_content.endswith("\n") else ""
                with open(env_file, "a", encoding="utf-8") as f:
                    f.write(f"{prefix}GEMINI_API_KEY={user_key}\n")

            os.environ["GEMINI_API_KEY"] = user_key
            print(f"✅ Saved GEMINI_API_KEY to {env_file.name}!")
            print("=" * 65 + "\n")
            return user_key
        except Exception as e:
            print(f"⚠️ Could not write to .env ({e}); using key for current session.")
            os.environ["GEMINI_API_KEY"] = user_key
            return user_key

    print("ℹ️ No key entered. Running in offline mock mode.")
    print("=" * 65 + "\n")
    return None


def run_diagnostic_check() -> int:
    """Run an environment and dependency sanity check."""
    print("\n" + "=" * 65)
    print(" 🔍 JAI Runtime System Diagnostic")
    print("=" * 65)

    # 1. Browser 3-Tier Check
    from src.browser.cdp import find_chrome_executable, find_edge_executable
    chrome_bin = find_chrome_executable()
    edge_bin = find_edge_executable() if sys.platform == "win32" else None

    print("\n[1] 3-Tier Browser Availability:")
    if chrome_bin:
        print(f"  • Tier 1 (Google Chrome):    ✅ Found at {chrome_bin}")
    else:
        print(f"  • Tier 1 (Google Chrome):    ❌ Not detected")

    if edge_bin:
        print(f"  • Tier 2 (Microsoft Edge):   ✅ Found at {edge_bin}")
    else:
        print(f"  • Tier 2 (Microsoft Edge):   ❌ Not detected")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw_path = pw.chromium.executable_path
            print(f"  • Tier 3 (Playwright Core):  ✅ Ready ({pw_path})")
    except Exception as e:
        print(f"  • Tier 3 (Playwright Core):  ⚠️ Playwright check note: {e}")

    # 2. Native C Sandbox Check
    from src.security.os_sandbox import _LOADED_LIB_PATH, _C_LIB
    print("\n[2] Native C Sandbox Isolation:")
    if _LOADED_LIB_PATH and _C_LIB:
        print(f"  • Pre-compiled C Library:    ✅ Active ({_LOADED_LIB_PATH})")
    else:
        print(f"  • Pre-compiled C Library:    ⚠️ Falling back to direct kernel ctypes")

    # 3. LLM Credentials Check
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    print("\n[3] LLM Planning Credentials ($0 Budget):")
    if gemini_key:
        masked = gemini_key[:4] + "..." + gemini_key[-4:] if len(gemini_key) > 8 else "***"
        print(f"  • GEMINI_API_KEY:            ✅ Configured ({masked})")
    else:
        print(f"  • GEMINI_API_KEY:            ❌ Not set")

    if groq_key:
        masked = groq_key[:4] + "..." + groq_key[-4:] if len(groq_key) > 8 else "***"
        print(f"  • GROQ_API_KEY:              ✅ Configured ({masked})")
    else:
        print(f"  • GROQ_API_KEY:              ❌ Not set")

    print("\n" + "=" * 65 + "\n")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for JAI."""
    parser = argparse.ArgumentParser(
        prog="jai",
        description="JAI Runtime - Autonomous, zero-setup browser agent with native security sandbox.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default="",
        help="Goal or browsing instruction to execute (e.g. 'Search Wikipedia for Quantum Computing').",
    )
    parser.add_argument(
        "--url",
        "-u",
        default="",
        help="Initial URL to navigate to before beginning task.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default: visible browser window).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run system diagnostic check for browsers, native C sandbox, and API keys.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in offline deterministic mock mode without invoking LLM APIs.",
    )
    parser.add_argument(
        "--benchmark",
        choices=["miniwob", "agentdojo"],
        help="Run reproducible benchmark suite.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=5,
        help="Maximum tasks to execute when running benchmarks (default: 5).",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version="jai-runtime 0.1.0",
        help="Show program version.",
    )

    args = parser.parse_args(argv)

    # 1. Diagnostic check command
    if args.check:
        return run_diagnostic_check()

    # 2. Benchmark suite execution
    if args.benchmark == "miniwob":
        from src.benchmarks.browsergym_adapter import run_browsergym_suite
        print(f"🚀 Running MiniWoB++ benchmark suite (max_tasks={args.max_tasks})...")
        summary = run_browsergym_suite(suite="miniwob", max_tasks=args.max_tasks)
        print(f"\n📊 Benchmark Complete: Success Rate = {summary.get('success_rate', 0.0) * 100:.1f}%")
        return 0

    # 3. Interactive first-run API key check if no mock requested
    task_str = args.task.strip()
    if not args.mock and not task_str and not args.url:
        if not sys.argv[1:]:
            prompt_first_run_api_key()
            if sys.stdin.isatty():
                try:
                    goal = input("🎯 Enter a browsing task (e.g. 'Search Wikipedia for artificial intelligence'): ").strip()
                    if goal:
                        task_str = goal
                    else:
                        print("No task entered. Run 'jai --help' for usage options.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    return 0
            else:
                parser.print_help()
                return 0

    if not args.mock:
        prompt_first_run_api_key()

    # 4. Standard task execution
    if not task_str and not args.url:
        print("Error: Please provide a task description or --url.")
        return 1

    print(f"\n⚡ [JAI] Initializing browser (headless={args.headless})...")
    from src.browser.cdp import ensure_browser_running, open_url, get_active_page
    from src.agent.loop import AgentLoop

    if not ensure_browser_running(headless=args.headless):
        print("❌ Error: Failed to initialize browser via 3-Tier resolver.")
        return 1

    if args.url:
        print(f"🌐 Navigating to {args.url}...")
        open_url(args.url)
        time.sleep(1.0)

    if not task_str and args.url:
        print(f"✅ Browser open at {args.url}.")
        return 0

    print(f"🎯 Goal: {task_str}\n")
    agent = AgentLoop(dry_run=args.mock)
    result = agent.run(task=task_str)

    print("\n" + "=" * 65)
    print(" 🏁 Execution Finished")
    print("=" * 65)
    status_str = "SUCCESS" if result.success else "STOPPED"
    print(f"Status:   {status_str}")
    print(f"Steps:    {result.steps_taken}")
    print(f"Duration: {result.total_wall_time_sec:.2f}s")
    if result.final_answer:
        print(f"\nReport / Result:\n{result.final_answer}")
    print("=" * 65 + "\n")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
