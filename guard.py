"""AgentGuard policy engine.

`inspect_tool_call(tool_name, arguments, context)` is the heart of the firewall.
It is called BEFORE a tool executes and returns a GuardDecision containing the
verdict, a risk score, human-readable reasons, the matched policy rules and a
complete forensic event ready to be persisted.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from models import ForensicEvent, GuardDecision
from policies import (
    ALL_TOOLS,
    ALLOW_MAX_SCORE,
    APPROVAL_MAX_SCORE,
    APPROVAL_REQUIRED_TOOLS,
    CANARY_TOKENS,
    DENIED_TOOLS,
    INJECTION_PATTERNS,
    RISK_WEIGHTS,
    SENSITIVE_PATTERNS,
    UNRELATED_SEARCH_KEYWORDS,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_injection_patterns(text: str) -> list[str]:
    return [p for p in INJECTION_PATTERNS if re.search(p, text, re.IGNORECASE)]


def _find_sensitive_patterns(text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if re.search(pattern, text, re.IGNORECASE)]


def _arguments_text(arguments: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in arguments.items())


def _is_unrelated_action(tool_name: str, arguments: dict[str, Any], task: str) -> bool:
    """+15 when the action has nothing to do with the user's task."""
    task_lower = (task or "").lower()
    if tool_name == "search_documents":
        query = str(arguments.get("query", "")).lower()
        if any(keyword in query for keyword in UNRELATED_SEARCH_KEYWORDS):
            return True
    if tool_name == "delete_ticket":
        if not any(word in task_lower for word in ("delete", "cancel", "remove", "close")):
            return True
    if tool_name == "send_external_message":
        if not any(word in task_lower for word in ("send", "reply", "notify", "forward", "email")):
            return True
    return False


def _is_suspicious_sequence(tool_name: str, history: list[str]) -> bool:
    """+10 for sequences that look like reconnaissance followed by abuse."""
    if tool_name == "send_external_message" and "search_documents" in history:
        return True
    if tool_name == "delete_ticket" and {"read_emails", "search_documents"}.issubset(set(history)):
        return True
    return False


def inspect_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> GuardDecision:
    """Inspect a proposed tool call and return ALLOW / REQUIRE_APPROVAL / BLOCK."""

    args = arguments or {}
    score = 0
    reasons: list[str] = []
    matched_rules: list[str] = []

    untrusted = list(context.get("untrusted_content_seen", []) or [])
    task = context.get("user_task", "")
    history = list(context.get("tool_history", []) or [])

    # --- Tool policy -------------------------------------------------------
    if tool_name not in ALL_TOOLS:
        score += 100
        reasons.append(f"Unknown tool '{tool_name}' is not in the agent tool registry.")
        matched_rules.append("unknown_tool")
    else:
        if tool_name in DENIED_TOOLS:
            score += RISK_WEIGHTS["denied_tool"]
            reasons.append(f"'{tool_name}' is on the denied tools list.")
            matched_rules.append("denied_tool")
        elif tool_name in APPROVAL_REQUIRED_TOOLS:
            score += RISK_WEIGHTS["approval_required_tool"]
            reasons.append(f"'{tool_name}' is on the approval-required list.")
            matched_rules.append("approval_required_tool")

    # --- External data transfer -------------------------------------------
    if tool_name == "send_external_message":
        to = str(args.get("to", ""))
        body = str(args.get("body", ""))
        if "@" in to:
            score += RISK_WEIGHTS["external_data_transfer"]
            reasons.append(f"External data transfer to '{to}'.")
            matched_rules.append("external_data_transfer")
        payload_hits = _find_sensitive_patterns(f"{to}\n{body}")
        if payload_hits:
            score += RISK_WEIGHTS["sensitive_data"]
            reasons.append("Outgoing payload contains sensitive data: " + ", ".join(payload_hits) + ".")
            matched_rules.append("sensitive_data")

    # --- Destructive action -------------------------------------------------
    if tool_name == "delete_ticket":
        score += RISK_WEIGHTS["destructive_action"]
        reasons.append(f"Destructive action: delete ticket '{args.get('ticket_id', '')}'.")
        matched_rules.append("destructive_action")

    arg_text = _arguments_text(args)

    # --- Prompt injection in the call itself --------------------------------
    if _find_injection_patterns(arg_text):
        score += RISK_WEIGHTS["prompt_injection"]
        reasons.append("Prompt-injection pattern detected in the tool arguments.")
        matched_rules.append("prompt_injection")

    # --- Sensitive data in the arguments ------------------------------------
    if "sensitive_data" not in matched_rules:
        arg_hits = _find_sensitive_patterns(arg_text)
        if arg_hits:
            score += RISK_WEIGHTS["sensitive_data"]
            reasons.append("Tool arguments contain sensitive data: " + ", ".join(arg_hits) + ".")
            matched_rules.append("sensitive_data")

    # --- Canary token (honeytoken exfiltration) ---------------------------
    tags: list[str] = []
    if tool_name == "send_external_message":
        payload_text = f"{args.get('to', '')}\n{args.get('body', '')}"
    else:
        payload_text = arg_text
    canary_hits = [token for token in CANARY_TOKENS if token in payload_text]
    if canary_hits:
        score += RISK_WEIGHTS["canary_token"]
        reasons.append("Canary token detected in outgoing payload — possible exfiltration.")
        matched_rules.append("canary_token")
        tags.append("canary")

    # --- Context heuristics --------------------------------------------------
    if _is_unrelated_action(tool_name, args, task):
        score += RISK_WEIGHTS["unrelated_action"]
        reasons.append("Action appears unrelated to the user's task.")
        matched_rules.append("unrelated_action")

    if _is_suspicious_sequence(tool_name, history):
        score += RISK_WEIGHTS["suspicious_sequence"]
        reasons.append("Suspicious tool-call sequence detected.")
        matched_rules.append("suspicious_sequence")

    score = max(0, min(100, score))
    # Precedence: DENIED > APPROVAL_REQUIRED > ALLOWED. A denied tool is always
    # blocked, and a canary-token hit is always blocked (immediate exfiltration
    # alert), regardless of the numeric score.
    if tool_name in DENIED_TOOLS or "canary" in tags:
        decision = "BLOCK"
    elif score <= ALLOW_MAX_SCORE:
        decision = "ALLOW"
    elif score <= APPROVAL_MAX_SCORE:
        decision = "REQUIRE_APPROVAL"
    else:
        decision = "BLOCK"

    # Injection indicators found in untrusted content the agent has seen.
    # These are surfaced on every subsequent event (UI + forensics).
    injection_indicators: list[str] = []
    for content in untrusted:
        injection_indicators.extend(_find_injection_patterns(content))

    event = ForensicEvent(
        event_id=uuid.uuid4().hex,
        timestamp=_now_iso(),
        run_id=context.get("run_id", ""),
        agent_name=context.get("agent_name", "support-agent"),
        user_task=task,
        tool_name=tool_name,
        arguments=args,
        decision=decision,
        risk_score=score,
        reasons=reasons or ["No policy violations detected."],
        matched_rules=matched_rules or ["none"],
        untrusted_content_seen=untrusted,
        injection_indicators=sorted(set(injection_indicators)),
        tags=tags,
    )

    return GuardDecision(
        decision=decision,
        risk_score=score,
        reasons=reasons,
        matched_rules=matched_rules,
        forensic_event=event.model_dump(),
    )
