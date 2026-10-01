from __future__ import annotations

import json

import pytest

from themis_ai.agents.base import ReviewKind
from themis_ai.orchestrator import PreflightError
from themis_ai.state import TaskState

from .conftest import FakeArchitect, git, make_spec


def states(task):
    return [t.to_state for t in task.transitions]


def test_happy_path_completes_on_feature_branch(build, repo, state_dir):
    orch, architect, impl = build()
    task = orch.start("Implement KN-MODULE-4")

    assert task.state is TaskState.COMPLETED
    assert states(task) == ["ANALYZING", "DESIGN_READY", "IMPLEMENTING", "TESTING", "CODE_REVIEW",
                            "SECURITY_REVIEW", "FINAL_REVIEW", "APPROVED", "COMMITTED", "COMPLETED"]
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == task.branch
    assert (repo / "feature.txt").read_text() == "done\n"
    # main is untouched; work lives on the branch
    assert git(repo, "show", "main:feature.txt") == "todo\n"
    # the target repository only ever receives code: no artifacts, no state
    assert git(repo, "ls-files").split() == ["README.md", "feature.txt"]
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / ".agents").exists() and not (repo / "agent-state").exists()
    # artifacts live in the state directory, per task
    artifacts = state_dir / "artifacts" / task.id
    for rel in ("openai/architecture.md", "openai/final-review.md", "claude/test-results.md",
                "claude/implementation.md"):
        assert (artifacts / rel).exists(), rel
    assert "feature.txt" in architect.diffs[0][1]
    # the architect was grounded with the repository hit for the requested ID
    assert "KN-MODULE-4 lives in internal/kn" in architect.contexts[0]
    # persisted state and metrics
    saved = json.loads((state_dir / "tasks" / f"{task.id}.json").read_text())
    assert saved["state"] == "COMPLETED"
    assert task.metrics.claude_cost_usd == 0.5
    assert task.metrics.openai_input_tokens == 100 + 3 * 10
    assert (state_dir / "audit.log").exists()


def test_test_failure_loops_through_fixing(build):
    orch, _, impl = build(outputs=["wrong", "done"])
    task = orch.start("Implement it")

    assert task.state is TaskState.COMPLETED
    assert task.metrics.iterations == 1
    assert task.metrics.test_runs == 2
    assert "FIXING" in states(task)
    assert "checks failed" in impl.calls[1]["feedback"]
    assert impl.calls[1]["session_id"] == "sess-1"


def test_review_rejection_goes_back_to_fixing_and_retests(build):
    architect = FakeArchitect(reviews={ReviewKind.SECURITY: [False, True]})
    orch, _, impl = build(architect=architect, outputs=["done", "done"])
    task = orch.start("Implement it")

    assert task.state is TaskState.COMPLETED
    s = states(task)
    i = s.index("SECURITY_REVIEW")
    assert s[i + 1:i + 3] == ["FIXING", "TESTING"]
    assert "security reviewer rejected" in impl.calls[1]["feedback"]


def test_blocking_finding_overrides_accept_verdict(build):
    architect = FakeArchitect()

    def review(kind, *a):
        from themis_ai.agents.base import Finding, Review
        findings = [Finding("critical", "sql injection")] if not architect.diffs else []
        architect.diffs.append((kind, ""))
        return Review(kind, True, "looks fine", findings)

    architect.review = review
    orch, _, _ = build(architect=architect, outputs=["done", "done"])
    task = orch.start("Implement it")
    assert task.state is TaskState.COMPLETED
    assert task.metrics.iterations == 1


def test_escalates_when_iteration_budget_exhausted_then_resumes(build, config):
    config.workflow.max_iterations = 2
    orch, _, impl = build(outputs=["a", "b", "c", "done"])
    task = orch.start("Implement it")

    assert task.state is TaskState.ESCALATED
    assert task.metrics.iterations == 2
    assert len(impl.calls) == 3

    task = orch.resume(task.id, guidance="the file must contain exactly 'done'")
    assert task.state is TaskState.COMPLETED
    assert "Guidance from the owner" in impl.calls[3]["feedback"]
    assert task.owner_guidance == ["the file must contain exactly 'done'"]


def test_owner_guidance_reaches_later_reviews(build, config):
    config.workflow.max_iterations = 1
    architect = FakeArchitect(reviews={ReviewKind.CODE: [False, False, True]})
    requests = []
    review = architect.review
    architect.review = lambda kind, request, *a: (requests.append(request), review(kind, request, *a))[1]
    orch, _, _ = build(architect=architect, outputs=["done", "done", "done"])
    task = orch.start("Implement it")
    assert task.state is TaskState.ESCALATED
    assert "Owner decisions" not in requests[0]

    task = orch.resume(task.id, guidance="keep the event id as the dedup key")
    assert task.state is TaskState.COMPLETED
    assert all("keep the event id as the dedup key" in r for r in requests[2:])
    assert len(requests) > 2


def test_merge_delivery_requires_approval(build, repo, config, state_dir):
    config.workflow.delivery = "merge"
    orch, _, _ = build()
    task = orch.start("Implement it")

    assert task.state is TaskState.AWAITING_APPROVAL
    assert task.approval.action == "git_merge"
    assert git(repo, "show", "main:feature.txt") == "todo\n"

    task = orch.decide(task.id, True, comment="lgtm")
    assert task.state is TaskState.COMPLETED
    assert git(repo, "show", "main:feature.txt") == "done\n"
    decisions = list((state_dir / "decisions" / task.id).glob("*-human-approval.json"))
    assert decisions


def test_merge_rejected_keeps_branch(build, repo, config):
    config.workflow.delivery = "merge"
    orch, _, _ = build()
    task = orch.start("Implement it")
    task = orch.decide(task.id, False)
    assert task.state is TaskState.COMPLETED
    assert "declined" in task.transitions[-1].note
    assert git(repo, "show", "main:feature.txt") == "todo\n"
    assert git(repo, "show", f"{task.branch}:feature.txt") == "done\n"


def test_open_questions_pause_for_design_approval(build):
    architect = FakeArchitect(spec=make_spec(open_questions=["KN-MODULE-4 is not defined anywhere"]))
    orch, _, impl = build(architect=architect)
    task = orch.start("Implement KN-MODULE-4")

    assert task.state is TaskState.AWAITING_APPROVAL
    assert task.approval.action == "approve_design"
    assert impl.calls == []

    task = orch.decide(task.id, False, comment="spec is wrong")
    assert task.state is TaskState.REJECTED


def test_design_approval_granted_continues(build, config):
    config.workflow.require_design_approval = True
    orch, _, _ = build()
    task = orch.start("Implement it")
    assert task.state is TaskState.AWAITING_APPROVAL
    task = orch.decide(task.id, True)
    assert task.state is TaskState.COMPLETED
    assert task.approval is None


def test_agent_failure_is_recorded_and_retryable(build):
    orch, _, _ = build(outputs=[None, "done"])
    task = orch.start("Implement it")
    assert task.state is TaskState.FAILED
    assert task.resume_state is TaskState.IMPLEMENTING
    assert "boom" in task.last_error

    task = orch.resume(task.id)
    assert task.state is TaskState.COMPLETED


def test_timed_out_implementation_resumes_from_partial_work(build, repo):
    orch, _, impl = build(outputs=["TIMEOUT", "done"])
    task = orch.start("Implement it")
    assert task.state is TaskState.FAILED
    assert (repo / "feature.txt").read_text() == "partial\n"

    task = orch.resume(task.id)
    assert task.state is TaskState.COMPLETED
    assert impl.calls[0]["feedback"] is None
    assert "Previous attempt was cut off" in impl.calls[1]["feedback"]
    assert task.pending_feedback is None


def test_empty_change_is_not_reviewed(build, repo):
    orch, architect, _ = build(outputs=["todo", "done"])
    config_test = orch.cfg.tests
    config_test.command = []  # checks trivially pass, so the empty diff reaches review
    task = orch.start("Implement it")
    assert "empty diff" in " ".join(t.note for t in task.transitions)
    assert task.state is TaskState.COMPLETED


def test_preflight_rejects_dirty_tree(build, repo):
    (repo / "feature.txt").write_text("local edit\n")
    orch, _, _ = build()
    with pytest.raises(PreflightError):
        orch.start("Implement it")


def test_abort_returns_to_base(build, repo, config):
    config.workflow.delivery = "push"
    orch, _, _ = build()
    task = orch.start("Implement it")
    assert task.approval.action == "git_push"
    task = orch.abort(task.id)
    assert task.state is TaskState.ABORTED
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert task.branch in git(repo, "branch")


def test_preflight_rejects_state_dir_inside_workspace(build, repo, config):
    config.workflow.state_dir = str(repo / "agent-state")
    orch, _, _ = build()
    with pytest.raises(PreflightError, match="inside the workspace"):
        orch.start("Implement it")
