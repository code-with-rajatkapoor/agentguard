"""Tests for configurable AI-provider support.

Run with:
    cd /home/rajat/agentguard && .venv/bin/python -m unittest discover -s tests -v

All provider calls are mocked - no network access, no real credentials.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import main
import providers
from guard import inspect_tool_call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def make_config(provider="nous", model="test-model", key="test-key-not-real"):
    return providers.ProviderConfig(
        provider=provider,
        model=model,
        api_key=key,
        base_url="http://localhost/chat/completions",
    )


def native_response(name, arguments, call_id="call_1", content=None):
    return FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    }
                }
            ]
        },
    )


def text_response(payload):
    return FakeResponse(
        200,
        {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]},
    )


class _NoForensicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self._record = main.record_event
        main.record_event = lambda event: None
        providers.clear_cache()

    def tearDown(self) -> None:
        main.record_event = self._record


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestProviderSelection(unittest.TestCase):
    def test_known_providers_are_registered(self):
        self.assertEqual(
            set(providers.PROVIDER_SPECS), {"nous", "openrouter", "openai", "gemini", "groq"}
        )

    def test_correct_provider_and_key_selection(self):
        env = {"AI_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "or-real-key"}
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.provider, "openrouter")
        self.assertEqual(config.model, providers.PROVIDER_SPECS["openrouter"].default_model)
        self.assertIn("openrouter.ai", config.base_url)

    def test_ai_model_overrides_default(self):
        env = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "oai-key", "AI_MODEL": "gpt-4o"}
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.model, "gpt-4o")

    def test_missing_key_raises_actionable_error(self):
        env = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(providers.ProviderConfigError) as ctx:
                providers.resolve_provider()
        message = str(ctx.exception)
        self.assertIn("OPENAI_API_KEY", message)
        self.assertIn("openai", message.lower())

    def test_placeholder_key_is_rejected(self):
        for placeholder in ("your_openai_api_key_here", "placeholder", "xxx", "<key>"):
            with self.subTest(placeholder=placeholder):
                env = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": placeholder}
                with patch.dict(os.environ, env, clear=False):
                    with self.assertRaises(providers.ProviderConfigError):
                        providers.resolve_provider()

    def test_unknown_provider_raises(self):
        with self.assertRaises(providers.ProviderConfigError) as ctx:
            providers.resolve_provider("bogus")
        self.assertIn("bogus", str(ctx.exception))

    def test_only_selected_provider_key_is_required(self):
        # openrouter selected; no other provider key present.
        env = {
            "AI_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "or-key",
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "",
            "NOUS_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.provider, "openrouter")

    def test_describe_provider_is_secret_free(self):
        env = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "super-secret-value"}
        with patch.dict(os.environ, env, clear=False):
            info = providers.describe_provider()
        self.assertTrue(info["configured"])
        self.assertNotIn("super-secret-value", json.dumps(info))

    def test_base_url_is_never_user_supplied(self):
        # There is no code path that accepts a base URL from the request model.
        self.assertFalse(hasattr(main.AgentRunRequest, "model_fields")
                         and "base_url" in main.AgentRunRequest.model_fields)

    def test_base_url_in_request_body_is_ignored(self):
        # Even if a client posts base_url, the adapter still uses the
        # hardcoded PROVIDER_SPECS URL for the selected provider.
        env = {"AI_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "or-key"}
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.base_url, providers.PROVIDER_SPECS["openrouter"].base_url)
        self.assertNotIn("evil", config.base_url)


# ---------------------------------------------------------------------------
# Provider adapter - error handling
# ---------------------------------------------------------------------------


class TestProviderErrors(unittest.TestCase):
    def setUp(self) -> None:
        providers.clear_cache()

    def test_auth_error_is_classified(self):
        with patch("providers._post", return_value=FakeResponse(401)):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config())
        self.assertEqual(result.error_kind, "auth")
        self.assertIn("NOUS_API_KEY", result.error_message or "")

    def test_timeout_is_classified(self):
        import requests as real_requests

        with patch("providers._post", side_effect=real_requests.Timeout()):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config(), max_retries=0)
        self.assertEqual(result.error_kind, "timeout")

    def test_rate_limit_is_retried_then_reported(self):
        calls = {"n": 0}

        def post(*a, **k):
            calls["n"] += 1
            return FakeResponse(429, headers={"Retry-After": "0"})

        with patch("providers._post", side_effect=post):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config(), max_retries=2)
        self.assertEqual(result.error_kind, "rate_limit")
        self.assertEqual(calls["n"], 3)

    def test_server_error_is_retried(self):
        calls = {"n": 0}

        def post(*a, **k):
            calls["n"] += 1
            return FakeResponse(503)

        with patch("providers._post", side_effect=post):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config(), max_retries=1)
        self.assertEqual(result.error_kind, "server")
        self.assertEqual(calls["n"], 2)

    def test_malformed_tool_arguments_are_rejected(self):
        bad = FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "read_emails", "arguments": "{not json"},
                                }
                            ],
                        }
                    }
                ]
            },
        )
        with patch("providers._post", return_value=bad):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config())
        self.assertEqual(result.error_kind, "invalid_arguments")

    def test_missing_choices_is_classified(self):
        with patch("providers._post", return_value=FakeResponse(200, {"foo": "bar"})):
            result = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config())
        self.assertEqual(result.error_kind, "invalid_response")

    def test_error_messages_never_contain_the_key(self):
        secret = "sk-live-super-secret-value"
        env = {"AI_PROVIDER": "openai", "OPENAI_API_KEY": secret}
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        with patch("providers._post", return_value=FakeResponse(401)):
            result = providers.chat_completion([{"role": "user", "content": "hi"}], config=config)
        self.assertNotIn(secret, result.error_message or "")


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


class TestAgentProviderIntegration(_NoForensicWrite):
    def _session(self, mode="ai", config=None):
        config = config or make_config()
        with patch.object(main, "resolve_provider", return_value=config):
            return main._new_session("test task", mode)

    def test_native_tool_call_is_normalised_with_id(self):
        captured = []

        def fake(messages, tools=None, config=None, **kwargs):
            captured.append(messages)
            if len(captured) == 1:
                return providers.ProviderResult(
                    content=None,
                    tool_calls=[
                        {"id": "call_abc", "type": "function",
                         "name": "read_emails", "arguments": {}}
                    ],
                )
            return providers.ProviderResult(
                content='{"tool": "finish", "arguments": {"summary": "done"}, "reason": "done"}'
            )

        with patch("providers.chat_completion", side_effect=fake):
            session = self._session()
            response = main._run(session)

        self.assertEqual(response["run_mode"], "LIVE_AI")
        self.assertFalse(response["fallback"])
        self.assertEqual(response["tool_calls"][0]["tool"], "read_emails")
        # The native tool_call_id must be preserved in the conversation history.
        history = session["ai_agent"].history
        self.assertTrue(any(m.get("tool_call_id") == "call_abc" for m in history), history)

    def test_multi_step_tool_results_feed_back(self):
        responses = [
            '{"tool": "read_emails", "arguments": {}, "reason": "read"}',
            '{"tool": "create_ticket", "arguments": {"subject": "S", "description": "D"}, "reason": "t"}',
            '{"tool": "finish", "arguments": {"summary": "done"}, "reason": "d"}',
        ]
        captured = []

        def fake(messages, tools=None, config=None, **kwargs):
            captured.append([dict(m) for m in messages])
            return providers.ProviderResult(content=responses[len(captured) - 1])

        with patch("providers.chat_completion", side_effect=fake):
            session = self._session()
            response = main._run(session)

        self.assertEqual(response["status"], "completed")
        self.assertEqual(len(captured), 3)
        self.assertTrue(any("Tool result:" in (m.get("content") or "") for m in captured[1]))
        self.assertTrue(any(m.get("role") == "assistant" for m in captured[1]))

    def test_blocked_calls_are_fed_back_and_not_executed(self):
        executed = []
        original_exec = main.execute_tool

        def spy(tool_name, arguments):
            executed.append(tool_name)
            return original_exec(tool_name, arguments)

        responses = [
            '{"tool": "delete_ticket", "arguments": {"ticket_id": "TICKET-1"}, "reason": "delete"}',
            '{"tool": "finish", "arguments": {"summary": "done"}, "reason": "d"}',
        ]
        captured = []

        def fake(messages, tools=None, config=None, **kwargs):
            captured.append([dict(m) for m in messages])
            return providers.ProviderResult(content=responses[len(captured) - 1])

        with patch("providers.chat_completion", side_effect=fake), \
                patch.object(main, "execute_tool", side_effect=spy):
            session = self._session()
            response = main._run(session)

        self.assertIn("delete_ticket", [b["tool"] for b in response["blocked_actions"]])
        self.assertNotIn("delete_ticket", executed)
        # The block notice must reach the model.
        self.assertTrue(any("BLOCKED" in (m.get("content") or "") for m in captured[1]))

    def test_provider_failure_is_labelled_accurately(self):
        with patch("providers.chat_completion",
                   return_value=providers.ProviderResult(error_kind="auth",
                                                         error_message="auth rejected")):
            session = self._session()
            response = main._run(session)

        self.assertTrue(response["fallback"])
        self.assertEqual(response["run_mode"], "SCRIPTED_FALLBACK")
        self.assertEqual(response["fallback_reason"], "auth rejected")
        # The scripted fallback still finishes the demo.
        self.assertEqual(response["status"], "completed")
        self.assertTrue(response["tool_calls"])

    def test_invalid_json_twice_triggers_fallback(self):
        with patch("providers.chat_completion",
                   return_value=providers.ProviderResult(content="not json at all")):
            session = self._session()
            response = main._run(session)

        self.assertTrue(response["fallback"])
        self.assertEqual(response["run_mode"], "SCRIPTED_FALLBACK")
        self.assertIn("invalid JSON", response["fallback_reason"])

    def test_separate_runs_do_not_share_history(self):
        def fake(messages, tools=None, config=None, **kwargs):
            return providers.ProviderResult(
                content='{"tool": "finish", "arguments": {"summary": "done"}, "reason": "d"}'
            )

        with patch("providers.chat_completion", side_effect=fake):
            s1 = self._session()
            main._run(s1)
            s2 = self._session()
            main._run(s2)

        self.assertIsNot(s1["ai_agent"], s2["ai_agent"])
        self.assertIsNot(s1["ai_agent"].history, s2["ai_agent"].history)

    def test_approval_resume_executes_at_most_once(self):
        executed = []
        original_exec = main.execute_tool

        def spy(tool_name, arguments):
            executed.append(tool_name)
            return original_exec(tool_name, arguments)

        session = self._session(mode="normal")
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
            "tool_call_id": "call_pending",
            "risk_score": decision.risk_score,
            "reasons": decision.reasons,
            "matched_rules": decision.matched_rules,
        }
        session["forensics"].append(event)

        with patch.object(main, "execute_tool", side_effect=spy):
            response = main._approve(session, event["event_id"], True)

        self.assertEqual(executed.count("send_external_message"), 1)
        self.assertIsNone(session["pending"])
        self.assertEqual(response["status"], "completed")

    def test_deterministic_demos_still_work(self):
        normal = main._new_session("demo", "normal")
        r_normal = main._run(normal)
        self.assertEqual(r_normal["run_mode"], "SCRIPTED")
        self.assertFalse(r_normal["fallback"])
        self.assertEqual([c["tool"] for c in r_normal["tool_calls"]],
                         ["read_emails", "create_ticket", "finish"])

        attack = main._new_session("demo", "attack")
        r_attack = main._run(attack)
        self.assertEqual(r_attack["run_mode"], "SCRIPTED")
        blocked = {b["tool"] for b in r_attack["blocked_actions"]}
        self.assertIn("send_external_message", blocked)
        self.assertIn("delete_ticket", blocked)

    def test_provider_failure_does_not_switch_provider(self):
        # A failing Nous call must fall back to the script, not call another
        # provider behind the operator's back.
        env = {
            "AI_PROVIDER": "nous",
            "NOUS_API_KEY": "nous-key",
            "OPENAI_API_KEY": "openai-key",
            "GEMINI_API_KEY": "gemini-key",
        }
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.provider, "nous")
        self.assertEqual(config.api_key, "nous-key")


class TestPerformanceOptimizations(unittest.TestCase):
    """Connection reuse, response caching and the Nous auth fallback."""

    def setUp(self) -> None:
        providers.clear_cache()

    def test_identical_request_is_served_from_cache(self):
        calls = {"n": 0}

        def post(*a, **k):
            calls["n"] += 1
            return native_response("read_emails", {})

        with patch("providers._post", side_effect=post):
            first = providers.chat_completion([{"role": "user", "content": "hi"}],
                                              config=make_config())
            second = providers.chat_completion([{"role": "user", "content": "hi"}],
                                               config=make_config())

        self.assertEqual(calls["n"], 1)
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(second.latency_ms, 0)

    def test_cache_can_be_disabled(self):
        calls = {"n": 0}

        def post(*a, **k):
            calls["n"] += 1
            return native_response("read_emails", {})

        with patch.dict(os.environ, {"AI_CACHE": "0"}, clear=False), \
                patch("providers._post", side_effect=post):
            providers.chat_completion([{"role": "user", "content": "hi"}], config=make_config())
            providers.chat_completion([{"role": "user", "content": "hi"}], config=make_config())

        self.assertEqual(calls["n"], 2)

    def test_session_pool_is_reused(self):
        self.assertIs(providers._get_session(), providers._get_session())

    def test_nous_key_falls_back_to_hermes_auth(self):
        env = {"AI_PROVIDER": "nous", "NOUS_API_KEY": "your_key_here"}
        with patch.dict(os.environ, env, clear=False), \
                patch("providers._hermes_nous_key", return_value="hermes-token"):
            config = providers.resolve_provider()
        self.assertEqual(config.api_key, "hermes-token")

    def test_provider_model_and_base_url_env_overrides(self):
        env = {
            "AI_PROVIDER": "nous",
            "NOUS_API_KEY": "k",
            "NOUS_MODEL": "fast/model",
            "NOUS_BASE_URL": "https://example.com/v1/chat/completions",
        }
        with patch.dict(os.environ, env, clear=False):
            config = providers.resolve_provider()
        self.assertEqual(config.model, "fast/model")
        self.assertEqual(config.base_url, "https://example.com/v1/chat/completions")

    def test_lower_default_budget_keeps_demo_snappy(self):
        config = providers.ProviderConfig("nous", "m", "k", "u")
        self.assertEqual(config.max_tokens, 256)
        self.assertEqual(config.timeout, 20)
        self.assertEqual(config.max_retries, 1)


class TestCacheWarmUp(unittest.TestCase):
    """AI_WARM pre-warms the cache without polluting the forensic log."""

    def test_warm_run_writes_no_forensics(self):
        written = []
        original = main.record_event
        main.record_event = lambda event: written.append(event)
        try:
            main._warm_cache("warm task")
        finally:
            main.record_event = original
        self.assertEqual(written, [])

    def test_record_helper_respects_warm_flag(self):
        written = []
        original = main.record_event
        main.record_event = lambda event: written.append(event)
        try:
            session = main._new_session("demo", "normal")
            main._record(session, {"event_id": "a"})
            session["warm"] = True
            main._record(session, {"event_id": "b"})
        finally:
            main.record_event = original
        self.assertEqual([e["event_id"] for e in written], ["a"])


    def test_groq_spec_registration(self):
        from providers import PROVIDER_SPECS
        self.assertIn("groq", PROVIDER_SPECS)
        spec = PROVIDER_SPECS["groq"]
        self.assertEqual(spec.key_env, "GROQ_API_KEY")
        self.assertEqual(
            spec.base_url, "https://api.groq.com/openai/v1/chat/completions"
        )
        self.assertEqual(spec.default_model, "llama-3.3-70b-versatile")

    def test_groq_rate_limit_is_classified(self):
        import os
        os.environ["GROQ_API_KEY"] = "test-key-not-a-real-secret"
        from unittest.mock import patch
        from providers import chat_completion, resolve_provider
        config = resolve_provider(provider="groq", model="llama-3.3-70b-versatile")
        fake = type(
            "Resp",
            (),
            {
                "status_code": 429,
                "headers": {"Retry-After": "1"},
                "json": lambda self: {"error": {"message": "too many requests"}},
            },
        )()
        with patch("providers._post", return_value=fake):
            result = chat_completion(
                [{"role": "user", "content": "hi"}],
                config=config,
                max_tokens=8,
                max_retries=0,
            )
        self.assertEqual(result.error_kind, "rate_limit")


if __name__ == "__main__":
    unittest.main()
