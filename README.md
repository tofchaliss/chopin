# chopin

The owner's development orchestrator for the Themis platform. Features are developed mainly in
the AI Harness, [themis-ai-runtime](https://github.com/tofchaliss/themis-ai-runtime), and, when
a feature needs it, in [Themis](https://github.com/tofchaliss/themis) too. chopin is part of
neither: they only ever receive the feature's code. A feature is **done** only after the owner
tries it on the enterprise VM, where Themis and the Harness run together.

```
 owner ── feature request
   │
   ▼
 chopin (laptop)   plan → build → test → review, across the repositories the feature needs
   ├──▶ themis-ai-runtime   agent/<feature>          (main target)
   └──▶ themis              agent/<feature>          (only when needed)
   │  push both, after the owner approves
   ▼
 GitHub ──▶ enterprise VM (owner, by hand): Themis + Harness together
   │
   ▼
 chopin verify  → done          chopin reopen → back to the fix loop
```

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
| Where chopin runs | Your laptop (for example a MacBook), from a terminal or a Claude Code session. It needs no local model: OpenAI and Claude Code are called over their APIs. |
| The code the agents write | The checkout you pass as `--workspace` (for example `~/src/themis`, a clone used only by chopin), on a new local branch `agent/<slug>-<id>` created from `main` |
| Who edits it | Claude Code edits the working tree. It cannot commit, push, merge, reset or reach the network. |
| Who commits it | The orchestrator. It commits each iteration to the feature branch, so every step can be diffed and rolled back. These work-in-progress commits are made before the gate passes; only the reviewed result must pass it. |
| Where tests run | The same checkout, on the feature branch, on your laptop. The orchestrator runs the configured command (Themis: `make check`, its full quality gate) itself and never trusts an agent's claim that tests passed. |
| Agent artifacts | `<state dir>/artifacts/<task>/openai/{architecture,review,security,final-review}.md` and `.../claude/{implementation,test-results}.md` |
| Runtime state | `<state dir>/` (task records, decisions, audit log). The state dir is `~/.chopin/<workspace name>` by default (`CHOPIN_HOME` or `workflow.state_dir` to move it) and must be outside the checkout. **The target repository only ever receives code.** |
| `main` / GitHub | Unchanged unless `delivery: merge` or `delivery: push` is set **and** you approve |

## Architecture

```
 YOU ── run / status / approve / reject / resume / abort
  │
  ▼
┌──────────────────────── ORCHESTRATOR (themis_ai.orchestrator) ─────────────────────┐
│ Task planner → Agent router → Workflow engine (explicit state machine)             │
│ State manager (~/.chopin/<target>/)   Guardrails & approval (themis_ai.guardrails) │
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

Every decision is appended to `<state dir>/audit.log` (JSONL). Every review verdict and human
decision is saved under `<state dir>/decisions/<task>/`.

## Install

On the laptop that runs chopin (macOS or Linux):

```bash
# Clone all three side by side in one folder (any folder); the project file finds
# themis and themis-ai-runtime next to chopin. They are clones used only by chopin.
cd <base>
git clone https://github.com/tofchaliss/chopin
git clone https://github.com/tofchaliss/themis-ai-runtime
git clone https://github.com/tofchaliss/themis
cd chopin
python3 -m venv .venv && . .venv/bin/activate    # Python 3.11+
pip install -e '.[all]'          # openai + mcp extras
export OPENAI_API_KEY=...        # OpenAI platform API key (billing enabled)
claude --version                 # Claude Code CLI, authenticated, on PATH
gh auth status                   # optional: GitHub CLI, so chopin can open the PRs
```

The Themis checkout also needs what `make check` needs: Go 1.25 and the linters its Makefile
calls. The Harness needs Go 1.24+. chopin can be started from inside a Claude Code session; the nested `claude -p` it
spawns is started without the parent session's `CLAUDECODE` marker. On a Mac, prefix long runs
with `caffeinate -i` so the machine does not sleep mid-task (a stopped task can be resumed).

## Use

```bash
```bash
export CHOPIN="chopin -p <base>/chopin/config/projects/themis-platform.yaml"
$CHOPIN run "Add retry to outward actions"        # the architect decides which repos it touches
$CHOPIN run "..." --base themis=feat/harness-integration   # Themis side builds on that branch
$CHOPIN status                                    # state, iterations, cost, branches, pending approval
$CHOPIN run "..." --dry-run                       # local branches only: build, test, review; no push/PR/VM
$CHOPIN run "..." --primary themis                # lead in Themis (a Themis-only milestone)
$CHOPIN revise -m "feedback on the design"        # design discussion: the architect redesigns; repeat
$CHOPIN approve -m "spec looks right"             # design approval (every feature), push approval
$CHOPIN resume -g "keep the change inside L4"     # after an escalation or failure
$CHOPIN verify -m "ran X on the VM, saw Y"        # it works on the enterprise VM: done
$CHOPIN verify -m "live proof passes" --where mac # checked on the laptop instead (recorded as such)
$CHOPIN reopen -m "on the VM, X failed: <output>" # back to the fix loop, then to the VM again
$CHOPIN abort                                     # keep the branches, return to base
$CHOPIN history
```

Exit codes: 0 completed · 10 awaiting approval · 11 escalated · 12 failed · 13 rejected ·
14 aborted · 15 awaiting VM verification. (`themis-ai` is the same command.)

### A feature across both repositories

- The architect sees both repositories (the Harness first) and lists in the specification which
  ones the feature changes. A secondary repository gets the feature branch only then.
- Claude Code works in the Harness checkout with the Themis checkout added to its session.
- Themis imports the Harness as a Go module. For a feature that changes both, chopin writes a
  `go.work` **in its state directory** linking the two working trees and runs every check (and
  Claude) with `GOWORK` pointing at it, so Themis builds against the Harness as changed —
  without a file in either repository. Before the Themis side merges, its `go.mod` pin moves to
  the pushed Harness commit (the VM checklist says so).
- Reviewers see one diff with a section per repository, and review the contract between them.
- Delivery pushes every branch of the feature together, after one approval. The Harness's
  push marker (`THEMIS_PUSH_APPROVED=1`) is set on the approved push. With
  `workflow.pull_request: true` (the platform project's setting) chopin then opens one PR per
  pushed branch with the GitHub CLI (`gh`, logged in), bodies ending `Generated by chopin`;
  without `gh` it records a GitHub compare link instead. PR links show in `chopin status` and in
  the VM checklist. Merging stays yours.

### Enterprise-VM verification

After delivery the feature waits in `AWAITING_VM_VERIFICATION`, and chopin writes
`<state dir>/artifacts/<task>/vm-checklist.md`: the branch and head commit per repository, the
build/run steps (`vm_steps` in each target config), and the acceptance criteria to check.
`chopin verify` closes the feature with your evidence (`--where mac` when you checked on the
laptop — e.g. a test-only fix whose proof is a live test — so the record says where); `chopin reopen` sends what failed back
to Claude Code, which fixes it, and the feature goes through tests, reviews, push approval and
the VM again. Every verdict is recorded under `<state dir>/decisions/<task>/`.

Single repository: `chopin -c config/targets/<target>.yaml -w <checkout> run "..."`.

## Targets

| Target | Config | Architecture notes | Gate | Design approval |
|---|---|---|---|---|
| Themis | [`themis.yaml`](config/targets/themis.yaml) | [`themis.md`](config/targets/themis.md): greenfield layout, frozen legacy tree, ADR/EDR/OpenSpec, invariants | `make check` | when the architect has open questions |
| AI Harness | [`themis-ai-runtime.yaml`](config/targets/themis-ai-runtime.yaml) | [`themis-ai-runtime.md`](config/targets/themis-ai-runtime.md): ownership, 11 layers, G1/G2, DAY-0, change classes | gofmt · vet · test · build over `src/harness` (as CI) | **every task** (DAY-0) |

The prompts are target-neutral; a target's notes file is appended to every OpenAI role
(architect and all reviewers) as the authoritative rules for that repository. Claude Code reads
the target's own `CLAUDE.md`.

The owner's engineering principles, [`AGENT.md`](AGENT.md), go to every role, the builder
included, ahead of the target's notes; where the two disagree, the target's rules win.

## Configuration

Per-target configs live in this repo under [`config/targets/`](config/targets/) and are passed
with `-c`, so target repos carry no orchestrator files. Without `-c`, `<workspace>/.themis-ai.yaml` is used if present. The config is merged over the defaults in
[`src/themis_ai/config.py`](src/themis_ai/config.py). Unknown keys are rejected. See
[`config/themis-ai.example.yaml`](config/themis-ai.example.yaml).
`THEMIS_AI_OPENAI_MODEL` overrides the OpenAI model.

## MCP server

```bash
themis-ai-mcp -c config/targets/themis.yaml --workspace ~/src/themis   # stdio
```

Tools: `read_file`, `write_file`, `list_files`, `search_code`, `run_tests`, `run_lint`,
`git_status`, `git_diff`, `git_log`, `task_status`, plus read-only tools over a running Themis
greenfield stack, one URL per service (`themis.services`, default `localhost:8081–8086`):

| Service | Tools |
|---|---|
| Registry | `list_products`, `list_projects`, `list_releases`, `get_release`, `get_blast_radius` |
| Evidence | `list_evidence`, `get_sbom_inventory` |
| Knowledge | `find_vulnerability` (by CVE), `get_faultline`, `feed_health` |
| Governance | `list_findings`, `get_finding`, `get_finding_assessment`, `get_release_posture` |

`THEMIS_API_KEY`, when set, is sent as `X-API-Key`. Git writes are not exposed; only the
orchestrator writes history. To give Claude Code these tools during a task, set
`claude.mcp_config` to a file like [`config/claude-mcp.example.json`](config/claude-mcp.example.json).

## Development

```bash
pip install -e '.[all,dev]' && pytest
```

The tests use fake agents against real temporary Git repositories. They cover the happy path,
test-failure and review-rejection loops, severity overrides, escalation and resume, merge
approval and rejection, the design gate, failure and retry, empty diffs, preflight, abort, the
guardrail policy, both agent adapters, the MCP toolset and its Themis service routing, the state
directory staying outside the checkout, features spanning two repositories (branches, commits,
per-repository checks and diffs, one push approval), the Go-module link through a state-directory
`go.work`, enterprise-VM verification (checklist, verify, reopen into the fix loop), and that the
shipped target and project configs load.

## Roadmap (add only when a real need appears)

- Separate model or configuration per review role; specialist agents (documentation, maintenance)
- A local, OpenAI-compatible model for the architecture/review side
- Deterministic security tools as checks (govulncheck, gosec) alongside the security review
- More Themis domain tools as the API grows
- Parallel tasks on git worktrees
- Bump Themis's Harness pin automatically after the Harness branch is pushed
- Open linked PRs for a feature's branches and follow their CI
