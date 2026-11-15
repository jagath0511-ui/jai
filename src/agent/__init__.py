"""src/agent - JAI Autonomous Agent Core (Phase 1).

Provides budget-guarded ReAct execution engine and schema-validated tool registry.
"""

from __future__ import annotations

from src.agent.tools import ToolRegistry, register_tool, get_default_registry
from src.agent.loop import AgentLoop, AgentRunResult, BudgetExceededError, KillSwitchTriggeredError

__all__ = [
    "AgentLoop",
    "AgentRunResult",
    "BudgetExceededError",
    "KillSwitchTriggeredError",
    "ToolRegistry",
    "get_default_registry",
    "register_tool",
]

