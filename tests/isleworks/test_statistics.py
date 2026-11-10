import tempfile
import unittest
import json
from pathlib import Path

from isleworks.analysis import build_statistics, _primary_activity
from isleworks.domain import ScenarioConfig
from isleworks.runner import Runner
from isleworks.storage import ArchiveError, Store


class StatisticsTest(unittest.TestCase):
    def make_run(self, days=3):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / "run.sqlite3")
        runner = Runner(store)
        run_id = runner.create(config=ScenarioConfig(days=days), review_enabled=False)
        runner.advance(run_id, max_days=days)
        return store, run_id

    def test_statistics_are_exactly_truncated_to_requested_committed_day(self):
        store, run_id = self.make_run()
        config = ScenarioConfig.from_dict(store.run(run_id)["config"])
        result = build_statistics(config, store.archive(run_id), as_of_day=1)
        self.assertEqual(result["as_of_day"], 1)
        self.assertEqual(result["last_committed_day"], 3)
        self.assertEqual([row["day"] for row in result["daily"]], [0, 1])
        self.assertIsNone(result["daily"][0]["planned_activity_counts"])

    def test_planned_and_executed_activity_are_separate_measurements(self):
        store, run_id = self.make_run(days=1)
        config = ScenarioConfig.from_dict(store.run(run_id)["config"])
        row = build_statistics(config, store.archive(run_id))["daily"][1]
        self.assertIsInstance(row["planned_activity_counts"], dict)
        self.assertIsInstance(row["executed_labor_blocks"], dict)
        self.assertGreater(sum(row["executed_labor_blocks"].values()), 0)

    def test_shortage_uses_committed_consume_events(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / "run.sqlite3")
        runner = Runner(store)
        config = ScenarioConfig(days=1, initial_fish_units=0)
        run_id = runner.create(config=config, review_enabled=False)
        runner.advance(run_id)
        result = build_statistics(config, store.archive(run_id))
        self.assertEqual(result["totals"]["shortage"],
                         sum(row["shortage"] for row in result["daily"]))

    def test_planned_activity_tie_uses_first_and_keeps_unknown_actions(self):
        self.assertEqual(_primary_activity(["fish", "gather", "gather", "fish"]), "fish")
        self.assertEqual(_primary_activity(["learn", "learn", "rest", "rest"]), "learn")
        self.assertIsNone(_primary_activity([]))

    def test_invalid_or_uncommitted_day_is_rejected(self):
        store, run_id = self.make_run(days=1)
        config = ScenarioConfig.from_dict(store.run(run_id)["config"])
        with self.assertRaises(ValueError):
            build_statistics(config, store.archive(run_id), as_of_day=2)

    def test_statistics_archive_is_bounded_and_excludes_model_channels(self):
        store, run_id = self.make_run()
        store.save_call(run_id, {"id": "call-1", "purpose": "decision"})
        bounded = store.statistics_archive(run_id, 1)
        self.assertEqual([day["state"]["day"] for day in bounded["days"]], [0, 1])
        self.assertNotIn("calls", bounded)
        self.assertNotIn("decisions", bounded)
        self.assertEqual(bounded["last_committed_day"], 3)

    def test_statistics_archive_rejects_corrupt_historical_snapshot(self):
        store, run_id = self.make_run(days=2)
        with store.connect() as db:
            row = db.execute("SELECT body FROM days WHERE run_id=? AND day=1", (run_id,)).fetchone()
            body = json.loads(row[0])
            body["state_hash"] = "0" * 64
            db.execute("UPDATE days SET body=? WHERE run_id=? AND day=1", (json.dumps(body), run_id))
        with self.assertRaises(ArchiveError):
            store.statistics_archive(run_id, 1)


if __name__ == "__main__":
    unittest.main()
