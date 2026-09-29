from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from themis_ai.agents.base import ReviewKind
from themis_ai.agents.claude_code import ClaudeCodeImplementer
from themis_ai.agents.openai_agent import OpenAIArchitectReviewer
from themis_ai.config import ClaudeConfig, GuardrailConfig, OpenAIConfig, RuntimeConfig, resolve_state_dir
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
def test_mcp_toolset_is_policy_checked(repo, config, state_dir):
    tools = Toolset(repo, config)
    assert "KN-MODULE-4" in tools.read_file("README.md")
    assert any("README.md" in hit for hit in tools.search_code("KN-MODULE-4"))
    tools.write_file("internal/kn/x.go", "package kn\n")
    with pytest.raises(GuardrailViolation):
        tools.write_file(".env", "SECRET=1")
    with pytest.raises(GuardrailViolation):
        tools.read_file("../../etc/passwd")
    audit = (state_dir / "audit.log").read_text().splitlines()
    assert any('"actor": "mcp"' in line and '"deny"' in line for line in audit)
    assert not (repo / "agent-state").exists()


def test_mcp_themis_tools_route_to_greenfield_services(repo, config, monkeypatch):
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("X-api-key")))
        return Resp(b'{"ok": true}')

    monkeypatch.setattr("themis_ai.mcp_server.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("THEMIS_API_KEY", "k-123")
    config.themis.services["knowledge"] = "http://kn.example:9000/api/v1"
    tools = Toolset(repo, config)

    assert tools.get_release("r/1") == {"ok": True}
    tools.list_findings(release_id="r1")
    tools.find_vulnerability("CVE-2024-1234")
    tools.get_sbom_inventory("e1")
    assert seen == [
        ("http://localhost:8082/api/v1/releases/r%2F1", "k-123"),
        ("http://localhost:8083/api/v1/findings?release=r1", "k-123"),
        ("http://kn.example:9000/api/v1/faultlines?cve=CVE-2024-1234", "k-123"),
        ("http://localhost:8081/api/v1/evidence/e1/inventory", "k-123"),
    ]
    config.themis.services.pop("governance")
    with pytest.raises(ValueError, match="governance"):
        tools.get_finding("f1")


def test_state_dir_resolution(tmp_path, monkeypatch):
    ws = tmp_path / "themis"
    cfg = RuntimeConfig()
    monkeypatch.setenv("CHOPIN_HOME", str(tmp_path / "home"))
    assert resolve_state_dir(cfg, ws) == (tmp_path / "home" / "themis").resolve()
    monkeypatch.delenv("CHOPIN_HOME")
    assert resolve_state_dir(cfg, ws) == (Path.home() / ".chopin" / "themis").resolve()
    cfg.workflow.state_dir = str(tmp_path / "explicit")
    assert resolve_state_dir(cfg, ws) == (tmp_path / "explicit").resolve()


def test_config_explicit_path_and_service_merge(tmp_path):
    with pytest.raises(FileNotFoundError):
        RuntimeConfig.load(tmp_path, tmp_path / "missing.yaml")
    target = tmp_path / "themis.yaml"
    target.write_text("themis:\n  services:\n    registry: http://reg:1/api/v1\n")
    cfg = RuntimeConfig.load(tmp_path, target)
    assert cfg.themis.services["registry"] == "http://reg:1/api/v1"
    assert cfg.themis.services["governance"] == "http://localhost:8083/api/v1"


TARGETS = Path(__file__).resolve().parent.parent / "config" / "targets"


def test_shipped_themis_target_config_loads(tmp_path):
    cfg = RuntimeConfig.load(tmp_path, TARGETS / "themis.yaml")
    assert cfg.target.name == "themis"
    assert "internal/<context>/{domain,app,adapters}" in cfg.target.notes()
    assert cfg.tests.command == ["make", "check"]
    assert "CLAUDE.md" in cfg.workflow.context_globs
    assert cfg.themis.services["knowledge"].endswith(":8085/api/v1")


def test_shipped_harness_target_config_loads(tmp_path):
    cfg = RuntimeConfig.load(tmp_path, TARGETS / "themis-ai-runtime.yaml")
    assert cfg.target.name == "themis-ai-runtime"
    notes = cfg.target.notes()
    assert "DAY-0" in notes and "G2" in notes and "Themis owns" in notes
    assert cfg.workflow.require_design_approval is True
    assert cfg.workflow.delivery == "commit"
    assert "go test ./src/harness/..." in cfg.tests.command[-1]
    assert ".claude/policy/DAY-0.md" in cfg.workflow.context_globs


def test_target_notes_resolve_relative_to_config_and_must_exist(tmp_path):
    (tmp_path / "rules.md").write_text("# rules\nno legacy edits\n")
    target = tmp_path / "t.yaml"
    target.write_text("target:\n  name: x\n  notes_file: rules.md\n")
    cfg = RuntimeConfig.load(tmp_path / "elsewhere", target)
    assert cfg.target.notes_file == str((tmp_path / "rules.md").resolve())
    assert "no legacy edits" in cfg.target.notes()
    target.write_text("target:\n  notes_file: missing.md\n")
    with pytest.raises(FileNotFoundError, match="notes_file"):
        RuntimeConfig.load(tmp_path, target)


def test_target_notes_reach_every_openai_role():
    spec_payload = {k: v for k, v in make_spec().__dict__.items() if k != "usage"}
    review_payload = {"verdict": "accept", "summary": "ok", "findings": []}
    responses = FakeResponses([spec_payload, review_payload])
    agent = OpenAIArchitectReviewer(OpenAIConfig(), client=SimpleNamespace(responses=responses),
                                    target_notes="RULE-XYZ: never touch the frozen tree")
    spec = agent.design("req", "ctx")
    agent.review(ReviewKind.CODE, "req", spec, "diff", "checks")
    assert all("RULE-XYZ" in call["instructions"] for call in responses.calls)
    bare = OpenAIArchitectReviewer(OpenAIConfig(), client=SimpleNamespace(responses=FakeResponses([spec_payload])))
    bare.design("req", "ctx")
    assert "Target architecture notes" not in bare.client.responses.calls[0]["instructions"]


@pytest.mark.parametrize("rel,pattern,expected", [
    ("README.md", "README.md", True),
    ("docs/README.md", "README.md", False),
    ("docs/README.md", "**/README.md", True),
    ("README.md", "**/README.md", True),
    ("openspec/changes/phase3-a/tasks.md", "openspec/changes/phase3-*/tasks.md", True),
    ("openspec/changes/archive/phase3-a/tasks.md", "openspec/changes/phase3-*/tasks.md", False),
])
def test_context_globs_are_anchored_at_the_root(rel, pattern, expected):
    from themis_ai.workspace import glob_match
    assert glob_match(rel, pattern) is expected


def test_claude_runner_strips_nested_session_marker(tmp_path, monkeypatch):
    from themis_ai.agents import claude_code

    seen = {}
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda cmd, **kw: seen.update(kw) or subprocess.CompletedProcess(cmd, 0, "", ""))
    claude_code._default_runner(["claude"], tmp_path, 5)
    assert "CLAUDECODE" not in seen["env"]
