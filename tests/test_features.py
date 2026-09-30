"""Multi-repository features, Go-module linking and enterprise-VM verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from themis_ai.agents.base import ImplementationResult, Usage
from themis_ai.config import CheckConfig, Repo, RuntimeConfig, load_project
from themis_ai.orchestrator import Orchestrator, PreflightError
from themis_ai.state import TaskState

from .conftest import FakeArchitect, git, make_spec

PASS_IF_DONE = [sys.executable, "-c",
                "import pathlib,sys; sys.exit(0 if pathlib.Path('feature.txt').read_text().strip()"
                "=='done' else 1)"]


def make_repo(root: Path, go_version: str | None = None) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "owner@example.com")
    git(root, "config", "user.name", "Owner")
    (root / "README.md").write_text(f"# {root.name}\n")
    (root / "feature.txt").write_text("todo\n")
    if go_version:
        (root / "go.mod").write_text(f"module example.com/{root.name}\n\ngo {go_version}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def repo_config(name: str, state_dir: Path | None = None, go: bool = False) -> RuntimeConfig:
    cfg = RuntimeConfig()
    cfg.target.name = name
    cfg.tests = CheckConfig(command=PASS_IF_DONE)
    cfg.lint = CheckConfig()
    if go:
        cfg.target.go_module_dir = "."
    if state_dir:
        cfg.workflow.state_dir = str(state_dir)
    return cfg


class MultiRepoImplementer:
    """Writes 'done' into feature.txt of the primary and every extra dir it is given."""

    def __init__(self, primary: Path):
        self.primary = primary
        self.calls: list[dict] = []

    def implement(self, request, spec, *, feedback=None, session_id=None, extra_dirs=None, env=None):
        self.calls.append({"feedback": feedback, "extra_dirs": extra_dirs, "env": env})
        for root in [self.primary, *(extra_dirs or [])]:
            (Path(root) / "feature.txt").write_text("done\n")
        return ImplementationResult(True, "done in all repos", "sess", Usage(cost_usd=0.1))


@pytest.fixture
def platform(tmp_path):
    """A Harness-like primary and a Themis-like secondary, both Go modules."""
    runtime = make_repo(tmp_path / "runtime", go_version="1.24")
    core = make_repo(tmp_path / "core", go_version="1.25.0")
    state = tmp_path / "state"

    def build(repositories=("runtime", "core"), verification="none", delivery="commit"):
        cfg = repo_config("runtime", state, go=True)
        cfg.workflow.verification = verification
        cfg.workflow.delivery = delivery
        architect = FakeArchitect(make_spec(repositories=list(repositories),
                                            acceptance_criteria=["feature.txt says done in both"]))
        impl = MultiRepoImplementer(runtime)
        orch = Orchestrator(runtime, cfg, architect, impl,
                            others=[Repo("core", core, repo_config("core", go=True))])
        return orch, architect, impl

    return runtime, core, state, build


def test_feature_spanning_two_repositories(platform):
    runtime, core, state, build = platform
    orch, architect, impl = build()
    task = orch.start("Add retry to outward actions")

    assert task.state is TaskState.COMPLETED, task.last_error
    # the architect saw both repositories, primary first
    ctx = architect.contexts[0]
    assert ctx.index("# Repository: runtime (primary)") < ctx.index("# Repository: core")
    # both repositories got the same feature branch, with commits, from main
    assert git(core, "rev-parse", "--abbrev-ref", "HEAD").strip() == task.branch
    assert task.secondary["core"].base_branch == "main" and task.secondary["core"].commits
    assert task.commits
    assert git(core, "show", "main:feature.txt") == "todo\n"
    # Claude worked in both: the secondary repository was added to its session
    assert impl.calls[0]["extra_dirs"] == [core]
    # reviewers saw one diff with a section per repository
    diff = architect.diffs[0][1]
    assert "### Repository: runtime" in diff and "### Repository: core" in diff
    # checks ran per repository, and neither repository carries orchestrator files
    for repo in (runtime, core):
        assert git(repo, "ls-files").split() == ["README.md", "feature.txt", "go.mod"]
    checks = (state / "artifacts" / task.id / "claude" / "test-results.md").read_text()
    assert "runtime:tests: PASS" in checks and "core:tests: PASS" in checks


def test_secondary_repository_untouched_when_spec_does_not_name_it(platform):
    runtime, core, state, build = platform
    orch, _, impl = build(repositories=("runtime",))
    task = orch.start("Harness-only change")

    assert task.state is TaskState.COMPLETED
    assert task.secondary == {}
    assert impl.calls[0]["extra_dirs"] is None
    assert git(core, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(core, "branch", "--list").split() == ["*", "main"]


def test_unknown_repository_in_spec_pauses_for_the_owner(platform):
    _, _, _, build = platform
    orch, _, _ = build(repositories=("runtime", "nonexistent"))
    task = orch.start("Change something")

    assert task.state is TaskState.AWAITING_APPROVAL
    assert task.approval.action == "approve_design"
    assert any("nonexistent" in q for q in task.spec["open_questions"])
    assert task.spec["repositories"] == ["runtime"]


def test_go_modules_are_linked_through_a_go_work_outside_the_repositories(platform):
    runtime, core, state, build = platform
    orch, _, impl = build()
    task = orch.start("Change both modules")

    env = impl.calls[0]["env"]
    work = Path(env["GOWORK"])
    assert state in work.parents
    text = work.read_text()
    assert text.startswith("go 1.25.0\n")  # the highest go directive of the linked modules
    assert str(runtime) in text and str(core) in text
    assert not (runtime / "go.work").exists() and not (core / "go.work").exists()
    assert task.state is TaskState.COMPLETED


def test_single_repository_feature_gets_no_go_work(platform):
    _, _, _, build = platform
    orch, _, impl = build(repositories=("runtime",))
    orch.start("Harness-only change")
    assert impl.calls[0]["env"] is None


def test_base_branch_override_for_a_secondary_repository(platform):
    _, core, _, build = platform
    git(core, "branch", "feat/harness-integration")
    orch, _, _ = build()
    task = orch.start("Build on the integration branch", bases={"core": "feat/harness-integration"})

    assert task.state is TaskState.COMPLETED, task.last_error
    assert task.secondary["core"].base_branch == "feat/harness-integration"


def test_base_override_for_an_unknown_repository_is_refused(platform):
    _, _, _, build = platform
    orch, _, _ = build()
    with pytest.raises(PreflightError, match="unknown repository"):
        orch.start("x", bases={"nope": "main"})


def test_preflight_checks_every_repository(platform):
    _, core, _, build = platform
    (core / "stray.txt").write_text("uncommitted\n")
    orch, _, _ = build()
    with pytest.raises(PreflightError, match="core: working tree has uncommitted changes"):
        orch.start("x")


def test_vm_verification_gates_completion_and_reopen_feeds_the_fix_loop(platform):
    runtime, core, state, build = platform
    orch, _, impl = build(verification="vm")
    task = orch.start("Add retry to outward actions")

    assert task.state is TaskState.AWAITING_VM_VERIFICATION, task.last_error
    checklist = (state / "artifacts" / task.id / "vm-checklist.md").read_text()
    assert task.branch in checklist and "| core |" in checklist and "| runtime |" in checklist
    assert "- [ ] feature.txt says done in both" in checklist
    assert "chopin verify" in checklist and "chopin reopen" in checklist

    task = orch.reopen(task.id, "retry never fired: intake log shows one attempt")
    assert task.state is TaskState.AWAITING_VM_VERIFICATION  # fixed, re-tested, re-reviewed
    assert "retry never fired" in impl.calls[-1]["feedback"]
    assert "enterprise VM" in impl.calls[-1]["feedback"]
    assert [v["passed"] for v in task.vm_results] == [False]

    task = orch.verify(task.id, "retry fired 3x on the VM; proposal raised")
    assert task.state is TaskState.COMPLETED
    assert [v["passed"] for v in task.vm_results] == [False, True]
    decisions = list((state / "decisions" / task.id).glob("*-vm-verification.json"))
    assert len(decisions) == 2


def test_verify_requires_a_task_awaiting_vm_verification(platform):
    _, _, _, build = platform
    orch, _, _ = build(verification="none")
    task = orch.start("x")
    with pytest.raises(RuntimeError, match="not awaiting VM verification"):
        orch.verify(task.id, "looks fine")


def test_push_delivers_every_repository_after_one_approval(platform, tmp_path):
    runtime, core, _, build = platform
    remotes = {}
    for repo in (runtime, core):
        remote = tmp_path / f"{repo.name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(repo, "remote", "add", "origin", str(remote))
        remotes[repo.name] = remote
    orch, _, _ = build(delivery="push", verification="vm")
    task = orch.start("Add retry to outward actions")

    assert task.state is TaskState.AWAITING_APPROVAL and task.approval.action == "git_push"
    for remote in remotes.values():
        assert git(remote, "branch", "--list").strip() == ""
    task = orch.decide(task.id, True, comment="push both")
    assert task.state is TaskState.AWAITING_VM_VERIFICATION, task.last_error
    for remote in remotes.values():
        assert task.branch in git(remote, "branch", "--list")


def test_vm_steps_render_per_repository_and_tolerate_missing_repositories(platform):
    runtime, core, state, build = platform
    orch, _, _ = build(repositories=("runtime",), verification="vm")
    orch.repos["runtime"].cfg.target.vm_steps = ["checkout {branch} at {sha}",
                                                 "pin core to {sha_core}"]
    task = orch.start("Harness-only change")
    checklist = (state / "artifacts" / task.id / "vm-checklist.md").read_text()
    assert f"checkout {task.branch} at {task.commits[-1]}" in checklist
    assert "<sha_core: not part of this feature>" in checklist


def test_load_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOPIN_HOME", str(tmp_path / "home"))
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "a.yaml").write_text("target:\n  name: a\nworkflow:\n  delivery: commit\n")
    (targets / "b.yaml").write_text("target:\n  name: b\n")
    project = tmp_path / "projects" / "plat.yaml"
    project.parent.mkdir()
    project.write_text(f"name: plat\nrepos:\n"
                       f"  - {{name: a, path: {tmp_path / 'ra'}, config: ../targets/a.yaml}}\n"
                       f"  - {{name: b, path: {tmp_path / 'rb'}, config: ../targets/b.yaml}}\n"
                       f"overrides:\n  workflow:\n    delivery: push\n    verification: vm\n")
    repos = load_project(project)
    assert [r.name for r in repos] == ["a", "b"]
    assert repos[0].root == (tmp_path / "ra").resolve()
    assert repos[0].cfg.workflow.delivery == "push"         # overrides apply to the primary
    assert repos[1].cfg.workflow.delivery == "commit"       # not to secondaries
    assert repos[0].cfg.workflow.state_dir == str(tmp_path / "home" / "plat")

    project.write_text("name: plat\nrepos: []\n")
    with pytest.raises(ValueError, match="no repos"):
        load_project(project)
    project.write_text("name: plat\nrepo: []\n")
    with pytest.raises(ValueError, match="unknown project key"):
        load_project(project)


def test_shipped_platform_project_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOPIN_HOME", str(tmp_path))
    project = Path(__file__).resolve().parent.parent / "config" / "projects" / "themis-platform.yaml"
    repos = load_project(project)
    assert [r.name for r in repos] == ["themis-ai-runtime", "themis"]
    primary = repos[0].cfg
    assert primary.workflow.delivery == "push"
    assert primary.workflow.verification == "vm"
    assert primary.workflow.require_design_approval is True
    assert primary.target.go_module_dir == "src/harness"
    assert repos[1].cfg.target.go_module_dir == "."
    assert "Integration with Themis" in primary.target.notes()


def test_cli_verify_and_reopen_parse(tmp_path, monkeypatch, platform):
    from themis_ai import cli

    runtime, core, state, build = platform
    orch, _, _ = build(verification="vm")
    task = orch.start("x")
    monkeypatch.setattr(cli, "build_orchestrator", lambda repos: orch)
    cfg = tmp_path / "t.yaml"
    cfg.write_text(f"target:\n  name: runtime\nworkflow:\n  state_dir: {state}\n")
    code = cli.main(["-c", str(cfg), "-w", str(runtime), "verify", task.id, "-m", "works on the VM"])
    assert code == 0
    assert cli.main(["-c", str(cfg), "-w", str(runtime), "verify", task.id, "-m", "again"]) == 2


def test_claude_gets_the_other_repositories_and_the_go_workspace(tmp_path):
    from themis_ai.agents.claude_code import ClaudeCodeImplementer
    from themis_ai.config import ClaudeConfig

    seen = {}

    def runner(cmd, cwd, timeout, env):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0, '{"result": "ok", "session_id": "s"}', "")

    impl = ClaudeCodeImplementer(ClaudeConfig(), tmp_path, runner=runner)
    impl.implement("do it", make_spec(repositories=["runtime", "core"]),
                   extra_dirs=[tmp_path / "core"], env={"GOWORK": "/state/go.work"})
    cmd = seen["cmd"]
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "core")
    assert str(tmp_path / "core") in cmd[2]          # the prompt names the other repository
    assert seen["env"] == {"GOWORK": "/state/go.work"}
