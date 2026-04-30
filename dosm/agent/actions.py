from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from dosm.config import Config


@dataclass
class ActionResult:
    """Standardized outcome shape from any agent tool invocation."""

    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


_OPENAI_TYPE: dict[str, str] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "secret": "string",
    "textarea": "string",
    "object": "object",
}


@dataclass
class ActionSpec:
    """Metadata describing a tool the agent may propose.

    `runner` is an async callable: `await runner(cfg, args) -> ActionResult`.
    `args_schema` entries: {name, type, required, description}.
    Supported types: string, number, boolean, textarea, secret, object.
    `elevated_confirm_field`: if set and tier=="elevated", the operator must
    type the value of that arg (e.g. host name, credential name) to confirm.
    """

    name: str
    description: str
    args_schema: list[dict]
    runner: Callable[..., Awaitable[ActionResult]]
    classify: Callable[[dict], str] = field(default=lambda args: "safe")
    elevated_confirm_field: str | None = None

    def to_openai_schema(self) -> dict:
        properties = {
            a["name"]: {
                "type": _OPENAI_TYPE.get(a["type"], "string"),
                "description": a.get("description", ""),
            }
            for a in self.args_schema
        }
        required = [a["name"] for a in self.args_schema if a.get("required")]
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


_REGISTRY: dict[str, ActionSpec] = {}


def register_action(spec: ActionSpec) -> None:
    _REGISTRY[spec.name] = spec


def list_actions() -> list[ActionSpec]:
    return list(_REGISTRY.values())


def get_action(name: str) -> ActionSpec | None:
    return _REGISTRY.get(name)


def action_tools() -> list[dict]:
    """Return all registered action tools as OpenAI-compatible tool schemas."""
    return [spec.to_openai_schema() for spec in list_actions()]


def classify_command(cfg: Config, command: str) -> str:
    """`safe` if `command` matches one of the allow-list globs, else `elevated`."""
    cmd = command.strip()
    if not cmd:
        return "elevated"
    for pattern in cfg.ssh_command_policy.allow_list:
        if fnmatch.fnmatch(cmd, pattern):
            return "safe"
    return "elevated"


# ---- ssh_exec ------------------------------------------------------------


async def _ssh_exec_runner(cfg: Config, args: dict) -> ActionResult:
    import asyncio
    import time

    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.jumps.connections import build_jump_chain, connect_through_chain
    from dosm.models import Host

    host_id = args.get("host_id")
    host_name = args.get("host")
    command = (args.get("command") or "").strip()
    timeout = float(args.get("timeout") or 30.0)

    if not command:
        return ActionResult(ok=False, summary="empty command")

    # Guard: bare `ping <host>` runs forever on Linux. Inject -c 4 if missing.
    import re as _re
    if _re.match(r"^ping\s+(?!.*-c\s)", command):
        command = _re.sub(r"^ping\s+", "ping -c 4 ", command)

    # Resolve host + jump chain inside a session, then materialize to plain
    # values so the rest of the runner can release the DB connection.
    with session_scope() as s:
        host: Host | None = None
        if host_id is not None:
            host = s.get(Host, int(host_id))
        elif host_name:
            host = s.execute(select(Host).where(Host.name == host_name)).scalar_one_or_none()
        if host is None:
            return ActionResult(ok=False, summary=f"host not found: {host_id or host_name!r}")
        if host.protocol != "ssh":
            return ActionResult(
                ok=False, summary=f"host {host.name!r} protocol is {host.protocol}, not ssh"
            )
        host_label = host.name
        jump_count = 0
        try:
            jump_hops, target = build_jump_chain(s, cfg, host)
            jump_count = len(jump_hops)
        except RuntimeError as e:
            return ActionResult(ok=False, summary=str(e))
        non_ssh = [h for h in jump_hops if s.get(Host, h.host_id).protocol != "ssh"]
        if non_ssh:
            names = ", ".join(f"{h.name!r}" for h in non_ssh)
            return ActionResult(
                ok=False,
                summary=(
                    f"jump chain has non-SSH hops ({names}); ssh_exec needs an "
                    f"all-SSH chain."
                ),
            )

    started = time.monotonic()
    conn = None
    try:
        import asyncssh as _asyncssh
    except ImportError:
        _asyncssh = None  # type: ignore
    try:
        conn = await connect_through_chain(jump_hops, target)
        res = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        ok = res.exit_status == 0
        chain_note = f" via {jump_count} jump host{'' if jump_count == 1 else 's'}" if jump_count else ""
        summary = (
            f"{host_label}: {command}{chain_note} → exit {res.exit_status} in {duration_ms}ms"
            if ok
            else f"{host_label}: {command}{chain_note} FAILED (exit {res.exit_status})"
        )
        return ActionResult(
            ok=ok,
            summary=summary,
            stdout=str(res.stdout or ""),
            stderr=str(res.stderr or ""),
            exit_code=int(res.exit_status) if res.exit_status is not None else None,
            duration_ms=duration_ms,
            extra={"host": host_label, "command": command, "jumps": jump_count},
        )
    except asyncio.TimeoutError:
        return ActionResult(
            ok=False,
            summary=f"{host_label}: {command} timed out after {timeout}s",
            duration_ms=int((time.monotonic() - started) * 1000),
            extra={"host": host_label, "command": command, "jumps": jump_count},
        )
    except Exception as e:
        if _asyncssh is not None and isinstance(e, _asyncssh.PermissionDenied):
            summary = (
                f"{host_label}: SSH authentication rejected for user "
                f"{target.username!r} — check the credential profile"
            )
        else:
            summary = f"{host_label}: {type(e).__name__}: {e}"
        return ActionResult(
            ok=False,
            summary=summary,
            stderr=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
            extra={"host": host_label, "command": command, "jumps": jump_count},
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _ssh_exec_classify(args: dict) -> str:
    # Late-bound config lookup so registration doesn't depend on a Config
    # instance; the runner-side classify happens in routes.py with the live cfg.
    return "safe" if args.get("_pre_classified") == "safe" else "elevated"


SSH_EXEC = ActionSpec(
    name="ssh_exec",
    description=(
        "Run a shell command ON a specific registered Linux/SSH host — the host is the executor. "
        "Use whenever a named host should run the command: "
        "'check disk on herupa', 'restart the agent on app-server', "
        "'from herupa, can it reach the DB?', 'what processes are running on host X?'. "
        "Jump-host chains are resolved automatically from the inventory — "
        "always name the FINAL target host, never a jump box. "
        "Do NOT use this for connectivity checks FROM DOSM to a host — use local_exec for that. "
        "Always use ping -c 4, not plain ping."
    ),
    args_schema=[
        {"name": "host", "type": "string", "required": True, "description": "Host name from the inventory (e.g. 'herupa')."},
        {"name": "command", "type": "string", "required": True, "description": "Shell command to run on the remote host. For ping always include -c 4."},
        {"name": "timeout", "type": "number", "required": False, "description": "Seconds. Default 30."},
    ],
    runner=_ssh_exec_runner,
    classify=_ssh_exec_classify,
    elevated_confirm_field="host",
)
register_action(SSH_EXEC)


# ---- local_exec ----------------------------------------------------------


async def _local_exec_runner(cfg: Config, args: dict) -> ActionResult:
    import asyncio
    import re as _re
    import time

    command = (args.get("command") or "").strip()
    timeout = float(args.get("timeout") or 30.0)
    if not command:
        return ActionResult(ok=False, summary="empty command")

    # Resolve inventory host labels to their real hostname/IP.
    # The LLM often uses the display name (e.g. "herupa") which isn't DNS-resolvable;
    # swap any whole-word occurrences for the actual address recorded in the inventory.
    try:
        from sqlalchemy import select as _sel
        from dosm.db import session_scope as _scope
        from dosm.models import Host as _Host
        with _scope() as _s:
            _rows = list(_s.execute(_sel(_Host.name, _Host.hostname)).all())
        for _label, _addr in _rows:
            if _label and _addr and _label != _addr:
                command = _re.sub(r'\b' + _re.escape(_label) + r'\b', _addr, command)
    except Exception:
        pass

    # Guard: bare `ping <host>` runs forever on Linux. Inject -c 4 if missing.
    if _re.match(r"^ping\s+(?!.*-c\s)", command):
        command = _re.sub(r"^ping\s+", "ping -c 4 ", command)

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return ActionResult(ok=False, summary=f"spawn failed: {type(e).__name__}: {e}", stderr=str(e))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return ActionResult(
            ok=False,
            summary=f"local: {command} timed out after {timeout}s",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    ok = proc.returncode == 0
    summary = (
        f"local: {command} → exit {proc.returncode} in {duration_ms}ms"
        if ok
        else f"local: {command} FAILED (exit {proc.returncode})"
    )
    return ActionResult(
        ok=ok,
        summary=summary,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        extra={"command": command},
    )


LOCAL_EXEC = ActionSpec(
    name="local_exec",
    description=(
        "Run a shell command on the DOSM server/container itself — DOSM is always the executor. "
        "Use for diagnostics FROM DOSM's network perspective: "
        "'can DOSM reach herupa?', 'ping herupa', 'is port 5432 open on db-server?'. "
        "When a host name appears in your command (e.g. 'ping herupa'), "
        "that host is the TARGET of the diagnostic — DOSM is still the one running the command. "
        "IMPORTANT: inventory host names are labels, not DNS names. "
        "Always call list_hosts first to get the real hostname or IP, then use that in your command. "
        "Use ssh_exec or winrm_exec when a registered host should be the executor. "
        "This tool has NO 'host' parameter — never pass host= here. "
        "Always use ping -c 4, not plain ping."
    ),
    args_schema=[
        {"name": "command", "type": "string", "required": True, "description": "Shell command to run on the DOSM server. For ping always include -c 4."},
        {"name": "timeout", "type": "number", "required": False, "description": "Seconds. Default 30."},
    ],
    runner=_local_exec_runner,
    classify=lambda args: "elevated",  # actual tier resolved in routes.py via classify_command
    elevated_confirm_field=None,
)
register_action(LOCAL_EXEC)


# ---- winrm_exec ----------------------------------------------------------


async def _winrm_exec_runner(cfg: Config, args: dict) -> ActionResult:
    import asyncio
    import time

    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.jumps.tunnels import get_tunnel_manager
    from dosm.models import Host
    from dosm.secrets import SecretNotFound, get_backend

    host_name = (args.get("host") or "").strip()
    command = (args.get("command") or "").strip()
    timeout = float(args.get("timeout") or 30.0)
    if not host_name:
        return ActionResult(ok=False, summary="host is required")
    if not command:
        return ActionResult(ok=False, summary="empty command")

    mcfg = cfg.metrics
    winrm_port = mcfg.winrm_port
    use_https = mcfg.winrm_use_https
    transport = mcfg.winrm_transport

    # Step 1: resolve host + creds — close session before async tunnel work.
    with session_scope() as s:
        host = s.execute(select(Host).where(Host.name == host_name)).scalar_one_or_none()
        if host is None:
            return ActionResult(ok=False, summary=f"host {host_name!r} not found")
        cred = host.credential
        if cred is None:
            return ActionResult(ok=False, summary=f"host {host_name!r} has no credential profile; WinRM needs username + password")
        if not cred.username:
            return ActionResult(ok=False, summary=f"credential {cred.name!r} has no username; WinRM needs one")
        if cred.kind != "login":
            return ActionResult(ok=False, summary=f"credential {cred.name!r} kind is {cred.kind!r}; WinRM requires a 'login' credential")
        try:
            secret_text = get_backend(cfg).get_str(cred.secret_ref)
        except SecretNotFound:
            return ActionResult(ok=False, summary=f"credential {cred.name!r} secret not found in backend")
        host_id = host.id
        host_label = host.name
        host_hostname = host.hostname
        username = cred.username
        if cred.domain:
            username = f"{cred.domain}\\{username}"

    # Step 2: acquire jump tunnel if this host is behind a jump box.
    mgr = get_tunnel_manager()
    lease = None
    started = time.monotonic()
    try:
        with session_scope() as s2:
            host_again = s2.get(Host, host_id)
            try:
                lease = await mgr.acquire(s2, cfg, host_again, target_port=winrm_port)
            except Exception as e:
                return ActionResult(
                    ok=False,
                    summary=f"{host_label}: jump tunnel failed: {type(e).__name__}: {e}",
                    stderr=str(e),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

        if lease is not None:
            connect_host = "127.0.0.1" if lease.bind_host in ("0.0.0.0", "") else lease.bind_host
            connect_port = lease.bind_port
        else:
            connect_host = host_hostname
            connect_port = winrm_port

        scheme = "https" if use_https else "http"
        endpoint = f"{scheme}://{connect_host}:{connect_port}/wsman"
        cert_validation = "ignore" if use_https else "validate"

        def _run_sync():
            import winrm  # type: ignore
            session = winrm.Session(
                endpoint,
                auth=(username, secret_text),
                transport=transport,
                server_cert_validation=cert_validation,
            )
            return session.run_ps(command)

        try:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _run_sync),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ActionResult(
                ok=False,
                summary=f"{host_label}: WinRM timed out after {timeout}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                extra={"host": host_label, "command": command, "via_jump": lease is not None},
            )
        except Exception as e:
            return ActionResult(
                ok=False,
                summary=f"{host_label}: {type(e).__name__}: {e}",
                stderr=str(e),
                duration_ms=int((time.monotonic() - started) * 1000),
                extra={"host": host_label, "command": command, "via_jump": lease is not None},
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = (result.std_out or b"").decode("utf-8", errors="replace")
        stderr = (result.std_err or b"").decode("utf-8", errors="replace")
        ok = result.status_code == 0
        chain_note = " via jump host" if lease else ""
        summary = (
            f"{host_label}: PowerShell{chain_note} → exit {result.status_code} in {duration_ms}ms"
            if ok
            else f"{host_label}: PowerShell{chain_note} FAILED (exit {result.status_code})"
        )
        return ActionResult(
            ok=ok,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=int(result.status_code),
            duration_ms=duration_ms,
            extra={"host": host_label, "command": command, "via_jump": lease is not None},
        )
    finally:
        if lease is not None:
            try:
                await lease.release()
            except Exception:
                pass


WINRM_EXEC = ActionSpec(
    name="winrm_exec",
    description=(
        "Run a PowerShell command on a Windows host in the inventory over WinRM. "
        "Use for Windows servers — service queries, network checks, file ops, WMI. "
        "Examples: 'Get-Service spooler', 'Test-NetConnection target -Port 443', "
        "'query session', 'Get-WmiObject Win32_Process'. Jump-host chains are "
        "handled automatically. For Linux hosts use ssh_exec; for commands from "
        "DOSM itself (no registered host needed) use local_exec."
    ),
    args_schema=[
        {"name": "host", "type": "string", "required": True, "description": "Windows host name from the inventory."},
        {"name": "command", "type": "string", "required": True, "description": "PowerShell command to run."},
        {"name": "timeout", "type": "number", "required": False, "description": "Seconds. Default 30."},
    ],
    runner=_winrm_exec_runner,
    classify=lambda args: "elevated",  # actual tier resolved in routes.py via classify_command
    elevated_confirm_field="host",
)
register_action(WINRM_EXEC)


# ---- run_pipeline --------------------------------------------------------


async def _run_pipeline_runner(cfg: Config, args: dict) -> ActionResult:
    """Trigger a registered pipeline by name and return the resulting run."""
    import time

    from dosm.db import session_scope
    from dosm.pipelines import repo as pipelines_repo

    name = (args.get("name") or "").strip()
    inputs = args.get("inputs") or {}
    if not name:
        return ActionResult(ok=False, summary="run_pipeline requires `name`")
    if not isinstance(inputs, dict):
        return ActionResult(ok=False, summary="`inputs` must be an object")

    started = time.monotonic()
    with session_scope() as s:
        pipeline = pipelines_repo.get_pipeline_by_name(s, name)
        if pipeline is None:
            return ActionResult(ok=False, summary=f"pipeline {name!r} not found")
        try:
            run = await pipelines_repo.trigger_pipeline(
                cfg, s, pipeline, inputs=inputs, user_id=None
            )
        except Exception as e:
            return ActionResult(
                ok=False,
                summary=f"trigger crashed: {type(e).__name__}: {e}",
                stderr=repr(e),
                duration_ms=int((time.monotonic() - started) * 1000),
                extra={"pipeline": name},
            )
        run_id = run.id
        run_status = run.status
        run_external = run.external_id
        run_url = run.html_url
        run_error = run.error

    duration_ms = int((time.monotonic() - started) * 1000)
    ok = run_status not in ("failed", "cancelled")
    summary_parts = [f"pipeline {name!r} → {run_status}"]
    if run_external:
        summary_parts.append(f"external={run_external}")
    summary_parts.append(f"in {duration_ms}ms")
    return ActionResult(
        ok=ok,
        summary=" ".join(summary_parts) + (f" (err: {run_error[:80]})" if run_error else ""),
        stdout=(run_url or ""),
        stderr=(run_error or ""),
        duration_ms=duration_ms,
        extra={
            "pipeline": name,
            "run_id": run_id,
            "external_id": run_external,
            "html_url": run_url,
            "dosm_run_url": f"/pipelines/runs/{run_id}",
        },
    )


def _run_pipeline_classify(args: dict) -> str:
    # Pipeline runs always state-changing — keep them at safe tier so the
    # plan card uses the standard Approve flow (no typed confirmation), but
    # the user still has to approve. Elevated triggers can come later (e.g.
    # for pipelines tagged `prod`).
    return "safe"


RUN_PIPELINE = ActionSpec(
    name="run_pipeline",
    description="Trigger a registered CI/CD pipeline by name with optional inputs. Returns the created run id and provider URL.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Pipeline name from /pipelines."},
        {"name": "inputs", "type": "object", "required": False, "description": "Provider-specific inputs map."},
    ],
    runner=_run_pipeline_runner,
    classify=_run_pipeline_classify,
)
register_action(RUN_PIPELINE)


# ---- create_pipeline / update_pipeline / delete_pipeline ------------------


async def _create_pipeline_runner(cfg: Config, args: dict) -> ActionResult:
    import json
    from sqlalchemy.exc import IntegrityError
    from dosm.db import session_scope
    from dosm.models import AuditLog
    from dosm.pipelines.repo import create_pipeline
    from dosm.pipelines.adapters import PipelineProviderError, list_providers

    name = (args.get("name") or "").strip()
    provider = (args.get("provider") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")
    if not provider:
        return ActionResult(ok=False, summary="provider is required")
    if provider not in list_providers():
        return ActionResult(ok=False, summary=f"unknown provider {provider!r}; valid: {', '.join(list_providers())}")

    raw_config = args.get("config") or "{}"
    try:
        config_dict = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    except json.JSONDecodeError as e:
        return ActionResult(ok=False, summary=f"config is not valid JSON: {e}")

    raw_inputs = args.get("inputs_schema")
    try:
        inputs_schema = json.loads(raw_inputs) if isinstance(raw_inputs, str) and raw_inputs else None
    except json.JSONDecodeError:
        inputs_schema = None

    credential_id = args.get("credential_id")
    try:
        credential_id = int(credential_id) if credential_id else None
    except (TypeError, ValueError):
        credential_id = None

    description = (args.get("description") or "").strip() or None

    try:
        with session_scope() as s:
            p = create_pipeline(
                s,
                name=name,
                provider=provider,
                description=description,
                config=config_dict,
                inputs_schema=inputs_schema,
                credential_id=credential_id,
            )
            pid = p.id
            s.add(AuditLog(action="pipeline.create", target=f"pipeline:{pid}", details=f"agent provider={provider}"))
    except PipelineProviderError as e:
        return ActionResult(ok=False, summary=f"Provider config error: {e}", stderr=str(e))
    except IntegrityError as e:
        return ActionResult(ok=False, summary=f"Duplicate pipeline name: {e.__cause__ or e}", stderr=str(e))
    except Exception as e:
        return ActionResult(ok=False, summary=f"Create failed: {type(e).__name__}: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Created pipeline {name!r} (id={pid})", extra={"pipeline_id": pid, "pipeline": name})


CREATE_PIPELINE = ActionSpec(
    name="create_pipeline",
    description="Register a new CI/CD pipeline in DOSM. Pass provider-specific settings as a JSON string in the config field.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Unique pipeline name."},
        {"name": "provider", "type": "string", "required": True, "description": "Provider: github_actions, azure_devops, awx, octopus, or terraform_cloud."},
        {"name": "config", "type": "string", "required": True, "description": 'Provider config as JSON. github_actions example: {"owner":"acme","repo":"api","workflow":"ci.yml","ref":"main"}'},
        {"name": "description", "type": "string", "required": False, "description": "Optional description."},
        {"name": "inputs_schema", "type": "string", "required": False, "description": "Optional JSON array of input definitions for parametrised runs."},
        {"name": "credential_id", "type": "number", "required": False, "description": "Credential profile ID holding the provider token."},
    ],
    runner=_create_pipeline_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(CREATE_PIPELINE)


async def _update_pipeline_runner(cfg: Config, args: dict) -> ActionResult:
    import json
    from dosm.db import session_scope
    from dosm.models import AuditLog
    from dosm.pipelines.repo import get_pipeline_by_name, update_pipeline
    from dosm.pipelines.adapters import PipelineProviderError, list_providers

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    try:
        with session_scope() as s:
            p = get_pipeline_by_name(s, name)
            if p is None:
                return ActionResult(ok=False, summary=f"Pipeline {name!r} not found")

            new_provider = (args.get("provider") or p.provider).strip()
            if new_provider not in list_providers():
                return ActionResult(ok=False, summary=f"unknown provider {new_provider!r}")

            raw_config = args.get("config")
            if raw_config is not None:
                try:
                    config_dict = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
                except json.JSONDecodeError as e:
                    return ActionResult(ok=False, summary=f"config is not valid JSON: {e}")
            else:
                config_dict = json.loads(p.config or "{}")

            raw_inputs = args.get("inputs_schema")
            if raw_inputs is not None:
                try:
                    inputs_schema = json.loads(raw_inputs) if isinstance(raw_inputs, str) and raw_inputs else None
                except json.JSONDecodeError:
                    inputs_schema = None
            else:
                inputs_schema = json.loads(p.inputs_schema) if p.inputs_schema else None

            credential_id = args.get("credential_id")
            try:
                credential_id = int(credential_id) if credential_id is not None else p.credential_id
            except (TypeError, ValueError):
                credential_id = p.credential_id

            description = args.get("description", p.description)
            if isinstance(description, str):
                description = description.strip() or None

            update_pipeline(
                s, p,
                name=name,
                provider=new_provider,
                description=description,
                config=config_dict,
                inputs_schema=inputs_schema,
                credential_id=credential_id,
            )
            s.add(AuditLog(action="pipeline.update", target=f"pipeline:{p.id}", details=f"agent name={name}"))
    except PipelineProviderError as e:
        return ActionResult(ok=False, summary=f"Provider config error: {e}", stderr=str(e))
    except Exception as e:
        return ActionResult(ok=False, summary=f"Update failed: {type(e).__name__}: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Updated pipeline {name!r}", extra={"pipeline": name})


UPDATE_PIPELINE = ActionSpec(
    name="update_pipeline",
    description="Update an existing pipeline registration. Omit any field to keep its current value.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Existing pipeline name."},
        {"name": "provider", "type": "string", "required": False, "description": "New provider (github_actions, azure_devops, etc.)."},
        {"name": "config", "type": "string", "required": False, "description": "New provider config as JSON."},
        {"name": "description", "type": "string", "required": False, "description": "New description."},
        {"name": "inputs_schema", "type": "string", "required": False, "description": "New inputs schema as JSON."},
        {"name": "credential_id", "type": "number", "required": False, "description": "New credential profile ID."},
    ],
    runner=_update_pipeline_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(UPDATE_PIPELINE)


async def _delete_pipeline_runner(cfg: Config, args: dict) -> ActionResult:
    from dosm.db import session_scope
    from dosm.models import AuditLog
    from dosm.pipelines.repo import get_pipeline_by_name, delete_pipeline

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        p = get_pipeline_by_name(s, name)
        if p is None:
            return ActionResult(ok=False, summary=f"Pipeline {name!r} not found")
        pid = p.id
        delete_pipeline(s, p)
        s.add(AuditLog(action="pipeline.delete", target=f"pipeline:{pid}", details=f"agent name={name}"))

    return ActionResult(ok=True, summary=f"Deleted pipeline {name!r}", extra={"pipeline": name})


DELETE_PIPELINE = ActionSpec(
    name="delete_pipeline",
    description="Permanently delete a pipeline registration and all its run history.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Exact pipeline name to delete."},
    ],
    runner=_delete_pipeline_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(DELETE_PIPELINE)


# ---- upsert_doc ----------------------------------------------------------


async def _upsert_doc_runner(cfg: Config, args: dict) -> ActionResult:
    import time
    from dosm.docs_index import vault
    from dosm.docs_index.indexer import reindex_async

    path = (args.get("path") or "").strip()
    title = (args.get("title") or "").strip()
    folder_slug = (args.get("folder_slug") or vault.UNFILED_SLUG).strip()
    body_md = args.get("body_md") or ""
    author = args.get("_author") or "agent"

    if not title:
        return ActionResult(ok=False, summary="title is required")
    if not body_md.strip():
        return ActionResult(ok=False, summary="body_md is required")

    started = time.monotonic()
    try:
        if path:
            # Update existing doc — derive folder_slug + doc_slug from path
            parts = path.replace("\\", "/").strip("/")
            if "/" in parts:
                save_folder_slug = parts.rsplit("/", 1)[0]
                doc_slug = parts.rsplit("/", 1)[1].removesuffix(".md")
            else:
                save_folder_slug = vault.UNFILED_SLUG
                doc_slug = parts.removesuffix(".md")
            saved = vault.save_doc(cfg, folder_slug=save_folder_slug, doc_slug=doc_slug, title=title, body_md=body_md, author=author)
        else:
            # New doc
            slug_base = vault.slugify(title)
            folder_dir = cfg.docs_dir / folder_slug
            doc_slug = vault.find_unique_slug(folder_dir, slug_base)
            saved = vault.save_doc(cfg, folder_slug=folder_slug, doc_slug=doc_slug, title=title, body_md=body_md, author=author)
    except Exception as e:
        return ActionResult(ok=False, summary=f"Save failed: {type(e).__name__}: {e}", stderr=str(e))

    rel_saved = saved.relative_to(cfg.docs_dir).as_posix()
    reindex_async(cfg, force=False)
    duration_ms = int((time.monotonic() - started) * 1000)
    action = "updated" if path else "created"
    return ActionResult(
        ok=True,
        summary=f"Doc {action}: {rel_saved} in {duration_ms}ms",
        stdout=rel_saved,
        duration_ms=duration_ms,
        extra={"rel_path": rel_saved},
    )


UPSERT_DOC = ActionSpec(
    name="upsert_doc",
    description="Create or update a vault markdown document. Provide 'path' to update an existing doc; omit for a new one.",
    args_schema=[
        {"name": "path", "type": "string", "required": False, "description": "Relative doc path (e.g. service-fabric/runbook.md). Omit to create new."},
        {"name": "title", "type": "string", "required": True, "description": "Document title."},
        {"name": "folder_slug", "type": "string", "required": False, "description": "Folder slug for new docs (default: _unfiled)."},
        {"name": "body_md", "type": "textarea", "required": True, "description": "Markdown body content."},
    ],
    runner=_upsert_doc_runner,
)
register_action(UPSERT_DOC)


# ---- delete_doc ----------------------------------------------------------


async def _delete_doc_runner(cfg: Config, args: dict) -> ActionResult:
    from dosm.docs_index import vault
    from dosm.docs_index.indexer import reindex_async

    path = (args.get("path") or "").strip()
    if not path:
        return ActionResult(ok=False, summary="path is required")
    try:
        vault.delete_doc(cfg, path)
    except ValueError as e:
        return ActionResult(ok=False, summary=f"Invalid path: {e}", stderr=str(e))
    except FileNotFoundError:
        return ActionResult(ok=False, summary=f"Doc not found: {path!r}")
    except Exception as e:
        return ActionResult(ok=False, summary=f"Delete failed: {e}", stderr=str(e))

    reindex_async(cfg, force=False)
    return ActionResult(ok=True, summary=f"Deleted: {path}", stdout=path)


DELETE_DOC = ActionSpec(
    name="delete_doc",
    description="Permanently delete a vault document by its relative path.",
    args_schema=[
        {"name": "path", "type": "string", "required": True, "description": "Relative doc path (e.g. service-fabric/runbook.md)."},
    ],
    runner=_delete_doc_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="path",
)
register_action(DELETE_DOC)


# ---- sync_org_group ------------------------------------------------------


async def _sync_org_group_runner(cfg: Config, args: dict) -> ActionResult:
    import asyncio
    import time
    from functools import partial
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import Department

    group_name = (args.get("group_name") or "").strip()
    if not group_name:
        return ActionResult(ok=False, summary="group_name is required")

    # Resolve the department ID first (fast DB read)
    with session_scope() as s:
        dept_row = s.execute(
            select(Department).where(Department.ad_group_name == group_name)
        ).scalar_one_or_none()
        if dept_row is None:
            return ActionResult(ok=False, summary=f"No department with AD group name {group_name!r}")
        dept_id = dept_row.id
        dept_name = dept_row.name

    # Run the blocking WinRM sync in a thread with its own DB session
    def _run() -> dict:
        from dosm.directory.sync import sync_department
        with session_scope() as s2:
            dept = s2.get(Department, dept_id)
            if dept is None:
                raise RuntimeError(f"Department {dept_id} disappeared")
            return sync_department(s2, cfg, dept, actor_id=None)

    started = time.monotonic()
    loop = asyncio.get_event_loop()
    try:
        summary = await loop.run_in_executor(None, _run)
    except Exception as e:
        return ActionResult(ok=False, summary=f"AD sync failed: {type(e).__name__}: {e}", stderr=str(e))

    duration_ms = int((time.monotonic() - started) * 1000)
    return ActionResult(
        ok=True,
        summary=f"Synced {dept_name!r}: +{summary.get('added', 0)} added, -{summary.get('removed', 0)} removed, ={summary.get('kept', 0)} kept in {duration_ms}ms",
        duration_ms=duration_ms,
        extra={"summary": summary},
    )


SYNC_ORG_GROUP = ActionSpec(
    name="sync_org_group",
    description="Sync an Active Directory group into the organisation directory. Updates members, manager chain, and hierarchy.",
    args_schema=[
        {"name": "group_name", "type": "string", "required": True, "description": "AD group name matching a configured department."},
    ],
    runner=_sync_org_group_runner,
)
register_action(SYNC_ORG_GROUP)


# ---- create_host / update_host -------------------------------------------


async def _create_host_runner(cfg: Config, args: dict) -> ActionResult:
    from dosm.db import session_scope
    from dosm.hosts.repo import create_host, HostValidationError
    from dosm.models import AuditLog

    name = (args.get("name") or "").strip()
    hostname = (args.get("hostname") or "").strip()
    if not name or not hostname:
        return ActionResult(ok=False, summary="name and hostname are required")

    protocol = (args.get("protocol") or "ssh").strip()
    try:
        port = int(args.get("port") or (22 if protocol == "ssh" else 3389 if protocol == "rdp" else 5900))
    except (TypeError, ValueError):
        port = 22
    description = (args.get("description") or "").strip() or None
    credential_id = args.get("credential_id")
    try:
        credential_id = int(credential_id) if credential_id else None
    except (TypeError, ValueError):
        credential_id = None
    jump_host_id = args.get("jump_host_id")
    try:
        jump_host_id = int(jump_host_id) if jump_host_id else None
    except (TypeError, ValueError):
        jump_host_id = None
    is_jumpbox = bool(args.get("is_jumpbox") or False)
    tags_csv = (args.get("tags") or "").strip()

    try:
        with session_scope() as s:
            host = create_host(
                s,
                name=name, hostname=hostname, port=port, protocol=protocol,
                description=description, credential_id=credential_id,
                jump_host_id=jump_host_id, tags_csv=tags_csv, is_jumpbox=is_jumpbox,
            )
            host_id = host.id
            s.add(AuditLog(action="host.create", target=f"host:{host_id}", details=f"agent name={name}"))
    except HostValidationError as e:
        return ActionResult(ok=False, summary=f"Validation error: {e}", stderr=str(e))
    except Exception as e:
        return ActionResult(ok=False, summary=f"Create failed: {type(e).__name__}: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Created host {name!r} (id={host_id})", extra={"host_id": host_id, "host": name})


CREATE_HOST = ActionSpec(
    name="create_host",
    description="Add a new host to the inventory.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Unique display name."},
        {"name": "hostname", "type": "string", "required": True, "description": "Hostname or IP address."},
        {"name": "port", "type": "number", "required": False, "description": "Port (default: 22 for SSH, 3389 for RDP, 5900 for VNC)."},
        {"name": "protocol", "type": "string", "required": False, "description": "ssh | rdp | vnc (default: ssh)."},
        {"name": "description", "type": "string", "required": False, "description": "Optional description."},
        {"name": "credential_id", "type": "number", "required": False, "description": "Credential profile ID to bind."},
        {"name": "is_jumpbox", "type": "boolean", "required": False, "description": "Mark as a jump host."},
        {"name": "tags", "type": "string", "required": False, "description": "Comma-separated tags."},
    ],
    runner=_create_host_runner,
)
register_action(CREATE_HOST)


async def _update_host_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.hosts.repo import update_host, HostValidationError
    from dosm.models import AuditLog, Host

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        host = s.execute(select(Host).where(Host.name == name)).scalar_one_or_none()
        if host is None:
            return ActionResult(ok=False, summary=f"Host {name!r} not found")

        protocol = (args.get("protocol") or host.protocol).strip()
        try:
            port = int(args.get("port") or host.port)
        except (TypeError, ValueError):
            port = host.port
        hostname = (args.get("hostname") or host.hostname).strip()
        description = args.get("description", host.description)
        if isinstance(description, str):
            description = description.strip() or None
        credential_id = args.get("credential_id")
        try:
            credential_id = int(credential_id) if credential_id is not None else host.credential_id
        except (TypeError, ValueError):
            credential_id = host.credential_id
        jump_host_id = args.get("jump_host_id")
        try:
            jump_host_id = int(jump_host_id) if jump_host_id is not None else host.jump_host_id
        except (TypeError, ValueError):
            jump_host_id = host.jump_host_id
        is_jumpbox = bool(args.get("is_jumpbox")) if "is_jumpbox" in args else host.is_jumpbox
        tags_csv = (args.get("tags") or "").strip()

        try:
            update_host(
                s, host,
                name=name, hostname=hostname, port=port, protocol=protocol,
                description=description, credential_id=credential_id,
                jump_host_id=jump_host_id, tags_csv=tags_csv, is_jumpbox=is_jumpbox,
            )
            s.add(AuditLog(action="host.update", target=f"host:{host.id}", details=f"agent name={name}"))
        except HostValidationError as e:
            return ActionResult(ok=False, summary=f"Validation error: {e}", stderr=str(e))
        except Exception as e:
            return ActionResult(ok=False, summary=f"Update failed: {type(e).__name__}: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Updated host {name!r}", extra={"host": name})


UPDATE_HOST = ActionSpec(
    name="update_host",
    description="Update an existing host in the inventory. Supply only the fields to change; others keep their current value.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Existing host name (used to find the record)."},
        {"name": "hostname", "type": "string", "required": False, "description": "New hostname or IP."},
        {"name": "port", "type": "number", "required": False, "description": "New port."},
        {"name": "protocol", "type": "string", "required": False, "description": "New protocol (ssh | rdp | vnc)."},
        {"name": "description", "type": "string", "required": False, "description": "New description."},
        {"name": "credential_id", "type": "number", "required": False, "description": "New credential profile ID."},
        {"name": "tags", "type": "string", "required": False, "description": "Comma-separated tags (replaces existing)."},
    ],
    runner=_update_host_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(UPDATE_HOST)


# ---- create_credential / update_credential --------------------------------


def _auto_secret_ref(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return f"credentials/{slug}"


async def _create_credential_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy.exc import IntegrityError
    from dosm.db import session_scope
    from dosm.models import AuditLog, Credential
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    kind = (args.get("kind") or "login").strip()
    username = (args.get("username") or "").strip() or None
    domain = (args.get("domain") or "").strip() or None
    secret_value = (args.get("secret_value") or "").strip()

    if not name:
        return ActionResult(ok=False, summary="name is required")
    if kind not in ("login", "ssh_key", "pat"):
        return ActionResult(ok=False, summary=f"invalid kind {kind!r}; use login, ssh_key, or pat")
    if not secret_value:
        return ActionResult(ok=False, summary="secret_value is required — enter it in the plan card form")

    secret_ref = _auto_secret_ref(name)
    cred_id: int

    # Commit DB row first, then write secret (single-writer SQLite rule)
    with session_scope() as s:
        cred = Credential(name=name, kind=kind, username=username, domain=domain, secret_ref=secret_ref)
        s.add(cred)
        try:
            s.flush()
        except IntegrityError as e:
            return ActionResult(ok=False, summary=f"Duplicate name: {e.__cause__ or e}", stderr=str(e))
        cred_id = cred.id
        s.add(AuditLog(action="credential.create", target=f"credential:{cred_id}", details=f"agent kind={kind}"))

    try:
        get_backend(cfg).set_str(secret_ref, secret_value)
    except Exception as e:
        return ActionResult(ok=False, summary=f"Credential row created (id={cred_id}) but secret write failed: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Created credential {name!r} (id={cred_id})", extra={"credential_id": cred_id})


CREATE_CREDENTIAL = ActionSpec(
    name="create_credential",
    description="Create a new credential profile. The secret value is entered by the operator in the plan card — never proposed by the agent.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Unique profile name."},
        {"name": "kind", "type": "string", "required": True, "description": "Credential kind: 'login' (username+password for SSH, RDP, WinRM, or any Windows/Linux server), 'ssh_key' (private key), or 'pat' (personal access token). Use 'login' for all username/password credentials regardless of OS or protocol."},
        {"name": "username", "type": "string", "required": False, "description": "Username (for login/ssh_key kinds)."},
        {"name": "domain", "type": "string", "required": False, "description": "Windows domain (optional)."},
        {"name": "secret_value", "type": "secret", "required": True, "description": "Password / SSH key / PAT — entered by operator, never from LLM."},
    ],
    runner=_create_credential_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(CREATE_CREDENTIAL)


async def _update_credential_runner(cfg: Config, args: dict) -> ActionResult:
    from datetime import datetime, timezone
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, Credential
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    username = args.get("username")
    domain = args.get("domain")
    secret_value = (args.get("secret_value") or "").strip()

    with session_scope() as s:
        cred = s.execute(select(Credential).where(Credential.name == name)).scalar_one_or_none()
        if cred is None:
            return ActionResult(ok=False, summary=f"Credential {name!r} not found")
        if username is not None:
            cred.username = username.strip() or None
        if domain is not None:
            cred.domain = domain.strip() or None
        cred.updated_at = datetime.now(timezone.utc)
        secret_ref = cred.secret_ref
        cred_id = cred.id
        s.add(AuditLog(action="credential.update", target=f"credential:{cred_id}", details="agent"))

    if secret_value:
        try:
            get_backend(cfg).set_str(secret_ref, secret_value)
        except Exception as e:
            return ActionResult(ok=False, summary=f"Metadata updated but secret write failed: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Updated credential {name!r}", extra={"credential_id": cred_id})


UPDATE_CREDENTIAL = ActionSpec(
    name="update_credential",
    description="Update an existing credential profile. Leave secret_value blank to keep the current secret.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Existing credential name."},
        {"name": "username", "type": "string", "required": False, "description": "New username (omit to keep current)."},
        {"name": "domain", "type": "string", "required": False, "description": "New domain (omit to keep current)."},
        {"name": "secret_value", "type": "secret", "required": False, "description": "New secret — leave blank to keep existing."},
    ],
    runner=_update_credential_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(UPDATE_CREDENTIAL)


# ---- delete_credential ---------------------------------------------------


async def _delete_credential_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, Credential
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        cred = s.execute(select(Credential).where(Credential.name == name)).scalar_one_or_none()
        if cred is None:
            return ActionResult(ok=False, summary=f"Credential {name!r} not found")
        secret_ref = cred.secret_ref
        cred_id = cred.id
        s.delete(cred)
        s.add(AuditLog(action="credential.delete", target=f"credential:{cred_id}", details="agent"))

    try:
        get_backend(cfg).delete(secret_ref)
    except Exception:
        pass  # secret may already be absent; deletion succeeded

    return ActionResult(ok=True, summary=f"Deleted credential {name!r}")


DELETE_CREDENTIAL = ActionSpec(
    name="delete_credential",
    description="Permanently delete a credential profile and its stored secret.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Name of the credential to delete."},
    ],
    runner=_delete_credential_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(DELETE_CREDENTIAL)


# ---- enable/disable monitoring source ------------------------------------


async def _toggle_monitoring_runner(cfg: Config, args: dict, *, enable: bool) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, MonitoringSource

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        src = s.execute(select(MonitoringSource).where(MonitoringSource.name == name)).scalar_one_or_none()
        if src is None:
            return ActionResult(ok=False, summary=f"Monitoring source {name!r} not found")
        src.enabled = enable
        s.add(AuditLog(action=f"monitoring.source.{'enable' if enable else 'disable'}", target=f"source:{src.id}", details="agent"))

    verb = "Enabled" if enable else "Disabled"
    return ActionResult(ok=True, summary=f"{verb} monitoring source {name!r}", extra={"source": name, "enabled": enable})


# ---- delete_host ---------------------------------------------------------


async def _delete_host_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, Host

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        host = s.execute(select(Host).where(Host.name == name)).scalar_one_or_none()
        if host is None:
            return ActionResult(ok=False, summary=f"Host {name!r} not found")
        host_id = host.id
        s.delete(host)
        s.add(AuditLog(action="host.delete", target=f"host:{host_id}", details="agent"))

    return ActionResult(ok=True, summary=f"Deleted host {name!r}")


DELETE_HOST = ActionSpec(
    name="delete_host",
    description="Permanently remove a host from the inventory.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Exact host name to delete."},
    ],
    runner=_delete_host_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(DELETE_HOST)


# ---- create/update/delete monitoring source --------------------------------

_MONITORING_TOOL_CHOICES = {"dynatrace", "datadog", "servicenow", "prometheus"}


async def _create_monitoring_source_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy.exc import IntegrityError
    from dosm.db import session_scope
    from dosm.models import AuditLog, MonitoringSource
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    tool = (args.get("tool") or "").strip()
    url = (args.get("url") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")
    if not tool:
        return ActionResult(ok=False, summary="tool is required")
    if tool not in _MONITORING_TOOL_CHOICES:
        return ActionResult(ok=False, summary=f"unknown tool {tool!r}; valid: {', '.join(sorted(_MONITORING_TOOL_CHOICES))}")
    if not url:
        return ActionResult(ok=False, summary="url is required")

    username = (args.get("username") or "").strip() or None
    token = (args.get("token") or "").strip()
    token2 = (args.get("token2") or "").strip()
    enabled = bool(args.get("enabled", True))

    with session_scope() as s:
        src = MonitoringSource(name=name, tool=tool, url=url, username=username, enabled=enabled)
        s.add(src)
        try:
            s.flush()
        except IntegrityError as e:
            return ActionResult(ok=False, summary=f"Duplicate name: {e.__cause__ or e}", stderr=str(e))
        sid = src.id
        s.add(AuditLog(action="monitoring_source.create", target=f"source:{sid}", details=f"agent tool={tool}"))

    backend = get_backend(cfg)
    try:
        if token:
            path = f"monitoring/{sid}/token"
            backend.set_str(path, token)
            with session_scope() as s2:
                src2 = s2.get(MonitoringSource, sid)
                if src2:
                    src2.token_secret = path
        if token2:
            path2 = f"monitoring/{sid}/token2"
            backend.set_str(path2, token2)
            with session_scope() as s2:
                src2 = s2.get(MonitoringSource, sid)
                if src2:
                    src2.token2_secret = path2
    except Exception as e:
        return ActionResult(ok=False, summary=f"Source row created (id={sid}) but secret write failed: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Created monitoring source {name!r} (id={sid})", extra={"source_id": sid, "source": name})


CREATE_MONITORING_SOURCE = ActionSpec(
    name="create_monitoring_source",
    description="Register a new monitoring source (Dynatrace, Datadog, ServiceNow, Prometheus). Secrets are stored in the secrets backend.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Unique display name."},
        {"name": "tool", "type": "string", "required": True, "description": "Tool type: dynatrace, datadog, servicenow, or prometheus."},
        {"name": "url", "type": "string", "required": True, "description": "Base URL (Dynatrace env URL, Datadog site like datadoghq.com, ServiceNow instance URL, or Prometheus base URL)."},
        {"name": "username", "type": "string", "required": False, "description": "Username (ServiceNow only)."},
        {"name": "token", "type": "secret", "required": False, "description": "Primary API token — entered by operator in plan card."},
        {"name": "token2", "type": "secret", "required": False, "description": "Secondary token (Datadog app key) — entered by operator in plan card."},
        {"name": "enabled", "type": "boolean", "required": False, "description": "Start enabled (default true)."},
    ],
    runner=_create_monitoring_source_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(CREATE_MONITORING_SOURCE)


async def _update_monitoring_source_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, MonitoringSource
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        src = s.execute(select(MonitoringSource).where(MonitoringSource.name == name)).scalar_one_or_none()
        if src is None:
            return ActionResult(ok=False, summary=f"Monitoring source {name!r} not found")

        if "tool" in args:
            new_tool = (args["tool"] or "").strip()
            if new_tool not in _MONITORING_TOOL_CHOICES:
                return ActionResult(ok=False, summary=f"unknown tool {new_tool!r}")
            src.tool = new_tool
        if "url" in args:
            src.url = (args["url"] or "").strip()
        if "username" in args:
            src.username = (args["username"] or "").strip() or None
        if "enabled" in args:
            src.enabled = bool(args["enabled"])

        sid = src.id
        token_secret = src.token_secret
        token2_secret = src.token2_secret
        s.add(AuditLog(action="monitoring_source.update", target=f"source:{sid}", details=f"agent name={name}"))

    token = (args.get("token") or "").strip()
    token2 = (args.get("token2") or "").strip()
    backend = get_backend(cfg)
    try:
        if token:
            path = token_secret or f"monitoring/{sid}/token"
            backend.set_str(path, token)
            if path != token_secret:
                with session_scope() as s2:
                    src2 = s2.get(MonitoringSource, sid)
                    if src2:
                        src2.token_secret = path
        if token2:
            path2 = token2_secret or f"monitoring/{sid}/token2"
            backend.set_str(path2, token2)
            if path2 != token2_secret:
                with session_scope() as s2:
                    src2 = s2.get(MonitoringSource, sid)
                    if src2:
                        src2.token2_secret = path2
    except Exception as e:
        return ActionResult(ok=False, summary=f"Metadata updated but secret write failed: {e}", stderr=str(e))

    return ActionResult(ok=True, summary=f"Updated monitoring source {name!r}", extra={"source": name})


UPDATE_MONITORING_SOURCE = ActionSpec(
    name="update_monitoring_source",
    description="Update an existing monitoring source. Omit any field to keep its current value. Leave token blank to keep the existing secret.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Existing monitoring source name."},
        {"name": "tool", "type": "string", "required": False, "description": "New tool type (dynatrace, datadog, servicenow, prometheus)."},
        {"name": "url", "type": "string", "required": False, "description": "New base URL."},
        {"name": "username", "type": "string", "required": False, "description": "New username (ServiceNow only)."},
        {"name": "token", "type": "secret", "required": False, "description": "New primary token — leave blank to keep existing."},
        {"name": "token2", "type": "secret", "required": False, "description": "New secondary token — leave blank to keep existing."},
        {"name": "enabled", "type": "boolean", "required": False, "description": "Enable or disable the source."},
    ],
    runner=_update_monitoring_source_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(UPDATE_MONITORING_SOURCE)


async def _delete_monitoring_source_runner(cfg: Config, args: dict) -> ActionResult:
    from sqlalchemy import select
    from dosm.db import session_scope
    from dosm.models import AuditLog, MonitoringSource
    from dosm.secrets import get_backend

    name = (args.get("name") or "").strip()
    if not name:
        return ActionResult(ok=False, summary="name is required")

    with session_scope() as s:
        src = s.execute(select(MonitoringSource).where(MonitoringSource.name == name)).scalar_one_or_none()
        if src is None:
            return ActionResult(ok=False, summary=f"Monitoring source {name!r} not found")
        sid = src.id
        token_paths = [p for p in (src.token_secret, src.token2_secret) if p]
        s.delete(src)
        s.add(AuditLog(action="monitoring_source.delete", target=f"source:{sid}", details=f"agent name={name}"))

    backend = get_backend(cfg)
    for path in token_paths:
        try:
            backend.delete(path)
        except Exception:
            pass

    return ActionResult(ok=True, summary=f"Deleted monitoring source {name!r}", extra={"source": name})


DELETE_MONITORING_SOURCE = ActionSpec(
    name="delete_monitoring_source",
    description="Permanently delete a monitoring source and its stored secrets.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Exact monitoring source name to delete."},
    ],
    runner=_delete_monitoring_source_runner,
    classify=lambda args: "elevated",
    elevated_confirm_field="name",
)
register_action(DELETE_MONITORING_SOURCE)


ENABLE_MONITORING_SOURCE = ActionSpec(
    name="enable_monitoring_source",
    description="Enable a monitoring source (Dynatrace, Datadog, ServiceNow, etc.) by name.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Monitoring source name from /monitoring."},
    ],
    runner=lambda cfg, args: _toggle_monitoring_runner(cfg, args, enable=True),
)
register_action(ENABLE_MONITORING_SOURCE)

DISABLE_MONITORING_SOURCE = ActionSpec(
    name="disable_monitoring_source",
    description="Disable a monitoring source by name.",
    args_schema=[
        {"name": "name", "type": "string", "required": True, "description": "Monitoring source name from /monitoring."},
    ],
    runner=lambda cfg, args: _toggle_monitoring_runner(cfg, args, enable=False),
)
register_action(DISABLE_MONITORING_SOURCE)
