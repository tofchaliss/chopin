from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from themis_ai.agents.base import ReviewKind
from themis_ai.agents.claude_code import ClaudeCodeImplementer
from themis_ai.agents.openai_agent import OpenAIArchitectReviewer
from themis_ai.config import ClaudeConfig, GuardrailConfig, OpenAIConfig, RuntimeConfig
from themis_ai.guardrails import ApprovalRequired, Decision, GuardrailViolation, Guardrails
from themis_ai.mcp_server import Toolset
from themis_ai.state import InvalidTransition, StateManager, Task, TaskState

from .conftest import make_spec


# -- guardrails -------------------------------------------------------------
@pytest.fixture
def guard(tmp_path):
    events = []
    return Guardrails(GuardrailConfig(), tmp_path, audit=events.append), events


@pytest.mark.parametrize("action,path,expected", [
    ("write_file", "internal/kn/module.go", Decision.ALLOW),
    ("write_file", ".env", Decision.DENY),
    ("write_file", "deploy/secrets/db.yaml", Decision.DENY),
    ("write_file", "certs/server.pem", Decision.DENY),
    ("write_file", ".git/config", Decision.DENY),
    ("write_file", "agent-state/tasks/x.json", Decision.DENY),
    ("write_file", "../outside.txt", Decision.DENY),
    ("git_push", None, Decision.APPROVAL),
    ("deploy", None, Decision.APPROVAL),
    ("git_force_push", None, Decision.DENY),
    ("something_new", None, Decision.APPROVAL),
    ("run_tests", None, Decision.ALLOW),
])
def test_guardrail_policy(guard, action, path, expected):
    g, events = guard
    assert g.check(action, path=path).decision is expected
    assert events[-1]["action"] == action


def test_guardrail_enforce_and_approval(guard):
    g, _ = guard
    with pytest.raises(ApprovalRequired):
        g.enforce("git_merge")
    assert g.enforce("git_merge", approved=True).allowed
    with pytest.raises(GuardrailViolation):
        g.enforce("git_reset_hard", approved=True)


# -- state ------------------------------------------------------------------
def test_state_machine_rejects_illegal_transitions():
    task = Task.create("x", "main", "agent/")
    with pytest.raises(InvalidTransition):
        task.transition(TaskState.APPROVED)
    task.transition(TaskState.ANALYZING)
    task.transition(TaskState.ABORTED)
    with pytest.raises(InvalidTransition):
        task.transition(TaskState.ANALYZING)


def test_state_round_trip(tmp_path):
    sm = StateManager(tmp_path)
    task = Task.create("Implement KN-MODULE-4", "main", "agent/")
    task.transition(TaskState.ANALYZING)
    task.spec = {"summary": "s"}
    sm.save(task)
    loaded = sm.load()
    assert loaded.to_dict() == task.to_dict()
    assert loaded.branch.startswith("agent/implement-kn-module-4-")
    assert sm.history()[0]["state"] == "ANALYZING"


def test_config_loads_yaml_and_rejects_unknown_keys(tmp_path):
    (tmp_path / ".themis-ai.yaml").write_text(
        "tests:\n  command: [go, test, ./...]\nworkflow:\n  max_iterations: 5\n")
    cfg = RuntimeConfig.load(tmp_path)
    assert cfg.tests.command == ["go", "test", "./..."]
    assert cfg.workflow.max_iterations == 5
    assert cfg.workflow.base_branch == "main"
    (tmp_path / ".themis-ai.yaml").write_text("workflow:\n  max_iteratons: 5\n")
    with pytest.raises(ValueError, match="max_iteratons"):
        RuntimeConfig.load(tmp_path)


# -- Claude Code adapter ------------------------------------------------------
def test_claude_command_and_parse(tmp_path):
    seen = {}

    def runner(cmd, cwd, timeout):
        seen["cmd"] = cmd
        out = json.dumps({"type": "result", "result": "implemented", "is_error": False,
                          "session_id": "abc", "total_cost_usd": 1.25,
                          "usage": {"input_tokens": 10, "output_tokens": 20}})
        return subprocess.CompletedProcess(cmd, 0, out, "")

    impl = ClaudeCodeImplementer(ClaudeConfig(), tmp_path, runner=runner)
    res = impl.implement("do it", make_spec(), feedback="tests failed", session_id="prev")
    cmd = seen["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert "tests failed" in cmd[2]
    assert cmd[cmd.index("--resume") + 1] == "prev"
    assert "Bash(git push:*)" in cmd[cmd.index("--disallowedTools"):]
    assert res.ok and res.session_id == "abc" and res.usage.cost_usd == 1.25


def test_claude_error_result(tmp_path):
    runner = lambda cmd, cwd, t: subprocess.CompletedProcess(cmd, 1, "", "auth failed")  # noqa: E731
    res = ClaudeCodeImplementer(ClaudeConfig(), tmp_path, runner=runner).implement("x", make_spec())
    assert not res.ok and "auth failed" in res.summary


# -- OpenAI adapter -----------------------------------------------------------
class FakeResponses:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.payloads.pop(0)),
                               usage=SimpleNamespace(input_tokens=7, output_tokens=3))


def test_openai_design_and_review():
    spec_payload = {k: v for k, v in make_spec().__dict__.items() if k != "usage"}
    review_payload = {"verdict": "reject", "summary": "missing tests", "findings": [
        {"severity": "high", "file": "a.go", "message": "no tests", "required_change": "add tests"}]}
    responses = FakeResponses([spec_payload, review_payload])
    agent = OpenAIArchitectReviewer(OpenAIConfig(model="test-model"), client=SimpleNamespace(responses=responses))

    spec = agent.design("Implement KN-MODULE-4", "ctx")
    assert spec.summary == spec_payload["summary"] and spec.usage.input_tokens == 7
    review = agent.review(ReviewKind.SECURITY, "req", spec, "diff", "checks")
    assert not review.accepted and review.blocking("high")[0].file == "a.go"

    first, second = responses.calls
    assert first["model"] == "test-model"
    assert first["text"]["format"]["strict"] is True
    assert "Security Agent" in second["instructions"]


# -- MCP toolset ----------------------------------------------------------------
def test_mcp_toolset_is_policy_checked(repo):
    tools = Toolset(repo, RuntimeConfig())
    assert "KN-MODULE-4" in tools.read_file("README.md")
    assert any("README.md" in hit for hit in tools.search_code("KN-MODULE-4"))
    tools.write_file("internal/kn/x.go", "package kn\n")
    with pytest.raises(GuardrailViolation):
        tools.write_file(".env", "SECRET=1")
    with pytest.raises(GuardrailViolation):
        tools.read_file("../../etc/passwd")
    audit = (repo / "agent-state" / "audit.log").read_text().splitlines()
    assert any('"actor": "mcp"' in line and '"deny"' in line for line in audit)
