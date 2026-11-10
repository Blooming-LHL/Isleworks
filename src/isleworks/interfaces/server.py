"""Loopback-only observer with CSRF and host checks; bounded automatic reviews after explicit experiment launch."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from isleworks.analysis import build_metrics, build_statistics
from isleworks.config import configured_review_gateway
from isleworks.domain import ScenarioConfig
from isleworks.interfaces.cli import scenario
from isleworks.runner import Runner
from isleworks.storage import Store


class Manager:
    def __init__(self, store, review_factory=None):
        self.store = store
        self.review_factory = review_factory
        self.jobs = {}
        self.lock = threading.Lock()

    def launch(self, run_id, max_days=None):
        if self.store.run(run_id)["mode"] != "rules":
            raise ValueError("Resume real model runs through the explicit CLI, not this browser")
        with self.lock:
            old = self.jobs.get(run_id)
            if old and old[0].is_alive():
                raise ValueError("Run already active; pause before stepping")
            cancel = threading.Event()
            def work():
                try:
                    Runner(self.store, review_factory=self.review_factory).advance(
                        run_id, max_days=max_days, cancel=cancel, delay=0.12)
                except Exception:
                    # Runner preserves the failed state and error class in the archive.
                    pass
            thread = threading.Thread(target=work, daemon=True)
            self.jobs[run_id] = (thread, cancel)
            thread.start()

    def pause(self, run_id):
        with self.lock:
            job = self.jobs.get(run_id)
            if not job or not job[0].is_alive():
                raise ValueError("No local active run to pause")
            job[1].set()

    def close(self):
        with self.lock:
            jobs = list(self.jobs.values())
            for _, cancel in jobs:
                cancel.set()
        for thread, _ in jobs:
            if thread is not threading.current_thread():
                thread.join(timeout=5)


def make_server(store: Store, port: int = 8765, *, review_factory=None):
    manager, token = Manager(store, review_factory), secrets.token_urlsafe(32)
    web = Path(__file__).with_name("web")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_ok(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def send(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.host_ok():
                self.send({"error": "Invalid host"}, 403)
                return
            url = urlparse(self.path)
            try:
                if url.path == "/api/session":
                    self.send({"token": token})
                elif url.path == "/api/runs":
                    self.send({"runs": store.list_runs()})
                elif url.path.startswith("/api/runs/"):
                    parts = url.path.strip("/").split("/")
                    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "observer":
                        run_id = parts[2]
                        run = store.run(run_id)
                        requested = parse_qs(url.query).get("day", ["last"])[0]
                        day = None if requested == "last" else int(requested)
                        archive = store.statistics_archive(run_id, day)
                        selected = store.day(run_id, archive["days"][-1]["state"]["day"])
                        cutoff = selected["state"]["day"]
                        stats = build_statistics(ScenarioConfig.from_dict(run["config"]), archive, as_of_day=cutoff)
                        residents = selected["state"]["residents"]
                        plans = selected["plans"]
                        ids = list(residents)
                        sample_ids = ids if len(ids) <= 24 else [ids[(i * len(ids)) // 24] for i in range(24)]
                        samples = {rid: residents[rid] for rid in sample_ids}
                        sample_plans = {rid: plans[rid] for rid in sample_ids if rid in plans}
                        groups = stats["daily"][-1]["planned_activity_counts"]
                        counts = {key: 0 for key in ("fish", "gather", "craft", "rest", "other")}
                        if groups is not None:
                            for key, value in groups.items():
                                counts[key if key in counts else "other"] += value
                        events = archive["events"]
                        last = archive["last_committed_day"]
                        run["day"] = last
                        final_view = cutoff == last
                        facts = store.review(run_id) if final_view else None
                        llm = store.latest_model_review(run_id) if final_view else None
                        if final_view and run["status"] in ("completed", "collapsed") and llm is None:
                            llm = ({"status": "pending", "mode": "llm", "summary": "世界已完成，正在准备自动复盘。"}
                                   if run["metadata"].get("automatic_review") else
                                   {"status": "unavailable", "mode": "llm", "error_detail": {
                                    "code": "no_automatic_review", "message": "该运行未启用自动模型复盘。"}})
                        self.send({"run": run, "state": {"day": cutoff, "residents": samples},
                                   "plans": sample_plans, "decisions": store.observer_decisions(run_id, cutoff, sample_ids),
                                   "population": {"total": len(ids), "sample_size": len(sample_ids),
                                                  "counts": counts if groups is not None else None,
                                                  "planned": sum(groups.values()) if groups is not None else None},
                                   "metrics": {"metrics": stats["totals"],
                                               "daily": {str(row["day"]): row for row in stats["daily"]}},
                                   "recent_events": events[-40:], "facts_report": facts, "llm_review": llm})
                        return
                    if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "residents":
                        run_id, resident_id = parts[2], unquote(parts[4])
                        requested = parse_qs(url.query).get("day", ["last"])[0]
                        day = None if requested == "last" else int(requested)
                        selected = store.day(run_id, day)
                        resident = selected["state"]["residents"].get(resident_id)
                        if resident is None:
                            raise KeyError("Unknown resident")
                        selected_day = selected["state"]["day"]
                        self.send({"run_id": run_id, "day": selected_day, "resident": resident,
                                   "plan": selected["plans"].get(resident_id),
                                   "decision": store.observer_decisions(run_id, selected_day, [resident_id]).get(resident_id)})
                        return
                    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "statistics":
                        run_id = parts[2]
                        run = store.run(run_id)
                        query = parse_qs(url.query)
                        requested = query.get("day", ["last"])[0]
                        day = None if requested == "last" else int(requested)
                        archive = store.statistics_archive(run_id, day)
                        self.send(build_statistics(ScenarioConfig.from_dict(run["config"]), archive,
                                                   as_of_day=day))
                        return
                    run_id = parts[2]
                    run = store.run(run_id)
                    latest = store.day(run_id)
                    query = parse_qs(url.query)
                    requested = query.get("day", ["last"])[0]
                    day = None if requested == "last" else int(requested)
                    selected = store.day(run_id, day)
                    archive = store.archive(run_id)
                    cutoff = selected["state"]["day"]
                    events = [e for e in archive["events"] if e["day"] <= cutoff]
                    metrics = build_metrics(ScenarioConfig.from_dict(run["config"]), archive["days"][0]["state"], selected["state"], events)
                    run["day"] = latest["state"]["day"]
                    final_view = cutoff == latest["state"]["day"]
                    facts_report = store.review(run_id) if final_view else None
                    llm_review = store.latest_model_review(run_id) if final_view else None
                    if final_view and run["status"] in ("completed", "collapsed") and llm_review is None:
                        if run["metadata"].get("automatic_review"):
                            llm_review = {"status": "pending", "mode": "llm",
                                          "summary": "世界已完成，正在准备自动复盘。"}
                        else:
                            llm_review = {"status": "unavailable", "mode": "llm", "error_detail": {
                                "code": "no_automatic_review", "message": "该运行未启用自动模型复盘。"}}
                    sources = {d["resident_id"]: {"status": d["status"], "source": d.get("source"),
                               "failure_reason": (d.get("failure") or {}).get("reason")}
                               for d in archive["decisions"] if d["day"] == cutoff}
                    self.send({"run": run, "state": selected["state"], "plans": selected["plans"],
                               "decisions": sources,
                               "metrics": metrics, "recent_events": events[-40:],
                               "facts_report": facts_report, "llm_review": llm_review,
                               "review": llm_review or facts_report})
                else:
                    filename = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                                "/review.js": "review.js", "/style.css": "style.css",
                                "/pixel-scene.js": "pixel-scene.js", "/statistics.html": "statistics.html",
                                "/statistics.js": "statistics.js", "/statistics.css": "statistics.css"}.get(url.path)
                    if filename is None:
                        self.send({"error": "Not found"}, 404)
                        return
                    body = (web / filename).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", {"index.html":"text/html; charset=utf-8", "app.js":"text/javascript; charset=utf-8", "review.js":"text/javascript; charset=utf-8", "style.css":"text/css; charset=utf-8", "pixel-scene.js":"text/javascript; charset=utf-8", "statistics.html":"text/html; charset=utf-8", "statistics.js":"text/javascript; charset=utf-8", "statistics.css":"text/css; charset=utf-8"}[filename])
                    self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)
            except (KeyError, ValueError, OSError) as exc:
                self.send({"error": type(exc).__name__}, 400)

        def do_POST(self):
            if not self.host_ok() or not secrets.compare_digest(self.headers.get("X-Isleworks-Token", ""), token):
                self.send({"error": "Missing or invalid local session token"}, 403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected bounded JSON body")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected object")
                parts = urlparse(self.path).path.strip("/").split("/")
                if parts == ["api", "runs"]:
                    if set(body)-{"scenario", "seed"} or body.get("scenario", "baseline") not in ("baseline", "no-nets", "fishing-shock"):
                        raise ValueError("Invalid run request")
                    run_id = Runner(store, review_factory=manager.review_factory).create(
                        scenario(body.get("scenario", "baseline")), seed=body.get("seed", 42),
                        review_enabled=manager.review_factory is not None)
                    manager.launch(run_id)
                elif len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    run_id = parts[2]
                    if body:
                        raise ValueError("Unexpected fields")
                    if parts[3] == "pause":
                        manager.pause(run_id)
                    elif parts[3] in ("resume", "step"):
                        manager.launch(run_id, max_days=1 if parts[3] == "step" else None)
                    else:
                        raise ValueError("Unknown command")
                else:
                    raise ValueError("Unknown command")
                self.send({"run_id": run_id, "status": store.run(run_id)["status"]})
            except (ValueError, KeyError, RuntimeError) as exc:
                self.send({"error": str(exc)}, 409)

    class ObserverServer(ThreadingHTTPServer):
        def server_close(self):
            manager.close()
            super().server_close()

    return ObserverServer(("127.0.0.1", port), Handler)


def serve(store, port=8765, *, automatic_review=True):
    server = make_server(store, port, review_factory=configured_review_gateway if automatic_review else None)
    print(f"Isleworks observer: http://127.0.0.1:{server.server_address[1]}", flush=True)
    print("自动模型复盘：开启，每轮最多两次请求。" if automatic_review else "自动模型复盘：关闭，仅生成事实报告。", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
