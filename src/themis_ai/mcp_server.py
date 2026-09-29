"""Themis MCP server: controlled capabilities for any agent.

Agents reason; MCP provides capabilities; the orchestrator controls the
workflow. Every tool here runs through the same guardrails as the
orchestrator, so an agent connected over MCP cannot do more than policy allows.
Actions that need a human (push, merge, ...) are not exposed at all.

Run:  themis-ai-mcp --workspace /opt/themis          (stdio transport)
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import RuntimeConfig, ThemisConfig
from .gitops import GitError, LocalGit
from .guardrails import GuardrailViolation, Guardrails
from .state import StateManager
from .workspace import Workspace

ACTOR = "mcp"


class ThemisClient:
    """Read-only client for a running Themis API."""

    def __init__(self, cfg: ThemisConfig, guard: Guardrails):
        self.cfg = cfg
        self.guard = guard

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.guard.enforce("themis_read", actor=ACTOR)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if key := os.environ.get(self.cfg.api_key_env):
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:  # noqa: S310 - configured URL
            return json.loads(resp.read().decode() or "null")


class Toolset:
    """Plain-Python tool implementations (testable without an MCP runtime)."""

    def __init__(self, workspace: Path, cfg: RuntimeConfig):
        self.root = workspace.resolve()
        self.cfg = cfg
        self.state = StateManager(self.root)
        self.guard = Guardrails(cfg.guardrails, self.root, audit=self.state.audit)
        self.ws = Workspace(self.root, self.guard)
        self.git = LocalGit(self.root, self.guard)
        self.themis = ThemisClient(cfg.themis, self.guard)
        if self.git.is_repo():
            self.git.ensure_local_excludes()

    # Repository
    def read_file(self, path: str) -> str:
        return self.ws.read_file(path, actor=ACTOR)

    def write_file(self, path: str, content: str) -> str:
        self.ws.write_file(path, content, actor=ACTOR)
        return f"wrote {len(content)} characters to {path}"

    def list_files(self, pattern: str = "**/*") -> list[str]:
        return self.ws.list_files(pattern, actor=ACTOR)

    def search_code(self, query: str, glob: str | None = None) -> list[str]:
        return self.ws.search_code(query, glob=glob, actor=ACTOR)

    # Analysis
    def run_tests(self) -> dict:
        return self.ws.run_check("tests", self.cfg.tests, actor=ACTOR).to_dict()

    def run_lint(self) -> dict:
        return self.ws.run_check("lint", self.cfg.lint, actor=ACTOR).to_dict()

    # Git (read-only over MCP; the orchestrator owns history)
    def git_status(self) -> str:
        return self.git.status() or "(clean)"

    def git_diff(self, base: str | None = None) -> str:
        return self.git.diff(base or self.cfg.workflow.base_branch)

    def git_log(self, n: int = 20) -> str:
        return self.git.log(n)

    # Orchestrator state
    def task_status(self, task_id: str | None = None) -> dict:
        self.guard.enforce("read_file", actor=ACTOR)
        return self.state.load(task_id).to_dict()

    # Themis domain (read-only)
    def list_products(self) -> Any:
        return self.themis.get("products")

    def get_product(self, product_id: str) -> Any:
        return self.themis.get(f"products/{urllib.parse.quote(product_id)}")

    def get_scan(self, scan_id: str) -> Any:
        return self.themis.get(f"scans/{urllib.parse.quote(scan_id)}")

    def list_components(self, purl_prefix: str = "", limit: int = 100) -> Any:
        return self.themis.get("components", {"purl_prefix": purl_prefix, "limit": limit})

    def get_notification_rules(self) -> Any:
        return self.themis.get("config/notifications")


TOOLS = [
    ("read_file", "Read a workspace file (path relative to the repository root)."),
    ("write_file", "Write a workspace file. Protected paths (secrets, .git, runtime state) are refused."),
    ("list_files", "List tracked and untracked files matching a glob."),
    ("search_code", "Search the repository (git grep). Optional glob narrows the paths."),
    ("run_tests", "Run the configured test command and return the result."),
    ("run_lint", "Run the configured lint command and return the result."),
    ("git_status", "Working tree status (porcelain)."),
    ("git_diff", "Diff of HEAD against a base branch (default: configured base)."),
    ("git_log", "Recent commits."),
    ("task_status", "State of an orchestrator task (default: current task)."),
    ("list_products", "Themis: list products."),
    ("get_product", "Themis: get a product by id."),
    ("get_scan", "Themis: get a scan by id."),
    ("list_components", "Themis: list components, optionally filtered by purl prefix."),
    ("get_notification_rules", "Themis: current notification rules."),
]


def build_server(workspace: Path, cfg: RuntimeConfig):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    tools = Toolset(workspace, cfg)
    server = MCPServer(
        name="themis",
        instructions="Capabilities over the Themis workspace and a running Themis instance. "
                     "All calls are policy-checked and audited.",
    )
    for name, description in TOOLS:
        server.tool(name=name, description=description)(_explained(getattr(tools, name), ToolError))
    return server


def _explained(fn, tool_error: type[Exception]):
    """Surface policy and lookup failures to the agent as readable tool errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (GuardrailViolation, FileNotFoundError, IsADirectoryError, GitError) as e:
            raise tool_error(str(e)) from e

    return wrapper


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="themis-ai-mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    args = parser.parse_args(argv)
    cfg = RuntimeConfig.load(args.workspace)
    build_server(args.workspace, cfg).run(transport=args.transport)


if __name__ == "__main__":
    main()
