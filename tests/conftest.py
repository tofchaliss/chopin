from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from themis_ai.agents.base import Finding, ImplementationResult, Review, ReviewKind, Spec, Usage
from themis_ai.config import CheckConfig, RuntimeConfig
from themis_ai.orchestrator import Orchestrator


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "themis"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "owner@example.com")
    git(root, "config", "user.name", "Owner")
    (root / "README.md").write_text("# Themis\n\nKN-MODULE-4 lives in internal/kn.\n")
    (root / "feature.txt").write_text("todo\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def make_spec(**overrides) -> Spec:
    base = dict(summary="Make feature.txt say done", requirements=["feature.txt contains 'done'"],
                design="Edit the file.", files_to_change=["feature.txt"],
                acceptance_criteria=["feature.txt == done"], test_plan=["check feature.txt"],
                risks=[], open_questions=[], usage=Usage(input_tokens=100, output_tokens=50))
    base.update(overrides)
    return Spec(**base)


class FakeArchitect:
    """Scripted OpenAI side. `reviews` maps kind -> list of verdicts (consumed in order)."""

    def __init__(self, spec: Spec | None = None, reviews: dict[ReviewKind, list[bool]] | None = None):
        self.spec = spec or make_spec()
        self.reviews = {k: list(v) for k, v in (reviews or {}).items()}
        self.contexts: list[str] = []
        self.diffs: list[tuple[ReviewKind, str]] = []

    def design(self, request, repo_context):
        self.contexts.append(repo_context)
        return self.spec

    def review(self, kind, request, spec, diff, checks):
        self.diffs.append((kind, diff))
        queue = self.reviews.get(kind, [])
        accepted = queue.pop(0) if queue else True
        findings = [] if accepted else [Finding("high", "not good enough", "feature.txt", "fix it")]
        return Review(kind, accepted, "ok" if accepted else "rejected", findings, Usage(10, 5))


class FakeImplementer:
    """Scripted Claude side: each call writes the next content into feature.txt."""

    def __init__(self, root: Path, outputs: list[str | None]):
        self.root = root
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def implement(self, request, spec, *, feedback=None, session_id=None, extra_dirs=None, env=None):
        self.calls.append({"feedback": feedback, "session_id": session_id,
                           "extra_dirs": extra_dirs, "env": env})
        content = self.outputs.pop(0) if self.outputs else "done"
        if content is None:
            return ImplementationResult(False, "boom")
        (self.root / "feature.txt").write_text(content + "\n")
        return ImplementationResult(True, f"wrote {content}", session_id="sess-1", usage=Usage(cost_usd=0.5))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "chopin-state"


@pytest.fixture
def config(state_dir) -> RuntimeConfig:
    cfg = RuntimeConfig()
    cfg.workflow.state_dir = str(state_dir)
    cfg.tests = CheckConfig(command=[
        sys.executable, "-c",
        "import pathlib,sys; sys.exit(0 if pathlib.Path('feature.txt').read_text().strip()=='done' else 1)",
    ])
    cfg.lint = CheckConfig()
    return cfg


@pytest.fixture
def build(repo, config):
    def _build(architect=None, outputs=("done",)):
        architect = architect or FakeArchitect()
        impl = FakeImplementer(repo, list(outputs))
        return Orchestrator(repo, config, architect, impl), architect, impl
    return _build
