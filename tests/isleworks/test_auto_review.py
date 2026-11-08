"""Automatic post-run review tests.

All model transports in this module are in-process fakes.  In particular, a
plain ``Runner(store)`` must remain completely offline: these tests should
never need an environment key or a reachable endpoint.
"""

import json
import http.client
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from isleworks.domain import ScenarioConfig
from isleworks.llm import ModelGateway
from isleworks.runner import Runner
from isleworks.storage import Store
from isleworks.interfaces.server import make_server


class AutoReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "archive.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def review_factory(calls, response=None, started=None, release=None):
        response = response or {"summary": "fixture review", "claims": [], "proposal": None}

        def requestor(system, user, **kwargs):
            calls.append(json.loads(user))
            if started:
                started.set()
            if release:
                release.wait(2)
            return response

        def factory():
            return ModelGateway("http://localhost:1", "fixture-review",
                                requestor=requestor, max_calls=2)
        return factory

    def complete(self, *, runner=None, review_enabled=True):
        runner = runner or Runner(self.store)
        run_id = runner.create(ScenarioConfig(days=1), review_enabled=review_enabled)
        self.assertEqual(runner.advance(run_id)["status"], "completed")
        return run_id

    def test_plain_programmatic_runner_is_offline_and_facts_are_version_one(self):
        runner = Runner(self.store)
        run_id = self.complete(runner=runner)
        facts = self.store.review(run_id, "1")
        self.assertIsNotNone(facts)
        self.assertEqual(facts["review_version"], "1")
        self.assertEqual(facts["status"], "offline")
        model = self.store.latest_model_review(run_id)
        self.assertIsNotNone(model)
        self.assertEqual(model["status"], "unavailable")
        self.assertEqual([c for c in self.store.calls(run_id) if c.get("purpose") == "review"], [])

    def test_fixed_configuration_can_retry_without_repeating_a_paid_request(self):
        run_id = self.complete(runner=Runner(self.store))
        self.assertEqual(self.store.latest_model_review(run_id)["status"], "unavailable")
        calls = []
        runner = Runner(self.store, review_factory=self.review_factory(calls))
        report = runner.summarize(run_id, automatic=True)
        self.assertEqual(report["status"], "no_change")
        runner.summarize(run_id, automatic=True)
        self.assertEqual(len(calls), 1)

    def test_dead_review_process_displays_interrupted_without_resending(self):
        runner = Runner(self.store)
        run_id = self.complete(runner=runner, review_enabled=False)
        self.store.save_review(run_id, {"status": "pending", "owner_pid": 123456,
                                       "review_version": "llm-2"}, "llm-2")
        with patch("isleworks.storage.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(self.store.latest_model_review(run_id)["status"], "interrupted")
        self.assertEqual(self.store.review(run_id, "llm-2")["status"], "pending")
        calls = []
        Runner(self.store, review_factory=self.review_factory(calls)).summarize(run_id, automatic=True)
        self.assertEqual(calls, [])

    def test_invalid_factory_is_unavailable_without_claiming_fake_success(self):
        runner = Runner(self.store, review_factory=lambda: object())
        run_id = self.complete(runner=runner)
        model = self.store.latest_model_review(run_id)
        self.assertEqual(model["status"], "unavailable")
        self.assertNotIn(model["status"], ("success", "schema_validated", "no_change"))
        self.assertEqual([c for c in self.store.calls(run_id) if c.get("purpose") == "review"], [])

    def test_configured_factory_runs_one_automatic_llm_review_and_preserves_facts(self):
        calls = []
        runner = Runner(self.store, review_factory=self.review_factory(calls))
        run_id = self.complete(runner=runner)
        model = self.store.latest_model_review(run_id)
        self.assertEqual(len(calls), 1)
        self.assertIn(model["status"], ("no_change", "schema_validated"))
        self.assertEqual(model["review_version"], "llm-2")
        self.assertEqual(model["facts_report"]["review_version"], "1")
        self.assertEqual(self.store.review(run_id, "1")["status"], "offline")

    def test_review_failure_is_nonfatal_and_run_remains_completed(self):
        calls = []
        bad = {"summary": "not valid", "claims": [{"bad": True}], "proposal": None}
        runner = Runner(self.store, review_factory=self.review_factory(calls, bad))
        run_id = self.complete(runner=runner)
        self.assertEqual(self.store.run(run_id)["status"], "completed")
        self.assertEqual(self.store.latest_model_review(run_id)["status"], "failed")
        self.assertLessEqual(len(calls), 2)

    def test_repeated_automatic_review_does_not_make_another_call(self):
        calls = []
        runner = Runner(self.store, review_factory=self.review_factory(calls))
        run_id = self.complete(runner=runner)
        first = runner.summarize(run_id, automatic=True)
        second = runner.summarize(run_id, automatic=True)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_concurrent_finalization_has_one_claim_and_two_call_budget(self):
        calls, errors = [], []
        started, release = threading.Event(), threading.Event()
        factory = self.review_factory(calls, started=started, release=release)
        runner = Runner(self.store, review_factory=factory)
        run_id = self.complete(runner=Runner(self.store), review_enabled=False)

        def finish():
            try:
                Runner(self.store, review_factory=factory).summarize(run_id, automatic=True)
            except Exception as exc:  # the loser may observe the durable claim
                errors.append(exc)

        t1 = threading.Thread(target=finish); t1.start()
        self.assertTrue(started.wait(2))
        t2 = threading.Thread(target=finish); t2.start()
        t2.join(2); release.set(); t1.join(3)
        self.assertLessEqual(len(calls), 2)
        self.assertNotEqual(self.store.latest_model_review(run_id)["status"], "pending")
        self.assertEqual(len([c for c in self.store.calls(run_id) if c.get("purpose") == "review"]), len(calls))

    def test_server_returns_both_reports_only_for_latest_day(self):
        calls = []
        factory = self.review_factory(calls)
        runner = Runner(self.store, review_factory=factory)
        run_id = self.complete(runner=runner)
        server = make_server(self.store, 0, review_factory=factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            response = conn.getresponse()
            payload = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            return payload

        latest = get(f"/api/runs/{run_id}?day=last")
        self.assertIsNotNone(latest["facts_report"])
        self.assertIsNotNone(latest["llm_review"])
        self.assertEqual(latest["llm_review"]["review_version"], "llm-2")
        historical = get(f"/api/runs/{run_id}?day=0")
        self.assertIsNone(historical["facts_report"])
        self.assertIsNone(historical["llm_review"])


if __name__ == "__main__":
    unittest.main()
