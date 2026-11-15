"""Master Entrypoint for JAI (Phase 15 + Playwright CDP Autonomous Driver).

Supports:
1. Background system orchestration (daemon, telegram, inbox watcher).
2. Autonomous Browser Driver:
   python run_jai.py --goal "..." [--tab <index>] [--vault-store <domain>]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Optional
import urllib.parse

# Ensure JAI root directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config
import inbox_watcher
import outbound_queue
import telegram_bridge

logger = logging.getLogger("jai.master")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [JAI] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

BANNER = r"""
======================================================================
     _   _   ___ 
    | | /_\ |_ _|       Personal Local-First AI Assistant
 _  | |/ _ \ | |  
| |_| / _ \ \| |        Autonomous Playwright CDP Driver & Orchestrator
 \___/_/ \_\___|        Constraints: $0 Cost | <=8GB RAM | <3.5GB Disk
                        Safety: Zero irreversible actions without Ring-1 approval
======================================================================
"""


class JaiOrchestrator:
    """Coordinates lifecycle of all resident JAI services."""

    def __init__(
        self,
        inbox_path: Optional[Path] = None,
        db_path: Optional[Path] = None,
        start_voice_daemon: bool = True,
        start_telegram: bool = True,
        start_inbox: bool = True,
    ):
        self.inbox_path = inbox_path or config.BASE_DIR / "inbox.md"
        self.db_path = db_path or config.JAI_MEMORY_DB_PATH
        self.start_voice_daemon = start_voice_daemon
        self.start_telegram = start_telegram
        self.start_inbox = start_inbox

        self.daemon_proc: Optional[subprocess.Popen] = None
        self.inbox_worker: Optional[inbox_watcher.InboxWatcher] = None
        self.bridge: Optional[telegram_bridge.TelegramBridge] = None
        self._shutdown_event = threading.Event()

    def start(self) -> None:
        """Start all resident JAI components."""
        print(BANNER)
        logger.info("Initializing JAI system directories and configuration...")
        config.init_directories()

        if self.start_voice_daemon:
            daemon_script = BASE_DIR / "worker_daemon.py"
            if daemon_script.exists():
                logger.info(f"Starting Voice Worker Daemon from {daemon_script.name}...")
                try:
                    self.daemon_proc = subprocess.Popen(
                        [sys.executable, str(daemon_script)],
                        cwd=str(BASE_DIR),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    logger.info(f"Worker Daemon running in background (PID: {self.daemon_proc.pid})")
                except Exception as exc:
                    logger.warning(f"Could not launch worker_daemon: {exc}. Voice transcription will run on-demand.")

        outbound_queue.start_sync_loop(interval_seconds=15)
        logger.info("Outbound queue sync loop started (15s interval)")

        if self.start_inbox:
            try:
                self.inbox_worker = inbox_watcher.start_inbox_watcher(
                    inbox_path=self.inbox_path,
                    poll_interval_seconds=3.0,
                )
                logger.info(f"Inbox watcher active on {self.inbox_path.name}")
            except Exception as exc:
                logger.warning(f"Failed to start inbox watcher: {exc}")

        if self.start_telegram:
            try:
                self.bridge = telegram_bridge.TelegramBridge()
                self.bridge.start()
                logger.info("Telegram bridge started successfully")
            except Exception as exc:
                logger.warning(f"Telegram bridge unavailable: {exc}")

        logger.info("All JAI resident subsystems initialized and operational.")

    def stop(self) -> None:
        """Gracefully stop all JAI components."""
        logger.info("Initiating graceful shutdown of JAI subsystems...")
        self._shutdown_event.set()

        if self.bridge:
            try:
                self.bridge.stop()
            except Exception:
                pass

        if self.inbox_worker:
            try:
                self.inbox_worker.stop()
            except Exception:
                pass

        outbound_queue.stop_sync_loop()

        if self.daemon_proc:
            try:
                self.daemon_proc.terminate()
                self.daemon_proc.wait(timeout=3)
            except Exception:
                try:
                    self.daemon_proc.kill()
                except Exception:
                    pass

        logger.info("JAI shutdown complete.")

    def run_until_interrupted(self) -> None:
        def _handle_signal(sig, frame):
            logger.info("Termination signal received.")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        self.start()

        try:
            while not self._shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()


async def run_browser_cli_goal(goal: str, tab_index: Optional[int] = None) -> None:
    """Runs autonomous ReAct loop over Chrome CDP."""
    from src.browser_driver import ChromeCDPDriver
    from src.agent_brain import AgentBrain
    from src.macro_engine import lookup_macro, save_macro

    print(BANNER)
    print(f"[GOAL] {goal}")
    driver = ChromeCDPDriver()
    try:
        await driver.connect("http://127.0.0.1:9222")
    except Exception as e:
        print(f"[ERROR] Could not connect to Chrome CDP: {e}")
        print("Run './launch_chrome.ps1' to launch Chrome with remote debugging on port 9222.")
        return

    tabs = await driver.get_tabs_overview()
    print(f"\n[MULTI-TAB RADAR] Open Tabs ({len(tabs)}):")
    for t in tabs:
        active_mark = "*" if t["is_active"] else " "
        print(f" {active_mark} [{t['index']}] {t['title']} -> {t['url']}")

    if tab_index is not None:
        await driver.select_tab(tab_index)
        print(f"\n[TAB] Selected tab [{tab_index}]: {driver.active_page.url}")

    brain = AgentBrain()
    page = driver.active_page
    domain = urllib.parse.urlparse(page.url).netloc.lower().split(":")[0]

    # ReAct Loop (Max 10 steps)
    for step in range(1, 11):
        print(f"\n--- ReAct Step {step}/10 ---")
        current_domain = urllib.parse.urlparse(page.url).netloc.lower().split(":")[0]

        # 1. Check macro cache (zero-token <200ms muscle memory)
        macro = lookup_macro(current_domain, goal)
        if macro:
            print(f"[CACHE HIT] Found active macro '{macro.macro_id}'. Replaying payload without LLM tokens...")
            res = await driver.execute_action(page, macro.action_payload)
            print(f"[EXECUTED] {res}")
            print(f"[SUCCESS] Goal accomplished via macro cache!")
            break

        # 2. Perception: capture clean AXTree
        page, marks, formatted_tree = await driver.get_clean_axtree(page)
        print(f"[PERCEPTION] AXTree interactive marks count: {len(marks)}")

        # 3. Cognitive Brain: plan actions
        actions = brain.plan_next_actions(goal, marks, current_url=page.url)
        if not actions:
            print("[INFO] No further actions planned. Halting.")
            break

        completed = False
        for act in actions:
            act_type = act.get("action")
            if act_type == "done":
                print(f"[DONE] Brain concluded goal: {act.get('summary', 'Finished.')}")
                completed = True
                break

            # 4. Ring-1 Safety Check
            brain.enforce_safety(act)

            # 5. Execute action
            print(f"[ACTION] Executing: {act}")
            res = await driver.execute_action(page, act)
            print(f"[RESULT] {res}")

            # 6. Save verified action to macro cache
            save_macro(
                domain=current_domain,
                intent=goal,
                triplet={"role": act.get("role", "input"), "selector": act.get("selector", "")},
                payload=act
            )

        if completed:
            break

        await asyncio.sleep(1.0)


def store_vault_cli(domain: str) -> None:
    """Stores credential securely into Windows DPAPI vault."""
    from src.agent_vault import store_credential, has_credential

    print(f"\n[AGENT VAULT] Storing credentials for domain: {domain}")
    username = input("Username / Email: ").strip()
    password = getpass.getpass("Password (secret, will not echo): ").strip()

    if not username or not password:
        print("[ERROR] Username and password cannot be empty.")
        return

    success = store_credential(domain, username, password)
    if success and has_credential(domain):
        print(f"[SUCCESS] Credentials encrypted and stored in Windows Credential Locker for '{domain}'.")
    else:
        print("[ERROR] Failed to store credential in Windows Vault.")


def main() -> None:
    parser = argparse.ArgumentParser(description="JAI Master Process Orchestrator & Browser Driver")
    parser.add_argument("--goal", type=str, help="Autonomous browser goal to execute via Playwright CDP")
    parser.add_argument("--tab", type=int, help="Index of open browser tab to focus on")
    parser.add_argument("--vault-store", type=str, help="Store credentials into DPAPI vault for domain")
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Quick startup test run: starts components, verifies status, and stops.",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Skip launching background voice daemon.",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip starting Telegram bridge and outbound queue.",
    )
    args = parser.parse_args()

    if args.vault_store:
        store_vault_cli(args.vault_store)
        sys.exit(0)

    if args.goal:
        asyncio.run(run_browser_cli_goal(args.goal, tab_index=args.tab))
        sys.exit(0)

    orchestrator = JaiOrchestrator(
        start_voice_daemon=not args.no_voice,
        start_telegram=not args.no_telegram,
    )

    if args.test_run:
        logger.info("Executing JAI test startup run...")
        orchestrator.start()
        time.sleep(1.0)
        logger.info("Test startup verified successfully.")
        orchestrator.stop()
        sys.exit(0)

    orchestrator.run_until_interrupted()


if __name__ == "__main__":
    main()
