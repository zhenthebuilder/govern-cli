from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Optional

import yaml

from .checks import REGISTRY, CheckResult


@dataclasses.dataclass
class RunContext:
    workspace: Path
    spec_path: Path
    started_at: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class DeliverableVerdict:
    id: str
    description: str
    required: bool
    status: str  # pass | fail | warn
    checks: list

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "required": self.required,
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclasses.dataclass
class GovernanceReport:
    spec_path: str
    workspace: str
    generated_at: str
    deliverables: list
    overall_status: str
    summary: dict

    def to_dict(self) -> dict:
        return {
            "spec_path": self.spec_path,
            "workspace": self.workspace,
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "deliverables": [d.to_dict() for d in self.deliverables],
        }


def load_spec(spec_path: Path) -> dict:
    text = spec_path.read_text()
    if spec_path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def _combine_status(statuses: list[str]) -> str:
    if any(s == "fail" for s in statuses):
        return "fail"
    if any(s == "warn" for s in statuses):
        return "warn"
    return "pass"


def run_governance(spec_path: Path, workspace: Path) -> GovernanceReport:
    spec = load_spec(spec_path)
    ctx = RunContext(workspace=workspace, spec_path=spec_path)
    deliverables = []
    for item in spec.get("deliverables", []):
        results = []
        for check_spec in item.get("checks", []):
            kind = check_spec["type"]
            fn = REGISTRY.get(kind)
            if fn is None:
                results.append(CheckResult(kind, "fail", f"unknown check type '{kind}'", {}))
                continue
            try:
                res = fn(check_spec, workspace, ctx)
            except Exception as e:
                res = CheckResult(kind, "fail", f"check raised exception: {e!r}", {})
            results.append(res)
        required = item.get("required", True)
        statuses = [r.status for r in results] or ["pass"]
        status = _combine_status(statuses)
        deliverables.append(DeliverableVerdict(
            id=item["id"], description=item.get("description", ""),
            required=required, status=status, checks=results,
        ))

    required_statuses = [d.status for d in deliverables if d.required]
    overall = _combine_status(required_statuses) if required_statuses else "pass"
    summary = {
        "total": len(deliverables),
        "pass": sum(1 for d in deliverables if d.status == "pass"),
        "warn": sum(1 for d in deliverables if d.status == "warn"),
        "fail": sum(1 for d in deliverables if d.status == "fail"),
        "required_fail": sum(1 for d in deliverables if d.status == "fail" and d.required),
    }
    return GovernanceReport(
        spec_path=str(spec_path), workspace=str(workspace),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        deliverables=deliverables, overall_status=overall, summary=summary,
    )


def diff_reports(old: dict, new: dict) -> dict:
    """Compare two governance reports (e.g. across agent iterations) to
    surface regressions: deliverables that were pass/warn before and are
    fail now."""
    old_by_id = {d["id"]: d for d in old.get("deliverables", [])}
    new_by_id = {d["id"]: d for d in new.get("deliverables", [])}
    regressions, improvements, unchanged = [], [], []
    for did, nd in new_by_id.items():
        od = old_by_id.get(did)
        if od is None:
            continue
        rank = {"pass": 2, "warn": 1, "fail": 0}
        if rank[nd["status"]] < rank[od["status"]]:
            regressions.append({"id": did, "from": od["status"], "to": nd["status"]})
        elif rank[nd["status"]] > rank[od["status"]]:
            improvements.append({"id": did, "from": od["status"], "to": nd["status"]})
        else:
            unchanged.append(did)
    return {"regressions": regressions, "improvements": improvements, "unchanged": unchanged}
