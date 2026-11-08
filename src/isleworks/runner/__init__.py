"""Single writer orchestration. A failed draft never becomes an archived day."""

from __future__ import annotations

import threading
import time
import json
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from isleworks.agents import LLMStrategy, RuleStrategy, build_context, needs_replan
from isleworks.agents.decision import CONTRACT_VERSION, DECISION_SCHEMA, prepare_decision, validate_today
from isleworks.analysis import build_metrics
from isleworks.domain import DailyIntent, Plan, ScenarioConfig, WorldState, digest
from isleworks.engine import init_world, step_day
from isleworks.evolution import REVIEW_VERSION, compile_review, review, validate_proposal
from isleworks.llm import BudgetExceeded, ModelError
from isleworks.storage import ArchiveError, Store


class Runner:
    def __init__(self, store: Store, gateway: Any = None, review_factory: Any = None) -> None:
        self.store = store
        self.gateway = gateway
        self.review_factory = review_factory

    def create(self, config: ScenarioConfig | None = None, *, seed: int = 42,
               mode: str = "rules", review_enabled: bool = True) -> str:
        if mode == "llm" and self.gateway is None:
            raise ValueError("LLM mode requires an explicitly configured model gateway")
        config = config or self.store.adopted_config()
        metadata = {"rules_version": config.rules_version, "review_enabled": review_enabled,
                    "automatic_review": bool(review_enabled and self.review_factory is not None),
                    "model": None if mode == "rules" else self.gateway.model,
                    "decision_call_limit": 0 if mode == "rules" else self.gateway.max_calls,
                    "decision_source": "rule baseline" if mode == "rules" else (
                        "injected test transport" if getattr(self.gateway, "requestor", None) is not None
                        else "HTTP model")}
        if mode == "llm":
            metadata.update(decision_contract=CONTRACT_VERSION,
                            compiler_version="resident_compiler_v1",
                            prompt_version=LLMStrategy.PROMPT_VERSION,
                            preflight_version="today_preflight_v1",
                            decision_profile=self.gateway.decision_identity(DECISION_SCHEMA))
        return self.store.create_run(config, init_world(config), seed, mode, metadata)

    def advance(self, run_id: str, *, max_days: int | None = None,
                cancel: threading.Event | None = None, delay: float = 0) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run["status"] in ("completed", "collapsed", "error"):
            if run["status"] != "error":
                self.finalize_reports(run_id)
            return run
        config = ScenarioConfig.from_dict(run["config"])
        mode = run["mode"]
        strategy = RuleStrategy()
        if mode == "llm":
            if run["metadata"].get("decision_contract") != CONTRACT_VERSION:
                raise ArchiveError("Legacy LLM protocol: create a new run; archive and replay remain available")
            if (self.gateway is None or self.gateway.model != run["metadata"]["model"]
                    or self.gateway.decision_identity(DECISION_SCHEMA) != run["metadata"]["decision_profile"]
                    or run["metadata"].get("prompt_version") != LLMStrategy.PROMPT_VERSION
                    or run["metadata"].get("compiler_version") != "resident_compiler_v1"
                    or run["metadata"].get("preflight_version") != "today_preflight_v1"):
                raise ArchiveError("Resume requires the original model, route, adapter, contract and prompt")
            strategy = LLMStrategy(self.gateway)
        self.store.claim(run_id)
        completed = 0
        channel_closed = threading.Event()
        channel_lock = threading.Lock()

        def close_channel(detail):
            with channel_lock:
                channel_closed.set()
                self.store.disable_model_channel(run_id, detail)

        def channel_gate():
            if channel_closed.is_set():
                raise ModelError("Model channel unavailable for this run",
                                 error_detail={"stage": "transport", "code": "channel_disabled",
                                               "path": None, "message": "Run model channel is disabled"})

        try:
            if mode == "llm":
                calls = [c for c in self.store.calls(run_id) if c.get("purpose") == "decision"]
                self.gateway.attempts = len(calls)
                self.gateway.max_calls = min(self.gateway.max_calls, run["metadata"]["decision_call_limit"])
                self.gateway.on_record = lambda record: self.store.save_call(run_id, record)
                self.gateway.decision_gate = channel_gate
                if run["metadata"].get("channel_disabled"):
                    channel_closed.set()
                if any(c.get("status") == "pending" or c.get("uncertain") for c in calls):
                    close_channel({"stage": "transport", "code": "uncertain_call", "path": None,
                                   "message": "A previous request is uncertain; no automatic resend"})
                elif any(c.get("fatal") for c in calls):
                    close_channel(next(c["error_detail"] for c in calls if c.get("fatal")))

            while True:
                last = self.store.day(run_id)
                state = WorldState.from_dict(last["state"])
                plans = {rid: Plan.from_dict(plan) for rid, plan in last["plans"].items()}
                if not any(resident.active for resident in state.residents.values()):
                    self.store.set_status(run_id, "collapsed")
                    break
                if state.day >= config.days:
                    self.store.set_status(run_id, "completed")
                    break
                if (max_days is not None and completed >= max_days) or (cancel is not None and cancel.is_set()):
                    self.store.set_status(run_id, "paused")
                    break
                archive = self.store.archive(run_id)
                recent = archive["events"][-300:]
                market = [{k: event["data"][k] for k in ("good", "quantity", "price") if k in event["data"]}
                          for event in archive["events"] if event["kind"] == "trade"][-12:]

                def decide(rid: str) -> tuple[str, Plan]:
                    context = build_context(config, state, rid, recent_events=recent, market_summary=market)
                    context["run_id"] = run_id
                    old = plans.get(rid)
                    if old is not None:
                        context["current_plan"] = old.to_dict()
                    replan = needs_replan(old, context)
                    if mode == "llm" and old is not None and old.for_day(context["day"]) is not None:
                        try:
                            validate_today(old, context)
                        except (ValueError, KeyError, TypeError) as exc:
                            replan = True
                            context["previous_plan_errors"] = getattr(exc, "errors", [
                                {"stage": "precondition", "code": "invalid_previous_plan", "path": None,
                                 "message": "Previous intent is no longer executable"}])
                    context["replan_reason"] = "initial_or_expired" if replan else "reuse"
                    context_hash = digest(context)
                    cached = self.store.decision(run_id, context["day"], rid, context_hash)
                    if cached is not None:
                        return rid, Plan.from_dict(cached["plan"])
                    status, source, failure = "reused", "reused_plan", None
                    plan = old
                    if replan:
                        try:
                            previous = [c for c in archive["calls"]
                                        if c.get("purpose") == "decision" and c.get("context_hash") == context_hash
                                        and c.get("resident_id") == rid and c.get("day") == context["day"]]
                            successes = [c for c in previous if c.get("status") == "completed"]
                            if mode == "llm" and successes:
                                saved = max(successes, key=lambda c: c.get("attempt", 0))
                                try:
                                    plan, prepared = prepare_decision(saved["response"], context)
                                except (ValueError, KeyError, TypeError):
                                    raise ArchiveError("Saved validated response cannot be reconstructed") from None
                                if prepared.get("decision_hash") != saved.get("validation_metadata", {}).get("decision_hash"):
                                    raise ArchiveError("Saved model response does not match validated candidate")
                            elif mode == "llm" and previous:
                                saved = max(previous, key=lambda c: c.get("attempt", 0))
                                raise ModelError("Interrupted decision; no automatic repeated request",
                                                 error_detail=saved.get("error_detail") or {
                                                     "stage": "transport", "code": "interrupted_decision",
                                                     "path": None, "message": "Interrupted request is not resent"},
                                                 call_id=saved["id"])
                            else:
                                plan = strategy.plan(context)
                            status = "ok"
                            source = "model" if mode == "llm" else "rule_baseline"
                        except ArchiveError:
                            raise
                        except (BudgetExceeded, ModelError, ValueError, KeyError, TypeError) as exc:
                            if mode != "llm":
                                raise
                            detail = getattr(exc, "error_detail", None) or {
                                "stage": "budget" if isinstance(exc, BudgetExceeded) else "validation",
                                "code": "budget_exhausted" if isinstance(exc, BudgetExceeded) else type(exc).__name__,
                                "path": getattr(exc, "path", None),
                                "message": getattr(exc, "message", "No valid model decision was available")}
                            if isinstance(exc, ModelError) and exc.fatal:
                                close_channel(detail)
                            failure = {"call_id": getattr(exc, "call_id", None), "detail": detail,
                                       "reason": "budget_exhausted" if isinstance(exc, BudgetExceeded)
                                                 else detail.get("code", "decision_failed")}
                            # Never execute fragments of a failed response or silently reuse old actions.
                            day = context["day"]
                            plan = Plan(f"fallback-{rid}-{day}", rid, day,
                                        old.goal if old is not None else "Rest after decision failure",
                                        (DailyIntent(rid, day),),
                                        rationale="Program fallback_rest; not a model choice",
                                        replacement_reason="decision_failed")
                            status, source = "degraded", "fallback_rest"
                    if plan is None or plan.for_day(context["day"]) is None:
                        raise ValueError("Decision produced no current-day action")
                    self.store.save_decision(run_id, context["day"], rid, context_hash, plan, status,
                                             failure, source)
                    return rid, plan

                active = sorted(rid for rid, resident in state.residents.items() if resident.active)
                if mode == "llm":
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        decisions = list(pool.map(decide, active))
                else:
                    decisions = [decide(rid) for rid in active]
                for rid, plan in decisions:
                    plans[rid] = plan
                intents = tuple(plans[rid].for_day(state.day + 1) for rid in active)
                commit = step_day(state, intents, config, seed=run["seed"])
                self.store.commit(run_id, commit, plans)
                completed += 1
                if delay:
                    time.sleep(min(delay, 2))
        except KeyboardInterrupt:
            self.store.set_status(run_id, "paused", "Interrupted; inspect any pending model requests")
            raise
        except Exception as exc:
            self.store.set_status(run_id, "error", type(exc).__name__)
            raise
        final = self.store.run(run_id)
        if final["status"] in ("completed", "collapsed"):
            self.finalize_reports(run_id)
        return final

    def metrics(self, run_id: str) -> dict[str, Any]:
        archive = self.store.archive(run_id)
        config = ScenarioConfig.from_dict(archive["run"]["config"])
        result = build_metrics(config, archive["days"][0]["state"], archive["days"][-1]["state"], archive["events"])
        result["run_id"] = run_id
        result["config_hash"] = config.config_hash
        result["run_status"] = archive["run"]["status"]
        result["run_mode"] = archive["run"]["mode"]
        result["decision_transport"] = archive["run"]["metadata"].get("decision_source", "legacy")
        committed_day = archive["days"][-1]["state"]["day"]
        committed = [d for d in archive["decisions"] if d["day"] <= committed_day]
        pending = [d for d in archive["decisions"] if d["day"] > committed_day]
        result["decision_counts"] = {status: sum(d["status"] == status for d in committed)
                                      for status in ("ok", "reused", "degraded")}
        result["pending_decision_counts"] = {status: sum(d["status"] == status for d in pending)
                                              for status in ("ok", "reused", "degraded")}
        rest_counts = {"model_rest_blocks": 0, "fallback_rest_blocks": 0,
                       "rule_rest_blocks": 0, "fallback_resident_days": 0}
        for decision in committed:
            source = decision.get("source")
            if source is None:
                source = "legacy_degraded" if decision["status"] == "degraded" else "legacy"
            intent = Plan.from_dict(decision["plan"]).for_day(decision["day"])
            rests = 0 if intent is None else intent.labor.count("rest")
            if source == "fallback_rest":
                rest_counts["fallback_rest_blocks"] += rests
                rest_counts["fallback_resident_days"] += 1
            elif archive["run"]["mode"] == "rules":
                rest_counts["rule_rest_blocks"] += rests
            elif source != "legacy_degraded":
                rest_counts["model_rest_blocks"] += rests
        result["decision_sources"] = rest_counts
        diagnostics = self.diagnostics(run_id)
        result["failure_summary"] = {"run_error": diagnostics["run_error"],
                                     "failed_attempts": diagnostics["failed_attempts"],
                                     "by_reason": diagnostics["by_reason"],
                                     "latest": diagnostics["failures"][-5:],
                                     "latest_degraded": diagnostics["degraded_decisions"][-5:]}
        return result

    def diagnostics(self, run_id: str, *, include_response: bool = False) -> dict[str, Any]:
        """Project only failure data; never return prompts, config or successful output."""
        run = self.store.run(run_id)
        failures = []
        reasons: dict[tuple[str, str, str], int] = {}
        for call in self.store.calls(run_id):
            if call.get("status") != "error":
                continue
            detail = call.get("error_detail") or {
                "stage": "unknown", "code": call.get("error", "unknown"), "path": None,
                "message": "Legacy record: detailed failure reason was not recorded",
            }
            key = (detail.get("stage", "unknown"), detail.get("code", "unknown"),
                   detail.get("path") or "")
            reasons[key] = reasons.get(key, 0) + 1
            item = {name: call.get(name) for name in
                    ("id", "purpose", "resident_id", "day", "attempt", "retry", "error", "output_mode",
                     "contract_version", "finish_reason", "provider_request_id")}
            item["error_detail"] = detail
            if call.get("errors"):
                item["errors"] = call["errors"]
            if include_response:
                item["response_excerpt"] = call.get("response_excerpt")
                item["response_truncated"] = call.get("response_truncated", False)
            failures.append(item)
        failures.sort(key=lambda item: item["attempt"] or 0)
        archive = self.store.archive(run_id)
        committed_day = archive["days"][-1]["state"]["day"]
        degraded = [{"day": d["day"], "resident_id": d["resident_id"],
                     "committed": d["day"] <= committed_day, "failure": d.get("failure")}
                    for d in archive["decisions"] if d["status"] == "degraded"]
        return {"run_id": run_id, "status": run["status"], "run_error": run["error"],
                "channel_disabled": run["metadata"].get("channel_disabled", False),
                "committed_day": committed_day, "failed_attempts": len(failures),
                "by_reason": [{"stage": k[0], "code": k[1], "path": k[2] or None, "count": count}
                              for k, count in sorted(reasons.items())],
                "failures": failures, "degraded_decisions": degraded}

    def finalize_reports(self, run_id: str) -> dict[str, Any]:
        """Facts are permanent; model review failure never changes the economic outcome."""
        facts = self.summarize(run_id)
        run = self.store.run(run_id)
        model = None
        if run["metadata"].get("review_enabled", True):
            model = self.summarize(run_id, automatic=True)
        return {"facts_report": facts, "llm_review": model}

    def summarize(self, run_id: str, gateway: Any = None, *, automatic: bool = False) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run["status"] not in ("completed", "collapsed"):
            raise ArchiveError("Only economically completed runs can be reviewed")
        wants_model = gateway is not None or automatic
        version = REVIEW_VERSION if wants_model else "1"
        existing = self.store.review(run_id, version)
        retry_configuration = (wants_model and existing is not None and
                               existing.get("status") == "unavailable" and
                               (gateway is not None or self.review_factory is not None))
        if existing is not None and not retry_configuration:
            if existing.get("status") == "pending" and wants_model and not automatic:
                raise ArchiveError("Review is running or interrupted; no repeated request")
            return existing
        initial = {"status": "pending", "mode": "llm" if wants_model else "deterministic",
                   "review_version": version, "run_id": run_id,
                   "started_at": datetime.now(timezone.utc).isoformat(), "owner_pid": os.getpid(),
                   "source_config_hash": run["config_hash"], "call_limit": 2 if wants_model else 0}
        try:
            if retry_configuration:
                if not self.store.reclaim_unavailable_review(run_id, version, initial):
                    return self.store.review(run_id, version) or initial
            else:
                self.store.claim_review(run_id, version, initial)
        except ArchiveError:
            if automatic or not wants_model:
                return self.store.review(run_id, version) or initial
            raise
        metrics = self.metrics(run_id)
        config = ScenarioConfig.from_dict(run["config"])
        if not wants_model:
            result = review(metrics, config)
            result.update(run_id=run_id, review_version=version, source_config_hash=run["config_hash"])
            self.store.save_review(run_id, result, version)
            return result

        facts = self.store.review(run_id, "1")
        if facts is None:
            facts = self.summarize(run_id)
        base = {**initial, "facts_report": facts, "source_mode": run["mode"],
                "proposal": None, "candidate": None, "can_apply": False}
        # Old failed candidates can become valid after a validator bug fix, without another paid call.
        archived = sorted(self.store.calls(run_id), key=lambda call: call.get("attempt", 0), reverse=True)
        for call in archived:
            if (call.get("purpose") != "review" or call.get("review_version") == version
                    or call.get("status") != "error" or call.get("response_truncated", False)):
                continue
            excerpt = call.get("response_excerpt")
            if not excerpt:
                continue
            try:
                restored = compile_review(json.loads(excerpt), metrics, config)
            except (ValueError, KeyError, TypeError):
                continue
            restored = {**base, **restored, "facts_report": facts, "source": "archived_model_response",
                        "recovered_call_id": call["id"], "completed_at": datetime.now(timezone.utc).isoformat()}
            self.store.save_review(run_id, restored, version)
            return restored

        if gateway is None:
            if self.review_factory is None:
                result = {**base, "status": "unavailable", "error_detail": {
                    "stage": "configuration", "code": "review_not_configured", "path": None,
                    "message": "本运行未配置自动模型复盘；事实报告已保留。"}}
                self.store.save_review(run_id, result, version)
                return result
            try:
                gateway = self.review_factory()
                if not callable(getattr(gateway, "request", None)) or not hasattr(gateway, "max_calls"):
                    raise ValueError("Invalid review factory")
            except Exception as exc:
                result = {**base, "status": "unavailable", "error": type(exc).__name__,
                          "error_detail": {"stage": "configuration", "code": getattr(exc, "code", "review_configuration"),
                                           "path": None, "message": getattr(exc, "message",
                                               "自动复盘配置不可用，请检查密钥、接口和模型配置；事实报告已保留。")}}
                self.store.save_review(run_id, result, version)
                return result

        prior = [call for call in archived if call.get("purpose") == "review"
                 and call.get("review_version") == version]
        if any(call.get("status") == "pending" or call.get("uncertain") for call in prior):
            result = {**base, "status": "failed", "error_detail": {
                "stage": "transport", "code": "uncertain_review", "path": None,
                "message": "此前复盘请求状态不明，未自动重发；事实报告已保留。"}}
            self.store.save_review(run_id, result, version)
            return result
        gateway.attempts = len(prior)
        gateway.max_calls = min(gateway.max_calls, 2)
        gateway.on_record = lambda record: self.store.save_call(run_id, {**record, "review_version": version})
        try:
            result = review(metrics, config, gateway=gateway)
        except Exception as exc:
            detail = getattr(exc, "error_detail", None) or {
                "stage": "review", "code": type(exc).__name__, "path": getattr(exc, "path", None),
                "message": getattr(exc, "message", "自动模型复盘未成功，事实报告和世界结果已保留。")}
            result = {**base, "status": "failed", "error": type(exc).__name__, "error_detail": detail,
                      "call_id": getattr(exc, "call_id", None)}
        result = {**base, **result, "facts_report": facts,
                  "completed_at": datetime.now(timezone.utc).isoformat()}
        self.store.save_review(run_id, result, version)
        return result

    def replay(self, run_id: str) -> dict[str, Any]:
        archive = self.store.archive(run_id)
        config = ScenarioConfig.from_dict(archive["run"]["config"])
        state = init_world(config)
        if state.state_hash != archive["days"][0]["state_hash"]:
            raise ArchiveError("Initial state mismatch")
        for saved in archive["days"][1:]:
            intents = tuple(DailyIntent.from_dict(i) for i in saved["intents"])
            commit = step_day(state, intents, config, archive["run"]["seed"])
            if digest(commit.to_dict()) != digest({k: saved[k] for k in ("before_hash", "state", "state_hash", "events", "intents")}):
                raise ArchiveError(f"Replay mismatch on day {saved['state']['day']}")
            state = commit.state
        return {"run_id": run_id, "verified_days": state.day, "state_hash": state.state_hash, "model_calls": 0}

    def screen(self, run_id: str, *, seeds: tuple[int, ...] = (0, 1, 2, 100, 101)) -> dict[str, Any]:
        report = self.store.latest_model_review(run_id)
        if report is None or report.get("status") not in ("schema_validated", "awaiting_llm_validation") or not isinstance(report.get("proposal"), dict):
            raise ArchiveError("No model proposal to screen")
        if report.get("validation") is not None:
            return report
        config = ScenarioConfig.from_dict(self.store.run(run_id)["config"])
        candidate = validate_proposal(config, report["proposal"])
        comparisons = []
        for seed in seeds:
            pair = []
            for scenario in (config, candidate):
                child = self.create(scenario, seed=seed, mode="rules", review_enabled=False)
                self.advance(child)
                pair.append({"run_id": child, "metrics": self.metrics(child)["metrics"]})
            comparisons.append({"seed": seed, "baseline": pair[0], "candidate": pair[1]})
        report["validation"] = {"level": "rules_only", "comparisons": comparisons,
                                "note": "Not evidence that a real LLM world improved; requires separately budgeted LLM validation."}
        report["status"] = "awaiting_llm_validation"
        report["candidate"] = candidate.to_dict()
        self.store.save_review(run_id, report, report.get("review_version", REVIEW_VERSION))
        return report
