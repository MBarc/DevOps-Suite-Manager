from __future__ import annotations

import json
import re
from dataclasses import dataclass

from dosm.agent.actions import list_actions


@dataclass
class ParsedPlan:
    tool: str
    args: dict
    rationale: str | None
    rollback: str | None
    raw: str  # the full <plan>...</plan> block


_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(\{.*?\})\s*</plan>", re.DOTALL)


def agent_system_prompt(user: str | None = None) -> str:
    catalog_lines = []
    for spec in list_actions():
        args = ", ".join(
            f"{a['name']}{'?' if not a.get('required') else ''}:{a['type']}"
            for a in spec.args_schema
        )
        catalog_lines.append(f"- {spec.name}({args}) — {spec.description}")
    catalog = "\n".join(catalog_lines) if catalog_lines else "(no tools registered)"

    lines = [
        "You are DOSM in AGENT mode. You may propose actions, but a human "
        "operator approves every one before execution. You never act on your "
        "own.",
        "",
        "Available tools:",
        catalog,
        "",
        "When you want to take an action, emit ONE plan block per intended "
        "action, in this exact format (and nothing else inside the tags):",
        "",
        "<plan>",
        '{"tool": "ssh_exec", "args": {"host": "sf-prod-01", "command": "uptime"}, '
        '"rationale": "Why we want to run this.", "rollback": "How to recover '
        'if it goes wrong, or null if read-only."}',
        "</plan>",
        "",
        "Rules:",
        "1. Always include a brief plain-English explanation around the plan "
        "block — what you're about to do and what success looks like.",
        "2. Read-only diagnostics first. Only propose state-changing commands "
        "after you've confirmed the situation.",
        "3. Use exact host names from the operator's inventory; if you're "
        "unsure, ask before proposing a plan.",
        "4. After a tool is approved and executed, the tool result will be "
        "appended to the conversation so you can decide the next step.",
        "5. If you cannot answer or act safely from the available tools, say "
        "so plainly rather than guessing.",
    ]
    if user:
        lines.append(f"6. The operator speaking with you is {user}.")
    return "\n".join(lines)


def parse_plan_blocks(text: str) -> list[ParsedPlan]:
    """Extract all <plan>{...}</plan> blocks from `text`.

    Malformed JSON is silently skipped; the caller can still render the raw
    text so the operator sees what the model tried to say.
    """
    plans: list[ParsedPlan] = []
    for match in _PLAN_BLOCK_RE.finditer(text):
        raw = match.group(0)
        body = match.group(1)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        tool = obj.get("tool")
        args = obj.get("args") or {}
        if not isinstance(tool, str) or not isinstance(args, dict):
            continue
        plans.append(
            ParsedPlan(
                tool=tool,
                args=args,
                rationale=(obj.get("rationale") or None),
                rollback=(obj.get("rollback") or None),
                raw=raw,
            )
        )
    return plans


def strip_plan_blocks(text: str) -> str:
    """Return the assistant text with `<plan>...</plan>` blocks replaced by
    a placeholder so the visible message references each plan inline.
    """
    counter = {"n": 0}

    def repl(_m: re.Match) -> str:
        counter["n"] += 1
        return f"\n[plan #{counter['n']}]\n"

    return _PLAN_BLOCK_RE.sub(repl, text).strip()
