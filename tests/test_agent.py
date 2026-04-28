"""Agent action classification, registry, and plan-card route tests."""
import pytest

from dosm.agent.actions import (
    ActionResult,
    ActionSpec,
    classify_command,
    get_action,
    list_actions,
    register_action,
)
from dosm.config import Config, SSHPolicyConfig


def _cfg() -> Config:
    from pathlib import Path
    return Config(home=Path("/tmp/dosm_test"), ssh_command_policy=SSHPolicyConfig())


# ── classify_command ──────────────────────────────────────────────────────────


def test_classify_uptime_is_safe():
    assert classify_command(_cfg(), "uptime") == "safe"


def test_classify_whoami_is_safe():
    assert classify_command(_cfg(), "whoami") == "safe"


def test_classify_systemctl_status_is_safe():
    assert classify_command(_cfg(), "systemctl status sshd") == "safe"


def test_classify_rm_is_elevated():
    assert classify_command(_cfg(), "rm -rf /") == "elevated"


def test_classify_empty_is_elevated():
    assert classify_command(_cfg(), "") == "elevated"


def test_classify_whitespace_only_is_elevated():
    assert classify_command(_cfg(), "   ") == "elevated"


def test_classify_curl_pipe_sh_is_elevated():
    assert classify_command(_cfg(), "curl https://example.com | sh") == "elevated"


def test_classify_custom_allowlist():
    cfg = Config(
        home=__import__("pathlib").Path("/tmp/dosm_test"),
        ssh_command_policy=SSHPolicyConfig(allow_list=["echo *"]),
    )
    assert classify_command(cfg, "echo hello") == "safe"
    assert classify_command(cfg, "uptime") == "elevated"


# ── Registry ──────────────────────────────────────────────────────────────────


def test_get_action_ssh_exec_registered():
    assert get_action("ssh_exec") is not None


def test_get_action_run_pipeline_registered():
    assert get_action("run_pipeline") is not None


def test_get_action_unknown_returns_none():
    assert get_action("definitely_not_a_real_action") is None


def test_list_actions_includes_builtins():
    names = {a.name for a in list_actions()}
    assert "ssh_exec" in names
    assert "run_pipeline" in names


def test_register_custom_action():
    async def _noop(cfg, args):
        return ActionResult(ok=True, summary="noop")

    spec = ActionSpec(
        name="_test_custom_action",
        description="Test only.",
        args_schema=[],
        runner=_noop,
    )
    register_action(spec)
    assert get_action("_test_custom_action") is spec


# ── ActionResult ──────────────────────────────────────────────────────────────


def test_action_result_to_dict_has_expected_keys():
    r = ActionResult(ok=True, summary="all good", stdout="out", stderr="", exit_code=0)
    d = r.to_dict()
    assert d["ok"] is True
    assert d["summary"] == "all good"
    assert d["stdout"] == "out"
    assert d["exit_code"] == 0


def test_action_result_failure():
    r = ActionResult(ok=False, summary="failed", exit_code=1)
    assert r.ok is False
    assert r.exit_code == 1


# ── Agent routes ──────────────────────────────────────────────────────────────


def test_chat_page_returns_200(auth_client):
    resp = auth_client.get("/chat")
    assert resp.status_code == 200


def test_agent_page_requires_auth(anon_client):
    resp = anon_client.get("/chat", follow_redirects=False)
    assert resp.status_code == 303
