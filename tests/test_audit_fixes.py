"""Regression tests for the 2026-11-11 audit-and-fix pass.

Run with:
    cd /home/rajat/agentguard && .venv/bin/python -m unittest discover -s tests -v

Covers the bugs found during the audit:
  * malformed tool arguments escaping as unhandled exceptions (500 / stuck run)
  * a hash chain that breaks after clear_events() or after a partial tail line
  * two concurrent approvals executing the same pending action twice
  * POST /api/stop not stopping an in-flight run
  * a pending approval left in the queue after a stop
  * /api/health leaking the absolute server path
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import forensic
import main
import tools


class _NoForensicWrite(unittest.TestCase):
    """Keep the test run from appending to logs/forensics.jsonl."""

    def setUp(self) -> None:
        self._record = main.record_event
        main.record_event = lambda event: None

    def tearDown(self) -> None:
        main.record_event = self._record


class TestMalformedArguments(_NoForensicWrite):
    def test_non_string_query_is_rejected_not_raised(self) -> None:
        result = tools.execute_tool("search_documents", {"query": {"a": 1}})
        self.assertFalse(result["ok"])
        self.assertIn("invalid arguments", result["error"])

    def test_missing_required_argument_is_rejected_not_raised(self) -> None:
        result = tools.execute_tool("search_documents", {})
        self.assertFalse(result["ok"])
        self.assertIn("bad arguments", result["error"])

    def test_unknown_tool_is_rejected(self) -> None:
        result = tools.execute_tool("not_a_real_tool", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])

    def test_run_with_malformed_arguments_completes(self) -> None:
        session = main._new_session("audit repro", "normal")
        session["plan"] = [
            {"tool": "search_documents", "arguments": {"query": {"bad": "type"}}},
            {"tool": "finish", "arguments": {"summary": "done"}},
        ]
        response = main._run(session)

        self.assertIn(response["status"], ("completed", "blocked"))
        executed = [c for c in response["tool_calls"] if c["tool"] == "search_documents"]
        self.assertEqual(len(executed), 1)
        self.assertFalse(executed[0]["result"]["ok"])
        self.assertIn("invalid arguments", executed[0]["result"]["error"])
        # The run must not be left in "running" state.
        self.assertNotEqual(session["status"], "running")


class TestHashChainRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_log = forensic.LOG_FILE
        self._orig_last = forensic._LAST_HASH
        forensic.LOG_FILE = Path(self._tmp.name) / "forensics.jsonl"
        forensic._LAST_HASH = None

    def tearDown(self) -> None:
        forensic.LOG_FILE = self._orig_log
        forensic._LAST_HASH = self._orig_last
        self._tmp.cleanup()

    def test_chain_survives_clear_events(self) -> None:
        forensic.record_event({"event_id": "a"})
        forensic.clear_events()
        forensic.record_event({"event_id": "b"})
        result = forensic.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["broken_at"], None)
        self.assertEqual(result["verified"], 1)

    def test_chain_recovers_from_a_partial_tail_line(self) -> None:
        forensic.record_event({"event_id": "a"})
        good_hash = forensic._LAST_HASH
        with open(forensic.LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write('{"event_id": "b", "hash": "partial')

        self.assertEqual(forensic._last_hash_from_file(), good_hash)

        forensic._LAST_HASH = None  # simulate a cold start
        forensic.record_event({"event_id": "c"})
        result = forensic.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual([e["event_id"] for e in forensic.read_events()], ["a", "c"])

    def test_hash_less_record_is_reported_as_legacy_not_a_break(self) -> None:
        # A record without a "hash" is skipped by verification and surfaced via
        # the "legacy" count. This is documented, not silent: inserting a
        # hash-less record raises "legacy" instead of breaking the chain.
        forensic.record_event({"event_id": "a"})
        with open(forensic.LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_id": "legacy"}) + "\n")
        forensic._LAST_HASH = forensic._last_hash_from_file()
        forensic.record_event({"event_id": "c"})
        result = forensic.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["legacy"], 1)
        self.assertEqual(result["verified"], 2)
        self.assertIn("legacy", [e["event_id"] for e in forensic.read_events()])

    def test_truncated_head_is_reported_without_hiding_the_intact_tail(self) -> None:
        # The live log on 2026-11-10 had its earliest records removed: the first
        # surviving record still pointed at a predecessor that was no longer in
        # the file. The old verifier stopped at record 0 and reported
        # verified=0, which hid that the remaining 144 records were intact.
        forensic.record_event({"event_id": "a"})
        forensic.record_event({"event_id": "b"})
        forensic.record_event({"event_id": "c"})
        surviving = forensic.read_events()[1:]
        tail_hash = surviving[-1]["hash"]
        with open(forensic.LOG_FILE, "w", encoding="utf-8") as handle:
            for event in surviving:
                handle.write(json.dumps(event) + "\n")

        result = forensic.verify_chain()

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["head_orphaned"], result)
        self.assertEqual(result["broken_at"], None)
        self.assertEqual(result["verified"], 2)
        self.assertEqual(result["last_hash"], tail_hash)

    def test_a_tampered_tail_still_breaks_the_chain(self) -> None:
        forensic.record_event({"event_id": "a"})
        forensic.record_event({"event_id": "b"})
        events = forensic.read_events()
        events[-1]["execution_result"] = {"ok": True, "result": "tampered"}
        with open(forensic.LOG_FILE, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

        result = forensic.verify_chain()

        self.assertFalse(result["ok"], result)
        self.assertFalse(result["head_orphaned"], result)
        self.assertEqual(result["broken_at"], "b")
        self.assertEqual(result["verified"], 1)


class TestApprovalConcurrency(_NoForensicWrite):
    def _pending_session(self) -> dict:
        session = main._new_session("send an email to the customer", "normal")
        session["plan"] = [
            {
                "tool": "send_external_message",
                "arguments": {"to": "customer@example.com", "body": "hello"},
            }
        ]
        session["index"] = 0
        first = main._run(session)
        self.assertEqual(first["status"], "approval_required")
        return session

    def test_two_concurrent_approvals_execute_once(self) -> None:
        session = self._pending_session()
        event_id = session["pending"]["event_id"]
        calls: list[str] = []
        original = main.execute_tool

        def slow_execute(tool_name, arguments):
            calls.append(tool_name)
            time.sleep(0.2)
            return original(tool_name, arguments)

        outcomes: list[object] = []

        def approve() -> None:
            try:
                outcomes.append(main._approve(session, event_id, True)["status"])
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                outcomes.append(exc)

        with mock.patch.object(main, "execute_tool", side_effect=slow_execute):
            threads = [threading.Thread(target=approve) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(calls.count("send_external_message"), 1, outcomes)
        self.assertEqual(session["pending"], None)
        # Exactly one request won; the other must be rejected, not re-executed.
        self.assertEqual(len([o for o in outcomes if o == "completed"]), 1)


class TestStopBehaviour(_NoForensicWrite):
    def test_stop_halts_an_in_flight_run(self) -> None:
        session = main._new_session("stop repro", "normal")
        session["plan"] = [{"tool": "read_emails", "arguments": {}} for _ in range(6)]
        session["index"] = 0

        original = main.execute_tool

        def slow_execute(tool_name, arguments):
            time.sleep(0.15)
            return original(tool_name, arguments)

        with mock.patch.object(main, "execute_tool", side_effect=slow_execute):
            box: dict[str, object] = {}
            thread = threading.Thread(target=lambda: box.setdefault("r", main._run(session)))
            thread.start()
            time.sleep(0.35)
            response = main.stop_agent(main.StopRequest(run_id=session["run_id"]))
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(session["status"], "blocked")
        self.assertEqual(len(response["tool_calls"]), 2)
        self.assertLess(session["iterations"], 6)

    def test_stop_resolves_a_pending_approval(self) -> None:
        session = main._new_session("send an email to the customer", "normal")
        session["plan"] = [
            {
                "tool": "send_external_message",
                "arguments": {"to": "customer@example.com", "body": "hello"},
            }
        ]
        session["index"] = 0
        main._run(session)
        self.assertIsNotNone(session["pending"])

        response = main.stop_agent(main.StopRequest(run_id=session["run_id"]))

        self.assertIsNone(session["pending"])
        self.assertEqual(response["approval_request"], {})
        self.assertEqual(len(response["blocked_actions"]), 1)
        self.assertEqual(response["blocked_actions"][0]["tool"], "send_external_message")
        event = next(e for e in session["forensics"] if e["event_id"] == response["blocked_actions"][0]["event_id"])
        self.assertIn("run stopped", event["block_reason"])

    def test_stop_does_not_relable_a_finished_run(self) -> None:
        session = main._new_session("done", "normal")
        session["status"] = "completed"
        session["agent_output"] = "all done"
        session["iterations"] = 3
        session["index"] = 3
        session["plan"] = [{"tool": "read_emails", "arguments": {}}]

        response = main.stop_agent(main.StopRequest(run_id=session["run_id"]))

        # The run had already finished; stopping must not rewrite history and
        # report a successful run as blocked.
        self.assertEqual(response["status"], "completed")
        self.assertEqual(session["status"], "completed")
        self.assertEqual(response["agent_output"], "all done")
        self.assertEqual(response["blocked_actions"], [])
        self.assertTrue(session["stop_requested"])


class TestConfigExposure(_NoForensicWrite):
    def test_health_does_not_leak_the_absolute_path(self) -> None:
        payload = main.health()
        self.assertEqual(payload["frontend_index"], "frontend/index.html")
        self.assertNotIn(str(main.BASE_DIR), payload["frontend_index"])


class TestRunStoreBound(unittest.TestCase):
    def setUp(self) -> None:
        self._record = main.record_event
        main.record_event = lambda event: None
        self._runs = dict(main.RUNS)
        self._max = main.MAX_RUNS
        main.RUNS.clear()

    def tearDown(self) -> None:
        main.record_event = self._record
        main.MAX_RUNS = self._max
        main.RUNS.clear()
        main.RUNS.update(self._runs)

    def test_eviction_never_drops_a_live_or_pending_session(self) -> None:
        finished = []
        for i in range(5):
            session = main._new_session(f"task {i}", "normal")
            session["status"] = "completed"
            finished.append(session["run_id"])

        running = main._new_session("live", "normal")
        running["status"] = "running"
        pending = main._new_session("waiting", "normal")
        pending["status"] = "approval_required"
        pending["pending"] = {"event_id": "e1"}

        main.MAX_RUNS = 2
        main._evict_finished_runs()

        self.assertEqual(set(main.RUNS), {running["run_id"], pending["run_id"]})
        self.assertTrue(all(run_id not in main.RUNS for run_id in finished))


if __name__ == "__main__":
    unittest.main()
