"""Goals & control interface (``chopin``; ``themis-ai`` is the same command).

    chopin -p PROJECT run "Add retry to outward actions" [--base themis=feat/x]
                                         start a feature and drive it
    chopin status [TASK]                 state, metrics, pending approval
    chopin run "..." --dry-run           build, test and review on local branches only
    chopin run "..." --primary themis    lead in another repository of the project
    chopin revise  [TASK] -m FEEDBACK    design discussion: architect redesigns with it
    chopin approve [TASK] [-m COMMENT]   grant the pending approval, continue
    chopin reject  [TASK] [-m COMMENT]   deny the pending approval, continue
    chopin resume  [TASK] [-g GUIDANCE]  continue after escalation / failure
    chopin verify  [TASK] -m EVIDENCE    it works on the enterprise VM: close it
    chopin reopen  [TASK] -m FAILURE     it failed on the VM: back to the fix loop
    chopin abort   [TASK]                stop; keep the branches, return to base
    chopin history                       all tasks

Select the repositories with -p (a project: several repositories developed
together, e.g. config/projects/themis-platform.yaml) or with -c/-w (one).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Repo, RuntimeConfig, load_project, resolve_state_dir
from .orchestrator import ARTIFACTS, Orchestrator, PreflightError
from .state import StateManager, Task, TaskState

EXIT = {
    TaskState.COMPLETED: 0,
    TaskState.AWAITING_APPROVAL: 10,
    TaskState.ESCALATED: 11,
    TaskState.FAILED: 12,
    TaskState.REJECTED: 13,
    TaskState.ABORTED: 14,
    TaskState.AWAITING_VM_VERIFICATION: 15,
}


def build_orchestrator(repos: list[Repo]) -> Orchestrator:
    from .agents.claude_code import ClaudeCodeImplementer
    from .agents.openai_agent import OpenAIArchitectReviewer

    primary, others = repos[0], repos[1:]
    cfg = primary.cfg
    mcp = Path(cfg.claude.mcp_config) if cfg.claude.mcp_config else None
    return Orchestrator(
        primary.root, cfg,
        architect=OpenAIArchitectReviewer(cfg.openai, target_notes=combined_notes(repos)),
        implementer=ClaudeCodeImplementer(cfg.claude, primary.root, mcp_config=mcp),
        on_event=lambda msg, task: print(f"[{task.id}] {msg}", file=sys.stderr, flush=True),
        others=others,
    )


def combined_notes(repos: list[Repo]) -> str:
    """Each repository's architecture notes, labelled; a single repo's notes as is."""
    if len(repos) == 1:
        return repos[0].cfg.target.notes()
    return "\n\n".join(f"# Repository: {r.name}{' (primary)' if i == 0 else ''}\n\n"
                        + (r.cfg.target.notes() or "(no architecture notes configured)")
                        for i, r in enumerate(repos))


def load_repos(workspace: Path, config: Path | None, project: Path | None,
               primary: str | None = None) -> list[Repo]:
    if project is not None:
        return load_project(project, primary)
    cfg = RuntimeConfig.load(workspace, config)
    return [Repo(cfg.target.name or workspace.name, workspace, cfg)]


def render(task: Task, state: StateManager | None = None) -> str:
    """Human-readable task record. With ``state``, it also prints where the
    artifacts are, so the design and the VM checklist can be opened directly."""
    m = task.metrics

    def artifact(rel: str) -> str:
        return str(state.artifact_path(task.id, rel)) if state else f"artifacts/{task.id}/{rel}"

    lines = [
        f"task:       {task.id}",
        f"request:    {task.request}",
        f"state:      {task.state.value}",
        f"branch:     {task.branch} (base {task.base_branch})"
        + (f"   repo {task.repo}" if task.repo else ""),
        *[f"            {w.name}: {w.branch} (base {w.base_branch}), "
          f"commits {', '.join(c[:10] for c in w.commits) or '-'}" for w in task.secondary.values()],
        f"iterations: {m.iterations}/{task.iteration_budget}   test runs: {m.test_runs}",
        f"cost:       claude ${m.claude_cost_usd:.2f}   openai tokens in/out "
        f"{m.openai_input_tokens}/{m.openai_output_tokens}",
        f"commits:    {', '.join(c[:10] for c in task.commits) or '-'}",
    ]
    if state:
        lines.append(f"artifacts:  {state.artifact_path(task.id, '')}")
    if task.spec:
        lines.append(f"design:     {artifact(ARTIFACTS['architecture'])}")
    if task.approval and task.approval.granted is None:
        lines.append(f"APPROVAL NEEDED: {task.approval.action} ({task.approval.reason})")
        lines.append("  -> chopin approve | chopin reject")
    if task.dry_run:
        lines.append("DRY RUN: local branches only; nothing is pushed")
    if task.design_rounds:
        lines.append(f"design rounds: {len(task.design_rounds)} (previous designs kept as "
                     "openai/architecture-rN.md)")
    if task.approval and task.approval.granted is None and task.approval.action == "approve_design":
        lines.append(f"  -> read the design: cat {artifact(ARTIFACTS['architecture'])}")
        lines.append("  -> or discuss it: chopin revise -m \"<your feedback>\"")
    if task.state is TaskState.AWAITING_VM_VERIFICATION:
        lines.append(f"AWAITING VM VERIFICATION: follow {artifact(ARTIFACTS['vm'])}, "
                     "then `chopin verify -m \"...\"` or `chopin reopen -m \"...\"`")
    for v in task.vm_results[-3:]:
        lines.append(f"{v.get('where', 'vm')} {'PASS' if v['passed'] else 'FAIL'} {v['at']} by {v['by']}: "
                     f"{v['evidence'][:200]}")
    for name, url in task.pull_requests.items():
        lines.append(f"PR {name}: {url}")
    if task.state is TaskState.ESCALATED:
        lines.append(f"ESCALATED: review {artifact('')} (reviews: openai/, test results: claude/), then "
                     "`chopin resume -g \"...\"` or `chopin abort`")
        if task.pending_feedback:
            lines.append("last feedback:\n" + task.pending_feedback[:2000])
    if task.last_error:
        lines.append(f"error:      {task.last_error}")
    if task.transitions:
        lines.append("transitions:")
        lines += [f"  {t.at}  {t.from_state} -> {t.to_state}  {t.note[:100]}" for t in task.transitions[-15:]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chopin", description="chopin: orchestrated development of Themis "
                                "and its AI Harness", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("-p", "--project", type=Path,
                   help="project file: several repositories developed together (overrides -w/-c)")
    p.add_argument("-w", "--workspace", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    p.add_argument("-c", "--config", type=Path, help="config file (default: <workspace>/.themis-ai.yaml)")
    p.add_argument("--json", action="store_true", help="print the task record as JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start a new task")
    run.add_argument("request", nargs="+")
    run.add_argument("--base", action="append", default=[], metavar="REPO=BRANCH",
                     help="base branch for a repository (repeatable), e.g. themis=feat/harness-integration")
    run.add_argument("--dry-run", action="store_true",
                     help="local branches only: build, test and review, but never push, open a PR or wait for the VM")
    run.add_argument("--primary", metavar="REPO",
                     help="with -p: the repository this feature leads in (default: the project's first)")
    for name in ("status", "approve", "reject", "revise", "resume", "abort", "verify", "reopen"):
        sp = sub.add_parser(name)
        sp.add_argument("task", nargs="?")
        if name in ("approve", "reject"):
            sp.add_argument("-m", "--comment")
        if name == "revise":
            sp.add_argument("-m", "--message", required=True,
                            help="your feedback on the proposed design")
        if name in ("verify", "reopen"):
            sp.add_argument("-m", "--message", required=True,
                            help="what you ran and what you saw")
            sp.add_argument("--where", default="vm",
                            help="where you checked: vm (default), mac, or any short label")
        if name == "resume":
            sp.add_argument("-g", "--guidance")
    sub.add_parser("history")
    args = p.parse_args(argv)
    ws = args.workspace.resolve()

    try:
        repos = load_repos(ws, args.config, args.project, getattr(args, "primary", None))
        bases = dict(b.split("=", 1) for b in getattr(args, "base", []))
        state = StateManager(resolve_state_dir(repos[0].cfg, repos[0].root))
        if args.project is not None and args.cmd not in ("run", "history"):
            # A task keeps the primary repository it was started with.
            try:
                led = state.load(getattr(args, "task", None)).repo
            except FileNotFoundError:
                led = ""
            if led and led != repos[0].name:
                repos = load_repos(ws, args.config, args.project, led)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if args.cmd == "history":
        for row in state.history():
            print(f"{row['id']}  {row['state']:<18} {row['branch']:<50} {row['request'][:60]}")
        return 0
    if args.cmd == "status":
        task = state.load(args.task)
    else:
        orch = build_orchestrator(repos)
        try:
            if args.cmd == "run":
                task = orch.start(" ".join(args.request), bases=bases, dry_run=args.dry_run)
            elif args.cmd == "revise":
                task = orch.revise(args.task, args.message)
            elif args.cmd == "verify":
                task = orch.verify(args.task, args.message, where=args.where)
            elif args.cmd == "reopen":
                task = orch.reopen(args.task, args.message, where=args.where)
            elif args.cmd in ("approve", "reject"):
                task = orch.decide(args.task, args.cmd == "approve", comment=args.comment)
            elif args.cmd == "resume":
                task = orch.resume(args.task, guidance=args.guidance)
            else:
                task = orch.abort(args.task)
        except PreflightError as e:
            print(f"preflight: {e}", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    print(json.dumps(task.to_dict(), indent=2) if args.json else render(task, state))
    return EXIT.get(task.state, 0)


if __name__ == "__main__":
    sys.exit(main())
