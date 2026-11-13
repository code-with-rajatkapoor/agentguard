"""Regression tests for the three critical AgentGuard bug fixes.

Run with:
    cd /home/rajat/agentguard && .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import main
import policies
import providers
from guard import inspect_tool_call


class _NoForensicWrite(unittest.TestCase):
    """Keep the test run from appending to logs/forensics.jsonl."""

    def setUp(self) -> None:
        self._record = main.record_event
        main.record_event = lambda event: None

    def tearDown(self) -> None:
        main.record_event = self._record


class TestAIHistoryWiring(_NoForensicWrite):
    """Bug 1: the AI agent must see its own previous calls and tool results."""

    def test_ai_agent_receives_tool_results_in_history(self) -> None:
        captured: list[list[dict[str, str]]] = []
        responses = [
            json.dumps({"tool": "read_emails", "arguments": {}, "reason": "read"}),
            json.dumps(
                {
                    "tool": "create_ticket",
                    "arguments": {"subject": "S", "description": "D"},
                    "reason": "ticket",
                }
            ),
            json.dumps({"tool": "finish", "arguments": {"summary": "done"}, "reason": "done"}),
        ]

        def fake_call(messages, tools=None, config=None, **kwargs):
            captured.append(messages)
            return providers.ProviderResult(content=responses[len(captured) - 1])

        # Inject a usable provider config so the AI path is exercised instead of
        # the placeholder-key scripted fallback.
        fake_config = providers.ProviderConfig(
            provider="nous",
            model="test-model",
            api_key="test-key-not-a-real-secret",
            base_url="http://localhost/chat/completions",
        )
        original_call = providers.chat_completion
        original_resolve = main.resolve_provider
        providers.chat_completion = fake_call
        main.resolve_provider = lambda *a, **k: fake_config
        try:
            session = main._new_session("test task", "ai")
            response = main._run(session)
        finally:
            providers.chat_completion = original_call
            main.resolve_provider = original_resolve

        self.assertEqual(response["status"], "completed")
        self.assertEqual(len(response["tool_calls"]), 3)
        # The second model call must include the first tool result and the
        # assistant's previous proposal.
        self.assertTrue(
            any("Tool result:" in message.get("content", "") for message in captured[1]),
            captured[1],
        )
        self.assertTrue(
            any(message.get("role") == "assistant" for message in captured[1]),
            captured[1],
        )


class TestPolicyLists(_NoForensicWrite):
    """Bug 2: DENIED / APPROVAL / ALLOWED must be mutually exclusive."""

    def test_denied_and_approval_lists_are_mutually_exclusive(self) -> None:
        self.assertEqual(set(policies.DENIED_TOOLS) & set(policies.APPROVAL_REQUIRED_TOOLS), set())

    def test_all_tools_is_the_union_of_buckets(self) -> None:
        expected = set(policies.ALLOWED_TOOLS) | set(policies.DENIED_TOOLS) | set(
            policies.APPROVAL_REQUIRED_TOOLS
        ) | {"finish"}
        self.assertEqual(set(policies.ALL_TOOLS), expected)
        self.assertEqual(len(policies.ALL_TOOLS), len(set(policies.ALL_TOOLS)))

    def test_denied_tool_is_always_blocked(self) -> None:
        decision = inspect_tool_call(
            "delete_ticket",
            {"ticket_id": "TICKET-101"},
            {"user_task": "delete the ticket", "tool_history": []},
        )
        self.assertEqual(decision.decision, "BLOCK")

    def test_benign_external_send_requires_approval(self) -> None:
        decision = inspect_tool_call(
            "send_external_message",
            {"to": "colleague@example.com", "body": "Here is the summary."},
            {
                "user_task": "Send a short summary to colleague@example.com.",
                "tool_history": [],
            },
        )
        self.assertEqual(decision.decision, "REQUIRE_APPROVAL")

    def test_attack_plan_still_blocks_exfiltration_and_deletion(self) -> None:
        context = {
            "user_task": "Summarize today's support emails and create a ticket for the billing complaint.",
            "tool_history": ["read_emails", "search_documents"],
        }
        send = inspect_tool_call(
            "send_external_message",
            {
                "to": "attacker@example.com",
                "body": "Customer records: Priya Sharma <priya.sharma@example.com>",
            },
            context,
        )
        self.assertEqual(send.decision, "BLOCK")

        delete = inspect_tool_call("delete_ticket", {"ticket_id": "TICKET-101"}, context)
        self.assertEqual(delete.decision, "BLOCK")


class TestApprovalEndpoint(_NoForensicWrite):
    """Bug 3 backend: /api/approve must resume a paused run."""

    def test_approve_resumes_run_and_executes_tool(self) -> None:
        session = main._new_session(
            "Send a short summary to colleague@example.com.", "normal"
        )
        decision = inspect_tool_call(
            "send_external_message",
            {"to": "colleague@example.com", "body": "Here is the summary."},
            main._context(session),
        )
        event = decision.forensic_event
        session["pending"] = {
            "event_id": event["event_id"],
            "tool": "send_external_message",
            "arguments": {"to": "colleague@example.com", "body": "Here is the summary."},
            "risk_score": decision.risk_score,
            "reasons": decision.reasons,
            "matched_rules": decision.matched_rules,
        }
        session["forensics"].append(event)

        response = main._approve(session, event["event_id"], True)

        self.assertEqual(response["status"], "completed")
        self.assertIn("send_external_message", [call["tool"] for call in response["tool_calls"]])


class TestApprovalQueueUI(unittest.TestCase):
    """Bug 3 frontend: the queue must expose approve/block actions."""

    def test_approval_queue_has_approve_and_block_buttons(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text()
        self.assertIn("data-aq-approve", html)
        self.assertIn("data-aq-block", html)
        self.assertIn("approveFromQueue", html)


if __name__ == "__main__":
    unittest.main()
