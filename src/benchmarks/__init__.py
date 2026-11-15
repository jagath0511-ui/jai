"""src/benchmarks - Official Third-Party Benchmark Adapters for JAI.

Provides:
- BrowserGym / MiniWoB++ agent adapter (`browsergym_adapter.py`)
- AgentDojo safety & defense pipeline adapter (`agentdojo_adapter.py`)
"""
from __future__ import annotations

from src.benchmarks.browsergym_adapter import JAIBrowserGymAgent
from src.benchmarks.agentdojo_adapter import (
    JAIAgentDojoDefense,
    JAIAgentDojoPipeline,
    AgentDojoTaskResult,
    AgentDojoSuiteSummary,
)

__all__ = [
    "JAIBrowserGymAgent",
    "JAIAgentDojoDefense",
    "JAIAgentDojoPipeline",
    "AgentDojoTaskResult",
    "AgentDojoSuiteSummary",
]
