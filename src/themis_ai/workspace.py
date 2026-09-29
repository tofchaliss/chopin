"""Shared workspace capabilities: files, search, deterministic checks.

These are the functions behind the MCP layer. The orchestrator calls them
in-process; external agents reach them through ``themis_ai.mcp_server``.
Both paths pass through the same guardrails.
"""

from __future__ import annotations

import fnmatch
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CheckConfig
from .guardrails import Guardrails

MAX_READ_BYTES = 512_000


@dataclass
class CheckResult:
    name: str
    command: list[str]
    passed: bool
    exit_code: int | None
    duration_seconds: float
    output_tail: str
    skipped: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        if self.skipped:
            return f"### {self.name}: skipped (no command configured)\n"
        status = "PASS" if self.passed else "FAIL"
        return (f"### {self.name}: {status}\n\n"
                f"- command: `{' '.join(self.command)}`\n- exit code: {self.exit_code}\n"
                f"- duration: {self.duration_seconds:.1f}s\n\n```\n{self.output_tail}\n```\n")


def glob_match(rel: str, pattern: str) -> bool:
    """Match a repository-relative path against a glob anchored at the root.

    ``README.md`` names only the root file; ``**/README.md`` names every one.
    """
    if fnmatch.fnmatchcase(rel, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:])


class Workspace:
    def __init__(self, root: Path, guardrails: Guardrails):
        self.root = root.resolve()
        self.guard = guardrails

    # -- files -------------------------------------------------------------
    def read_file(self, path: str, *, actor: str = "orchestrator") -> str:
        self.guard.enforce("read_file", actor=actor)
        p = self.guard.resolve_path(path)
        data = p.read_bytes()[:MAX_READ_BYTES]
        return data.decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str, *, actor: str = "orchestrator") -> None:
        self.guard.enforce("write_file", path=path, actor=actor)
        p = self.guard.resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def list_files(self, pattern: str = "**/*", *, limit: int = 2000, actor: str = "orchestrator") -> list[str]:
        """Tracked + untracked (non-ignored) files, filtered by glob."""
        self.guard.enforce("list_files", actor=actor)
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=self.root, capture_output=True, text=True,
        ).stdout.splitlines()
        files = sorted({f for f in out if pattern in ("**/*", "*") or glob_match(f, pattern)})
        return files[:limit]

    def search_code(self, query: str, *, glob: str | None = None, limit: int = 200,
                    actor: str = "orchestrator") -> list[str]:
        self.guard.enforce("search_code", actor=actor)
        cmd = ["git", "grep", "-n", "-I", "--untracked", "-e", query]
        if glob:
            cmd += ["--", glob]
        out = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True).stdout
        return out.splitlines()[:limit]

    # -- checks ------------------------------------------------------------
    def run_check(self, name: str, check: CheckConfig, *, actor: str = "orchestrator") -> CheckResult:
        action = "run_tests" if name == "tests" else "run_lint"
        self.guard.enforce(action, actor=actor)
        if not check.command:
            return CheckResult(name, [], True, None, 0.0, "", skipped=True)
        start = time.monotonic()
        try:
            proc = subprocess.run(check.command, cwd=self.root, capture_output=True, text=True,
                                  timeout=check.timeout_seconds)
            output, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            output = (e.stdout or "") if isinstance(e.stdout, str) else ""
            output += f"\n[timed out after {check.timeout_seconds}s]"
            code = None
        except FileNotFoundError as e:
            output, code = f"[command not found: {e}]", None
        return CheckResult(
            name=name,
            command=list(check.command),
            passed=code == 0,
            exit_code=code,
            duration_seconds=time.monotonic() - start,
            output_tail=output[-check.output_tail_chars:],
        )
