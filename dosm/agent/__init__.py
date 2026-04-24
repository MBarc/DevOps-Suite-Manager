"""Agent mode: tool catalog, plan cards, and approval-gated execution.

The agent never executes anything autonomously. Every action goes through a
PlanCard that a human approves (and can edit) first.
"""
from dosm.agent.actions import (
    ActionResult,
    ActionSpec,
    classify_command,
    list_actions,
    register_action,
)
from dosm.agent.routes import router as agent_router

__all__ = [
    "ActionResult",
    "ActionSpec",
    "agent_router",
    "classify_command",
    "list_actions",
    "register_action",
]
