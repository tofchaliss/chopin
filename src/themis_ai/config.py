"""Runtime configuration.

Configuration is loaded from ``<workspace>/.themis-ai.yaml`` (if present) and
merged over the defaults below. Every field has a safe default so the runtime
can operate on a repository that has not been prepared for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = ".themis-ai.yaml"


@dataclass
class OpenAIConfig:
    """Architecture / review side."""

    model: str = "gpt-5"
    # Name of the environment variable holding the API key. The key itself is
    # never written to config or state.
    api_key_env: str = "OPENAI_API_KEY"
    # Character budget for repository context sent to the architect.
    context_budget_chars: int = 120_000
    # Character budget for diffs sent to reviewers.
    diff_budget_chars: int = 150_000
    timeout_seconds: int = 600


@dataclass
class ClaudeConfig:
    """Implementation side (Claude Code, headless)."""

    binary: str = "claude"
    model: str | None = None
    permission_mode: str = "acceptEdits"
    # Claude edits the tree; the orchestrator owns every git write. Claude is
    # therefore allowed to read/edit/run builds and tests, but not touch git
    # history or the network.
    allowed_tools: list[str] = field(
        default_factory=lambda: [
            "Read", "Edit", "Write", "Glob", "Grep",
            "Bash(go build:*)", "Bash(go test:*)", "Bash(go vet:*)", "Bash(gofmt:*)",
            "Bash(make test:*)", "Bash(make lint:*)", "Bash(make build:*)",
            "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
            "Bash(ls:*)", "Bash(cat:*)",
        ]
    )
    disallowed_tools: list[str] = field(
        default_factory=lambda: [
            "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)",
            "Bash(git reset:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
            "Bash(git branch:*)", "Bash(rm -rf:*)", "Bash(curl:*)", "Bash(wget:*)",
            "WebFetch", "WebSearch",
        ]
    )
    # Resume the same Claude session across fix iterations so it keeps context.
    resume_session: bool = True
    # Optional MCP config handed to Claude Code (e.g. one that starts themis-ai-mcp).
    mcp_config: str | None = None
    timeout_seconds: int = 1800


@dataclass
class CheckConfig:
    """A deterministic check run by the orchestrator (not by an agent)."""

    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 900
    # Tail of output passed back to Claude on failure.
    output_tail_chars: int = 20_000


@dataclass
class WorkflowConfig:
    base_branch: str = "main"
    branch_prefix: str = "agent/"
    # Total fix cycles (test failures + review rejections) before escalating.
    max_iterations: int = 3
    # Escalate to a human once the task's combined agent spend exceeds this.
    max_cost_usd: float = 25.0
    # Pause after the architecture spec for a human approve/reject.
    require_design_approval: bool = False
    # Also pause when the architect reports open questions (e.g. it could not
    # find a referenced module in the repository).
    pause_on_open_questions: bool = True
    # Enable/disable individual review gates.
    security_review: bool = True
    final_review: bool = True
    # Reviewer findings at or above this severity block, whatever the verdict.
    block_on_severity: str = "high"
    # What happens after all gates pass:
    #   commit - commit on the feature branch (no approval needed)
    #   merge  - also merge into base_branch (approval required)
    #   push   - also push the feature branch to the remote (approval required)
    delivery: str = "commit"
    remote: str = "origin"
    # Repository files given to the architect as context (globs, in order).
    context_globs: list[str] = field(
        default_factory=lambda: [
            "README.md", "CLAUDE.md", "AGENTS.md",
            "docs/**/*.md", "openspec/**/*.md",
        ]
    )


@dataclass
class GuardrailConfig:
    allow: list[str] = field(
        default_factory=lambda: [
            "read_file", "list_files", "search_code", "write_file",
            "run_tests", "run_lint", "git_status", "git_diff", "git_log",
            "git_branch", "git_commit", "write_docs", "themis_read",
        ]
    )
    require_approval: list[str] = field(
        default_factory=lambda: [
            "git_push", "git_merge", "delete_branch", "delete_data",
            "deploy", "modify_credentials", "modify_infrastructure",
            "approve_design",
        ]
    )
    deny: list[str] = field(
        default_factory=lambda: ["git_force_push", "git_reset_hard", "git_rewrite_history"]
    )
    # Writes to these paths are denied regardless of the action policy.
    protected_paths: list[str] = field(
        default_factory=lambda: [
            ".git", ".git/**", "agent-state/**", ".themis-ai.yaml",
            "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*",
            "**/secrets/**", "**/credentials*",
        ]
    )


@dataclass
class ThemisConfig:
    """Read-only access to a running Themis instance (MCP domain tools)."""

    base_url: str = "http://localhost:8080/api/v1"
    api_key_env: str = "THEMIS_API_KEY"
    timeout_seconds: int = 30


@dataclass
class RuntimeConfig:
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    tests: CheckConfig = field(default_factory=lambda: CheckConfig(command=["make", "test"]))
    lint: CheckConfig = field(default_factory=CheckConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)
    themis: ThemisConfig = field(default_factory=ThemisConfig)

    @classmethod
    def load(cls, workspace: Path, path: Path | None = None) -> "RuntimeConfig":
        cfg_path = path or (workspace / CONFIG_FILENAME)
        data: dict[str, Any] = {}
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text()) or {}
        cfg = _merge(cls(), data)
        if model := os.environ.get("THEMIS_AI_OPENAI_MODEL"):
            cfg.openai.model = model
        return cfg


def _merge(obj: Any, data: dict[str, Any]) -> Any:
    """Recursively apply a plain dict onto a dataclass instance."""
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {type(obj).__name__}.{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config key {key} must be a mapping")
            _merge(current, value)
        else:
            setattr(obj, key, value)
    return obj
