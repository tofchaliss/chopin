# chopin

Autonomous multi-agent runtime for [Themis](https://github.com/tofchaliss/themis).

**OpenAI** handles architecture and independent review. **Claude Code** handles implementation.
An **orchestrator** controls the loop between them, working on a **local Git** checkout and
exposing capabilities through a small **MCP** server. GitHub is optional.

> Agents reason. MCP provides capabilities. The orchestrator controls the workflow.
> Claude is never the sole judge of whether its own implementation is correct.

This is the v1 design: one orchestrator, one OpenAI architect/reviewer (four roles chosen by
prompt), Claude Code, a shared local workspace, local Git and a small MCP server. We add
specialist agents only when real work needs them.

## Where code lives and where it is tested

| What | Where |
|---|---|
| The code the agents write | The checkout you pass as `--workspace` (for example `/opt/themis`), on a new local branch `agent/<slug>-<id>` created from `main` |
| Who edits it | Claude Code edits the working tree. It cannot commit, push, merge, reset or reach the network. |
| Who commits it | The orchestrator. It commits each iteration to the feature branch, so every step can be diffed and rolled back. |
| Where tests run | The same checkout, on the feature branch, on your server. The orchestrator runs the configured command (Themis: `make test`, which is `go test ./... -count=1`) itself and never trusts an agent's claim that tests passed. |
| Agent artifacts | `.agents/openai/{architecture,review,security,final-review}.md` and `.agents/claude/{implementation,test-results}.md`, committed with the code |
| Runtime state | `agent-state/` (task records, decisions, audit log), git-excluded and never committed |
| `main` / GitHub | Unchanged unless `delivery: merge` or `delivery: push` is set **and** you approve |

## Architecture

```
 YOU ── run / status / approve / reject / resume / abort
  │
  ▼
┌──────────────────────── ORCHESTRATOR (themis_ai.orchestrator) ─────────────────────┐
│ Task planner → Agent router → Workflow engine (explicit state machine)             │
│ State manager (agent-state/)          Guardrails & approval (themis_ai.guardrails) │
└───────────────┬─────────────────────────────────────────────┬──────────────────────┘
                ▼                                             ▼
   OpenAI (agents/openai_agent.py)                Claude Code (agents/claude_code.py)
   architect · code review ·                      `claude -p`, restricted tool list,
   security review · final approver               session resumed across fix rounds
   JSON-schema-constrained verdicts
                └──────────────────────┬──────────────────────┘
                                       ▼
          Workspace + LocalGit (in-process)  ·  themis-ai-mcp (the same tools over MCP)
          files · search · tests · lint · git status/diff/log · Themis API (read-only)
                                       ▼
                       LOCAL THEMIS REPO (.git)  ──optional push──▶  GitHub
```

### State machine

```
NEW → ANALYZING → DESIGN_READY → IMPLEMENTING → TESTING ──fail──▶ FIXING ─┐
                                                   │    ◀──────────────────┘
                                                   ▼
                     CODE_REVIEW → SECURITY_REVIEW → FINAL_REVIEW   (any reject → FIXING)
                                                   ▼
                                  APPROVED → COMMITTED → COMPLETED

control states: AWAITING_APPROVAL · ESCALATED · FAILED · REJECTED · ABORTED
```

- **FIXING always returns to TESTING.** After any fix, tests run again and review restarts at
  CODE_REVIEW.
- A reviewer's **accept** is overridden if any finding is at or above `block_on_severity`
  (default `high`).
- An **empty diff** is sent back to FIXING without spending a review.
- **Escalation.** After `max_iterations` fix cycles (default 3), or once spend passes
  `max_cost_usd`, the task stops at ESCALATED with the last feedback. Continue with
  `themis-ai resume -g "guidance"` or stop with `themis-ai abort`.
- **Design gate.** If the architect reports open questions (for example the requested module
  doesn't exist in the repository), the task waits for your approve/reject before any code is
  written. `require_design_approval: true` makes this gate apply to every task.
- **Failures.** An agent or tool failure moves the task to FAILED with the error recorded.
  `themis-ai resume` retries from the step that failed.

### Guardrails

| Automatically allowed | Approval required | Denied |
|---|---|---|
| read / write / search files, run tests and lint, git status/diff/log, create branch, commit on the feature branch, read the Themis API | push, merge, delete branch or data, deploy, modify credentials or infrastructure, design approval, **any unknown action** | force-push, `reset --hard`, history rewrite; writes to `.git/`, `.env*`, `*.pem`, `*.key`, `secrets/`, `credentials*`, `agent-state/`, `.themis-ai.yaml`, or anywhere outside the workspace |

Every decision is appended to `agent-state/audit.log` (JSONL). Every review verdict and human
decision is saved under `agent-state/decisions/<task>/`.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[all]'          # openai + mcp extras
export OPENAI_API_KEY=...        # architecture/review side
claude --version                 # Claude Code CLI, authenticated, on PATH
```

## Use

```bash
export THEMIS_AI="themis-ai -c /opt/chopin/config/targets/themis.yaml -w /opt/themis"
$THEMIS_AI run "Implement KN-MODULE-4"            # drive the loop until done, a gate, or escalation
$THEMIS_AI status                                 # state, iterations, cost, pending approval
$THEMIS_AI approve -m "spec looks right"          # or: themis-ai reject -m "why"
$THEMIS_AI resume -g "use the existing port in internal/port/outbound"
$THEMIS_AI abort                                  # keep the branch, return to main
$THEMIS_AI history
git diff main...agent/<branch>                   # review the result yourself
```

Exit codes: 0 completed · 10 awaiting approval · 11 escalated · 12 failed · 13 rejected · 14 aborted.

## Configuration

Per-target configs live in this repo under [`config/targets/`](config/targets/) (e.g.
[`themis.yaml`](config/targets/themis.yaml)) and are passed with `-c`, so target repos carry no
orchestrator files. Without `-c`, `<workspace>/.themis-ai.yaml` is used if present. The config is merged over the defaults in
[`src/themis_ai/config.py`](src/themis_ai/config.py). Unknown keys are rejected. See
[`config/themis-ai.example.yaml`](config/themis-ai.example.yaml).
`THEMIS_AI_OPENAI_MODEL` overrides the OpenAI model.

## MCP server

```bash
themis-ai-mcp --workspace /opt/themis            # stdio
```

Tools: `read_file`, `write_file`, `list_files`, `search_code`, `run_tests`, `run_lint`,
`git_status`, `git_diff`, `git_log`, `task_status`, plus read-only Themis domain tools:
`list_products`, `get_product`, `get_scan`, `list_components`, `get_notification_rules`.
These call `THEMIS_API_KEY` against `themis.base_url`. Git writes are not exposed; only the
orchestrator writes history. To give Claude Code these tools during a task, set
`claude.mcp_config` to a file like [`config/claude-mcp.example.json`](config/claude-mcp.example.json).

## Development

```bash
pip install -e '.[all,dev]' && pytest
```

The tests use fake agents against real temporary Git repositories. They cover the happy path,
test-failure and review-rejection loops, severity overrides, escalation and resume, merge
approval and rejection, the design gate, failure and retry, empty diffs, preflight, abort, the
guardrail policy, both agent adapters, and the MCP toolset.

## Roadmap (add only when a real need appears)

- Separate model or configuration per review role; specialist agents (documentation, maintenance)
- More Themis domain tools (vulnerabilities, findings, SBOM export) as the API grows
- Parallel tasks on git worktrees
- GitHub delivery: open a PR and follow its CI
