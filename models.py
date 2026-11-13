"""AgentGuard — runtime firewall and forensic recorder for AI agents.

Pydantic models shared by the API, the policy engine and the forensic logger.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    """POST /api/run-agent body."""

    task: str
    mode: str = "normal"  # normal | attack | ai
    # Optional explicit provider choice. Must be one of the known providers;
    # it is validated server-side. No base URL is accepted from the client.
    provider: str | None = None


class ApproveRequest(BaseModel):
    """POST /api/approve body."""

    run_id: str
    event_id: str
    approved: bool


class StopRequest(BaseModel):
    """POST /api/stop body — halts a run (used by Stop / Block Agent)."""

    run_id: str


class RedTeamRequest(BaseModel):
    """POST /api/red-team body — which attack scenario to generate and run."""

    attack_type: str


class GuardDecision(BaseModel):
    """The verdict returned by guard.inspect_tool_call()."""

    decision: str  # ALLOW | REQUIRE_APPROVAL | BLOCK
    risk_score: int
    reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    forensic_event: dict[str, Any] = Field(default_factory=dict)


class ForensicEvent(BaseModel):
    """One immutable record per inspected tool call (logs/forensics.jsonl)."""

    event_id: str
    timestamp: str
    run_id: str
    agent_name: str
    user_task: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision: str
    risk_score: int
    reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    untrusted_content_seen: list[str] = Field(default_factory=list)
    injection_indicators: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    execution_result: Optional[dict[str, Any]] = None
    block_reason: Optional[str] = None


class RunResponse(BaseModel):
    """POST /api/run-agent response."""

    run_id: str
    status: str  # completed | blocked | approval_required
    agent_output: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    forensics: list[dict[str, Any]] = Field(default_factory=list)
    blocked_action: dict[str, Any] = Field(default_factory=dict)
    blocked_actions: list[dict[str, Any]] = Field(default_factory=list)
    approval_request: dict[str, Any] = Field(default_factory=dict)
    fallback: bool = False
    mode: str = "normal"
    task: str = ""
    # LIVE_AI when the model drove the run; SCRIPTED_FALLBACK when the
    # deterministic agent finished it after a provider failure.
    run_mode: str = "LIVE_AI"
    fallback_reason: str | None = None
    provider: str | None = None
    model: str | None = None
