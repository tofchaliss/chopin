"""Goals & control interface (``chopin``; ``themis-ai`` is the same command).

    chopin -p PROJECT run "Add retry to outward actions" [--base themis=feat/x]
                                         start a feature and drive it
    chopin status [TASK]                 state, metrics, pending approval
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
from .orchestrator import Orchestrator, PreflightError
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


def load_repos(workspace: Path, config: Path | None, project: Path | None) -> list[Repo]:
    if project is not None:
        return load_project(project)
    cfg = RuntimeConfig.load(workspace, config)
    return [Repo(cfg.target.name or workspace.name, workspace, cfg)]


def render(task: Task) -> str:
    m = task.metrics
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
    if task.approval and task.approval.granted is None:
        lines.append(f"APPROVAL NEEDED: {task.approval.action} ({task.approval.reason})")
        lines.append("  -> chopin approve | chopin reject")
    if task.state is TaskState.AWAITING_VM_VERIFICATION:
        lines.append("AWAITING VM VERIFICATION: follow artifacts/<task>/vm-checklist.md in the state "
                     "directory, then `chopin verify -m \"...\"` or `chopin reopen -m \"...\"`")
    for v in task.vm_results[-3:]:
        lines.append(f"{v.get('where', 'vm')} {'PASS' if v['passed'] else 'FAIL'} {v['at']} by {v['by']}: "
                     f"{v['evidence'][:200]}")
    for name, url in task.pull_requests.items():
        lines.append(f"PR {name}: {url}")
    if task.state is TaskState.ESCALATED:
        lines.append("ESCALATED: review artifacts/ and decisions/ in the state directory, then "
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
    for name in ("status", "approve", "reject", "resume", "abort", "verify", "reopen"):
        sp = sub.add_parser(name)
        sp.add_argument("task", nargs="?")
        if name in ("approve", "reject"):
            sp.add_argument("-m", "--comment")
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
        repos = load_repos(ws, args.config, args.project)
        bases = dict(b.split("=", 1) for b in getattr(args, "base", []))
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    state = StateManager(resolve_state_dir(repos[0].cfg, repos[0].root))
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
                task = orch.start(" ".join(args.request), bases=bases)
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
    print(json.dumps(task.to_dict(), indent=2) if args.json else render(task))
    return EXIT.get(task.state, 0)


if __name__ == "__main__":
    sys.exit(main())
