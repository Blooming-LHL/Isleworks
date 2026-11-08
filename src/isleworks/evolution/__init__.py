"""Evidence-backed review and bounded parameter proposals; never edits a live world."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

from isleworks.domain import ScenarioConfig

REVIEW_VERSION = "llm-2"


class ReviewValidationError(ValueError):
    """Safe, structured validation failure for a model review response."""
    def __init__(self, code: str, path: str, message: str,
                 errors: list[dict[str, str]] | None = None):
        self.code, self.path, self.message = code, path, message
        self.errors = errors or [{"code": code, "path": path, "message": message}]
        super().__init__(message)


def _review_error(code: str, path: str, message: str) -> None:
    raise ReviewValidationError(code, path, message)


def _review_refs(value, evidence: dict, path: str) -> list[str]:
    if type(value) is not list or not value:
        _review_error("wrong_type", path, "Evidence references must be a non-empty list")
    result = []
    for index, ref in enumerate(value):
        ref_path = f"{path}[{index}]"
        if type(ref) is not str:
            _review_error("wrong_type", ref_path, "Evidence reference must be a string")
        if ref not in evidence:
            _review_error("unknown_reference", ref_path, "Evidence reference is not in the supplied evidence")
        if ref not in result:
            result.append(ref)
    return result


def _review_proposal(raw, config: ScenarioConfig, evidence: dict):
    if raw is None:
        return None
    if type(raw) is not dict:
        _review_error("wrong_type", "$.proposal", "Proposal must be an object or null")
    allowed = {"parent_hash", "initial_fish_units_delta", "fishing_abundance_percent",
               "evidence_ids", "hypothesis", "risk"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        _review_error("unknown_field", f"$.proposal.{unknown[0]}", "Unknown proposal field")
    for field in ("hypothesis", "risk"):
        if field in raw and type(raw[field]) is not str:
            _review_error("wrong_type", f"$.proposal.{field}", "Expected a string")
    if "evidence_ids" not in raw:
        _review_error("missing_field", "$.proposal.evidence_ids", "Required evidence references are missing")
    normalized = dict(raw)
    normalized["evidence_ids"] = _review_refs(raw["evidence_ids"], evidence, "$.proposal.evidence_ids")
    try:
        validate_proposal(config, normalized)
    except ValueError as exc:
        code = "stale_parent" if str(exc) == "Stale parent configuration" else "invalid_value"
        path = "$.proposal.parent_hash" if code == "stale_parent" else "$.proposal"
        _review_error(code, path, "Proposal violates the supported bounded adjustment rules")
    return normalized


def review_from_response(raw, metrics: dict, config: ScenarioConfig) -> dict:
    """Validate and normalize a saved provider response without making a model call."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            _review_error("invalid_json", "$", "Review response is not valid JSON")
    if type(raw) is not dict:
        _review_error("wrong_type", "$", "Review response must be an object")
    unknown = sorted(set(raw) - {"summary", "claims", "proposal"})
    if unknown:
        _review_error("unknown_field", f"$.{unknown[0]}", "Unknown review field")
    if type(raw.get("summary")) is not str:
        _review_error("wrong_type", "$.summary", "Summary must be a string")
    if len(raw["summary"]) > 2500:
        _review_error("invalid_value", "$.summary", "Summary exceeds the supported length")
    facts, evidence = metrics.get("metrics", {}), metrics.get("evidence", {})
    if type(facts) is not dict:
        _review_error("wrong_type", "$.metrics", "Engine metrics must be an object")
    if type(evidence) is not dict:
        _review_error("wrong_type", "$.evidence", "Engine evidence must be an object")
    claims = raw.get("claims", [])
    if type(claims) is not list:
        _review_error("wrong_type", "$.claims", "Claims must be a list")
    normalized = []
    for index, claim in enumerate(claims):
        path = f"$.claims[{index}]"
        if type(claim) is not dict:
            _review_error("wrong_type", path, "Claim must be an object")
        unknown = sorted(set(claim) - {"metric_id", "evidence_ids", "interpretation"})
        if unknown:
            _review_error("unknown_field", f"{path}.{unknown[0]}", "Unknown claim field")
        metric = claim.get("metric_id")
        if type(metric) is not str:
            _review_error("wrong_type", f"{path}.metric_id", "Metric id must be a string")
        if metric not in facts:
            _review_error("unknown_metric", f"{path}.metric_id", "Metric id is not supplied by the engine")
        if type(claim.get("interpretation")) is not str:
            _review_error("wrong_type", f"{path}.interpretation", "Interpretation must be a string")
        if len(claim["interpretation"]) > 600:
            _review_error("invalid_value", f"{path}.interpretation", "Interpretation exceeds the supported length")
        normalized.append({"metric_id": metric,
                           "evidence_ids": _review_refs(claim.get("evidence_ids"), evidence,
                                                          f"{path}.evidence_ids"),
                           "interpretation": claim["interpretation"]})
    return {"summary": raw["summary"], "claims": normalized,
            "proposal": _review_proposal(raw.get("proposal"), config, evidence)}


def compile_review(raw, metrics: dict, config: ScenarioConfig) -> dict:
    """Validate an archived response and compile it into the display report."""
    return _compile_review(review_from_response(raw, metrics, config), metrics, config)


def _compile_review(normalized: dict, metrics: dict, config: ScenarioConfig) -> dict:
    facts = copy.deepcopy(metrics["metrics"])
    counts = metrics.get("decision_counts", {})
    degraded = counts.get("degraded", 0)
    healthy = degraded <= 0.1 * max(1, sum(counts.values()))
    proposal = normalized["proposal"]
    candidate = validate_proposal(config, proposal) if proposal and healthy else None
    claims = [{**claim, "value": facts[claim["metric_id"]]} for claim in normalized["claims"]]
    eligible = candidate is not None
    blocked = "degraded decisions exceed 10%" if proposal and not healthy else None
    recommendation = ("建议进入验证，尚未采纳。" if eligible else
                      ("模型候选仅作诊断展示，不可应用。" if proposal else "未提出参数调整建议。"))
    return {"status": "diagnostic_only" if proposal and not healthy else ("schema_validated" if candidate else "no_change"),
            "mode": "llm", "source_mode": metrics.get("run_mode", "unknown"),
            "summary": normalized["summary"], "summary_kind": "interpretation_not_verified_causality",
            "metrics": facts, "facts_report": facts, "claims": claims,
            "evidence_ids": sorted({r for c in claims for r in c["evidence_ids"]} |
                                     set((proposal or {}).get("evidence_ids", []))),
            "proposal": proposal, "candidate": candidate.to_dict() if candidate else None,
            "can_apply": False, "eligible_for_screening": eligible,
            "blocked_reason": blocked, "recommendations": [recommendation]}


def validate_proposal(config: ScenarioConfig, raw) -> ScenarioConfig:
    allowed = {"parent_hash", "initial_fish_units_delta", "fishing_abundance_percent",
               "evidence_ids", "hypothesis", "risk"}
    if not isinstance(raw, dict) or set(raw)-allowed:
        raise ValueError("Unknown proposal fields")
    if raw.get("parent_hash") != config.config_hash:
        raise ValueError("Stale parent configuration")
    keys = [k for k in ("initial_fish_units_delta", "fishing_abundance_percent") if k in raw]
    if len(keys) != 1:
        raise ValueError("Exactly one parameter may change")
    if keys[0] == "initial_fish_units_delta":
        delta = raw[keys[0]]
        if type(delta) is not int or delta not in (-250, 250):
            raise ValueError("Food adjustment must be exactly +/-250 millifish")
        new = config.initial_fish_units+delta
        if not 1000 <= new <= 3000:
            raise ValueError("Initial food outside calibration range")
        return replace(config, initial_fish_units=new, parent_hash=config.config_hash)
    new = raw["fishing_abundance_percent"]
    if type(new) is not int or new not in (80,100,120) or abs(new-config.fishing_abundance_percent) != 20:
        raise ValueError("Abundance must move exactly one permitted step")
    return replace(config, fishing_abundance_percent=new, parent_hash=config.config_hash)


def review(metrics: dict, config: ScenarioConfig, gateway=None) -> dict:
    facts = copy.deepcopy(metrics["metrics"])
    if gateway is None:
        return {"status": "offline", "mode": "deterministic",
                "source_mode": metrics.get("run_mode", "unknown"),
                "summary": "本复盘由程序统计生成，未额外调用模型。",
                "metrics": facts, "facts_report": facts, "claims": [], "evidence_ids": [],
                "proposal": None, "candidate": None, "can_apply": False,
                "recommendations": ["规则基线未提出参数调整建议。"]}
    prompt = (
        "Review one completed Isleworks economic experiment. Output JSON only. Numerical facts are "
        "provided by the engine, not invented by you. Use Chinese for summary and interpretation; explain uncertainty and possible causes, not proven causality. "
        "Do not equate easier resources with smarter agents. Prefer no change unless evidence warrants it. "
        "Schema: {summary:string, claims:[{metric_id:existing metric key,evidence_ids:[existing event id],"
        "interpretation:string}], proposal:null OR {parent_hash:provided hash, "
        "initial_fish_units_delta:250 or -250,evidence_ids:[existing event id],hypothesis:string,risk:string}}. "
        "Instead of initial_fish_units_delta you may use fishing_abundance_percent:80|100|120, "
        "moving only one step from the current value. Every evidence_id must be one of the supplied references; "
        "all supplied valid references are allowed (there is no arbitrary eight-reference cap). NEVER include both parameters. No other fields. "
        "Claims contain no numeric value; the UI will insert the trusted metric value. "
        "Use null proposal when uncertain, when the baseline is healthy, or when failures are technical."
    )
    payload = {"config": config.to_dict(), "parent_hash": config.config_hash,
               "facts": {key: metrics[key] for key in ("metrics", "daily", "evidence", "facts", "run_mode", "decision_counts", "decision_sources") if key in metrics}, "note": "Fish and prices are integer millifish, 1000 units=one fish."}
    raw = gateway.request(prompt, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                          purpose="review",
                          validator=lambda value: review_from_response(value, metrics, config))
    return compile_review(raw, metrics, config)


__all__ = ["review", "review_from_response", "compile_review", "validate_proposal", "ReviewValidationError", "REVIEW_VERSION"]
