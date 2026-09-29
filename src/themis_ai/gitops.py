"""Local Git: branching, commits, diffs, rollback. GitHub is optional (push only).

Every write goes through guardrails. The orchestrator is the only component
that writes git history; Claude Code only edits the working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .guardrails import Guardrails

# Paths that belong to the runtime and never enter a commit or a review diff.
LOCAL_EXCLUDES = ["agent-state/"]


class GitError(RuntimeError):
    pass


class LocalGit:
    def __init__(self, workspace: Path, guardrails: Guardrails):
        self.workspace = workspace
        self.guard = guardrails

    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True)
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
    def ensure_local_excludes(self) -> None:
        exclude = self.workspace / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        missing = [p for p in LOCAL_EXCLUDES if p not in existing.splitlines()]
        if missing:
            with exclude.open("a") as f:
                f.write(("\n" if existing and not existing.endswith("\n") else "")
                        + "# themis-ai runtime state\n" + "\n".join(missing) + "\n")

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

    def push(self, remote: str, branch: str, *, approved: bool) -> None:
        self.guard.enforce("git_push", approved=approved)
        self._git("push", "-u", remote, branch)

    def _has_identity(self) -> bool:
        return bool(self._git("config", "user.email", check=False).strip())
