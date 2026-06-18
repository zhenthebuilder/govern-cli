"""
Individual check implementations for govern-cli.

Each check function has signature:
    check(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult

A CheckResult never raises on "the deliverable is bad" -- it returns a
structured FAIL with evidence. It only raises on programmer error (bad spec).
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "warn"
    detail: str
    evidence: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _resolve(workspace: Path, pattern: str) -> list[Path]:
    """Resolve a glob pattern relative to workspace into matching paths."""
    if any(ch in pattern for ch in "*?["):
        return sorted(workspace.glob(pattern))
    p = workspace / pattern
    return [p] if p.exists() else []


# ---------------------------------------------------------------------------
# check: exists
# ---------------------------------------------------------------------------
def check_exists(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    pattern = item["path"]
    matches = _resolve(workspace, pattern)
    non_empty = [m for m in matches if m.is_dir() or (m.is_file() and m.stat().st_size > 0)]
    if non_empty:
        return CheckResult(
            "exists", "pass", f"found {len(non_empty)} match(es) for '{pattern}'",
            {"matches": [str(m.relative_to(workspace)) for m in non_empty]},
        )
    if matches:
        return CheckResult(
            "exists", "fail", f"'{pattern}' matched but file(s) are empty (0 bytes)",
            {"matches": [str(m.relative_to(workspace)) for m in matches]},
        )
    return CheckResult("exists", "fail", f"no path matches '{pattern}'", {})


# ---------------------------------------------------------------------------
# check: min_size -- guards against stub/placeholder files
# ---------------------------------------------------------------------------
def check_min_size(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    pattern = item["path"]
    min_bytes = int(item.get("min_bytes", 1))
    matches = _resolve(workspace, pattern)
    if not matches:
        return CheckResult("min_size", "fail", f"no path matches '{pattern}'", {})
    sizes = {}
    ok = True
    for m in matches:
        sz = sum(f.stat().st_size for f in m.rglob("*") if f.is_file()) if m.is_dir() else m.stat().st_size
        sizes[str(m.relative_to(workspace))] = sz
        if sz < min_bytes:
            ok = False
    status = "pass" if ok else "fail"
    return CheckResult(
        "min_size", status,
        f"sizes={sizes} threshold={min_bytes}", {"sizes": sizes, "min_bytes": min_bytes},
    )


# ---------------------------------------------------------------------------
# check: contains -- regex/substring must be present (e.g. install command)
# ---------------------------------------------------------------------------
def check_contains(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    pattern = item["path"]
    needle = item["pattern"]
    is_regex = item.get("regex", False)
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
        if is_regex:
            if re.search(needle, text):
                found_in.append(str(m.relative_to(workspace)))
        else:
            if needle in text:
                found_in.append(str(m.relative_to(workspace)))
    status = "pass" if found_in else "fail"
    return CheckResult(
        "contains", status,
        f"pattern {'regex ' if is_regex else ''}'{needle}' found_in={found_in}",
        {"found_in": found_in},
    )


# ---------------------------------------------------------------------------
# check: json_schema_lite -- required keys present in a JSON/YAML file
# ---------------------------------------------------------------------------
def check_required_keys(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    path = workspace / item["path"]
    required = item["keys"]
    if not path.exists():
        return CheckResult("required_keys", "fail", f"'{item['path']}' missing", {})
    try:
        if path.suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(path.read_text())
        else:
            data = json.loads(path.read_text())
    except Exception as e:
        return CheckResult("required_keys", "fail", f"parse error: {e}", {})
    missing = [k for k in required if not _has_key(data, k)]
    status = "pass" if not missing else "fail"
    return CheckResult("required_keys", status, f"missing={missing}", {"missing": missing})


def _has_key(data: Any, dotted: str) -> bool:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


# ---------------------------------------------------------------------------
# check: run -- execute a command in workspace, must exit 0 within timeout
# ---------------------------------------------------------------------------
def check_run(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    cmd = item["cmd"]
    timeout = int(item.get("timeout_sec", 60))
    expect_exit = int(item.get("expect_exit", 0))
    must_contain = item.get("stdout_contains")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(workspace), capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult("run", "fail", f"command timed out after {timeout}s: {cmd}", {"cmd": cmd})
    dt = time.time() - t0
    ok = proc.returncode == expect_exit
    if ok and must_contain:
        ok = must_contain in proc.stdout
    ev = {
        "cmd": cmd, "exit_code": proc.returncode, "expect_exit": expect_exit,
        "duration_sec": round(dt, 2),
        "stdout_tail": proc.stdout[-1500:],
        "stderr_tail": proc.stderr[-1500:],
    }
    status = "pass" if ok else "fail"
    return CheckResult("run", status, f"exit={proc.returncode} expect={expect_exit} dur={dt:.2f}s", ev)


# ---------------------------------------------------------------------------
# check: no_placeholder -- scan for telltale stub/fabrication markers
# ---------------------------------------------------------------------------
PLACEHOLDER_MARKERS = [
    r"\bTODO\b", r"\bTBD\b", r"\bFIXME\b", r"\bLorem ipsum\b",
    r"\bplaceholder\b", r"\bXXX\b", r"\[insert .*?\]", r"<INSERT", r"\.\.\. ?\(truncated\)",
    r"coming soon", r"not yet implemented", r"NotImplementedError",
]


def check_no_placeholder(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    pattern = item["path"]
    matches = _resolve(workspace, pattern)
    extra = item.get("extra_markers", [])
    markers = PLACEHOLDER_MARKERS + extra
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
            hits[str(m.relative_to(workspace))] = found
    status = "fail" if hits else "pass"
    return CheckResult("no_placeholder", status, f"hits={hits}", {"hits": hits})


# ---------------------------------------------------------------------------
# check: numeric_grounding -- every numeric claim in a doc must trace to a
# value present in a cited evidence/log file (regression/fabrication guard).
# This is a heuristic, conservative check: it flags numbers that appear in
# the target doc's "Results"-like sections but do NOT appear anywhere in the
# evidence files, as *candidates* for fabrication needing human/agent review.
# ---------------------------------------------------------------------------
NUMERIC_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:\.\d{1,3})?)\s?%?")


def check_numeric_grounding(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    doc_path = workspace / item["path"]
    evidence_globs = item["evidence"]
    if not doc_path.exists():
        return CheckResult("numeric_grounding", "fail", f"'{item['path']}' missing", {})
    text = doc_path.read_text(errors="ignore")
    section = item.get("section_regex")
    if section:
        m = re.search(section, text, re.DOTALL)
        text = m.group(0) if m else ""
    nums_in_doc = set(NUMERIC_RE.findall(text))
    # common boilerplate numbers to ignore (years, single digits used as enumerations)
    ignore = set(item.get("ignore_numbers", [])) | {"0", "1", "2", "3", "4", "5"}
    nums_in_doc = {n for n in nums_in_doc if n not in ignore and len(n) > 1}

    evidence_text = ""
    ev_files = []
    for g in evidence_globs:
        for p in _resolve(workspace, g):
            if p.is_file():
                ev_files.append(str(p.relative_to(workspace)))
                try:
                    evidence_text += p.read_text(errors="ignore") + "\n"
                except Exception:
                    pass
    nums_in_evidence = set(NUMERIC_RE.findall(evidence_text))

    unsupported = sorted(n for n in nums_in_doc if n not in nums_in_evidence)
    status = "warn" if unsupported else "pass"
    return CheckResult(
        "numeric_grounding", status,
        f"{len(unsupported)} number(s) in doc not found verbatim in evidence files",
        {"unsupported_numbers": unsupported, "evidence_files": ev_files},
    )


# ---------------------------------------------------------------------------
# check: git_freshness -- file must have been modified at/after a given
# commit-ish (regression guard: catches "claimed updated but actually stale")
# ---------------------------------------------------------------------------
def check_git_freshness(item: dict, workspace: Path, ctx: "RunContext") -> CheckResult:
    path = item["path"]
    since_ref = item.get("since", "HEAD~5")
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", path],
            cwd=str(workspace), capture_output=True, text=True, timeout=20,
        )
        last_commit = proc.stdout.strip()
        if not last_commit:
            return CheckResult("git_freshness", "fail", f"'{path}' has no git history", {})
        proc2 = subprocess.run(
            ["git", "merge-base", "--is-ancestor", since_ref, last_commit],
            cwd=str(workspace), capture_output=True, text=True, timeout=20,
        )
        fresh = proc2.returncode == 0
    except Exception as e:
        return CheckResult("git_freshness", "warn", f"could not evaluate: {e}", {})
    status = "pass" if fresh else "fail"
    return CheckResult("git_freshness", status, f"last_commit={last_commit} since={since_ref} fresh={fresh}", {})


REGISTRY = {
    "exists": check_exists,
    "min_size": check_min_size,
    "contains": check_contains,
    "required_keys": check_required_keys,
    "run": check_run,
    "no_placeholder": check_no_placeholder,
    "numeric_grounding": check_numeric_grounding,
    "git_freshness": check_git_freshness,
}
