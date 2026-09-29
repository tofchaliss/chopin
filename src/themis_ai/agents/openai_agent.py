"""OpenAI architecture / review agent.

One model, four roles (architect, code reviewer, security reviewer, final
approver), selected by prompt. Outputs are constrained with JSON schemas so the
orchestrator gets typed verdicts, not prose it has to interpret.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..config import OpenAIConfig
from . import prompts
from .base import SEVERITIES, Finding, Review, ReviewKind, Spec, Usage

_STR_LIST = {"type": "array", "items": {"type": "string"}}

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "requirements": _STR_LIST,
        "design": {"type": "string"},
        "files_to_change": _STR_LIST,
        "acceptance_criteria": _STR_LIST,
        "test_plan": _STR_LIST,
        "risks": _STR_LIST,
        "open_questions": _STR_LIST,
    },
    "required": ["summary", "requirements", "design", "files_to_change",
                 "acceptance_criteria", "test_plan", "risks", "open_questions"],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "file": {"type": "string"},
                    "message": {"type": "string"},
                    "required_change": {"type": "string"},
                },
                "required": ["severity", "file", "message", "required_change"],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
}


class OpenAIArchitectReviewer:
    def __init__(self, cfg: OpenAIConfig, client: Any | None = None, target_notes: str = ""):
        self.cfg = cfg
        self.target_notes = target_notes
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover - depends on install extras
                raise RuntimeError("install the 'openai' extra: pip install chopin[openai]") from e
            api_key = os.environ.get(cfg.api_key_env)
            if not api_key:
                raise RuntimeError(f"{cfg.api_key_env} is not set")
            client = OpenAI(api_key=api_key, timeout=cfg.timeout_seconds)
        self.client = client

    def design(self, request: str, repo_context: str) -> Spec:
        data, usage = self._call(
            prompts.with_target(prompts.ARCHITECT, self.target_notes),
            f"## Request\n\n{request}\n\n## Repository context\n\n{repo_context}",
            "implementation_spec", SPEC_SCHEMA,
        )
        return Spec(**data, usage=usage)

    def review(self, kind: ReviewKind, request: str, spec: Spec, diff: str, checks: str) -> Review:
        diff = _truncate(diff, self.cfg.diff_budget_chars)
        data, usage = self._call(
            prompts.with_target(prompts.REVIEW_PROMPTS[kind.value], self.target_notes),
            f"## Original request\n\n{request}\n\n{spec.to_markdown()}\n\n"
            f"## Check results\n\n{checks}\n\n## Diff\n\n```diff\n{diff}\n```",
            f"{kind.value}_review", REVIEW_SCHEMA,
        )
        return Review(
            kind=kind,
            accepted=data["verdict"] == "accept",
            summary=data["summary"],
            findings=[Finding(**f) for f in data["findings"]],
            usage=usage,
        )

    def _call(self, instructions: str, content: str, name: str,
              schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        resp = self.client.responses.create(
            model=self.cfg.model,
            instructions=instructions,
            input=content,
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        )
        usage = Usage()
        if getattr(resp, "usage", None) is not None:
            usage.input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
            usage.output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
        try:
            return json.loads(resp.output_text), usage
        except (json.JSONDecodeError, TypeError) as e:
            raise RuntimeError(f"OpenAI returned non-JSON output for {name}: {resp.output_text!r:.500}") from e


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} characters ...]"
