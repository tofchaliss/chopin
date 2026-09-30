"""Local Git: branching, commits, diffs, rollback. GitHub is optional (push only).

Every write goes through guardrails. The orchestrator is the only component
that writes git history; Claude Code only edits the working tree.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .guardrails import Guardrails

PUSH_APPROVED_ENV = "THEMIS_PUSH_APPROVED"


class GitError(RuntimeError):
    pass


class LocalGit:
    def __init__(self, workspace: Path, guardrails: Guardrails):
        self.workspace = workspace
        self.guard = guardrails

    def _git(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True,
                              env={**os.environ, **env} if env else None)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    # -- read --------------------------------------------------------------
    def is_repo(self) -> bool:
        return (self.workspace / ".git").exists()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def branch_exists(self, name: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip() != ""

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def status(self) -> str:
        self.guard.enforce("git_status")
        return self._git("status", "--porcelain")

    def is_clean(self) -> bool:
        return self.status().strip() == ""

    def diff(self, base: str, *, stat: bool = False, exclude: list[str] | None = None) -> str:
        """Committed changes on HEAD since it diverged from base."""
        self.guard.enforce("git_diff")
        args = ["diff", "--no-color", *(["--stat"] if stat else []), f"{base}...HEAD"]
        if exclude:
            args += ["--", ".", *(f":(exclude){p}" for p in exclude)]
        return self._git(*args)

    def log(self, n: int = 20) -> str:
        self.guard.enforce("git_log")
        return self._git("log", f"-{n}", "--oneline", "--decorate")

    # -- write -------------------------------------------------------------
    def create_branch(self, name: str, base: str) -> None:
        self.guard.enforce("git_branch")
        self._git("checkout", "-b", name, base)

    def checkout(self, name: str) -> None:
        self.guard.enforce("git_branch")
        self._git("checkout", name)

    def commit_all(self, message: str, *, actor: str = "orchestrator") -> str | None:
        """Stage everything and commit. Returns the sha, or None if nothing changed."""
        self.guard.enforce("git_commit", actor=actor)
        self._git("add", "-A")
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.workspace)
        if staged.returncode == 0:
            return None
        identity = [] if self._has_identity() else \
            ["-c", "user.name=themis-ai", "-c", "user.email=themis-ai@localhost"]
        self._git(*identity, "commit", "-q", "-m", message)
        return self.head()

    def merge(self, branch: str, into: str, *, approved: bool) -> str:
        self.guard.enforce("git_merge", approved=approved)
        self._git("checkout", into)
        self._git("merge", "--no-ff", "-m", f"Merge {branch}", branch)
        return self.head()

    def github_repo(self, remote: str) -> str | None:
        """``owner/repo`` when the remote points at GitHub, else None."""
        url = self._git("remote", "get-url", remote, check=False).strip()
        return parse_github_repo(url)

    def open_pull_request(self, tool: str, *, base: str, branch: str, title: str, body: str,
                          approved: bool) -> str:
        """Open a PR with the GitHub CLI (or return the existing one); returns its URL."""
        self.guard.enforce("open_pr", approved=approved)
        proc = subprocess.run([tool, "pr", "create", "--base", base, "--head", branch,
                               "--title", title, "--body-file", "-"],
                              cwd=self.workspace, input=body, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[-1]
        existing = subprocess.run([tool, "pr", "view", branch, "--json", "url", "--jq", ".url"],
                                  cwd=self.workspace, capture_output=True, text=True)
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip().splitlines()[-1]
        raise GitError(f"{tool} pr create failed: {(proc.stderr or proc.stdout).strip()[:500]}")

    def push(self, remote: str, branch: str, *, approved: bool) -> None:
        self.guard.enforce("git_push", approved=approved)
        # themis-ai-runtime records an owner-approved push by this marker.
        self._git("push", "-u", remote, branch, env={PUSH_APPROVED_ENV: "1"})

    def _has_identity(self) -> bool:
        return bool(self._git("config", "user.email", check=False).strip())


def parse_github_repo(url: str) -> str | None:
    """``owner/repo`` from a GitHub remote URL (https or ssh), else None."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def compare_url(repo: str, base: str, branch: str) -> str:
    return f"https://github.com/{repo}/compare/{base}...{branch}?expand=1"
