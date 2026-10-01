"""Explicit task state machine and persistent state manager.

The orchestrator never relies on conversation history to know where a task is.
Everything it needs to resume is in the state directory, which lives OUTSIDE
the target repository (default ``~/.chopin/<workspace name>/``):

    <state dir>/
    ├── current-task.json      id of the active task
    ├── task-history.json      one summary row per task
    ├── tasks/<id>.json        full task record (state, transitions, metrics)
    ├── decisions/<id>/        every review verdict and human decision
    ├── artifacts/<id>/        agent artifacts (openai/*.md, claude/*.md)
    └── audit.log              JSONL audit trail of every guarded action
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class TaskState(str, Enum):
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    DESIGN_READY = "DESIGN_READY"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    FIXING = "FIXING"
    CODE_REVIEW = "CODE_REVIEW"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    FINAL_REVIEW = "FINAL_REVIEW"
    APPROVED = "APPROVED"
    COMMITTED = "COMMITTED"
    # Code delivered; the owner tries the feature on the enterprise VM (Themis +
    # Harness together). Only the owner's verdict moves it on.
    AWAITING_VM_VERIFICATION = "AWAITING_VM_VERIFICATION"
    COMPLETED = "COMPLETED"
    # Control states
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


TERMINAL_STATES = {TaskState.COMPLETED, TaskState.REJECTED, TaskState.ABORTED}

# Control states reachable from any non-terminal state.
_CONTROL = {TaskState.AWAITING_APPROVAL, TaskState.ESCALATED, TaskState.FAILED, TaskState.ABORTED}

TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.NEW: {TaskState.ANALYZING},
    TaskState.ANALYZING: {TaskState.DESIGN_READY},
    TaskState.DESIGN_READY: {TaskState.IMPLEMENTING, TaskState.REJECTED},
    TaskState.IMPLEMENTING: {TaskState.TESTING},
    TaskState.TESTING: {TaskState.CODE_REVIEW, TaskState.FIXING},
    TaskState.FIXING: {TaskState.TESTING},
    TaskState.CODE_REVIEW: {TaskState.SECURITY_REVIEW, TaskState.FINAL_REVIEW, TaskState.APPROVED, TaskState.FIXING},
    TaskState.SECURITY_REVIEW: {TaskState.FINAL_REVIEW, TaskState.APPROVED, TaskState.FIXING},
    TaskState.FINAL_REVIEW: {TaskState.APPROVED, TaskState.FIXING},
    TaskState.APPROVED: {TaskState.COMMITTED},
    TaskState.COMMITTED: {TaskState.COMPLETED, TaskState.AWAITING_VM_VERIFICATION},
    # verify -> COMPLETED; reopen (the VM test failed) -> FIXING.
    TaskState.AWAITING_VM_VERIFICATION: {TaskState.COMPLETED, TaskState.FIXING},
    # Leaving a control state returns to the state recorded in `resume_state`.
    TaskState.AWAITING_APPROVAL: set(TaskState) - {TaskState.NEW},
    TaskState.ESCALATED: {TaskState.FIXING, TaskState.ABORTED, TaskState.REJECTED},
    TaskState.FAILED: set(TaskState) - {TaskState.NEW},
    TaskState.REJECTED: set(),
    TaskState.COMPLETED: set(),
    TaskState.ABORTED: set(),
}


class InvalidTransition(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:limit].rstrip("-") or "task"


@dataclass
class Transition:
    at: str
    from_state: str
    to_state: str
    note: str = ""


@dataclass
class PendingApproval:
    action: str
    reason: str
    requested_at: str
    granted: bool | None = None  # None = undecided
    decided_at: str | None = None
    decided_by: str | None = None
    comment: str | None = None


@dataclass
class Metrics:
    iterations: int = 0
    test_runs: int = 0
    claude_cost_usd: float = 0.0
    openai_input_tokens: int = 0
    openai_output_tokens: int = 0


@dataclass
class RepoWork:
    """A secondary repository touched by a multi-repo feature."""

    name: str
    workspace: str
    branch: str
    base_branch: str
    commits: list[str] = field(default_factory=list)


@dataclass
class Task:
    id: str
    request: str
    branch: str
    base_branch: str
    state: TaskState = TaskState.NEW
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    # State to return to after AWAITING_APPROVAL / FAILED.
    resume_state: TaskState | None = None
    # Feedback for the next FIXING round (test output or review findings).
    pending_feedback: str | None = None
    claude_session_id: str | None = None
    # Implementation spec from the architecture agent (Spec as a dict).
    spec: dict[str, Any] | None = None
    # Fix cycles allowed before escalating; a human can extend it on resume.
    iteration_budget: int = 3
    approval: PendingApproval | None = None
    last_error: str | None = None
    commits: list[str] = field(default_factory=list)
    # Name of the primary repository (the target of the loaded config).
    repo: str = ""
    # Secondary repositories this feature changes, by name (multi-repo features).
    secondary: dict[str, RepoWork] = field(default_factory=dict)
    # Base branch per secondary repository, when not its configured default.
    base_overrides: dict[str, str] = field(default_factory=dict)
    # The owner's verdicts (verify/reopen), latest last; each records where.
    vm_results: list[dict[str, Any]] = field(default_factory=list)
    # Pull request (or compare link) per pushed repository, by name.
    pull_requests: dict[str, str] = field(default_factory=dict)
    # Dry run: build, test and review on local branches only — nothing is
    # pushed, no PR, no VM step.
    dry_run: bool = False
    # Design discussion: the owner's feedback per revision round, oldest first.
    design_rounds: list[dict[str, Any]] = field(default_factory=list)
    # The owner's guidance given on resume, oldest first. Every later review sees
    # it too, so a reviewer does not keep blocking on what the owner decided.
    owner_guidance: list[str] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)

    @classmethod
    def create(cls, request: str, base_branch: str, branch_prefix: str,
               iteration_budget: int = 3, repo: str = "") -> "Task":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        task_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        branch = f"{branch_prefix}{_slug(request)}-{task_id[-6:]}"
        return cls(id=task_id, request=request, branch=branch, base_branch=base_branch,
                   iteration_budget=iteration_budget, repo=repo)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, to: TaskState, note: str = "") -> None:
        allowed = TRANSITIONS[self.state] | (_CONTROL if not self.is_terminal else set())
        if to not in allowed:
            raise InvalidTransition(f"{self.state.value} -> {to.value} is not allowed")
        self.transitions.append(Transition(_now(), self.state.value, to.value, note))
        self.state = to
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["resume_state"] = self.resume_state.value if self.resume_state else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        d = dict(d)
        d["state"] = TaskState(d["state"])
        d["resume_state"] = TaskState(d["resume_state"]) if d.get("resume_state") else None
        d["approval"] = PendingApproval(**d["approval"]) if d.get("approval") else None
        d["transitions"] = [Transition(**t) for t in d.get("transitions", [])]
        d["metrics"] = Metrics(**d.get("metrics", {}))
        d["secondary"] = {k: RepoWork(**v) for k, v in (d.get("secondary") or {}).items()}
        return cls(**d)


class StateManager:
    def __init__(self, root: Path):
        self.root = root
        self.tasks_dir = self.root / "tasks"
        self.decisions_dir = self.root / "decisions"
        self.current_file = self.root / "current-task.json"
        self.history_file = self.root / "task-history.json"
        self.audit_file = self.root / "audit.log"
        self.artifacts_dir = self.root / "artifacts"

    def init(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_dir.mkdir(parents=True, exist_ok=True)

    # -- tasks -------------------------------------------------------------
    def save(self, task: Task) -> None:
        self.init()
        task.updated_at = _now()
        _atomic_write(self.tasks_dir / f"{task.id}.json", task.to_dict())
        _atomic_write(self.current_file, {"id": task.id})
        history = self._read_json(self.history_file, [])
        row = {
            "id": task.id,
            "request": task.request,
            "branch": task.branch,
            "state": task.state.value,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        history = [h for h in history if h.get("id") != task.id] + [row]
        _atomic_write(self.history_file, history)

    def load(self, task_id: str | None = None) -> Task:
        if task_id is None:
            current = self._read_json(self.current_file, None)
            if not current:
                raise FileNotFoundError("no current task")
            task_id = current["id"]
        path = self.tasks_dir / f"{task_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"unknown task: {task_id}")
        return Task.from_dict(json.loads(path.read_text()))

    def history(self) -> list[dict[str, Any]]:
        return self._read_json(self.history_file, [])

    # -- decisions & audit -------------------------------------------------
    def record_decision(self, task: Task, kind: str, payload: dict[str, Any]) -> Path:
        d = self.decisions_dir / task.id
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob("*.json"))) + 1
        path = d / f"{n:03d}-{kind}.json"
        _atomic_write(path, {"at": _now(), "kind": kind, "state": task.state.value, **payload})
        return path

    def artifact_path(self, task_id: str, rel: str) -> Path:
        return self.artifacts_dir / task_id / rel

    def write_artifact(self, task_id: str, rel: str, text: str) -> Path:
        path = self.artifact_path(task_id, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read_artifact(self, task_id: str, rel: str) -> str | None:
        path = self.artifact_path(task_id, rel)
        return path.read_text() if path.exists() else None

    def audit(self, event: dict[str, Any]) -> None:
        self.init()
        with self.audit_file.open("a") as f:
            f.write(json.dumps({"at": _now(), **event}, default=str) + "\n")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        return json.loads(path.read_text()) if path.exists() else default


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)
