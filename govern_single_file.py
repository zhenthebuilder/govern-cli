#!/usr/bin/env python3
"""
govern_single_file.py -- zero-dependency, single-file build of govern-cli.

For environments without pip/network access to install the full package.
Supports JSON specs natively (no YAML dependency). If PyYAML happens to be
installed, .yaml/.yml specs also work.

Usage:
    python3 govern_single_file.py check spec.json /path/to/workspace [--json] [--out report.json] [--strict]
    python3 govern_single_file.py init [--out spec.json]
    python3 govern_single_file.py diff old.json new.json

This file intentionally duplicates govern_cli's logic in one place so it can
be downloaded and run with a single `curl | python3` style command with no
other files needed.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
@dataclasses.dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    evidence: dict = dataclasses.field(default_factory=dict)

    def to_dict(self):
        return dataclasses.asdict(self)


def _resolve(workspace: Path, pattern: str):
    if any(ch in pattern for ch in "*?["):
        return sorted(workspace.glob(pattern))
    p = workspace / pattern
    return [p] if p.exists() else []


def check_exists(item, workspace, ctx):
    pattern = item["path"]
    matches = _resolve(workspace, pattern)
    non_empty = [m for m in matches if m.is_dir() or (m.is_file() and m.stat().st_size > 0)]
    if non_empty:
        return CheckResult("exists", "pass", f"found {len(non_empty)} match(es) for '{pattern}'",
                            {"matches": [str(m) for m in non_empty]})
    if matches:
        return CheckResult("exists", "fail", f"'{pattern}' matched but empty", {})
    return CheckResult("exists", "fail", f"no path matches '{pattern}'", {})


def check_min_size(item, workspace, ctx):
    pattern = item["path"]; min_bytes = int(item.get("min_bytes", 1))
    matches = _resolve(workspace, pattern)
    if not matches:
        return CheckResult("min_size", "fail", f"no path matches '{pattern}'", {})
    sizes = {}
    ok = True
    for m in matches:
        sz = sum(f.stat().st_size for f in m.rglob("*") if f.is_file()) if m.is_dir() else m.stat().st_size
        sizes[str(m)] = sz
        if sz < min_bytes:
            ok = False
    return CheckResult("min_size", "pass" if ok else "fail", f"sizes={sizes} threshold={min_bytes}", {"sizes": sizes})


def check_contains(item, workspace, ctx):
    pattern = item["path"]; needle = item["pattern"]; is_regex = item.get("regex", False)
    matches = _resolve(workspace, pattern)
    if not matches:
        return CheckResult("contains", "fail", f"no path matches '{pattern}'", {})
    found_in = []
    for m in matches:
        if not m.is_file():
            continue
        try:
            text = m.read_text(errors="ignore")
        except Exception:
            continue
        if (re.search(needle, text) if is_regex else needle in text):
            found_in.append(str(m))
    return CheckResult("contains", "pass" if found_in else "fail", f"found_in={found_in}", {"found_in": found_in})


def _has_key(data, dotted):
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def check_required_keys(item, workspace, ctx):
    path = workspace / item["path"]; required = item["keys"]
    if not path.exists():
        return CheckResult("required_keys", "fail", f"'{item['path']}' missing", {})
    try:
        if path.suffix in (".yaml", ".yml"):
            import yaml  # optional dependency
            data = yaml.safe_load(path.read_text())
        else:
            data = json.loads(path.read_text())
    except Exception as e:
        return CheckResult("required_keys", "fail", f"parse error: {e}", {})
    missing = [k for k in required if not _has_key(data, k)]
    return CheckResult("required_keys", "pass" if not missing else "fail", f"missing={missing}", {"missing": missing})


def check_run(item, workspace, ctx):
    cmd = item["cmd"]; timeout = int(item.get("timeout_sec", 60)); expect_exit = int(item.get("expect_exit", 0))
    must_contain = item.get("stdout_contains")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(workspace), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult("run", "fail", f"command timed out after {timeout}s: {cmd}", {})
    dt = time.time() - t0
    ok = proc.returncode == expect_exit
    if ok and must_contain:
        ok = must_contain in proc.stdout
    return CheckResult("run", "pass" if ok else "fail", f"exit={proc.returncode} dur={dt:.2f}s",
                        {"exit_code": proc.returncode, "stdout_tail": proc.stdout[-1500:]})


PLACEHOLDER_MARKERS = [
    r"\bTODO\b", r"\bTBD\b", r"\bFIXME\b", r"\bLorem ipsum\b", r"\bplaceholder\b",
    r"\bXXX\b", r"\[insert .*?\]", r"<INSERT", r"coming soon", r"not yet implemented",
    r"NotImplementedError",
]


def check_no_placeholder(item, workspace, ctx):
    pattern = item["path"]; matches = _resolve(workspace, pattern)
    markers = PLACEHOLDER_MARKERS + item.get("extra_markers", [])
    hits = {}
    for m in matches:
        if not m.is_file():
            continue
        try:
            text = m.read_text(errors="ignore")
        except Exception:
            continue
        found = [pat for pat in markers if re.search(pat, text, re.IGNORECASE)]
        if found:
            hits[str(m)] = found
    return CheckResult("no_placeholder", "fail" if hits else "pass", f"hits={hits}", {"hits": hits})


NUMERIC_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:\.\d{1,3})?)\s?%?")


def check_numeric_grounding(item, workspace, ctx):
    doc_path = workspace / item["path"]
    if not doc_path.exists():
        return CheckResult("numeric_grounding", "fail", f"'{item['path']}' missing", {})
    text = doc_path.read_text(errors="ignore")
    section = item.get("section_regex")
    if section:
        m = re.search(section, text, re.DOTALL)
        text = m.group(0) if m else ""
    ignore = set(item.get("ignore_numbers", [])) | {"0", "1", "2", "3", "4", "5"}
    nums_in_doc = {n for n in NUMERIC_RE.findall(text) if n not in ignore and len(n) > 1}
    evidence_text = ""
    for g in item["evidence"]:
        for p in _resolve(workspace, g):
            if p.is_file():
                try:
                    evidence_text += p.read_text(errors="ignore") + "\n"
                except Exception:
                    pass
    nums_in_evidence = set(NUMERIC_RE.findall(evidence_text))
    unsupported = sorted(n for n in nums_in_doc if n not in nums_in_evidence)
    return CheckResult("numeric_grounding", "warn" if unsupported else "pass",
                        f"{len(unsupported)} unsupported number(s)", {"unsupported_numbers": unsupported})


def check_git_freshness(item, workspace, ctx):
    path = item["path"]; since_ref = item.get("since", "HEAD~5")
    try:
        proc = subprocess.run(["git", "log", "-1", "--format=%H", "--", path],
                               cwd=str(workspace), capture_output=True, text=True, timeout=20)
        last_commit = proc.stdout.strip()
        if not last_commit:
            return CheckResult("git_freshness", "fail", f"'{path}' has no git history", {})
        proc2 = subprocess.run(["git", "merge-base", "--is-ancestor", since_ref, last_commit],
                                cwd=str(workspace), capture_output=True, text=True, timeout=20)
        fresh = proc2.returncode == 0
    except Exception as e:
        return CheckResult("git_freshness", "warn", f"could not evaluate: {e}", {})
    return CheckResult("git_freshness", "pass" if fresh else "fail",
                        f"last_commit={last_commit} since={since_ref} fresh={fresh}", {})


REGISTRY = {
    "exists": check_exists, "min_size": check_min_size, "contains": check_contains,
    "required_keys": check_required_keys, "run": check_run,
    "no_placeholder": check_no_placeholder, "numeric_grounding": check_numeric_grounding,
    "git_freshness": check_git_freshness,
}


def _combine_status(statuses):
    if any(s == "fail" for s in statuses):
        return "fail"
    if any(s == "warn" for s in statuses):
        return "warn"
    return "pass"


def load_spec(spec_path: Path) -> dict:
    text = spec_path.read_text()
    if spec_path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def run_governance(spec_path: Path, workspace: Path) -> dict:
    spec = load_spec(spec_path)
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
                res = fn(check_spec, workspace, None)
            except Exception as e:
                res = CheckResult(kind, "fail", f"check raised exception: {e!r}", {})
            results.append(res)
        required = item.get("required", True)
        statuses = [r.status for r in results] or ["pass"]
        status = _combine_status(statuses)
        deliverables.append({
            "id": item["id"], "description": item.get("description", ""),
            "required": required, "status": status,
            "checks": [r.to_dict() for r in results],
        })
    required_statuses = [d["status"] for d in deliverables if d["required"]]
    overall = _combine_status(required_statuses) if required_statuses else "pass"
    summary = {
        "total": len(deliverables),
        "pass": sum(1 for d in deliverables if d["status"] == "pass"),
        "warn": sum(1 for d in deliverables if d["status"] == "warn"),
        "fail": sum(1 for d in deliverables if d["status"] == "fail"),
        "required_fail": sum(1 for d in deliverables if d["status"] == "fail" and d["required"]),
    }
    return {
        "spec_path": str(spec_path), "workspace": str(workspace),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall_status": overall, "summary": summary, "deliverables": deliverables,
    }


def diff_reports(old: dict, new: dict) -> dict:
    old_by_id = {d["id"]: d for d in old.get("deliverables", [])}
    new_by_id = {d["id"]: d for d in new.get("deliverables", [])}
    rank = {"pass": 2, "warn": 1, "fail": 0}
    regressions, improvements, unchanged = [], [], []
    for did, nd in new_by_id.items():
        od = old_by_id.get(did)
        if od is None:
            continue
        if rank[nd["status"]] < rank[od["status"]]:
            regressions.append({"id": did, "from": od["status"], "to": nd["status"]})
        elif rank[nd["status"]] > rank[od["status"]]:
            improvements.append({"id": did, "from": od["status"], "to": nd["status"]})
        else:
            unchanged.append(did)
    return {"regressions": regressions, "improvements": improvements, "unchanged": unchanged}


TEMPLATE = {
    "deliverables": [
        {
            "id": "example-doc",
            "description": "Example: a README must exist and be non-trivial",
            "required": True,
            "checks": [
                {"type": "exists", "path": "README.md"},
                {"type": "min_size", "path": "README.md", "min_bytes": 200},
                {"type": "no_placeholder", "path": "README.md"},
            ],
        }
    ]
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="govern_single_file.py")
    sub = p.add_subparsers(dest="command", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("spec"); pc.add_argument("workspace")
    pc.add_argument("--json", action="store_true")
    pc.add_argument("--out"); pc.add_argument("--strict", action="store_true")
    pd = sub.add_parser("diff")
    pd.add_argument("old"); pd.add_argument("new")
    pi = sub.add_parser("init")
    pi.add_argument("--out", default="govern.spec.json")
    args = p.parse_args(argv)

    if args.command == "init":
        Path(args.out).write_text(json.dumps(TEMPLATE, indent=2))
        print(f"wrote template spec to {args.out}")
        return 0
    if args.command == "diff":
        old = json.loads(Path(args.old).read_text())
        new = json.loads(Path(args.new).read_text())
        d = diff_reports(old, new)
        print(json.dumps(d, indent=2))
        return 1 if d["regressions"] else 0
    if args.command == "check":
        data = run_governance(Path(args.spec), Path(args.workspace))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(f"govern check :: spec={args.spec} workspace={args.workspace}")
            print("-" * 72)
            for d in data["deliverables"]:
                req = "required" if d["required"] else "optional"
                print(f"[{d['status'].upper()}] {d['id']} ({req}) - {d['description']}")
                for c in d["checks"]:
                    print(f"    - {c['name']}: {c['status'].upper()} :: {c['detail'][:140]}")
            s = data["summary"]
            print("-" * 72)
            print(f"Summary: {s['pass']} pass, {s['warn']} warn, {s['fail']} fail "
                  f"(of {s['total']}); required_fail={s['required_fail']}")
            print(f"Overall: {data['overall_status'].upper()}")
        if args.out:
            Path(args.out).write_text(json.dumps(data, indent=2))
        if data["overall_status"] == "fail":
            return 1
        if data["overall_status"] == "warn" and args.strict:
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
