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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .agents.base import ArchitectReviewer, Implementer, Review, ReviewKind, Spec, Usage
from .config import Repo, RuntimeConfig, resolve_state_dir
from .gitops import GitError, LocalGit, compare_url
from .guardrails import ApprovalRequired, GuardrailViolation, Guardrails
from .state import PendingApproval, RepoWork, StateManager, Task, TaskState, _now
from .workspace import Workspace

# Agent artifacts, relative to <state dir>/artifacts/<task id>/. They are kept
# outside the target repository: the target only ever receives code.
ARTIFACTS = {
    "architecture": "openai/architecture.md",
    ReviewKind.CODE: "openai/review.md",
    ReviewKind.SECURITY: "openai/security.md",
    ReviewKind.FINAL: "openai/final-review.md",
    "implementation": "claude/implementation.md",
    "tests": "claude/test-results.md",
    "vm": "vm-checklist.md",
}

REVIEW_STATE = {
    TaskState.CODE_REVIEW: ReviewKind.CODE,
    TaskState.SECURITY_REVIEW: ReviewKind.SECURITY,
    TaskState.FINAL_REVIEW: ReviewKind.FINAL,
}

EventSink = Callable[[str, Task], None]


class PreflightError(RuntimeError):
    pass


@dataclass
class RepoCtx:
    """A repository the orchestrator can work in: its config, git and workspace."""

    name: str
    root: Path
    cfg: RuntimeConfig
    git: LocalGit
    ws: Workspace


class Orchestrator:
    def __init__(self, workspace: Path, config: RuntimeConfig, architect: ArchitectReviewer,
                 implementer: Implementer, on_event: EventSink | None = None,
                 others: list[Repo] | None = None):
        """``workspace``/``config`` is the primary repository; ``others`` are the
        further repositories a feature may also change (see ``load_project``)."""
        self.root = workspace.resolve()
        self.cfg = config
        self.state_root = resolve_state_dir(config, self.root)
        self.state = StateManager(self.state_root)
        self.guard = Guardrails(config.guardrails, self.root, audit=self.state.audit)
        self.git = LocalGit(self.root, self.guard)
        self.ws = Workspace(self.root, self.guard)
        self.architect = architect
        self.implementer = implementer
        self._emit = on_event or (lambda _msg, _task: None)
        self.primary = config.target.name or self.root.name
        self.repos: dict[str, RepoCtx] = {
            self.primary: RepoCtx(self.primary, self.root, config, self.git, self.ws)}
        for other in others or []:
            root = other.root.resolve()
            guard = Guardrails(other.cfg.guardrails, root, audit=self.state.audit)
            self.repos[other.name] = RepoCtx(other.name, root, other.cfg,
                                             LocalGit(root, guard), Workspace(root, guard))

    # ------------------------------------------------------------------ API
    def start(self, request: str, bases: dict[str, str] | None = None, *,
              dry_run: bool = False) -> Task:
        """Task planner entry point: create a task on its own branch and run it.

        ``bases`` overrides the base branch per repository name (e.g. Themis work
        that builds on an integration branch instead of main).
        """
        bases = dict(bases or {})
        unknown = set(bases) - set(self.repos)
        if unknown:
            raise PreflightError(f"unknown repository in --base: {', '.join(sorted(unknown))}")
        wf = self.cfg.workflow
        base = bases.pop(self.primary, wf.base_branch)
        self._preflight(base)
        task = Task.create(request, base, wf.branch_prefix, wf.max_iterations, repo=self.primary)
        task.base_overrides = bases
        task.dry_run = dry_run
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
                task.owner_guidance.append(guidance)
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

    def revise(self, task_id: str | None, feedback: str, *, by: str = "owner") -> Task:
        """Design discussion: send the owner's feedback on the proposed design back
        to the architect for a new round. Only while the design awaits approval."""
        task = self.state.load(task_id)
        if (task.state is not TaskState.AWAITING_APPROVAL or task.approval is None
                or task.approval.action != "approve_design"):
            raise RuntimeError(f"task {task.id} has no design awaiting approval (state {task.state.value})")
        round_no = len(task.design_rounds) + 1
        entry = {"round": round_no, "at": _now(), "by": by, "feedback": feedback}
        task.design_rounds.append(entry)
        self.state.record_decision(task, "design-revision", entry)
        self.state.audit({"type": "design_revision", "task": task.id, "round": round_no, "by": by})
        # Keep every proposed design; the architect then writes the next one.
        prior = self.state.read_artifact(task.id, ARTIFACTS["architecture"])
        if prior:
            self.state.write_artifact(task.id, f"openai/architecture-r{round_no - 1}.md", prior)
        task.approval = None
        task.transition(TaskState.ANALYZING, f"design revision {round_no} requested by {by}")
        self.state.save(task)
        return self.advance(task)

    def abort(self, task_id: str | None = None, reason: str = "aborted by owner") -> Task:
        """Stop a task and return to the base branch. The feature branch is kept."""
        task = self.state.load(task_id)
        if not task.is_terminal:
            task.transition(TaskState.ABORTED, reason)
            self.state.save(task)
        if self.git.current_branch() != task.base_branch and self.git.is_clean():
            self.git.checkout(task.base_branch)
        for work in task.secondary.values():
            repo = self.repos.get(work.name)
            if repo and repo.git.current_branch() != work.base_branch and repo.git.is_clean():
                repo.git.checkout(work.base_branch)
        return task

    def verify(self, task_id: str | None, evidence: str, *, by: str = "owner",
               where: str = "vm") -> Task:
        """The owner tried the feature (on the enterprise VM, or ``where``) and it works: close it."""
        task = self._awaiting_vm(task_id)
        result = {"at": _now(), "by": by, "passed": True, "where": where, "evidence": evidence}
        task.vm_results.append(result)
        self.state.record_decision(task, "vm-verification", result)
        self.state.audit({"type": "vm_verification", "task": task.id, "passed": True, "by": by,
                          "where": where})
        task.transition(TaskState.COMPLETED, f"verified on {_place(where)} by {by}")
        self.state.save(task)
        return task

    def reopen(self, task_id: str | None, failure: str, *, by: str = "owner",
               where: str = "vm") -> Task:
        """The owner's test failed: feed what failed back into the fix loop."""
        task = self._awaiting_vm(task_id)
        result = {"at": _now(), "by": by, "passed": False, "where": where, "evidence": failure}
        task.vm_results.append(result)
        self.state.record_decision(task, "vm-verification", result)
        self.state.audit({"type": "vm_verification", "task": task.id, "passed": False, "by": by,
                          "where": where})
        context = ("on the enterprise VM (Themis and the Harness together)" if where == "vm"
                   else f"on {where}")
        task.pending_feedback = f"The feature failed when the owner tried it {context}:\n\n{failure}"
        task.iteration_budget = task.metrics.iterations + self.cfg.workflow.max_iterations
        task.metrics.iterations += 1
        task.transition(TaskState.FIXING, f"failed on {_place(where)}; reopened by {by}")
        self.state.save(task)
        return self.advance(task)

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
        if len(self.repos) == 1:
            context = self._repo_context(task.request, self.repos[self.primary])
        else:
            context = "\n\n".join(
                f"# Repository: {name}{' (primary)' if name == self.primary else ''}\n\n"
                + self._repo_context(task.request, repo) for name, repo in self.repos.items())
        spec = self.architect.design(self._design_request(task), context)
        self._account(task, spec.usage, "openai")
        unknown = [r for r in spec.repositories if r not in self.repos]
        if unknown:
            spec.open_questions.append(
                f"The specification names repositories chopin does not have: {', '.join(unknown)}.")
        spec.repositories = [self.primary] + [
            r for r in dict.fromkeys(spec.repositories) if r in self.repos and r != self.primary]
        task.spec = {k: v for k, v in asdict(spec).items() if k != "usage"}
        self._write_artifact(task, "architecture", spec.to_markdown())
        self.state.record_decision(task, "architecture", task.spec)
        task.transition(TaskState.DESIGN_READY, spec.summary[:200])

    def _design_request(self, task: Task) -> str:
        """The request, plus — in a design discussion — the current design and
        every round of the owner's feedback, so the architect revises rather
        than starting over."""
        if not task.design_rounds or not task.spec:
            return task.request
        rounds = "\n\n".join(f"### Round {r['round']}\n\n{r['feedback']}" for r in task.design_rounds)
        return (f"{task.request}\n\n## Design under discussion (your previous specification)\n\n"
                f"{self._spec(task).to_markdown()}\n\n## The owner's feedback on it\n\n{rounds}\n\n"
                "Produce a revised specification that addresses every point of the feedback. "
                "Change only what the feedback asks for: where it names a section, change that "
                "section and keep the others as they were, word for word, unless the change "
                "forces an update (say which under risks). "
                "Where you disagree, keep your position and say why under risks.")

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
        self._open_secondary_branches(task)
        task.transition(TaskState.IMPLEMENTING)

    def _implement(self, task: Task) -> None:
        fixing = task.state is TaskState.FIXING
        result = self.implementer.implement(
            task.request, self._spec(task),
            # Set outside FIXING only after an attempt was cut off at its time limit.
            feedback=task.pending_feedback,
            session_id=task.claude_session_id,
            extra_dirs=[self.repos[w.name].root for w in task.secondary.values()] or None,
            env=self._run_env(task) or None,
        )
        self._account(task, result.usage, "claude")
        if result.session_id:
            task.claude_session_id = result.session_id
        heading = f"Fix iteration {task.metrics.iterations}" if fixing else "Initial implementation"
        self._append_artifact(task, "implementation", f"## {heading}\n\n{result.summary}\n")
        if not result.ok:
            if result.timed_out:
                # A retry (chopin resume) continues from the partial work in the tree.
                task.pending_feedback = (
                    f"{task.pending_feedback or ''}\n\n## Previous attempt was cut off\n\n"
                    "The previous attempt hit its time limit before finishing. Its partial "
                    "changes are still in the working tree: review them, keep what is right, "
                    "and finish the specification. Run only the tests of the packages you "
                    "changed; the orchestrator runs the full gate.").strip()
            raise RuntimeError(f"implementation agent failed: {result.summary[:500]}")
        label = f"fix {task.metrics.iterations}" if fixing else "implement"
        self._commit_all(task, f"wip({task.id}): {label}\n\n{task.request}", actor="claude")
        task.pending_feedback = None
        task.transition(TaskState.TESTING, label)

    def _test(self, task: Task) -> None:
        task.metrics.test_runs += 1
        env = self._run_env(task) or None
        multi = bool(task.secondary)
        results = []
        for repo in self._involved(task):
            prefix = f"{repo.name}:" if multi else ""
            results.append(repo.ws.run_check(f"{prefix}tests", repo.cfg.tests, env=env))
            results.append(repo.ws.run_check(f"{prefix}lint", repo.cfg.lint, env=env))
        report = "\n".join(r.to_markdown() for r in results)
        self._write_artifact(task, "tests", f"# Test results (run {task.metrics.test_runs})\n\n{report}")
        self.state.record_decision(task, "checks", {"results": [r.to_dict() for r in results]})
        failed = [r for r in results if not r.passed]
        if failed:
            self._to_fixing(task, "checks failed:\n\n" + "\n".join(r.to_markdown() for r in failed),
                            f"{', '.join(r.name for r in failed)} failed")
        else:
            task.transition(TaskState.CODE_REVIEW, "checks passed")

    def _review(self, task: Task) -> None:
        kind = REVIEW_STATE[task.state]
        diff = self._feature_diff(task)
        if not diff.strip():
            self._to_fixing(task, "The change is empty: no code was modified relative to "
                                  f"{task.base_branch}. Implement the specification.", "empty diff")
            return
        review = self.architect.review(kind, self._review_request(task), self._spec(task), diff,
                                       self._checks_summary(task))
        self._account(task, review.usage, "openai")
        self._write_artifact(task, kind, review.to_markdown())
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
        # Iteration commits already hold the code; this catches anything left in
        # the tree (normally nothing, since artifacts live outside the repository).
        sha = self._commit_all(task, f"feat: {task.request}\n\nthemis-ai task {task.id}: "
                                     f"reviewed and approved by the orchestrated workflow.")
        task.transition(TaskState.COMMITTED, sha or f"approved at {self.git.head()[:12]}")

    def _deliver(self, task: Task) -> None:
        delivery = self.cfg.workflow.delivery
        if task.dry_run:
            branches = ", ".join(f"{r.name}:{task.branch}" for r in self._involved(task))
            task.transition(TaskState.COMPLETED, f"dry run: reviewed and committed on local branches "
                                                 f"({branches}); nothing pushed")
            return
        if delivery == "commit":
            self._finish(task, f"committed on {task.branch}")
            return
        action = {"merge": "git_merge", "push": "git_push"}.get(delivery)
        if action is None:
            raise ValueError(f"unknown delivery mode: {delivery}")
        status = self._approval_status(task, action)
        if status is None:
            raise ApprovalRequired(self.guard.check(action))
        if status is False:
            task.approval = None
            self._finish(task, f"{delivery} declined by owner; work kept on {task.branch}")
            return
        notes = []
        for repo in self._involved(task):
            base = self._base_of(task, repo.name)
            if delivery == "merge":
                sha = repo.git.merge(task.branch, base, approved=True)
                notes.append(f"{repo.name}: merged into {base} at {sha[:12]}")
            else:
                repo.git.push(repo.cfg.workflow.remote, task.branch, approved=True)
                notes.append(f"{repo.name}: pushed {task.branch} to {repo.cfg.workflow.remote}")
        if delivery == "push" and self.cfg.workflow.pull_request:
            notes += self._open_pull_requests(task)
        task.approval = None
        self._finish(task, "; ".join(notes))

    def _open_pull_requests(self, task: Task) -> list[str]:
        """One PR per pushed repository, under the push approval. A failure
        records a compare link instead of failing the delivered task."""
        notes = []
        for repo in self._involved(task):
            base = self._base_of(task, repo.name)
            try:
                url = repo.git.open_pull_request(
                    self.cfg.workflow.pr_tool, base=base, branch=task.branch,
                    title=self._pr_title(task), body=self._pr_body(task, repo.name), approved=True)
                notes.append(f"{repo.name}: PR {url}")
            except (GitError, OSError) as e:
                gh_repo = repo.git.github_repo(repo.cfg.workflow.remote)
                url = compare_url(gh_repo, base, task.branch) if gh_repo else ""
                self.state.audit({"type": "pull_request_failed", "task": task.id,
                                  "repo": repo.name, "error": str(e)[:300]})
                notes.append(f"{repo.name}: PR not opened ({str(e)[:120]}); "
                             + (f"open it at {url}" if url else "open it by hand"))
            if url:
                task.pull_requests[repo.name] = url
        return notes

    def _pr_title(self, task: Task) -> str:
        summary = (self._spec(task).summary or task.request).strip().splitlines()[0]
        return summary if len(summary) <= 72 else summary[:69].rstrip() + "..."

    def _pr_body(self, task: Task, repo_name: str) -> str:
        spec = self._spec(task)
        lines = ["## Summary", "", spec.summary.strip(), "", "## Request", "", task.request.strip(), ""]
        others = [r.name for r in self._involved(task) if r.name != repo_name]
        if others:
            lines += ["## Same feature in other repositories", ""]
            lines += [f"- {name}: branch `{task.branch}`" for name in others] + [""]
        lines += ["## Acceptance criteria", ""]
        lines += [f"- [ ] {c}" for c in spec.acceptance_criteria] or ["- (none)"]
        lines += ["", "## Evidence", "",
                  f"- chopin task `{task.id}`: design approved by the owner; gate green; "
                  "code, security and final reviews accepted.",
                  "- Owner verification (VM or live proof): pending — recorded with `chopin verify`.",
                  "", "Generated by chopin", ""]
        return "\n".join(lines)

    def _finish(self, task: Task, note: str) -> None:
        if self.cfg.workflow.verification == "vm":
            self._write_artifact(task, "vm", self._vm_checklist(task))
            task.transition(TaskState.AWAITING_VM_VERIFICATION,
                            f"{note}; try it on the enterprise VM, then verify or reopen")
        elif self.cfg.workflow.verification == "none":
            task.transition(TaskState.COMPLETED, note)
        else:
            raise ValueError(f"unknown verification mode: {self.cfg.workflow.verification}")

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
        if task.is_terminal or task.state in (TaskState.COMMITTED, TaskState.AWAITING_VM_VERIFICATION):
            return
        for repo in self._involved(task):
            if repo.git.current_branch() != task.branch:
                repo.git.checkout(task.branch)

    def _preflight(self, base: str) -> None:
        for name, repo in self.repos.items():
            if not repo.git.is_repo():
                raise PreflightError(f"{repo.root} is not a git repository")
            if self.state_root == repo.root or repo.root in self.state_root.parents:
                raise PreflightError(f"state directory {self.state_root} is inside the workspace "
                                     f"{repo.root}; set workflow.state_dir or CHOPIN_HOME outside it")
            if not repo.git.is_clean():
                raise PreflightError(f"{name}: working tree has uncommitted changes; "
                                     "commit or stash them first")
        if not self.git.branch_exists(base):
            raise PreflightError(f"base branch {base!r} does not exist")

    # ------------------------------------------------------ multi-repo helpers
    def _involved(self, task: Task) -> list[RepoCtx]:
        """Repositories this feature changes: the primary, then the secondaries."""
        return [self.repos[self.primary]] + [self.repos[w.name] for w in task.secondary.values()
                                             if w.name in self.repos]

    def _base_of(self, task: Task, name: str) -> str:
        return task.base_branch if name == self.primary else task.secondary[name].base_branch

    def _open_secondary_branches(self, task: Task) -> None:
        """Create the feature branch in every secondary repository the spec names."""
        for name in self._spec(task).repositories:
            if name == self.primary or name in task.secondary:
                continue
            repo = self.repos[name]
            base = task.base_overrides.get(name, repo.cfg.workflow.base_branch)
            if not repo.git.branch_exists(base):
                raise PreflightError(f"{name}: base branch {base!r} does not exist")
            repo.git.create_branch(task.branch, base)
            task.secondary[name] = RepoWork(name, str(repo.root), task.branch, base)
            self._emit(f"{name}: created branch {task.branch} from {base}", task)

    def _commit_all(self, task: Task, message: str, *, actor: str = "orchestrator") -> str | None:
        """Commit in every involved repository; returns the primary's new sha."""
        primary_sha = None
        for repo in self._involved(task):
            sha = repo.git.commit_all(message, actor=actor)
            if not sha:
                continue
            if repo.name == self.primary:
                task.commits.append(sha)
                primary_sha = sha
            else:
                task.secondary[repo.name].commits.append(sha)
        return primary_sha

    def _feature_diff(self, task: Task) -> str:
        if not task.secondary:
            return self.git.diff(task.base_branch)
        parts = []
        for repo in self._involved(task):
            diff = repo.git.diff(self._base_of(task, repo.name))
            if diff.strip():
                parts.append(f"### Repository: {repo.name}\n\n{diff}")
        return "\n".join(parts)

    def _run_env(self, task: Task) -> dict[str, str]:
        """Environment for checks and Claude: every involved target's ``env``,
        plus the Go-module link of a multi-repo feature."""
        env: dict[str, str] = {}
        for repo in self._involved(task):
            env.update(repo.cfg.target.env)
        env.update(self._go_env(task))
        return env

    def _go_env(self, task: Task) -> dict[str, str]:
        """Link the Go modules of a multi-repo feature through a go.work kept in the
        state directory, so each repository builds against the others' working
        trees. Nothing is written into the repositories."""
        modules = [(repo.root / repo.cfg.target.go_module_dir).resolve()
                   for repo in self._involved(task) if repo.cfg.target.go_module_dir]
        if len(modules) < 2:
            return {}
        versions = [_go_version(m / "go.mod") for m in modules]
        go = max(versions, key=lambda v: tuple(int(x) for x in v.split("."))) if all(versions) else "1.24"
        work = self.state_root / "work" / task.id / "go.work"
        work.parent.mkdir(parents=True, exist_ok=True)
        work.write_text(f"go {go}\n\nuse (\n" + "".join(f"\t{m}\n" for m in modules) + ")\n")
        return {"GOWORK": str(work)}

    def _vm_checklist(self, task: Task) -> str:
        spec = self._spec(task)
        lines = [f"# Enterprise VM checklist — task {task.id}", "", f"**Feature:** {task.request}", "",
                 "Themis and the Harness run together on the VM. Nothing here is automated: "
                 "chopin never touches the VM.", "", "## Code", "",
                 "| Repository | Branch | Base | Head |", "|---|---|---|---|"]
        heads = {}
        for repo in self._involved(task):
            heads[repo.name] = repo.git.head()
            lines.append(f"| {repo.name} | `{task.branch}` | `{self._base_of(task, repo.name)}` | "
                         f"`{heads[repo.name][:12]}` |")
        if task.pull_requests:
            lines += ["", "## Pull requests", ""]
            lines += [f"- {name}: {url}" for name, url in task.pull_requests.items()]
        lines += ["", "## Build and run", ""]
        n = 1
        for repo in self._involved(task):
            for step in repo.cfg.target.vm_steps:
                values = _Placeholders(branch=task.branch, base=self._base_of(task, repo.name),
                                       sha=heads[repo.name], repo=repo.name,
                                       **{f"sha_{k.replace('-', '_')}": v for k, v in heads.items()})
                text = step.format_map(values)
                lines.append(f"{n}. **{repo.name}:** {text}")
                n += 1
        if n == 1:
            lines.append("(no vm_steps configured for these targets)")
        lines += ["", "## What to check (acceptance criteria)", ""]
        lines += [f"- [ ] {c}" for c in spec.acceptance_criteria] or ["- (none)"]
        lines += ["", "## Record the result", "",
                  "- Works: `chopin verify -m \"<what you ran and saw>\"` "
                  "(add `--where mac` when the check ran on the laptop, not the VM)",
                  "- Fails: `chopin reopen -m \"<what failed, with output>\"` — goes back to the fix loop", ""]
        return "\n".join(lines)

    def _spec(self, task: Task) -> Spec:
        if not task.spec:
            raise RuntimeError("task has no specification")
        return Spec(**task.spec)

    @staticmethod
    def _review_request(task: Task) -> str:
        if not task.owner_guidance:
            return task.request
        decisions = "\n".join(f"- {g}" for g in task.owner_guidance)
        return (f"{task.request}\n\n## Owner decisions (override the specification where they differ)\n\n"
                f"{decisions}\n\nDo not block on a point the owner has decided; review the rest as usual.")

    def _checks_summary(self, task: Task) -> str:
        return self.state.read_artifact(task.id, ARTIFACTS["tests"]) or "(no checks recorded)"

    def _write_artifact(self, task: Task, key, text: str) -> None:
        self.state.write_artifact(task.id, ARTIFACTS[key], text)

    def _append_artifact(self, task: Task, key: str, text: str) -> None:
        prior = self.state.read_artifact(task.id, ARTIFACTS[key]) or "# Implementation log\n\n"
        self._write_artifact(task, key, prior + text + "\n")

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

    def _awaiting_vm(self, task_id: str | None) -> Task:
        task = self.state.load(task_id)
        if task.state is not TaskState.AWAITING_VM_VERIFICATION:
            raise RuntimeError(f"task {task.id} is not awaiting VM verification (state {task.state.value})")
        return task

    def _repo_context(self, request: str, repo: RepoCtx) -> str:
        """Deterministic context bundle for the architect: tree, docs, and hits
        for identifiers mentioned in the request (e.g. KN-MODULE-4)."""
        budget = repo.cfg.openai.context_budget_chars
        parts: list[str] = []
        files = repo.ws.list_files()
        parts.append("### File tree\n\n" + "\n".join(files[:1500]))
        ids = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+\b", request))
        # CamelCase identifiers (SbomParser, handleGetProduct), not SHOUTING words.
        ids |= set(re.findall(r"\b[A-Za-z_]*[a-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*\b", request))
        for ident in sorted(ids):
            hits = repo.ws.search_code(ident, limit=50)
            parts.append(f"### References to `{ident}`\n\n" + ("\n".join(hits) or "(no matches in repository)"))
        seen: set[str] = set()
        for pattern in repo.cfg.workflow.context_globs:
            for f in repo.ws.list_files(pattern):
                if f in seen:
                    continue
                seen.add(f)
                parts.append(f"### {f}\n\n{repo.ws.read_file(f)}")
        out, used = [], 0
        for p in parts:
            if used + len(p) > budget:
                out.append(f"\n[... context truncated at {budget} characters ...]")
                break
            out.append(p)
            used += len(p)
        return "\n\n".join(out)


def _go_version(go_mod: Path) -> str:
    """The ``go`` directive of a go.mod, e.g. "1.25.0" -> "1.25.0"; "" if absent."""
    if not go_mod.exists():
        return ""
    m = re.search(r"^go\s+(\d+(?:\.\d+)*)\s*$", go_mod.read_text(), re.M)
    return m.group(1) if m else ""


class _Placeholders(dict):
    """format_map values for vm_steps; a repository not in this feature reads as such."""

    def __missing__(self, key: str) -> str:
        return f"<{key}: not part of this feature>"


def _place(where: str) -> str:
    return "the enterprise VM" if where == "vm" else where
