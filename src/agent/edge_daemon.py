"""src/agent/edge_daemon.py - Laptop Background Agent Daemon for Edge Task Processing.

Pulls tasks queued from mobile Telegram on Cloudflare D1:
1. Polls `GET /api/tasks/poll` with atomic lease locking.
2. Checks remote & local kill-switch before execution.
3. Generates plan and triggers "Plan Once, Approve Once" Telegram UI.
4. Executes approved workflows autonomously with budget caps.
5. Pushes final output to Telegram via `/api/tasks/complete`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.parse
import urllib.request

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from src.agent.loop import AgentLoop, AgentRunResult, KillSwitchTriggeredError, BudgetExceededError
    from src.agent.tools import get_default_registry
    from src.llm_router import get_default_router
except ImportError:
    from agent.loop import AgentLoop, AgentRunResult, KillSwitchTriggeredError, BudgetExceededError
    from agent.tools import get_default_registry
    from llm_router import get_default_router

try:
    from src.agent.planner import PlanCoordinator, ExecutionPlan
except ImportError:
    try:
        from agent.planner import PlanCoordinator, ExecutionPlan
    except ImportError:
        try:
            from archive.planner import PlanCoordinator, ExecutionPlan
        except ImportError:
            PlanCoordinator = Any  # type: ignore
            ExecutionPlan = Any  # type: ignore

logger = logging.getLogger("jai.agent.edge_daemon")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [EdgeDaemon] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class EdgeAgentDaemon:
    """Coordinates pull-based task processing from Cloudflare Worker queue."""

    def __init__(
        self,
        worker_url: Optional[str] = None,
        pool_secret: Optional[str] = None,
        poll_interval_sec: float = 25.0,
        stop_file: Optional[Path] = None,
    ):
        self.worker_url = (worker_url or os.getenv("JAI_WORKER_URL", "")).rstrip("/")
        self.pool_secret = pool_secret or os.getenv("POOL_SECRET", "")
        self.poll_interval_sec = poll_interval_sec
        self.stop_file = stop_file or (_REPO_ROOT / "data" / "STOP")
        self.coordinator = PlanCoordinator(worker_url=self.worker_url, pool_secret=self.pool_secret)
        self.loop = AgentLoop(stop_file=self.stop_file)

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make authenticated HTTP call to worker."""
        if not self.worker_url:
            return {"status": "unconfigured"}

        url = f"{self.worker_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "JAI-EdgeDaemon/1.0",
        }
        if self.pool_secret:
            headers["X-Pool-Secret"] = self.pool_secret

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def check_kill_switch(self) -> bool:
        """Check both local stop file and remote Cloudflare KV halt flag."""
        # 1. Local STOP file check (instant 0ms)
        if self.stop_file.exists():
            logger.warning(f"🛑 Local kill-switch active: {self.stop_file}")
            return True

        # 2. Remote Cloudflare KV check
        if self.worker_url and self.pool_secret:
            try:
                res = self._call("GET", "/api/kill_switch")
                if res.get("halt"):
                    logger.warning("🛑 Remote kill-switch active on Cloudflare KV.")
                    return True
            except Exception as e:
                logger.debug(f"Kill-switch check failed: {e}")

        return False

    def poll_once(self) -> Optional[Dict[str, Any]]:
        """Poll worker for pending agent task. Returns task dict or None."""
        if not self.worker_url or not self.pool_secret:
            logger.debug("Edge daemon running in offline mode (Worker URL or POOL_SECRET not set).")
            return None

        if self.check_kill_switch():
            logger.warning("Edge daemon idle: kill-switch is currently active.")
            return None

        try:
            res = self._call("GET", "/api/tasks/poll")
            if res.get("halt"):
                logger.warning("Worker reported active kill-switch.")
                return None
            return res.get("task")
        except Exception as e:
            logger.debug(f"Task poll probe failed: {e}")
            return None

    def complete_task(self, task_id: str, status: str, result: str, error: str = "") -> None:
        """Report task execution result back to worker."""
        if not self.worker_url:
            return

        body = {
            "id": task_id,
            "status": status,
            "result": result,
            "error": error,
        }
        try:
            self._call("POST", "/api/tasks/complete", body)
            logger.info(f"Delivered task completion status ({status}) for {task_id}.")
        except Exception as e:
            logger.error(f"Failed to report task completion to worker: {e}")

    def execute_task(self, task_data: Dict[str, Any]) -> None:
        """Execute a single pulled agent task."""
        task_id = str(task_data.get("id", ""))
        prompt = str(task_data.get("prompt", ""))
        logger.info(f"🎯 Acquired Task [{task_id}]: '{prompt}'")

        try:
            # 1. Generate execution plan
            logger.info("Decomposing task and generating execution plan...")
            plan = self.coordinator.generate_plan(prompt)

            # 2. Request upfront approval via Telegram
            logger.info(f"Submitting plan with {len(plan.steps)} steps for user approval...")
            approved = self.coordinator.request_approval(plan, timeout_sec=120)

            if not approved:
                logger.warning(f"Plan for task {task_id} was rejected or timed out.")
                self.complete_task(
                    task_id=task_id,
                    status="failed",
                    result="",
                    error=f"Plan was {plan.status} by user.",
                )
                return

            # 3. Run ReAct loop on approved plan
            logger.info(f"Executing approved task {task_id} via ReAct loop...")
            run_result: AgentRunResult = self.loop.run(prompt)

            if run_result.success:
                logger.info(f"🎉 Task {task_id} succeeded in {run_result.total_steps} steps!")
                self.complete_task(
                    task_id=task_id,
                    status="completed",
                    result=run_result.final_answer,
                )
            else:
                logger.warning(f"⚠️ Task {task_id} failed: {run_result.error}")
                self.complete_task(
                    task_id=task_id,
                    status="failed",
                    result=run_result.final_answer or "Incomplete execution",
                    error=run_result.error or "Unknown failure",
                )

        except KillSwitchTriggeredError as k_err:
            logger.error(f"Task {task_id} aborted by kill switch: {k_err}")
            self.complete_task(task_id, "failed", "", error=str(k_err))
        except Exception as exc:
            logger.error(f"Task {task_id} crashed with unhandled exception: {exc}")
            self.complete_task(task_id, "failed", "", error=str(exc))

    def run_forever(self) -> None:
        """Main loop continuously polling for edge tasks."""
        logger.info(f"🚀 JAI Edge Agent Daemon started (Poll Interval: {self.poll_interval_sec}s).")
        logger.info(f"Endpoint: {self.worker_url or '[OFFLINE]'}")

        while True:
            try:
                task = self.poll_once()
                if task:
                    self.execute_task(task)
            except KeyboardInterrupt:
                logger.info("Edge daemon stopped by user (Ctrl+C).")
                break
            except Exception as e:
                logger.error(f"Unexpected error in daemon loop: {e}")

            time.sleep(self.poll_interval_sec)


def main():
    parser = argparse.ArgumentParser(description="JAI Edge Agent Daemon")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")
    parser.add_argument("--interval", type=float, default=25.0, help="Poll interval in seconds")
    args = parser.parse_args()

    daemon = EdgeAgentDaemon(poll_interval_sec=args.interval)
    if args.once:
        task = daemon.poll_once()
        print("Poll Result:", task)
    else:
        daemon.run_forever()


if __name__ == "__main__":
    main()

