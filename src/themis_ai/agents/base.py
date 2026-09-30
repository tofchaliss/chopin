"""Agent contracts.

Agents reason; the orchestrator decides. Each side has a narrow, typed
interface so any implementation (OpenAI, Claude Code, a fake in tests, or a
future specialist agent) can be swapped in without touching the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

SEVERITIES = ["info", "low", "medium", "high", "critical"]


class ReviewKind(str, Enum):
    CODE = "code"
    SECURITY = "security"
    FINAL = "final"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Spec:
    """Implementation specification produced by the architecture agent."""

    summary: str
    requirements: list[str]
    design: str
    files_to_change: list[str]
    acceptance_criteria: list[str]
    test_plan: list[str]
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    # Repositories the change touches (multi-repo features); empty = primary only.
    repositories: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def to_markdown(self) -> str:
        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {i}" for i in items) or "- (none)"

        repos = f"## Repositories\n\n{bullets(self.repositories)}\n\n" if self.repositories else ""
        return (
            f"# Implementation specification\n\n## Summary\n\n{self.summary}\n\n{repos}"
            f"## Requirements\n\n{bullets(self.requirements)}\n\n"
            f"## Design\n\n{self.design}\n\n"
            f"## Files to change\n\n{bullets(self.files_to_change)}\n\n"
            f"## Acceptance criteria\n\n{bullets(self.acceptance_criteria)}\n\n"
            f"## Test plan\n\n{bullets(self.test_plan)}\n\n"
            f"## Risks\n\n{bullets(self.risks)}\n\n"
            f"## Open questions\n\n{bullets(self.open_questions)}\n"
        )


@dataclass
class Finding:
    severity: str
    message: str
    file: str = ""
    required_change: str = ""

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else 0


@dataclass
class Review:
    kind: ReviewKind
    accepted: bool
    summary: str
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def blocking(self, threshold: str) -> list[Finding]:
        t = SEVERITIES.index(threshold)
        return [f for f in self.findings if f.rank >= t]

    def to_markdown(self) -> str:
        verdict = "ACCEPT" if self.accepted else "REJECT"
        lines = [f"# {self.kind.value.title()} review: {verdict}", "", self.summary, "", "## Findings", ""]
        if not self.findings:
            lines.append("- (none)")
        for f in self.findings:
            loc = f" `{f.file}`" if f.file else ""
            lines.append(f"- **{f.severity}**{loc}: {f.message}")
            if f.required_change:
                lines.append(f"  - required change: {f.required_change}")
        return "\n".join(lines) + "\n"


@dataclass
class ImplementationResult:
    ok: bool
    summary: str
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    raw: str = ""


class ArchitectReviewer(Protocol):
    """OpenAI side: architecture and independent review."""

    def design(self, request: str, repo_context: str) -> Spec: ...

    def review(self, kind: ReviewKind, request: str, spec: Spec, diff: str,
               checks: str) -> Review: ...


class Implementer(Protocol):
    """Claude Code side: implementation, tests, fixes, documentation."""

    def implement(self, request: str, spec: Spec, *, feedback: str | None = None,
                  session_id: str | None = None, extra_dirs: list[Path] | None = None,
                  env: dict[str, str] | None = None) -> ImplementationResult: ...
