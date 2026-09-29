"""Agent orchestrator: task planner, agent router, workflow engine.

The orchestrator drives a task through an explicit state machine. Each state
has one handler; a handler does one unit of work, records its outputs, and
transitions. State is saved after every step, so a task can be resumed after a
crash, a human approval, or an escalation.

    NEW → ANALYZING → DESIGN_READY → IMPLEMENTING → TESTING
                                                     │  ▲
                                            fail ────┘  └──── FIXING ◀─┐
                                                     │                 │
                                          CODE_REVIEW ── reject ───────┤
                                                     │                 │
                                      SECURITY_REVIEW ── reject ───────┤
                                                     │                 │
                                         FINAL_REVIEW ── reject ───────┘
                                                     │
                                  APPROVED → COMMITTED → COMPLETED

A fix cycle past the task's iteration budget (or its cost budget) escalates
to a human instead of looping forever.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .agents.base import ArchitectReviewer, Implementer, Review, ReviewKind, Spec, Usage
from .config import RuntimeConfig
from .gitops import LocalGit
from .guardrails import ApprovalRequired, GuardrailViolation, Guardrails
from .state import PendingApproval, StateManager, Task, TaskState, _now
from .workspace import Workspace

ARTIFACT_DIR = ".agents"
ARTIFACTS = {
    "architecture": f"{ARTIFACT_DIR}/openai/architecture.md",
    ReviewKind.CODE: f"{ARTIFACT_DIR}/openai/review.md",
    ReviewKind.SECURITY: f"{ARTIFACT_DIR}/openai/security.md",
    ReviewKind.FINAL: f"{ARTIFACT_DIR}/openai/final-review.md",
    "implementation": f"{ARTIFACT_DIR}/claude/implementation.md",
    "tests": f"{ARTIFACT_DIR}/claude/test-results.md",
}

REVIEW_STATE = {
    TaskState.CODE_REVIEW: ReviewKind.CODE,
    TaskState.SECURITY_REVIEW: ReviewKind.SECURITY,
    TaskState.FINAL_REVIEW: ReviewKind.FINAL,
}

EventSink = Callable[[str, Task], None]


class PreflightError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, workspace: Path, config: RuntimeConfig, architect: ArchitectReviewer,
                 implementer: Implementer, on_event: EventSink | None = None):
        self.root = workspace.resolve()
        self.cfg = config
        self.state = StateManager(self.root)
        self.guard = Guardrails(config.guardrails, self.root, audit=self.state.audit)
        self.git = LocalGit(self.root, self.guard)
        self.ws = Workspace(self.root, self.guard)
        self.architect = architect
        self.implementer = implementer
        self._emit = on_event or (lambda _msg, _task: None)

    # ------------------------------------------------------------------ API
    def start(self, request: str) -> Task:
        """Task planner entry point: create a task on its own branch and run it."""
        self._preflight()
        wf = self.cfg.workflow
        task = Task.create(request, wf.base_branch, wf.branch_prefix, wf.max_iterations)
        self.git.create_branch(task.branch, task.base_branch)
        self.state.save(task)
        self._emit(f"created task {task.id} on branch {task.branch}", task)
        return self.advance(task)

    def resume(self, task_id: str | None = None, guidance: str | None = None) -> Task:
        task = self.state.load(task_id)
        if task.state is TaskState.ESCALATED:
            # A human has looked at the escalation: extend the budget and retry.
            task.iteration_budget = task.metrics.iterations + self.cfg.workflow.max_iterations
            task.metrics.iterations += 1
            if guidance:
                task.pending_feedback = f"{task.pending_feedback or ''}\n\n## Guidance from the owner\n\n{guidance}"
            task.transition(TaskState.FIXING, "resumed by owner after escalation")
        elif task.state is TaskState.FAILED and task.resume_state:
            task.transition(task.resume_state, "retry after failure")
            task.last_error = None
        elif task.state is TaskState.AWAITING_APPROVAL and task.approval and task.approval.granted is not None:
            task.transition(task.resume_state or TaskState.NEW, "approval decided")
        self.state.save(task)
        return self.advance(task)

    def decide(self, task_id: str | None, granted: bool, *, by: str = "owner",
               comment: str | None = None) -> Task:
        """Record a human approve/reject for the pending approval, then resume."""
        task = self.state.load(task_id)
        if task.state is not TaskState.AWAITING_APPROVAL or task.approval is None:
            raise RuntimeError(f"task {task.id} is not awaiting approval (state {task.state.value})")
        task.approval.granted = granted
        task.approval.decided_at = _now()
        task.approval.decided_by = by
        task.approval.comment = comment
        self.state.record_decision(task, "human-approval", asdict(task.approval))
        self.state.audit({"type": "approval", "task": task.id, "action": task.approval.action,
                          "granted": granted, "by": by, "comment": comment})
        self.state.save(task)
        return self.resume(task.id)

    def abort(self, task_id: str | None = None, reason: str = "aborted by owner") -> Task:
        """Stop a task and return to the base branch. The feature branch is kept."""
        task = self.state.load(task_id)
        if not task.is_terminal:
            task.transition(TaskState.ABORTED, reason)
            self.state.save(task)
        if self.git.current_branch() != task.base_branch and self.git.is_clean():
            self.git.checkout(task.base_branch)
        return task

    # ------------------------------------------------------- workflow engine
    def advance(self, task: Task) -> Task:
        handlers: dict[TaskState, Callable[[Task], None]] = {
            TaskState.NEW: lambda t: t.transition(TaskState.ANALYZING),
            TaskState.ANALYZING: self._analyze,
            TaskState.DESIGN_READY: self._design_gate,
            TaskState.IMPLEMENTING: self._implement,
            TaskState.FIXING: self._implement,
            TaskState.TESTING: self._test,
            TaskState.CODE_REVIEW: self._review,
            TaskState.SECURITY_REVIEW: self._review,
            TaskState.FINAL_REVIEW: self._review,
            TaskState.APPROVED: self._commit,
            TaskState.COMMITTED: self._deliver,
        }
        self._ensure_on_branch(task)
        while task.state in handlers:
            before = task.state
            try:
                handlers[task.state](task)
            except ApprovalRequired as e:
                self._request_approval(task, e.verdict.action, e.verdict.reason)
            except GuardrailViolation as e:
                self._fail(task, before, f"guardrail: {e}")
            except Exception as e:  # noqa: BLE001 - any agent/tool failure is recorded, not raised
                self._fail(task, before, f"{type(e).__name__}: {e}")
            self.state.save(task)
            if task.state is not before:
                self._emit(f"{before.value} -> {task.state.value}", task)
        return task

    # ------------------------------------------------------------- handlers
    def _analyze(self, task: Task) -> None:
        spec = self.architect.design(task.request, self._repo_context(task.request))
        self._account(task, spec.usage, "openai")
        task.spec = {k: v for k, v in asdict(spec).items() if k != "usage"}
        self.ws.write_file(ARTIFACTS["architecture"], spec.to_markdown())
        self.state.record_decision(task, "architecture", task.spec)
        task.transition(TaskState.DESIGN_READY, spec.summary[:200])

    def _design_gate(self, task: Task) -> None:
        wf = self.cfg.workflow
        needs_owner = wf.require_design_approval or (
            wf.pause_on_open_questions and bool(self._spec(task).open_questions))
        if needs_owner:
            status = self._approval_status(task, "approve_design")
            if status is None:
                raise ApprovalRequired(self.guard.check("approve_design"))
            task.approval = None
            if status is False:
                task.transition(TaskState.REJECTED, "design rejected by owner")
                return
        task.transition(TaskState.IMPLEMENTING)

    def _implement(self, task: Task) -> None:
        fixing = task.state is TaskState.FIXING
        result = self.implementer.implement(
            task.request, self._spec(task),
            feedback=task.pending_feedback if fixing else None,
            session_id=task.claude_session_id,
        )
        self._account(task, result.usage, "claude")
        if result.session_id:
            task.claude_session_id = result.session_id
        heading = f"Fix iteration {task.metrics.iterations}" if fixing else "Initial implementation"
        self._append_artifact("implementation", f"## {heading}\n\n{result.summary}\n")
        if not result.ok:
            raise RuntimeError(f"implementation agent failed: {result.summary[:500]}")
        label = f"fix {task.metrics.iterations}" if fixing else "implement"
        sha = self.git.commit_all(f"wip({task.id}): {label}\n\n{task.request}", actor="claude")
        if sha:
            task.commits.append(sha)
        task.pending_feedback = None
        task.transition(TaskState.TESTING, label)

    def _test(self, task: Task) -> None:
        task.metrics.test_runs += 1
        results = [self.ws.run_check("tests", self.cfg.tests), self.ws.run_check("lint", self.cfg.lint)]
        report = "\n".join(r.to_markdown() for r in results)
        self.ws.write_file(ARTIFACTS["tests"], f"# Test results (run {task.metrics.test_runs})\n\n{report}")
        self.state.record_decision(task, "checks", {"results": [r.to_dict() for r in results]})
        failed = [r for r in results if not r.passed]
        if failed:
            self._to_fixing(task, "checks failed:\n\n" + "\n".join(r.to_markdown() for r in failed),
                            f"{', '.join(r.name for r in failed)} failed")
        else:
            task.transition(TaskState.CODE_REVIEW, "checks passed")

    def _review(self, task: Task) -> None:
        kind = REVIEW_STATE[task.state]
        diff = self.git.diff(task.base_branch, exclude=[ARTIFACT_DIR])
        if not diff.strip():
            self._to_fixing(task, "The change is empty: no code was modified relative to "
                                  f"{task.base_branch}. Implement the specification.", "empty diff")
            return
        review = self.architect.review(kind, task.request, self._spec(task), diff, self._checks_summary())
        self._account(task, review.usage, "openai")
        self.ws.write_file(ARTIFACTS[kind], review.to_markdown())
        self.state.record_decision(task, f"{kind.value}-review", {
            "accepted": review.accepted, "summary": review.summary,
            "findings": [asdict(f) for f in review.findings]})
        blocking = review.blocking(self.cfg.workflow.block_on_severity)
        if not review.accepted or blocking:
            self._to_fixing(task, self._review_feedback(review, blocking),
                            f"{kind.value} review rejected ({len(blocking)} blocking)")
            return
        task.transition(self._next_gate(task.state), f"{kind.value} review accepted")

    def _commit(self, task: Task) -> None:
        sha = self.git.commit_all(f"feat: {task.request}\n\nthemis-ai task {task.id}: "
                                  f"reviewed and approved by the orchestrated workflow.")
        if sha:
            task.commits.append(sha)
        task.transition(TaskState.COMMITTED, sha or "no artifact changes")

    def _deliver(self, task: Task) -> None:
        delivery = self.cfg.workflow.delivery
        if delivery == "commit":
            task.transition(TaskState.COMPLETED, f"committed on {task.branch}")
            return
        action = {"merge": "git_merge", "push": "git_push"}.get(delivery)
        if action is None:
            raise ValueError(f"unknown delivery mode: {delivery}")
        status = self._approval_status(task, action)
        if status is None:
            raise ApprovalRequired(self.guard.check(action))
        if status is False:
            task.transition(TaskState.COMPLETED, f"{delivery} declined by owner; work kept on {task.branch}")
            return
        if delivery == "merge":
            sha = self.git.merge(task.branch, task.base_branch, approved=True)
            note = f"merged into {task.base_branch} at {sha[:12]}"
        else:
            self.git.push(self.cfg.workflow.remote, task.branch, approved=True)
            note = f"pushed {task.branch} to {self.cfg.workflow.remote}"
        task.approval = None
        task.transition(TaskState.COMPLETED, note)

    # -------------------------------------------------------------- helpers
    def _to_fixing(self, task: Task, feedback: str, note: str) -> None:
        task.pending_feedback = feedback
        spent = task.metrics.claude_cost_usd + self._openai_cost_estimate(task)
        if task.metrics.iterations >= task.iteration_budget:
            task.transition(TaskState.ESCALATED, f"{note}; iteration budget "
                                                 f"({task.iteration_budget}) exhausted")
            return
        if spent > self.cfg.workflow.max_cost_usd:
            task.transition(TaskState.ESCALATED, f"{note}; cost budget exceeded (${spent:.2f})")
            return
        task.metrics.iterations += 1
        task.transition(TaskState.FIXING, note)

    def _next_gate(self, state: TaskState) -> TaskState:
        wf = self.cfg.workflow
        order = [TaskState.CODE_REVIEW]
        if wf.security_review:
            order.append(TaskState.SECURITY_REVIEW)
        if wf.final_review:
            order.append(TaskState.FINAL_REVIEW)
        order.append(TaskState.APPROVED)
        return order[order.index(state) + 1]

    def _request_approval(self, task: Task, action: str, reason: str) -> None:
        task.approval = PendingApproval(action=action, reason=reason, requested_at=_now())
        task.resume_state = task.state
        task.transition(TaskState.AWAITING_APPROVAL, f"{action}: {reason}")
        self.state.audit({"type": "approval_requested", "task": task.id, "action": action})

    @staticmethod
    def _approval_status(task: Task, action: str) -> bool | None:
        if task.approval and task.approval.action == action:
            return task.approval.granted
        return None

    def _fail(self, task: Task, state: TaskState, error: str) -> None:
        task.last_error = error
        task.resume_state = state
        if task.state is state and not task.is_terminal:
            task.transition(TaskState.FAILED, error[:300])
        self.state.audit({"type": "failure", "task": task.id, "state": state.value, "error": error})

    def _ensure_on_branch(self, task: Task) -> None:
        if task.is_terminal or task.state is TaskState.COMMITTED:
            return
        if self.git.current_branch() != task.branch:
            self.git.checkout(task.branch)

    def _preflight(self) -> None:
        if not self.git.is_repo():
            raise PreflightError(f"{self.root} is not a git repository")
        self.git.ensure_local_excludes()
        if not self.git.is_clean():
            raise PreflightError("working tree has uncommitted changes; commit or stash them first")
        if not self.git.branch_exists(self.cfg.workflow.base_branch):
            raise PreflightError(f"base branch {self.cfg.workflow.base_branch!r} does not exist")

    def _spec(self, task: Task) -> Spec:
        if not task.spec:
            raise RuntimeError("task has no specification")
        return Spec(**task.spec)

    def _checks_summary(self) -> str:
        path = self.root / ARTIFACTS["tests"]
        return path.read_text() if path.exists() else "(no checks recorded)"

    def _append_artifact(self, key: str, text: str) -> None:
        path = self.root / ARTIFACTS[key]
        prior = path.read_text() if path.exists() else "# Implementation log\n\n"
        self.ws.write_file(ARTIFACTS[key], prior + text + "\n")

    @staticmethod
    def _review_feedback(review: Review, blocking: list) -> str:
        return (f"The independent {review.kind.value} reviewer rejected the change.\n\n"
                + review.to_markdown()
                + (f"\nBlocking findings: {len(blocking)}. Address every blocking finding.\n" if blocking else ""))

    @staticmethod
    def _account(task: Task, usage: Usage, provider: str) -> None:
        if provider == "claude":
            task.metrics.claude_cost_usd += usage.cost_usd
        else:
            task.metrics.openai_input_tokens += usage.input_tokens
            task.metrics.openai_output_tokens += usage.output_tokens

    @staticmethod
    def _openai_cost_estimate(task: Task) -> float:
        # Conservative blended estimate; exact pricing lives with the provider.
        m = task.metrics
        return m.openai_input_tokens * 5e-6 + m.openai_output_tokens * 20e-6

    def _repo_context(self, request: str) -> str:
        """Deterministic context bundle for the architect: tree, docs, and hits
        for identifiers mentioned in the request (e.g. KN-MODULE-4)."""
        budget = self.cfg.openai.context_budget_chars
        parts: list[str] = []
        files = self.ws.list_files()
        parts.append("### File tree\n\n" + "\n".join(files[:1500]))
        ids = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+\b", request))
        # CamelCase identifiers (SbomParser, handleGetProduct), not SHOUTING words.
        ids |= set(re.findall(r"\b[A-Za-z_]*[a-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*\b", request))
        for ident in sorted(ids):
            hits = self.ws.search_code(ident, limit=50)
            parts.append(f"### References to `{ident}`\n\n" + ("\n".join(hits) or "(no matches in repository)"))
        seen: set[str] = set()
        for pattern in self.cfg.workflow.context_globs:
            for f in self.ws.list_files(pattern):
                if f in seen or f.startswith(ARTIFACT_DIR):
                    continue
                seen.add(f)
                parts.append(f"### {f}\n\n{self.ws.read_file(f)}")
        out, used = [], 0
        for p in parts:
            if used + len(p) > budget:
                out.append(f"\n[... context truncated at {budget} characters ...]")
                break
            out.append(p)
            used += len(p)
        return "\n\n".join(out)
