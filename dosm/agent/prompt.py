from __future__ import annotations

import json
import re
from dataclasses import dataclass

from dosm.agent.actions import action_tools
from dosm.agent.queries import query_tools


@dataclass
class ParsedPlan:
    tool: str
    args: dict
    rationale: str | None
    rollback: str | None
    raw: str


@dataclass
class ParsedQuery:
    tool: str
    args: dict
    raw: str


_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(\{.*?\})\s*</plan>", re.DOTALL)
_QUERY_BLOCK_RE = re.compile(r"<query>\s*(\{.*?\})\s*</query>", re.DOTALL)


def tools_for_agent() -> list[dict]:
    """All tools the agent can call, as OpenAI-compatible schemas.

    Read-only query tools are auto-executed by the server.
    Mutating action tools create plan cards requiring operator approval.
    """
    return query_tools() + action_tools()


def agent_system_prompt(user: str | None = None) -> str:
    lines = [
        "You are DOSM, an AI operations assistant in AGENT mode.",
        "",
        "You have two categories of tools:",
        "1. READ-ONLY tools (list_hosts, host_metrics, search_docs, etc.) - call these freely.",
        "   They query the local database or lightweight APIs. They do NOT open connections.",
        "2. EXEC/MUTATING tools (ssh_exec, local_exec, create_host, etc.) - these create a",
        "   plan card that a human operator must approve before anything runs.",
        "",
        "Rules:",
        "1. Call a read-only tool first to verify names/state before proposing any exec or mutation.",
        "2. Use exact names from the inventory - call list_hosts if unsure of a host name.",
        "3. Read-only tool results are database records - they do NOT mean you are connected.",
        "4. State what you are proposing and why before calling a mutating tool.",
        "5. If you cannot answer or act safely, say so plainly.",
        "   IMPORTANT: Inventory host names (e.g. 'herupa') are labels - not DNS hostnames.",
        "   Before using a host address in any command, call list_hosts to get the real",
        "   hostname or IP, then use that value in the command (not the inventory label).",
        "",
        "6. Choosing where to execute - the key question is: WHO runs the command?",
        "   DOSM container is the executor to local_exec (has NO 'host' parameter). Use when:",
        "     - testing reachability FROM DOSM:  'ping herupa' to local_exec: ping -c 4 <herupa-addr>",
        "     - port checks FROM DOSM:  'is 5432 open on db?' to local_exec: nc -zv db 5432",
        "     - DOSM-side curl, dig, traceroute",
        "   A registered host is the executor to ssh_exec (Linux/SSH) or winrm_exec (Windows). Use when:",
        "     - running a command ON a host:  'df -h on herupa' to ssh_exec host=herupa: df -h",
        "     - the host is the subject:  'from herupa, ping the DB' to ssh_exec host=herupa",
        "     - checking services, processes, or files ON a host",
        "",
        "7. Jump chains are automatic in ssh_exec - always name the FINAL target host.",
        "   Never name a jump box as the host; the runner resolves the chain from the inventory.",
        "",
        "8. local_exec has NO 'host' parameter. Never pass host= to local_exec.",
    ]
    if user:
        lines.append(f"9. The operator speaking with you is {user}.")
    return "\n".join(lines)


def parse_plan_blocks(text: str) -> list[ParsedPlan]:
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
    counter = {"n": 0}

    def repl(_m: re.Match) -> str:
        counter["n"] += 1
        return f"\n[plan #{counter['n']}]\n"

    return _PLAN_BLOCK_RE.sub(repl, text).strip()


def parse_query_blocks(text: str) -> list[ParsedQuery]:
    """Extract all <query>{...}</query> blocks from `text`."""
    queries: list[ParsedQuery] = []
    for match in _QUERY_BLOCK_RE.finditer(text):
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
        queries.append(ParsedQuery(tool=tool, args=args, raw=raw))
    return queries


def strip_query_blocks(text: str) -> str:
    """Remove <query>...</query> blocks, leaving the surrounding text."""
    return _QUERY_BLOCK_RE.sub("", text).strip()
