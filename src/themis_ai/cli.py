"""Goals & control interface.

    themis-ai run "Implement KN-MODULE-4"   start a task and drive it
    themis-ai status [TASK]                 state, metrics, pending approval
    themis-ai approve [TASK] [-m COMMENT]   grant the pending approval, continue
    themis-ai reject  [TASK] [-m COMMENT]   deny the pending approval, continue
    themis-ai resume  [TASK] [-g GUIDANCE]  continue after escalation / failure
    themis-ai abort   [TASK]                stop; keep the branch, return to base
    themis-ai history                       all tasks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import RuntimeConfig, resolve_state_dir
from .orchestrator import Orchestrator, PreflightError
from .state import StateManager, Task, TaskState

EXIT = {
    TaskState.COMPLETED: 0,
    TaskState.AWAITING_APPROVAL: 10,
    TaskState.ESCALATED: 11,
    TaskState.FAILED: 12,
    TaskState.REJECTED: 13,
    TaskState.ABORTED: 14,
}


def build_orchestrator(workspace: Path, config_path: Path | None) -> Orchestrator:
    from .agents.claude_code import ClaudeCodeImplementer
    from .agents.openai_agent import OpenAIArchitectReviewer

    cfg = RuntimeConfig.load(workspace, config_path)
    mcp = Path(cfg.claude.mcp_config) if cfg.claude.mcp_config else None
    return Orchestrator(
        workspace, cfg,
        architect=OpenAIArchitectReviewer(cfg.openai),
        implementer=ClaudeCodeImplementer(cfg.claude, workspace, mcp_config=mcp),
        on_event=lambda msg, task: print(f"[{task.id}] {msg}", file=sys.stderr, flush=True),
    )


def render(task: Task) -> str:
    m = task.metrics
    lines = [
        f"task:       {task.id}",
        f"request:    {task.request}",
        f"state:      {task.state.value}",
        f"branch:     {task.branch} (base {task.base_branch})",
        f"iterations: {m.iterations}/{task.iteration_budget}   test runs: {m.test_runs}",
        f"cost:       claude ${m.claude_cost_usd:.2f}   openai tokens in/out "
        f"{m.openai_input_tokens}/{m.openai_output_tokens}",
        f"commits:    {', '.join(c[:10] for c in task.commits) or '-'}",
    ]
    if task.approval and task.approval.granted is None:
        lines.append(f"APPROVAL NEEDED: {task.approval.action} ({task.approval.reason})")
        lines.append("  -> themis-ai approve | themis-ai reject")
    if task.state is TaskState.ESCALATED:
        lines.append("ESCALATED: review artifacts/ and decisions/ in the state directory, then "
                     "`themis-ai resume -g \"...\"` or `themis-ai abort`")
        if task.pending_feedback:
            lines.append("last feedback:\n" + task.pending_feedback[:2000])
    if task.last_error:
        lines.append(f"error:      {task.last_error}")
    if task.transitions:
        lines.append("transitions:")
        lines += [f"  {t.at}  {t.from_state} -> {t.to_state}  {t.note[:100]}" for t in task.transitions[-15:]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="themis-ai", description="Themis autonomous multi-agent runtime",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-w", "--workspace", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    p.add_argument("-c", "--config", type=Path, help="config file (default: <workspace>/.themis-ai.yaml)")
    p.add_argument("--json", action="store_true", help="print the task record as JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start a new task")
    run.add_argument("request", nargs="+")
    for name in ("status", "approve", "reject", "resume", "abort"):
        sp = sub.add_parser(name)
        sp.add_argument("task", nargs="?")
        if name in ("approve", "reject"):
            sp.add_argument("-m", "--comment")
        if name == "resume":
            sp.add_argument("-g", "--guidance")
    sub.add_parser("history")
    args = p.parse_args(argv)
    ws = args.workspace.resolve()

    try:
        cfg = RuntimeConfig.load(ws, args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    state = StateManager(resolve_state_dir(cfg, ws))
    if args.cmd == "history":
        for row in state.history():
            print(f"{row['id']}  {row['state']:<18} {row['branch']:<50} {row['request'][:60]}")
        return 0
    if args.cmd == "status":
        task = state.load(args.task)
    else:
        orch = build_orchestrator(ws, args.config)
        try:
            if args.cmd == "run":
                task = orch.start(" ".join(args.request))
            elif args.cmd in ("approve", "reject"):
                task = orch.decide(args.task, args.cmd == "approve", comment=args.comment)
            elif args.cmd == "resume":
                task = orch.resume(args.task, guidance=args.guidance)
            else:
                task = orch.abort(args.task)
        except PreflightError as e:
            print(f"preflight: {e}", file=sys.stderr)
            return 2
    print(json.dumps(task.to_dict(), indent=2) if args.json else render(task))
    return EXIT.get(task.state, 0)


if __name__ == "__main__":
    sys.exit(main())
