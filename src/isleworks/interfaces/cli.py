"""Resident model requests are explicit; terminal runs get a bounded automatic review."""
from __future__ import annotations

import argparse
import json
import urllib.parse
from dataclasses import replace
from pathlib import Path

from isleworks.config import configured_review_gateway, deepseek_settings, resolve_chat_endpoint
from isleworks.domain import ScenarioConfig
from isleworks.llm import ModelGateway
from isleworks.runner import Runner
from isleworks.storage import Store


def scenario(value: str) -> ScenarioConfig:
    if value == "baseline":
        return ScenarioConfig()
    if value == "no-nets":
        return ScenarioConfig(name=value, nets_enabled=False)
    if value == "fishing-shock":
        return ScenarioConfig(name=value, shock_enabled=True)
    return ScenarioConfig.from_dict(json.loads(Path(value).read_text(encoding="utf-8")))


def gateway(args, limit=180):
    if not args.allow_paid:
        raise ValueError("Model calls require --allow-paid and an explicitly configured DEEPSEEK_API_KEY")
    settings = deepseek_settings()
    key = settings["DEEPSEEK_API_KEY"]
    if not key:
        raise ValueError("Set DEEPSEEK_API_KEY in local .env or environment; see .env.example")
    base_url = getattr(args, "base_url", None)
    legacy_endpoint = getattr(args, "endpoint", None)
    configured_url = settings.get("DEEPSEEK_BASE_URL", settings.get("DEEPSEEK_ENDPOINT", ""))
    endpoint = resolve_chat_endpoint(base_url if base_url is not None else
                                     legacy_endpoint if legacy_endpoint is not None else configured_url)
    model = args.model if args.model is not None else settings["DEEPSEEK_MODEL"]
    return ModelGateway(endpoint, model, key, max_calls=min(args.max_calls, limit),
                        decision_mode=getattr(args, "decision_output", "tool_strict"),
                        thinking=False if urllib.parse.urlparse(endpoint).hostname == "api.deepseek.com" else None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="isleworks", description="A small, auditable island economy")
    parser.add_argument("--db", default="runs/isleworks.sqlite3", help="Local SQLite archive")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Start a new economic experiment")
    run.add_argument("--scenario", default=None, help="baseline, no-nets, fishing-shock or a JSON file")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--days", type=int, default=None, help="Short development run (1-30 days)")
    run.add_argument("--mode", choices=("rules", "llm"), default="rules")
    run.add_argument("--no-llm-review", action="store_true", help="Keep only the automatic facts report")
    resume = sub.add_parser("resume", help="Continue a paused run from its committed day")
    resume.add_argument("run_id")
    report = sub.add_parser("review", help="Read an offline report or explicitly request LLM review")
    report.add_argument("run_id")
    report.add_argument("--mode", choices=("rules", "llm"), default="rules")
    for item in (run, resume, report):
        item.add_argument("--allow-paid", action="store_true")
        urls = item.add_mutually_exclusive_group()
        urls.add_argument("--base-url", default=None, help="Override DEEPSEEK_BASE_URL from environment or .env")
        urls.add_argument("--endpoint", default=None, help="Legacy full endpoint override")
        item.add_argument("--model", default=None, help="Override DEEPSEEK_MODEL (default: deepseek-flash)")
        item.add_argument("--max-calls", type=int, default=180)
        item.add_argument("--decision-output", choices=("tool_strict", "json_object"),
                          default="tool_strict", help="Advanced comparison override; new decisions default to strict tools")
    for command in ("show", "replay", "screen"):
        sub.add_parser(command).add_argument("run_id")
    diagnose = sub.add_parser("diagnose", help="Inspect recorded failure reasons without model calls")
    diagnose.add_argument("run_id")
    diagnose.add_argument("--include-response", action="store_true",
                          help="Include bounded, redacted failed-response excerpts")
    sub.add_parser("list")
    serve = sub.add_parser("serve", help="Local observer; rule residents plus automatic model review")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-llm-review", action="store_true", help="Disable model review for newly launched runs")
    args = parser.parse_args(argv)
    try:
        store = Store(args.db)
        if args.command == "serve":
            from isleworks.interfaces.server import serve as start_server
            start_server(store, args.port, automatic_review=not args.no_llm_review)
            return
        review_factory = None if getattr(args, "no_llm_review", False) else lambda: configured_review_gateway(
            endpoint=getattr(args, "endpoint", None) or getattr(args, "base_url", None),
            model=getattr(args, "model", None))
        runner = Runner(store, review_factory=review_factory)
        if args.command == "run":
            if args.mode == "llm":
                runner.gateway = gateway(args)
            config = scenario(args.scenario) if args.scenario else store.adopted_config()
            if args.days is not None:
                config = replace(config, days=args.days)
            run_id = runner.create(config, seed=args.seed, mode=args.mode,
                                   review_enabled=not args.no_llm_review)
            print(f"Run: {run_id}", flush=True)
            result = runner.advance(run_id)
            output = {"run_id": run_id, "status": result["status"], "metrics": runner.metrics(run_id),
                      "facts_report": store.review(run_id),
                      "llm_review": store.latest_model_review(run_id),
                      "review": store.latest_model_review(run_id) or store.review(run_id)}
        elif args.command == "resume":
            if store.run(args.run_id)["mode"] == "llm":
                runner.gateway = gateway(args)
            output = runner.advance(args.run_id)
        elif args.command == "list":
            output = store.list_runs()
        elif args.command == "show":
            output = {"run": store.run(args.run_id), "day": store.day(args.run_id), "metrics": runner.metrics(args.run_id)}
        elif args.command == "replay":
            output = runner.replay(args.run_id)
        elif args.command == "diagnose":
            output = runner.diagnostics(args.run_id, include_response=args.include_response)
        elif args.command == "review":
            output = runner.summarize(args.run_id, gateway(args, 2) if args.mode == "llm" else None)
        else:
            output = runner.screen(args.run_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("Interrupted. Inspect the archive before resuming; uncertain model requests are never silently repeated.")
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
