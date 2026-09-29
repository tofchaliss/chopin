"""Claude Code implementation agent, driven headless via ``claude -p``.

Claude edits the working tree with a restricted tool allow-list. It cannot
write git history, push, or reach the network; the orchestrator commits its
changes and hands them to the independent reviewers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from ..config import ClaudeConfig
from . import prompts
from .base import ImplementationResult, Spec, Usage

Runner = Callable[[list[str], Path, int], subprocess.CompletedProcess]


def _default_runner(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


class ClaudeCodeImplementer:
    def __init__(self, cfg: ClaudeConfig, workspace: Path, runner: Runner | None = None,
                 mcp_config: Path | None = None):
        self.cfg = cfg
        self.workspace = workspace
        self.runner = runner or _default_runner
        self.mcp_config = mcp_config

    def build_command(self, prompt: str, session_id: str | None) -> list[str]:
        cmd = [
            self.cfg.binary, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", self.cfg.permission_mode,
            "--append-system-prompt", prompts.IMPLEMENTER,
        ]
        if self.cfg.allowed_tools:
            cmd += ["--allowedTools", *self.cfg.allowed_tools]
        if self.cfg.disallowed_tools:
            cmd += ["--disallowedTools", *self.cfg.disallowed_tools]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.mcp_config:
            cmd += ["--mcp-config", str(self.mcp_config)]
        if session_id and self.cfg.resume_session:
            cmd += ["--resume", session_id]
        return cmd

    def implement(self, request: str, spec: Spec, *, feedback: str | None = None,
                  session_id: str | None = None) -> ImplementationResult:
        if feedback:
            prompt = (
                f"The previous iteration of this task did not pass. Fix the issues below, "
                f"re-run the relevant tests, and summarise what you changed.\n\n"
                f"## Task\n\n{request}\n\n## Feedback\n\n{feedback}\n\n"
                + ("" if session_id and self.cfg.resume_session else spec.to_markdown())
            )
        else:
            prompt = (f"Implement the following task in this repository.\n\n## Task\n\n{request}\n\n"
                      f"{spec.to_markdown()}")
        try:
            proc = self.runner(self.build_command(prompt, session_id), self.workspace,
                               self.cfg.timeout_seconds)
        except subprocess.TimeoutExpired:
            return ImplementationResult(False, f"Claude Code timed out after {self.cfg.timeout_seconds}s")
        except FileNotFoundError:
            return ImplementationResult(False, f"Claude Code binary not found: {self.cfg.binary}")
        return self._parse(proc)

    @staticmethod
    def _parse(proc: subprocess.CompletedProcess) -> ImplementationResult:
        raw = proc.stdout or ""
        try:
            data = json.loads(raw.strip().splitlines()[-1]) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        if not data:
            return ImplementationResult(False, f"Claude Code exited {proc.returncode}: "
                                               f"{(proc.stderr or raw)[-2000:]}", raw=raw)
        usage = Usage(
            input_tokens=(data.get("usage") or {}).get("input_tokens", 0) or 0,
            output_tokens=(data.get("usage") or {}).get("output_tokens", 0) or 0,
            cost_usd=float(data.get("total_cost_usd") or 0.0),
        )
        ok = proc.returncode == 0 and not data.get("is_error", False)
        return ImplementationResult(ok, str(data.get("result", "")), data.get("session_id"), usage, raw)
