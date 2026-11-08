import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isleworks.domain import DailyIntent, Plan, ScenarioConfig, digest
from isleworks.interfaces.cli import main
from isleworks.llm import ModelGateway
from isleworks.runner import Runner
from isleworks.storage import Store


class FailureDiagnosticsTest(unittest.TestCase):
    def make_runner(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "archive.sqlite3")
        return Runner(store), store

    def test_legacy_errors_are_readable_and_excerpts_are_opt_in(self):
        runner, store = self.make_runner()
        run_id = runner.create(ScenarioConfig(days=1))
        store.save_call(run_id, {"id": "legacy", "attempt": 1, "status": "error", "error": "TypeError"})
        store.save_call(run_id, {
            "id": "new", "attempt": 2, "status": "error", "error": "ValueError",
            "resident_id": "r1", "day": 1, "retry": 1,
            "error_detail": {"stage": "validation", "code": "missing_field",
                             "path": "$.schedule", "message": "Required field is missing"},
            "response_excerpt": '{"wrong": true}', "response_truncated": False,
            "system": "hidden prompt", "input": "hidden context",
        })
        before = store.calls(run_id)
        report = runner.diagnostics(run_id)
        self.assertEqual(report["failed_attempts"], 2)
        self.assertEqual(report["failures"][0]["error_detail"]["stage"], "unknown")
        self.assertNotIn("response_excerpt", report["failures"][1])
        self.assertNotIn("system", json.dumps(report))
        self.assertEqual(runner.diagnostics(run_id, include_response=True)["failures"][1]["response_excerpt"],
                         '{"wrong": true}')
        self.assertEqual(store.calls(run_id), before)
        self.assertEqual(runner.metrics(run_id)["failure_summary"]["failed_attempts"], 2)

    def test_cli_diagnose_is_offline_and_marks_uncommitted_degraded_decision(self):
        runner, store = self.make_runner()
        run_id = runner.create(ScenarioConfig(days=1))
        plan = Plan("fallback", "r1", 1, "rest", (DailyIntent("r1", 1),))
        failure = {"call_id": "c1", "detail": {"stage": "validation", "code": "invalid_type",
                                              "path": "$.schedule", "message": "Expected array"}}
        store.save_decision(run_id, 1, "r1", digest({}), plan, "degraded", failure)
        output = io.StringIO()
        with patch.object(ModelGateway, "request", side_effect=AssertionError("No network allowed")):
            with contextlib.redirect_stdout(output):
                main(["--db", str(store.path), "diagnose", run_id])
        report = json.loads(output.getvalue())
        self.assertFalse(report["degraded_decisions"][0]["committed"])
        self.assertEqual(report["degraded_decisions"][0]["failure"], failure)

    def test_failed_model_calls_link_to_degraded_decisions_without_secret_leak(self):
        runner, store = self.make_runner()
        secret = "test-secret-never-log"
        client = ModelGateway("http://localhost:1", "fake", api_key=secret, max_calls=12,
                              decision_mode="json_object",
                              requestor=lambda *a, **k: {secret: secret, "goal": secret})
        runner.gateway = client
        run_id = runner.create(ScenarioConfig(days=1), mode="llm")
        result = runner.advance(run_id)
        self.assertEqual(result["status"], "completed")
        report = runner.diagnostics(run_id, include_response=True)
        self.assertEqual(report["failed_attempts"], 12)
        by_id = {call["id"]: call for call in report["failures"]}
        self.assertEqual(len(report["degraded_decisions"]), 6)
        for decision in report["degraded_decisions"]:
            call = by_id[decision["failure"]["call_id"]]
            self.assertEqual(call["resident_id"], decision["resident_id"])
            self.assertEqual(call["day"], decision["day"])
            self.assertEqual(call["error_detail"]["stage"], "validation")
            self.assertTrue(decision["committed"])
        self.assertNotIn(secret, json.dumps(store.archive(run_id)))

    def test_preflight_failure_has_reason_without_spending_calls(self):
        runner, store = self.make_runner()
        requests = []
        runner.gateway = ModelGateway("http://localhost:1", "fake", input_chars=1,
                                      decision_mode="json_object",
                                      requestor=lambda *a, **k: requests.append(1))
        run_id = runner.create(ScenarioConfig(days=1), mode="llm")
        runner.advance(run_id)
        summary = runner.metrics(run_id)["failure_summary"]
        self.assertEqual(requests, [])
        self.assertEqual(summary["failed_attempts"], 0)
        self.assertEqual(summary["latest_degraded"][-1]["failure"]["detail"]["code"], "input_limit")


if __name__ == "__main__":
    unittest.main()

