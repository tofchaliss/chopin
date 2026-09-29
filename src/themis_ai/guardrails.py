"""Safety & guardrails: every side-effecting action is classified before it runs.

    AGENT REQUEST ──▶ Guardrail ──┬── ALLOW     ──▶ execute
                                  ├── APPROVAL  ──▶ pause for a human
                                  └── DENY      ──▶ refuse

Unknown actions require approval: a new capability must be explicitly allowed
in policy before it can run autonomously.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable

from .config import GuardrailConfig


class Decision(str, Enum):
    ALLOW = "allow"
    APPROVAL = "approval_required"
    DENY = "deny"


@dataclass
class Verdict:
    action: str
    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class GuardrailViolation(PermissionError):
    def __init__(self, verdict: Verdict):
        super().__init__(f"{verdict.action}: {verdict.decision.value} ({verdict.reason})")
        self.verdict = verdict


class ApprovalRequired(GuardrailViolation):
    pass


AuditSink = Callable[[dict], None]


class Guardrails:
    def __init__(self, policy: GuardrailConfig, workspace: Path, audit: AuditSink | None = None):
        self.policy = policy
        self.workspace = workspace.resolve()
        self._audit = audit or (lambda _event: None)

    def check(self, action: str, *, path: str | None = None, actor: str = "orchestrator",
              approved: bool = False) -> Verdict:
        verdict = self._classify(action, path)
        if verdict.decision is Decision.APPROVAL and approved:
            verdict = Verdict(action, Decision.ALLOW, "approved by human")
        self._audit({"type": "guardrail", "actor": actor, "action": action, "path": path,
                     "decision": verdict.decision.value, "reason": verdict.reason})
        return verdict

    def enforce(self, action: str, **kwargs) -> Verdict:
        verdict = self.check(action, **kwargs)
        if verdict.decision is Decision.DENY:
            raise GuardrailViolation(verdict)
        if verdict.decision is Decision.APPROVAL:
            raise ApprovalRequired(verdict)
        return verdict

    def resolve_path(self, path: str) -> Path:
        """Resolve a workspace-relative path, refusing anything outside it."""
        p = (self.workspace / path).resolve()
        if p != self.workspace and self.workspace not in p.parents:
            raise GuardrailViolation(Verdict("path", Decision.DENY, f"{path} escapes the workspace"))
        return p

    def _classify(self, action: str, path: str | None) -> Verdict:
        if action in self.policy.deny:
            return Verdict(action, Decision.DENY, "denied by policy")
        if path is not None and action in ("write_file", "write_docs", "delete_file"):
            try:
                rel = self.resolve_path(path).relative_to(self.workspace).as_posix()
            except GuardrailViolation as e:
                return e.verdict
            for pattern in self.policy.protected_paths:
                if match_path(rel, pattern):
                    return Verdict(action, Decision.DENY, f"{rel} is protected ({pattern})")
        if action in self.policy.require_approval:
            return Verdict(action, Decision.APPROVAL, "requires human approval")
        if action in self.policy.allow:
            return Verdict(action, Decision.ALLOW, "allowed by policy")
        return Verdict(action, Decision.APPROVAL, "unknown action; requires human approval")


def match_path(rel: str, pattern: str) -> bool:
    if fnmatch.fnmatch(rel, pattern):
        return True
    # "**/x" should also match "x" at the root.
    if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
        return True
    return PurePosixPath(rel).match(pattern)
