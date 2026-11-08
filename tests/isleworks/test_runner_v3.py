"""Offline v0.3 runner acceptance tests.

These tests deliberately exercise the Decision contract, rather than the
legacy full-Plan model fixture.  No environment configuration or network
transport is used.
"""

import json
import tempfile
import unittest
import urllib.error
import http.client
from pathlib import Path
from unittest.mock import patch

from isleworks.llm import ModelGateway
from isleworks.domain import ScenarioConfig
from isleworks.runner import Runner
from isleworks.storage import Store


def decision(labor=("rest",) * 4, days=1):
    return {"goal": "survive", "rationale": "offline fixture",
            "days": [{"labor": list(labor), "orders": []} for _ in range(days)]}


class RunnerV3AcceptanceTest(unittest.TestCase):
    def test_completed_call_before_decision_write_is_recovered_without_repaying(self):
        calls = []
        def fake(system, user, **kwargs):
            calls.append(1)
            return decision(("fish",) * 4)
        runner, store, gateway, run_id = self.make_run(fake)
        original_save = store.save_decision
        interrupted = []
        def save(*args, **kwargs):
            if args[2] == "r1" and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return original_save(*args, **kwargs)
        with patch.object(store, "save_decision", side_effect=save):
            with self.assertRaises(KeyboardInterrupt):
                runner.advance(run_id)
        before = len(calls)
        self.assertGreaterEqual(before, 1)
        self.assertLessEqual(before, 6)
        self.assertEqual(runner.advance(run_id)["status"], "completed")
        self.assertEqual(len(calls), 6)
        self.assertEqual(runner.replay(run_id)["verified_days"], 1)

    def test_interrupted_http_response_is_uncertain_and_world_continues(self):
        for error in (http.client.IncompleteRead(b"", 10), http.client.BadStatusLine("bad")):
            with self.subTest(error=type(error).__name__):
                seen = []
                def broken(*args, **kwargs):
                    seen.append(1)
                    raise error
                runner, store, gateway, run_id = self.make_run(broken)
                self.assertEqual(runner.advance(run_id)["status"], "completed")
                self.assertTrue(store.run(run_id)["metadata"]["channel_disabled"])
                self.assertTrue(all(call.get("uncertain") for call in store.calls(run_id)))
                self.assertLessEqual(len(seen), 3)
                self.assertEqual(store.day(run_id)["state"]["day"], 1)

    def make_run(self, requestor, *, days=1, max_calls=40):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "isleworks.sqlite3")
        gateway = ModelGateway("http://localhost:1", "fixture", max_calls=max_calls,
                               decision_mode="json_object", requestor=requestor)
        runner = Runner(store, gateway)
        run_id = runner.create(config=ScenarioConfig(days=days),
                               mode="llm", review_enabled=False)
        return runner, store, gateway, run_id

    @staticmethod
    def resident(user):
        return json.loads(user)["resident_id"]

    def test_one_model_failure_becomes_four_rest_and_world_advances(self):
        def fake(system, user, **kwargs):
            if self.resident(user) == "r1":
                return {"goal": "bad", "unexpected": True}
            return decision(("fish",) * 4)

        runner, store, gateway, run_id = self.make_run(fake)
        result = runner.advance(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.day(run_id)["state"]["day"], 1)
        r1 = next(d for d in store.archive(run_id)["decisions"] if d["resident_id"] == "r1")
        self.assertEqual(r1["source"], "fallback_rest")
        intent = r1["plan"]["schedule"][0]
        self.assertEqual(intent["labor"], ["rest"] * 4)
        self.assertEqual(intent["orders"], [])
        self.assertEqual(runner.metrics(run_id)["decision_sources"]["fallback_resident_days"], 1)

    def test_global_budget_exhaustion_still_commits_day_without_new_calls(self):
        calls = []

        def fake(system, user, **kwargs):
            calls.append(self.resident(user))
            return decision(("fish",) * 4)

        runner, store, gateway, run_id = self.make_run(fake, max_calls=1)
        result = runner.advance(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.day(run_id)["state"]["day"], 1)
        self.assertEqual(gateway.attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(runner.metrics(run_id)["decision_sources"]["fallback_resident_days"], 1)

    def test_fatal_http_disables_channel_but_does_not_pause_run(self):
        calls = []

        def fatal(system, user, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("http://localhost", 401, "unauthorized", {}, None)

        runner, store, gateway, run_id = self.make_run(fatal, days=2)
        result = runner.advance(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(store.run(run_id)["metadata"]["channel_disabled"])
        self.assertEqual(store.day(run_id)["state"]["day"], 2)
        # Up to three workers may already be in flight when the first fatal
        # response closes the channel; no later wave is started.
        self.assertLessEqual(len(calls), 3)

    def test_failed_replan_never_falls_back_to_old_plan(self):
        calls = []

        def fake(system, user, **kwargs):
            calls.append(self.resident(user))
            if len(calls) <= 6:
                return decision(("fish",) * 4)
            return {"goal": "broken"}

        runner, store, gateway, run_id = self.make_run(fake, days=2)
        result = runner.advance(run_id)
        self.assertEqual(result["status"], "completed")
        day2 = [d for d in store.archive(run_id)["decisions"] if d["day"] == 2]
        self.assertTrue(day2)
        self.assertTrue(all(d["source"] == "fallback_rest" for d in day2))
        self.assertTrue(all(d["plan"]["schedule"][0]["labor"] == ["rest"] * 4 for d in day2))

    def test_valid_plan_is_reused_for_today_without_new_paid_requests(self):
        calls = []

        def fake(system, user, **kwargs):
            calls.append(self.resident(user))
            return decision(("rest",) * 4, days=2)

        runner, store, gateway, run_id = self.make_run(fake, days=2)
        self.assertEqual(runner.advance(run_id)["status"], "completed")
        self.assertEqual(len(calls), 6)
        reused = [d for d in store.archive(run_id)["decisions"] if d["status"] == "reused"]
        self.assertEqual(len(reused), 6)

    def test_metrics_separate_voluntary_and_fallback_rest(self):
        def fake(system, user, **kwargs):
            if self.resident(user) == "r1":
                return decision(("rest",) * 4)
            return {"goal": "invalid"}

        runner, store, gateway, run_id = self.make_run(fake)
        runner.advance(run_id)
        sources = runner.metrics(run_id)["decision_sources"]
        self.assertGreaterEqual(sources["model_rest_blocks"], 4)
        self.assertGreaterEqual(sources["fallback_rest_blocks"], 4)
        self.assertGreaterEqual(sources["fallback_resident_days"], 1)

    def test_interruption_leaves_no_partial_day_and_replay_is_exact(self):
        def fake(system, user, **kwargs):
            return decision(("fish",) * 4)

        runner, store, gateway, run_id = self.make_run(fake, days=1)
        import isleworks.runner as runner_module
        with patch.object(runner_module, "step_day", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.advance(run_id)
        self.assertEqual(store.day(run_id)["state"]["day"], 0)
        self.assertEqual(store.run(run_id)["status"], "paused")
        self.assertEqual(runner.advance(run_id)["status"], "completed")
        self.assertEqual(runner.replay(run_id)["state_hash"], store.day(run_id)["state_hash"])

    def test_restart_reuses_cached_decisions_without_repeating_paid_requests(self):
        calls = []

        def fake(system, user, **kwargs):
            calls.append(self.resident(user))
            return decision(("fish",) * 4)

        runner, store, gateway, run_id = self.make_run(fake)
        import isleworks.runner as runner_module
        with patch.object(runner_module, "step_day", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.advance(run_id)
        before = len(calls)
        self.assertEqual(before, 6)
        restarted = Runner(store, gateway)
        self.assertEqual(restarted.advance(run_id)["status"], "completed")
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()
