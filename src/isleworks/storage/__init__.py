"""SQLite archive. Economic decisions and resource mutations never live here."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from isleworks.domain import DayCommit, Plan, ScenarioConfig, WorldState, canonical, digest


class ArchiveError(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path = "runs/isleworks.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS configs(hash TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, seed INTEGER NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
                    error TEXT, metadata TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS days(
                    run_id TEXT NOT NULL, day INTEGER NOT NULL, state_hash TEXT NOT NULL,
                    body TEXT NOT NULL, plans TEXT NOT NULL, PRIMARY KEY(run_id, day));
                CREATE TABLE IF NOT EXISTS decisions(
                    run_id TEXT NOT NULL, day INTEGER NOT NULL, resident_id TEXT NOT NULL,
                    context_hash TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, day, resident_id));
                CREATE TABLE IF NOT EXISTS calls(
                    run_id TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, id));
                CREATE TABLE IF NOT EXISTS reviews(
                    run_id TEXT NOT NULL, version TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, version));
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register_config(self, config: ScenarioConfig) -> str:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO configs VALUES(?,?)", (config.config_hash, canonical(config.to_dict())))
            db.execute("INSERT OR IGNORE INTO settings VALUES('adopted_config',?)", (config.config_hash,))
        return config.config_hash

    def adopted_config(self) -> ScenarioConfig:
        with self.connect() as db:
            row = db.execute("SELECT c.body FROM configs c JOIN settings s ON c.hash=s.value WHERE s.key='adopted_config'").fetchone()
        return ScenarioConfig() if row is None else ScenarioConfig.from_dict(json.loads(row[0]))

    def create_run(self, config: ScenarioConfig, initial: WorldState, seed: int, mode: str,
                   metadata: dict[str, Any] | None = None) -> str:
        if mode not in ("rules", "llm") or type(seed) is not int:
            raise ArchiveError("Invalid run mode or seed")
        initial.validate()
        if initial.day != 0:
            raise ArchiveError("New runs start at day zero")
        run_id = uuid4().hex
        body = {"before_hash": None, "state": initial.to_dict(), "state_hash": initial.state_hash,
                "events": [], "intents": []}
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO configs VALUES(?,?)", (config.config_hash, canonical(config.to_dict())))
            db.execute("INSERT OR IGNORE INTO settings VALUES('adopted_config',?)", (config.config_hash,))
            db.execute("INSERT INTO runs(id,config_hash,seed,mode,status,created,metadata) VALUES(?,?,?,?,?,?,?)",
                       (run_id, config.config_hash, seed, mode, "ready", datetime.now(timezone.utc).isoformat(), canonical(metadata or {})))
            db.execute("INSERT INTO days VALUES(?,?,?,?,?)", (run_id, 0, initial.state_hash, canonical(body), "{}"))
        return run_id

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT r.*,c.body AS config FROM runs r JOIN configs c ON r.config_hash=c.hash WHERE r.id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown run")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        result["metadata"] = json.loads(result["metadata"])
        return result

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT r.*,MAX(d.day) AS day FROM runs r JOIN days d ON r.id=d.run_id GROUP BY r.id ORDER BY r.created DESC").fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def set_status(self, run_id: str, status: str, error: str | None = None) -> None:
        if status not in ("ready", "running", "paused", "completed", "collapsed", "error"):
            raise ArchiveError("Invalid status")
        with self.connect() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown run")
            if row[0] in ("completed", "collapsed", "error") and status != row[0]:
                raise ArchiveError("A terminal run is immutable")
            db.execute("UPDATE runs SET status=?,error=? WHERE id=?", (status, error, run_id))

    def claim(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown run")
            if row[0] not in ("ready", "paused"):
                raise ArchiveError("Run is already running or terminal; do not start a second writer")
            db.execute("UPDATE runs SET status='running',error=NULL WHERE id=?", (run_id,))

    def day(self, run_id: str, day: int | None = None) -> dict[str, Any]:
        with self.connect() as db:
            if day is None:
                row = db.execute("SELECT body,plans FROM days WHERE run_id=? ORDER BY day DESC LIMIT 1", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT body,plans FROM days WHERE run_id=? AND day=?", (run_id, day)).fetchone()
        if row is None:
            raise KeyError("Unknown day")
        value = json.loads(row["body"])
        state = WorldState.from_dict(value["state"])
        if state.state_hash != value["state_hash"]:
            raise ArchiveError("Corrupt state hash")
        value["plans"] = json.loads(row["plans"])
        return value

    def commit(self, run_id: str, commit: DayCommit, plans: dict[str, Plan]) -> None:
        commit.state.validate()
        body = canonical(commit.to_dict())
        plan_body = canonical({key: plan.to_dict() for key, plan in sorted(plans.items())})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError("Unknown run")
            existing = db.execute("SELECT body,plans FROM days WHERE run_id=? AND day=?", (run_id, commit.state.day)).fetchone()
            if existing is not None:
                if existing["body"] != body or existing["plans"] != plan_body:
                    raise ArchiveError("Conflicting duplicate day")
                return
            if run[0] in ("completed", "collapsed", "error"):
                raise ArchiveError("Cannot append to terminal run")
            prior = db.execute("SELECT day,state_hash FROM days WHERE run_id=? ORDER BY day DESC LIMIT 1", (run_id,)).fetchone()
            if prior is None or prior["day"] + 1 != commit.state.day or prior["state_hash"] != commit.before_hash:
                raise ArchiveError("Stale, skipped or mismatched day commit")
            ids = [event.id for event in commit.events]
            if len(set(ids)) != len(ids) or any(e.day != commit.state.day for e in commit.events):
                raise ArchiveError("Invalid event sequence")
            db.execute("INSERT INTO days VALUES(?,?,?,?,?)", (run_id, commit.state.day, commit.state.state_hash, body, plan_body))

    def save_decision(self, run_id: str, day: int, resident_id: str, context_hash: str,
                      plan: Plan, status: str = "ok", failure: dict[str, Any] | None = None,
                      source: str | None = None) -> None:
        value = {"plan": plan.to_dict(), "status": status}
        if failure is not None:
            value["failure"] = failure
        if source is not None:
            value["source"] = source
        body = canonical(value)
        with self.connect() as db:
            existing = db.execute("SELECT context_hash,body FROM decisions WHERE run_id=? AND day=? AND resident_id=?", (run_id, day, resident_id)).fetchone()
            if existing is not None:
                if existing[0] != context_hash or existing[1] != body:
                    raise ArchiveError("Conflicting recorded decision")
                return
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (run_id, day, resident_id, context_hash, body))

    def decision(self, run_id: str, day: int, resident_id: str, context_hash: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT context_hash,body FROM decisions WHERE run_id=? AND day=? AND resident_id=?", (run_id, day, resident_id)).fetchone()
        if row is None:
            return None
        if row[0] != context_hash:
            raise ArchiveError("Decision context mismatch")
        return json.loads(row[1])

    def disable_model_channel(self, run_id: str, reason: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown run")
            metadata = json.loads(row[0])
            if not metadata.get("channel_disabled"):
                metadata["channel_disabled"] = True
                metadata["channel_failure"] = reason
                db.execute("UPDATE runs SET metadata=? WHERE id=?", (canonical(metadata), run_id))

    def save_call(self, run_id: str, record: dict[str, Any]) -> None:
        call_id = str(record["id"])
        with self.connect() as db:
            db.execute("INSERT INTO calls VALUES(?,?,?) ON CONFLICT(run_id,id) DO UPDATE SET body=excluded.body", (run_id, call_id, canonical(record)))

    def calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM calls WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def archive(self, run_id: str) -> dict[str, Any]:
        run = self.run(run_id)
        with self.connect() as db:
            rows = db.execute("SELECT body FROM days WHERE run_id=? ORDER BY day", (run_id,)).fetchall()
            decisions = db.execute("SELECT day,resident_id,body FROM decisions WHERE run_id=? ORDER BY day,resident_id", (run_id,)).fetchall()
        days = [json.loads(row[0]) for row in rows]
        return {"run": run, "days": days, "events": [event for day in days for event in day["events"]],
                "calls": self.calls(run_id), "decisions": [{"day": row[0], "resident_id": row[1], **json.loads(row[2])} for row in decisions]}

    def claim_review(self, run_id: str, version: str, initial: dict[str, Any] | None = None) -> None:
        """Unique durable claim prevents concurrent reviewers spending separate budgets."""
        with self.connect() as db:
            try:
                db.execute("INSERT INTO reviews VALUES(?,?,?)",
                           (run_id, version, canonical(initial or {"status": "pending", "review_version": version})))
            except sqlite3.IntegrityError:
                raise ArchiveError("Review already exists or is running; inspect its record") from None

    def reclaim_unavailable_review(self, run_id: str, version: str, initial: dict) -> bool:
        """Retry configuration-only failure, atomically, only if no request was ever recorded."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM reviews WHERE run_id=? AND version=?", (run_id, version)).fetchone()
            if row is None or json.loads(row[0]).get("status") != "unavailable":
                return False
            calls = db.execute("SELECT body FROM calls WHERE run_id=?", (run_id,)).fetchall()
            if any((call := json.loads(row[0])).get("purpose") == "review" and
                   call.get("review_version") == version for row in calls):
                return False
            db.execute("UPDATE reviews SET body=? WHERE run_id=? AND version=?", (canonical(initial), run_id, version))
            return True

    def save_review(self, run_id: str, value: dict[str, Any], version: str = "1") -> None:
        with self.connect() as db:
            db.execute("INSERT INTO reviews VALUES(?,?,?) ON CONFLICT(run_id,version) DO UPDATE SET body=excluded.body", (run_id, version, canonical(value)))

    def review(self, run_id: str, version: str = "1") -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM reviews WHERE run_id=? AND version=?", (run_id, version)).fetchone()
        return None if row is None else json.loads(row[0])

    def latest_model_review(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            rows = db.execute("SELECT version,body FROM reviews WHERE run_id=? AND version LIKE 'llm-%'",
                              (run_id,)).fetchall()
        if not rows:
            return None
        def version_number(row):
            try:
                return int(row[0].split("-", 1)[1])
            except ValueError:
                return 0
        report = json.loads(max(rows, key=version_number)[1])
        if report.get("status") == "pending" and type(report.get("owner_pid")) is int and report["owner_pid"] > 0:
            try:
                os.kill(report["owner_pid"], 0)
            except ProcessLookupError:
                report = {**report, "status": "interrupted", "error_detail": {
                    "code": "review_process_interrupted",
                    "message": "复盘进程已退出，请求结果可能不确定，未自动重发；事实报告已保留。"}}
            except PermissionError:
                pass
        return report

    def adopt(self, config: ScenarioConfig, expected_parent: str) -> None:
        if config.parent_hash != expected_parent:
            raise ArchiveError("Candidate parent mismatch")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key='adopted_config'").fetchone()
            if row is None or row[0] != expected_parent:
                raise ArchiveError("Stale proposal: adopted configuration changed")
            db.execute("INSERT OR IGNORE INTO configs VALUES(?,?)", (config.config_hash, canonical(config.to_dict())))
            db.execute("UPDATE settings SET value=? WHERE key='adopted_config'", (config.config_hash,))
