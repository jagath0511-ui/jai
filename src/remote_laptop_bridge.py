"""Remote Laptop Bridge for JAI.

Allows remote control of the laptop via Telegram, Kaggle Cloud Brain, or local CLI:
1. Homework & Exam Solving: Dispatches to Qwen 14B 4-agent pipeline (Parse -> Solve -> Verify -> DOM Selector).
2. Autonomous Git Repository Creation: Scaffolds directories, writes code, commits to Git, and pushes.
3. Full-Stack Web App Scaffolding & Deploy: Vite + React + Tailwind -> npm build verification -> Cloudflare Pages deploy.
4. Dynamic Endpoint Registration: Automatically updates KAGGLE_QWEN_ENDPOINT upon notebook boot.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Union

# Ensure JAI root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    import qwen_agent_council as qwen_council
except ImportError:
    from src import qwen_agent_council as qwen_council

try:
    import git_repo_agent
except ImportError:
    from src import git_repo_agent

logger = logging.getLogger("jai.remote_bridge")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [RemoteBridge] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class RemoteLaptopBridge:
    """Coordinates remote execution requests between Telegram/Cloud and local laptop OS."""

    def __init__(self, workspace_root: Optional[Union[str, Path]] = None):
        self.workspace_root = Path(workspace_root or BASE_DIR).resolve()
        self.repos_root = self.workspace_root / "workspace_repos"
        self.repos_root.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # 1. Homework / Exam Solving
    # -------------------------------------------------------------------------
    def solve_question(self, raw_input: str, html_context: Optional[str] = None) -> Dict[str, Any]:
        """Run 4-agent Qwen council on a homework/exam question and return verified result."""
        logger.info(f"Solving question remotely: {raw_input[:60]}...")
        result = qwen_council.solve_question_pipeline(raw_input, html_context=html_context)

        # Format clean markdown response for Telegram / UI
        opt_str = f" **(Option {result.verification.chosen_option})**" if result.verification.chosen_option else ""
        msg = (
            f"🎯 *Question Solved & Verified*{opt_str}\n\n"
            f"*Question:* {result.parsed.question_text[:200]}...\n\n"
            f"*Final Answer:* {result.verification.final_answer}\n\n"
            f"*Confidence:* `{result.verification.confidence:.0%}`\n"
            f"*Audit Notes:* {result.verification.audit_notes}\n"
            f"*Latency:* `{result.total_latency_ms:.0f}ms`"
        )
        if result.browser_action:
            msg += f"\n\n🖱️ *CDP DOM Target:* `{result.browser_action.css_selector}` ({result.browser_action.action_type})"

        return {
            "success": True,
            "message": msg,
            "result": as_dict(result),
        }

    # -------------------------------------------------------------------------
    # 2. Autonomous Git Repository Builder
    # -------------------------------------------------------------------------
    def build_git_repo(self, repo_name: str, specification: str) -> Dict[str, Any]:
        """Autonomously scaffold, code, and commit a new Git repository on the laptop."""
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_name).strip("_")
        logger.info(f"Building Git repository '{clean_name}': {specification[:60]}...")

        build_res = qwen_council.GitRepoEngineerAgent.plan_and_build_repo(
            repo_name=clean_name,
            specification=specification,
            target_dir=self.repos_root,
        )

        msg = (
            f"📦 *Git Repository Created Successfully!*\n\n"
            f"*Name:* `{clean_name}`\n"
            f"*Path:* `{build_res['repo_path']}`\n"
            f"*Files Created:* {', '.join(build_res['files_created'])}\n"
            f"*Git Status:* Initial commit created autonomously."
        )

        return {
            "success": True,
            "message": msg,
            "repo_path": build_res["repo_path"],
            "files": build_res["files_created"],
        }

    # -------------------------------------------------------------------------
    # 3. Full-Stack Web App Scaffolding & Cloudflare Deployment
    # -------------------------------------------------------------------------
    def build_and_deploy_web(
        self,
        app_name: str,
        deploy: bool = False,
    ) -> Dict[str, Any]:
        """Scaffold Vite+React+Tailwind, verify compilation, and optionally deploy to Cloudflare Pages."""
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "-", app_name.lower()).strip("-")
        logger.info(f"Scaffolding full-stack web application '{clean_name}'...")

        # 1. Scaffold
        scaffold_res = git_repo_agent.scaffold_vite_react_app(
            app_name=clean_name,
            target_dir=self.repos_root,
        )
        app_dir = Path(scaffold_res["app_dir"])

        msg = (
            f"🌐 *Web App Scaffolded Successfully!*\n\n"
            f"*Name:* `{clean_name}`\n"
            f"*Stack:* Vite + React 18 + TypeScript + Tailwind CSS\n"
            f"*Local Directory:* `{app_dir}`\n"
        )

        # 2. Deploy to Cloudflare if requested
        deploy_info = None
        if deploy:
            logger.info("Deploy requested. Running build verification and Cloudflare deployment...")
            build_res = git_repo_agent.verify_and_heal_build(app_dir)
            if build_res.get("success"):
                deploy_info = git_repo_agent.deploy_cloudflare_pages(app_dir, project_name=clean_name)
                msg += f"\n🚀 *Live Cloudflare URL:* {deploy_info.get('live_url')}"
            else:
                msg += f"\n⚠️ *Build Note:* {build_res.get('error', 'Build failed')}"

        return {
            "success": True,
            "message": msg,
            "app_dir": str(app_dir),
            "deploy_info": deploy_info,
        }

    # -------------------------------------------------------------------------
    # 4. Live Endpoint Dynamic Registration
    # -------------------------------------------------------------------------
    def update_qwen_endpoint(self, new_endpoint: str) -> Dict[str, Any]:
        """Dynamically update KAGGLE_QWEN_ENDPOINT in memory and persist to .env."""
        cleaned = new_endpoint.strip().rstrip("/")
        if not cleaned.endswith("/v1"):
            cleaned = f"{cleaned}/v1"

        os.environ["KAGGLE_QWEN_ENDPOINT"] = cleaned
        env_file = BASE_DIR / ".env"

        # Update or append in .env
        try:
            lines = []
            found = False
            if env_file.is_file():
                lines = env_file.read_text(encoding="utf-8").splitlines()
                for i, l in enumerate(lines):
                    if l.startswith("KAGGLE_QWEN_ENDPOINT="):
                        lines[i] = f"KAGGLE_QWEN_ENDPOINT={cleaned}"
                        found = True
                        break
            if not found:
                lines.append(f"KAGGLE_QWEN_ENDPOINT={cleaned}")

            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info(f"Updated KAGGLE_QWEN_ENDPOINT to: {cleaned}")

            return {
                "success": True,
                "message": f"✅ *Kaggle Qwen Endpoint Updated!*\n\n`{cleaned}`",
                "endpoint": cleaned,
            }
        except Exception as e:
            return {"success": False, "message": f"Failed to persist endpoint: {e}"}

    # -------------------------------------------------------------------------
    # 5. Laptop OS Remote Control & Telemetry (Phase 20)
    # -------------------------------------------------------------------------
    def get_laptop_status(self) -> Dict[str, Any]:
        """Query Win32 battery, RAM, active foreground window, and local clock."""
        from datetime import datetime
        now_str = datetime.now().strftime("%A, %B %d, %Y - %I:%M %p")

        power_str = "Unknown"
        ram_str = "Unknown"
        active_window = "Desktop / Taskbar"

        if sys.platform == "win32":
            import ctypes

            # 1. Battery / Power
            try:
                class SYSTEM_POWER_STATUS(ctypes.Structure):
                    _fields_ = [
                        ("ACLineStatus", ctypes.c_byte),
                        ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte),
                        ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong),
                    ]

                p_status = SYSTEM_POWER_STATUS()
                if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p_status)):
                    ac = "🔌 Plugged In (AC)" if p_status.ACLineStatus == 1 else "🔋 On Battery"
                    pct = p_status.BatteryLifePercent
                    power_str = f"{ac} ({pct}%)"
            except Exception as e:
                power_str = f"Error: {e}"

            # 2. RAM Usage
            try:
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                    total_gb = mem.ullTotalPhys / (1024**3)
                    avail_gb = mem.ullAvailPhys / (1024**3)
                    ram_str = f"{mem.dwMemoryLoad}% used ({avail_gb:.1f} GB free of {total_gb:.1f} GB)"
            except Exception as e:
                ram_str = f"Error: {e}"

            # 3. Active Foreground Window
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                if hwnd:
                    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                        active_window = buff.value or "Desktop / Taskbar"
            except Exception:
                pass

        msg = (
            f"💻 <b>Laptop Telemetry & Status:</b>\n\n"
            f"⚡ <b>Power:</b> {power_str}\n"
            f"🧠 <b>Memory:</b> {ram_str}\n"
            f"🪟 <b>Active Window:</b> <code>{active_window}</code>\n"
            f"🕒 <b>Local Time:</b> {now_str} (IST, UTC+05:30)\n"
            f"🤖 <b>JAI Core:</b> Online & Listening ($0 Running Cost)"
        )

        return {
            "success": True,
            "message": msg,
            "power": power_str,
            "ram": ram_str,
            "active_window": active_window,
            "time": now_str,
        }

    def take_screenshot(self, chat_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Capture active desktop screenshot and send photo to Telegram if chat_id provided."""
        try:
            from screen_watcher import ScreenWatcher
            sw = ScreenWatcher()
            shot_path = sw.grab_screen()

            # Attempt sending directly to Telegram chat if requested
            if chat_id:
                try:
                    import telegram_bridge
                    if hasattr(telegram_bridge, "send_photo_direct"):
                        telegram_bridge.send_photo_direct(
                            chat_id=chat_id,
                            photo_path=shot_path,
                            caption="📸 <b>Live Laptop Screen Capture</b>",
                            parse_mode="HTML",
                        )
                except Exception as exc:
                    logger.warning(f"Could not send photo directly to Telegram: {exc}")

            msg = (
                f"📸 <b>Screenshot Captured Successfully!</b>\n\n"
                f"📁 <b>Saved At:</b> <code>{shot_path}</code>\n"
                f"📊 <b>Size:</b> {shot_path.stat().st_size / 1024:.1f} KB"
            )
            return {"success": True, "message": msg, "path": str(shot_path)}
        except Exception as e:
            return {"success": False, "message": f"Failed to capture screenshot: {e}"}

    def open_target(self, target: str) -> Dict[str, Any]:
        """Open a website URL or launch a desktop application via physical laptop actuator."""
        cleaned = target.strip()
        if not cleaned:
            return {"success": False, "message": "Please specify a target to open, e.g. <code>/laptop open youtube</code> or <code>/laptop open notepad</code>"}

        try:
            from cloud_actuator_bridge import cloud_actuator_bridge
            goal = f"open {cleaned}" if not cleaned.lower().startswith("open ") else cleaned
            res = cloud_actuator_bridge.handle_natural_language_goal(goal)
            return {
                "success": res.get("failed_steps", 0) == 0,
                "message": f"🚀 <b>Laptop Actuator Executed:</b> <code>{cleaned}</code>\n\nLaunched on your laptop screen.",
                "details": res,
            }
        except Exception as e:
            return {"success": False, "message": f"Could not open target '{cleaned}': {e}"}

    def run_shell(self, command_line: str, timeout: int = 15) -> Dict[str, Any]:
        """Execute a shell command on the laptop and return stdout/stderr."""
        cleaned = command_line.strip()
        if not cleaned:
            return {"success": False, "message": "Please specify a command to run, e.g. <code>/laptop run ipconfig</code>"}

        import subprocess
        try:
            res = subprocess.run(
                cleaned,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            out = (res.stdout or "").strip()
            err = (res.stderr or "").strip()

            combined = out or err or "Command executed with no output."
            if len(combined) > 3000:
                combined = combined[:3000] + "\n... [Truncated for Telegram]"

            return {
                "success": res.returncode == 0,
                "message": f"🖥️ <b>Command Output:</b> <code>{cleaned}</code>\n\n<pre>{combined}</pre>",
                "stdout": out,
                "stderr": err,
                "returncode": res.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "message": f"⏱️ Command timed out after {timeout} seconds."}
        except Exception as e:
            return {"success": False, "message": f"Execution error: {e}"}

    def lock_workstation(self) -> Dict[str, Any]:
        """Lock the Windows workstation screen."""
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return {"success": True, "message": "🔒 Windows workstation screen has been locked."}
        return {"success": False, "message": "Lock is only supported on Windows."}

    def control_volume(self, action: str) -> Dict[str, Any]:
        """Adjust or mute Windows master volume."""
        if sys.platform != "win32":
            return {"success": False, "message": "Volume control only supported on Windows."}

        import ctypes
        user32 = ctypes.windll.user32
        VK_VOLUME_MUTE = 0xAD
        VK_VOLUME_DOWN = 0xAE
        VK_VOLUME_UP = 0xAF
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002

        def press(vk):
            user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

        act = action.lower().strip()
        if "mute" in act:
            press(VK_VOLUME_MUTE)
            return {"success": True, "message": "🔇 Volume mute toggled."}
        elif "up" in act or "raise" in act:
            for _ in range(5):
                press(VK_VOLUME_UP)
            return {"success": True, "message": "🔊 Volume raised (+10%)."}
        elif "down" in act or "lower" in act:
            for _ in range(5):
                press(VK_VOLUME_DOWN)
            return {"success": True, "message": "🔉 Volume lowered (-10%)."}
        else:
            return {"success": False, "message": "Usage: /laptop volume <up|down|mute>"}

    def run_assignment_solver(self, url_or_path: Optional[str] = None, chat_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Launch the visible Autonomous Assignment Solver in Chrome."""
        import threading
        target = (url_or_path or "").strip()

        def _bg_solve():
            try:
                from src.autonomous_assignment_solver import AutonomousAssignmentSolver
                solver = AutonomousAssignmentSolver()
                result = solver.solve_file_or_url(target) if target else solver.solve_assignment_in_chrome()
                
                if chat_id:
                    import telegram_bridge
                    ans_count = result.get("total_answered", 0)
                    msg = (
                        f"🎓 <b>Assignment Solver Finished!</b>\n\n"
                        f"✅ <b>Questions Answered:</b> {ans_count}\n"
                        f"📝 <b>Status:</b> Ready for review"
                    )
                    telegram_bridge.send_message_direct(chat_id=chat_id, text=msg, parse_mode="HTML")
            except Exception as e:
                if chat_id:
                    import telegram_bridge
                    telegram_bridge.send_message_direct(chat_id=chat_id, text=f"❌ Assignment solver failed: {e}")

        threading.Thread(target=_bg_solve, daemon=True).start()
        return {
            "success": True,
            "message": "🎓 <b>[Autonomous Assignment Solver] Launched!</b>\n\nOpening visible Chrome browser on your laptop, inspecting questions with live Chain-of-Thought reasoning, and filling answers. I will notify you once complete.",
        }

    def run_orchestrator(self, goal: str, chat_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Dispatch a goal to the Conductor multi-agent swarm."""
        cleaned = goal.strip()
        if not cleaned:
            return {"success": False, "message": "Please specify a goal, e.g. <code>/laptop orchestrate Research quantum computing trends</code>"}

        import threading

        def _bg_orchestrate():
            try:
                from src.conductor import Conductor
                cond = Conductor()
                summary = cond.orchestrate(cleaned)
                if chat_id:
                    import telegram_bridge
                    msg = f"🐝 <b>[Multi-Agent Swarm] Goal Complete!</b>\n\n{summary}"
                    telegram_bridge.send_message_direct(chat_id=chat_id, text=msg, parse_mode="HTML")
            except Exception as e:
                if chat_id:
                    import telegram_bridge
                    telegram_bridge.send_message_direct(chat_id=chat_id, text=f"❌ Swarm orchestration error: {e}")

        threading.Thread(target=_bg_orchestrate, daemon=True).start()
        return {
            "success": True,
            "message": f"🐝 <b>[Multi-Agent Swarm] Initiated!</b>\n\n🎯 <b>Goal:</b> \"{cleaned}\"\nPlanning DAG and dispatching specialized agents in parallel on your laptop. I will notify you with the final results.",
        }

    def handle_laptop_command(self, args: str, chat_id: Optional[Union[str, int]] = None) -> str:
        """Dispatcher for /laptop <command> received via Telegram or CLI."""
        raw = (args or "").strip()
        if not raw or raw.lower() in ("help", "?"):
            return (
                "💻 <b>JAI Laptop Remote Control:</b>\n\n"
                "• <code>/laptop status</code> - Battery, RAM, active window & time\n"
                "• <code>/laptop screenshot</code> - Capture & send screen\n"
                "• <code>/laptop open &lt;app/url&gt;</code> - Open app or website\n"
                "• <code>/laptop run &lt;cmd&gt;</code> - Execute shell command\n"
                "• <code>/laptop assignment [file]</code> - Run browser assignment solver\n"
                "• <code>/laptop orchestrate &lt;goal&gt;</code> - Run multi-agent swarm\n"
                "• <code>/laptop volume &lt;up|down|mute&gt;</code> - Control sound\n"
                "• <code>/laptop lock</code> - Lock Windows screen\n"
                "• <code>/laptop &lt;any instruction&gt;</code> - Natural language control"
            )

        parts = raw.split(maxsplit=1)
        subcmd = parts[0].lower()
        subarg = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "status":
            return self.get_laptop_status()["message"]
        elif subcmd in ("screenshot", "screen", "shot"):
            return self.take_screenshot(chat_id=chat_id)["message"]
        elif subcmd in ("open", "launch", "start"):
            return self.open_target(subarg)["message"]
        elif subcmd in ("run", "exec", "cmd", "sh"):
            return self.run_shell(subarg)["message"]
        elif subcmd == "lock":
            return self.lock_workstation()["message"]
        elif subcmd in ("volume", "vol", "sound"):
            return self.control_volume(subarg)["message"]
        elif subcmd in ("assignment", "solve", "exam", "homework"):
            return self.run_assignment_solver(url_or_path=subarg, chat_id=chat_id)["message"]
        elif subcmd in ("orchestrate", "swarm", "dag"):
            return self.run_orchestrator(goal=subarg, chat_id=chat_id)["message"]
        else:
            # Natural language command handling
            low = raw.lower()
            if "battery" in low or "ram" in low or "status" in low or "active window" in low:
                return self.get_laptop_status()["message"]
            elif "screenshot" in low or "take a picture of screen" in low:
                return self.take_screenshot(chat_id=chat_id)["message"]
            elif low.startswith("open ") or low.startswith("launch "):
                target = raw.split(maxsplit=1)[1] if len(raw.split()) > 1 else ""
                return self.open_target(target)["message"]
            elif "lock my laptop" in low or "lock screen" in low:
                return self.lock_workstation()["message"]
            elif "assignment" in low or "solve quiz" in low:
                return self.run_assignment_solver(chat_id=chat_id)["message"]
            else:
                # Fallback to shell or smart resolution
                res = self.run_shell(raw)
                if res.get("success"):
                    return res["message"]
                return f"💻 Interpreted instruction: \"{raw}\"\n\n{res.get('message')}"


def as_dict(obj: Any) -> Any:
    """Helper to serialize dataclasses to JSON-compatible dict."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: as_dict(getattr(obj, k)) for k in obj.__dataclass_fields__}
    elif isinstance(obj, list):
        return [as_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    return obj

