import tempfile
import unittest
import unittest.mock
import urllib.error
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from isleworks.agents import build_context, needs_replan
from isleworks.domain import (
    DailyIntent, Net, Order, Plan, Resident, ScenarioConfig, WorldState, digest,
    Event, DayCommit, fish_units,
)
from isleworks.engine import init_world, step_day
from isleworks.engine.ledger import apply_trade
from isleworks.evolution import validate_proposal
from isleworks.llm import BudgetExceeded, ModelError, ModelGateway
from isleworks.evolution import REVIEW_VERSION
from isleworks.market import Trade, match_orders
from isleworks.runner import Runner
from isleworks.storage import ArchiveError, Store


class AcceptanceTest(unittest.TestCase):
    def cfg(self, **kw):
        return ScenarioConfig(**kw)

    def intent(self, rid, day, labor=("rest",) * 4, orders=()):
        return DailyIntent(rid, day, tuple(labor), tuple(orders))

    def run_case(self, seed=0, config=None, max_days=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        store = Store(Path(td.name) / "run.sqlite3")
        runner = Runner(store)
        rid = runner.create(config=config, seed=seed, review_enabled=False)
        result = runner.advance(rid, max_days=max_days)
        return runner, store, rid, result

    def test_baseline_10_seeds_complete_30_days_nonnegative(self):
        for seed in range(10):
            runner, store, rid, result = self.run_case(seed)
            self.assertEqual(result["status"], "completed", seed)
            state = store.day(rid)["state"]
            self.assertEqual(state["day"], 30)
            self.assertTrue(all(r["fish"] >= 0 and r["fiber"] >= 0 for r in state["residents"].values()))
            self.assertTrue(all(0 <= n["durability"] <= 20 for n in state["nets"].values()))
            self.assertEqual(runner.replay(rid)["state_hash"], digest(state))

    def test_baseline_same_seed_has_same_final_hash(self):
        a, sa, ra, _ = self.run_case(77)
        b, sb, rb, _ = self.run_case(77)
        self.assertEqual(sa.day(ra)["state_hash"], sb.day(rb)["state_hash"])

    def test_classic_two_day_gather_then_craft_uses_four_fiber_and_crafter_two_blocks(self):
        c = self.cfg(days=2)
        state = init_world(c)
        # A crafter can gather two fiber blocks on day one, then cannot craft
        # until the second day because the recipe needs four fiber.
        r = state.residents["r5"]
        first = step_day(state, [self.intent(x, 1, ("rest",) * 4) for x in state.residents], c)
        state = first.state
        state.residents["r5"].fiber = 4
        second = step_day(state, [self.intent(x, 2, ("craft", "craft", "rest", "rest")) for x in state.residents], c)
        self.assertIn("net-r5-2-0", second.state.nets)
        self.assertEqual(second.state.residents["r5"].fiber, 0)
        self.assertTrue(any(e.kind == "craft" and e.data["blocks"] == 2 for e in second.events))

    def test_shock_fish_yield_is_375_vs_750_on_day_15(self):
        normal = self.cfg(days=16, shock_enabled=False)
        shocked = self.cfg(days=16, shock_enabled=True)
        def to_day15(cfg):
            s = init_world(cfg)
            for day in range(1, 16):
                c = step_day(s, [self.intent(r, day, ("fish",) * 4) for r in s.residents], cfg)
                s = c.state
            return c
        n, h = to_day15(normal), to_day15(shocked)
        self.assertEqual(n.events[0].data["deltas"]["r1"]["fish"], 750)
        self.assertEqual(h.events[0].data["deltas"]["r1"]["fish"], 375)

    def test_net_expiry_and_unique_ownership(self):
        s = init_world(self.cfg(days=2))
        s.nets["n"] = Net("n", "r1", 0)
        with self.assertRaises(ValueError):
            apply_trade(s, Trade("b", "s", "r2", "r1", "net", 1, 100, ("n",)))
        s.nets["n"].durability = 20
        apply_trade(s, Trade("b", "s", "r2", "r1", "net", 1, 100, ("n",)))
        self.assertEqual(s.nets["n"].owner, "r2")
        self.assertEqual(sum(n.owner == "r2" for n in s.nets.values()), 1)

    def market_state(self):
        s = init_world(self.cfg(days=1))
        s.residents["r1"].fish = 1000
        s.residents["r2"].fiber = 5
        return s

    def test_market_aggregate_overspend_rejects_buys(self):
        s = self.market_state()
        orders = [Order("b1", "r1", "buy", "fiber", 2, 600), Order("b2", "r1", "buy", "fiber", 1, 1),
                  Order("s", "r2", "sell", "fiber", 3, 1)]
        trades, rejected = match_orders(s, [self.intent("r1", 1, orders=orders[:2]), self.intent("r2", 1, orders=(orders[2],))], 1)
        self.assertEqual(trades, ())
        self.assertEqual(set(rejected), {"b1", "b2"})

    def test_market_oversell_duplicate_net_and_fresh_net_rules(self):
        s = self.market_state(); s.nets["n1"] = Net("n1", "r2", 20)
        dup = Order("x", "r2", "sell", "net", 1, 10, ("n1",))
        dup2 = Order("y", "r2", "sell", "net", 1, 10, ("n1",))
        buy = Order("b", "r1", "buy", "net", 2, 20)
        trades, rejected = match_orders(s, [self.intent("r1", 1, orders=(buy,)), self.intent("r2", 1, orders=(dup, dup2))], 1)
        self.assertEqual(trades, ())
        self.assertEqual(set(rejected), {"x", "y"})

    def test_market_partial_fiber_fill_and_distinct_buyers(self):
        s = self.market_state()
        sell = Order("sell", "r2", "sell", "fiber", 3, 5)
        b1 = Order("b1", "r1", "buy", "fiber", 2, 6)
        b2 = Order("b2", "r3", "buy", "fiber", 2, 6)
        trades, rejected = match_orders(s, [self.intent("r2", 1, orders=(sell,)), self.intent("r1", 1, orders=(b1,)), self.intent("r3", 1, orders=(b2,))], 1)
        self.assertEqual(sum(t.quantity for t in trades), 3)
        self.assertEqual({t.buyer for t in trades}, {"r1", "r3"})
        self.assertEqual(rejected, ())

    def test_market_self_trade_is_not_matched_and_order_order_independent(self):
        s = self.market_state()
        buy = Order("b", "r1", "buy", "fiber", 1, 10)
        sell = Order("s", "r1", "sell", "fiber", 1, 1)
        self.assertEqual(match_orders(s, [self.intent("r1", 1, orders=(buy, sell))], 1)[0], ())
        s.residents["r3"].fish = 1000
        ext = Order("e", "r3", "buy", "fiber", 1, 10)
        a = match_orders(s, [self.intent("r2", 1, orders=(Order("ss", "r2", "sell", "fiber", 1, 2),)), self.intent("r3", 1, orders=(ext,))], 1)[0]
        b = match_orders(s, [self.intent("r3", 1, orders=(ext,)), self.intent("r2", 1, orders=(Order("ss", "r2", "sell", "fiber", 1, 2),))], 1)[0]
        self.assertEqual(a, b)

    def test_atomic_ledger_failure_leaves_caller_state_unchanged(self):
        s = self.market_state(); before = s.to_dict()
        with self.assertRaises(ValueError):
            apply_trade(s, Trade("b", "s", "r1", "r2", "fiber", 3, 1000))
        self.assertEqual(s.to_dict(), before)

    def test_resource_event_deltas_reconcile_state(self):
        s = init_world(self.cfg(days=1))
        c = step_day(s, [self.intent(r, 1, ("fish", "gather", "rest", "rest")) for r in s.residents], self.cfg(days=1))
        for rid, r in c.state.residents.items():
            delta = sum(e.data.get("deltas", {}).get(rid, {}).get("fish", 0) for e in c.events)
            self.assertEqual(r.fish - s.residents[rid].fish, delta)

    def test_hostile_domain_bool_float_and_nan_inputs_rejected(self):
        with self.assertRaises(ValueError): self.cfg(days=True)
        with self.assertRaises(ValueError): self.cfg(fishing_abundance_percent=100.0)
        with self.assertRaises(ValueError): fish_units(float("nan"))
        with self.assertRaises(ValueError): fish_units(0.0001)

    def test_storage_day_idempotence_and_stale_parent_cas(self):
        runner, store, rid, _ = self.run_case(max_days=1)
        archive = store.archive(rid); day = store.day(rid, 1)
        commit = DayCommit(
            day["before_hash"], WorldState.from_dict(day["state"]),
            tuple(Event(**e) for e in day["events"]),
            tuple(DailyIntent.from_dict(i) for i in day["intents"]),
        )
        plans = {rid: Plan.from_dict(p) for rid, p in day["plans"].items()}
        # Re-committing the exact serialized day is idempotent through the
        # public commit contract; a competing parent hash is rejected.
        store.commit(rid, commit, plans)
        self.assertEqual(store.day(rid, 1)["state_hash"], day["state_hash"])
        with self.assertRaises(ArchiveError):
            store.commit(rid, DayCommit("x", commit.state, (), ()), {})
        config = self.cfg(initial_fish_units=2250, parent_hash=self.cfg().config_hash)
        store.register_config(self.cfg())
        with self.assertRaises(ArchiveError): store.adopt(config, "0" * 64)

    def test_resume_and_recorded_action_replay_same_hash(self):
        runner, store, rid, paused = self.run_case(seed=9, max_days=7)
        self.assertEqual(paused["status"], "paused")
        done = runner.advance(rid)
        self.assertEqual(done["status"], "completed")
        replay = runner.replay(rid)
        self.assertEqual(replay["state_hash"], store.day(rid)["state_hash"])
        self.assertEqual(replay["model_calls"], 0)

    def test_keyboard_interrupt_pauses_without_partial_day_then_resume_replays(self):
        import isleworks.runner as runner_module
        runner, store, rid, _ = self.run_case(seed=12, max_days=0)
        original = runner_module.step_day
        with unittest.mock.patch.object(runner_module, "step_day", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.advance(rid)
        self.assertEqual(store.run(rid)["status"], "paused")
        self.assertEqual(store.day(rid)["state"]["day"], 0)
        done = runner.advance(rid)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(runner.replay(rid)["state_hash"], store.day(rid)["state_hash"])

    def test_concurrent_llm_reviews_have_one_durable_claim_and_pending_blocks(self):
        runner, store, rid, _ = self.run_case(config=self.cfg(days=1), max_days=1)
        # run_case uses review_enabled=False, leaving a completed run suitable
        # for explicit concurrent review calls.
        started, release = threading.Event(), threading.Event()
        calls = []

        def requestor(system, user, **kwargs):
            calls.append(1)
            started.set()
            release.wait(2)
            return {"summary": "concurrent fake review", "proposal": None}

        g1 = ModelGateway("http://localhost:1", "fake", max_calls=2, requestor=requestor)
        g2 = ModelGateway("http://localhost:1", "fake", max_calls=2, requestor=requestor)
        errors = []

        def first():
            try:
                Runner(store).summarize(rid, g1)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=first); t1.start()
        self.assertTrue(started.wait(2))
        self.assertEqual(store.review(rid, REVIEW_VERSION)["status"], "pending")
        with self.assertRaises(ArchiveError):
            Runner(store).summarize(rid, g2)
        release.set(); t1.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(calls), 1)
        review_calls = [c for c in store.calls(rid) if c.get("purpose") == "review"]
        self.assertLessEqual(len(review_calls), 2)
        self.assertNotEqual(store.review(rid, REVIEW_VERSION)["status"], "pending")

    def test_rejected_overspend_is_owned_and_triggers_replan_from_context(self):
        config = self.cfg(days=1)
        state = init_world(config)
        state.residents["r1"].fish = 1000
        state.residents["r2"].fiber = 3
        overspend = Order("overspend", "r1", "buy", "fiber", 2, 600)
        commit = step_day(state, [
            self.intent("r1", 1, orders=(overspend,)),
            self.intent("r2", 1, orders=(Order("sell", "r2", "sell", "fiber", 2, 1),)),
        ], config)
        rejected = [e for e in commit.events if e.kind == "invalid_action" and e.data.get("order_id") == "overspend"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].actor, "r1")
        context = build_context(config, commit.state, "r1", recent_events=commit.events)
        self.assertTrue(any(e["kind"] == "invalid_action" and e["actor"] == "r1" for e in context["recent_events"]))
        self.assertTrue(needs_replan(None, context))

    def test_order_id_can_equal_another_resident_id(self):
        config = self.cfg(days=1)
        state = init_world(config)
        state.residents["r1"].fish = 2000
        state.residents["r2"].fiber = 1
        buy = Order("r2", "r1", "buy", "fiber", 1, 5)
        sell = Order("sell-r2", "r2", "sell", "fiber", 1, 5)
        commit = step_day(state, [self.intent("r1", 1, orders=(buy,)), self.intent("r2", 1, orders=(sell,))], config)
        self.assertTrue(any(e.kind == "trade" and e.data["quantity"] == 1 for e in commit.events))

    def test_privacy_context_never_contains_peer_deltas(self):
        s = init_world(self.cfg(days=1)); event = {"id": "e", "day": 1, "kind": "gather", "actor": "r2", "data": {"secret": 99, "deltas": {"r2": {"fish": 500}}}}
        c = build_context(self.cfg(days=1), s, "r1", recent_events=[event])
        self.assertEqual(c["recent_events"], [])
        trade = {"id": "t", "day": 1, "kind": "trade", "actor": "r2",
                 "data": {"good": "fiber", "quantity": 1, "price": 5,
                          "seller": "r3", "deltas": {"r2": {"fish": -5}}}}
        c = build_context(self.cfg(days=1), s, "r1", recent_events=[trade])
        self.assertEqual(c["recent_events"][0]["data"], {"good": "fiber", "quantity": 1, "price": 5})
        self.assertNotIn("fish", c["public"]["skills"]["r2"])

    def test_strict_proposal_delta_noop_bool_and_stale_evidence(self):
        c = self.cfg()
        with self.assertRaises(ValueError): validate_proposal(c, {"parent_hash": c.config_hash, "initial_fish_units_delta": True})
        with self.assertRaises(ValueError): validate_proposal(c, {"parent_hash": c.config_hash, "initial_fish_units_delta": 0})
        with self.assertRaises(ValueError): validate_proposal(c, {"parent_hash": "0" * 64, "initial_fish_units_delta": 250})
        with self.assertRaises(ValueError): validate_proposal(c, {"parent_hash": c.config_hash, "initial_fish_units_delta": 250, "fishing_abundance_percent": 120})

    def test_gateway_retries_500_counts_budget_and_records_callbacks(self):
        calls, records = [], []
        def requestor(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1: raise urllib.error.HTTPError("http://localhost", 500, "x", {}, None)
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}
        g = ModelGateway("http://localhost:1234/v1", "m", api_key="SECRET", max_calls=2, requestor=requestor, on_record=records.append)
        self.assertEqual(g.request("s", "u"), {"ok": True})
        self.assertEqual(g.attempts, 2); self.assertEqual(len(calls), 2)
        self.assertTrue(all("SECRET" not in str(r) for r in records))
        by_id = {}
        for record in records:
            by_id.setdefault(record["id"], []).append(record["status"])
        self.assertEqual(len(by_id), 2)
        self.assertTrue(any("error" in statuses for statuses in by_id.values()))
        self.assertTrue(any("completed" in statuses for statuses in by_id.values()))
        with self.assertRaises(BudgetExceeded): g.request("s", "u")

    def test_gateway_rejects_spoofed_remote_http_and_credentials(self):
        with self.assertRaises(ValueError): ModelGateway("http://example.com/x", "m")
        with self.assertRaises(ValueError): ModelGateway("https://u:p@example.com/x", "m")
        with self.assertRaises(ValueError): ModelGateway("https://example.com/x#fragment", "m")

    def test_gateway_callback_failure_fails_closed_before_requestor_call(self):
        seen = []
        calls = []
        def callback(r):
            seen.append(r)
            raise RuntimeError("observer failed")
        g = ModelGateway("http://localhost:1", "m", requestor=lambda *a, **k: calls.append(1), on_record=callback)
        with self.assertRaises(RuntimeError): g.request("s", "u")
        self.assertEqual(calls, [])
        self.assertEqual(len(seen), 1)

    def test_fake_three_day_llm_plan_commits_and_replays(self):
        def fake_model(system, user, **kwargs):
            context = json.loads(user)
            return {"goal": "survive", "rationale": "offline fake",
                    "days": [{"labor": ["rest"] * 4, "orders": []} for _ in range(3)]}

        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        store = Store(Path(td.name) / "llm.sqlite3")
        gateway = ModelGateway("http://localhost:1", "fake", max_calls=20,
                               decision_mode="json_object", requestor=fake_model)
        runner = Runner(store, gateway)
        rid = runner.create(config=self.cfg(days=3), seed=3, mode="llm", review_enabled=False)
        result = runner.advance(rid)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(gateway.attempts, 6)
        metrics = runner.metrics(rid)
        self.assertEqual(metrics["decision_counts"]["ok"], 6)
        self.assertEqual(metrics["decision_counts"]["reused"], 12)
        replay = runner.replay(rid)
        self.assertEqual(replay["verified_days"], 3)
        self.assertEqual(replay["state_hash"], store.day(rid)["state_hash"])

    def test_gateway_input_limit_rejects_before_consuming_budget(self):
        g = ModelGateway("http://localhost:1", "m", input_chars=3,
                         requestor=lambda *a, **k: {"ok": 1})
        with self.assertRaises(ModelError):
            g.request("xx", "xx")
        self.assertEqual(g.attempts, 0)

    def test_cli_run_and_replay_are_bounded_offline(self):
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        db = str(Path(td.name) / "cli.sqlite3")
        env = dict(__import__("os").environ, PYTHONPATH="src")
        run = subprocess.run([sys.executable, "-m", "isleworks", "--db", db,
                              "run", "--days", "1", "--seed", "4", "--no-llm-review"],
                             text=True, capture_output=True, env=env, check=True)
        run_id = run.stdout.splitlines()[0].removeprefix("Run: ").strip()
        self.assertRegex(run_id, r"^[0-9a-f]{32}$")
        replay = subprocess.run([sys.executable, "-m", "isleworks", "--db", db,
                                 "replay", run_id], text=True, capture_output=True,
                                env=env, check=True)
        self.assertIn('"verified_days": 1', replay.stdout)

    def test_local_server_requires_session_token_and_exposes_offline_runs(self):
        from isleworks.interfaces.server import make_server
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        store = Store(Path(td.name) / "server.sqlite3")
        server = make_server(store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/api/session") as response:
            session = json.load(response)
        token = session["token"]
        req = urllib.request.Request(base + "/api/runs", data=b'{"scenario":"baseline"}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(req)
        self.assertEqual(missing.exception.code, 403)
        req.add_header("X-Isleworks-Token", token)
        with urllib.request.urlopen(req) as response:
            created = json.load(response)
        self.assertRegex(created["run_id"], r"^[0-9a-f]{32}$")
        time.sleep(0.05)
        with urllib.request.urlopen(base + "/api/runs") as response:
            listing = json.load(response)
        self.assertTrue(any(item["id"] == created["run_id"] for item in listing["runs"]))


if __name__ == "__main__":
    unittest.main()
