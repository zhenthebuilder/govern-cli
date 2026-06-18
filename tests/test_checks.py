"""
Unit tests for govern-cli on synthetic fixtures: a "good" workspace and a
"sabotaged" workspace (missing files, stub files, placeholder text,
fabricated numbers). Run with: python3 -m pytest tests/ -q
or: python3 tests/test_checks.py (falls back to a tiny runner w/o pytest)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from govern_cli.engine import run_governance, diff_reports

SPEC = {
    "deliverables": [
        {
            "id": "readme", "required": True,
            "checks": [
                {"type": "exists", "path": "README.md"},
                {"type": "min_size", "path": "README.md", "min_bytes": 50},
                {"type": "no_placeholder", "path": "README.md"},
            ],
        },
        {
            "id": "config", "required": True,
            "checks": [
                {"type": "required_keys", "path": "config.json", "keys": ["name", "version"]},
            ],
        },
        {
            "id": "build", "required": True,
            "checks": [
                {"type": "run", "cmd": "python3 -c \"print('built ok')\"", "expect_exit": 0, "stdout_contains": "built ok"},
            ],
        },
        {
            "id": "results", "required": True,
            "checks": [
                {"type": "numeric_grounding", "path": "RESULTS.md", "evidence": ["evidence.log"]},
            ],
        },
    ]
}


def make_good_workspace(tmp: Path):
    (tmp / "README.md").write_text("A real README. " * 10)
    (tmp / "config.json").write_text(json.dumps({"name": "x", "version": "1.0"}))
    (tmp / "evidence.log").write_text("accuracy=87.5 n=300 latency_ms=42")
    (tmp / "RESULTS.md").write_text("We measured accuracy of 87.5 on n=300 examples (latency 42 ms).")


def make_bad_workspace(tmp: Path):
    (tmp / "README.md").write_text("TODO: write this")  # placeholder + too short
    (tmp / "config.json").write_text(json.dumps({"name": "x"}))  # missing version
    (tmp / "evidence.log").write_text("accuracy=10.0 n=5")
    (tmp / "RESULTS.md").write_text("We achieved 99.9 percent accuracy on 100000 examples.")  # fabricated


def test_good_workspace_passes():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        make_good_workspace(tmp)
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(SPEC))
        report = run_governance(spec_path, tmp)
        data = report.to_dict()
        assert data["overall_status"] == "pass", data
        assert data["summary"]["required_fail"] == 0


def test_bad_workspace_fails():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        make_bad_workspace(tmp)
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(SPEC))
        report = run_governance(spec_path, tmp)
        data = report.to_dict()
        assert data["overall_status"] == "fail"
        by_id = {d_["id"]: d_ for d_ in data["deliverables"]}
        assert by_id["readme"]["status"] == "fail"
        assert by_id["config"]["status"] == "fail"
        # numeric_grounding only warns (it's a heuristic), not hard fail
        assert by_id["results"]["status"] in ("warn", "fail")


def test_missing_file_is_fail_not_crash():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(SPEC))
        report = run_governance(spec_path, tmp)
        data = report.to_dict()
        assert data["overall_status"] == "fail"


def test_diff_detects_regression():
    old = {"deliverables": [{"id": "a", "status": "pass"}]}
    new = {"deliverables": [{"id": "a", "status": "fail"}]}
    d = diff_reports(old, new)
    assert d["regressions"] == [{"id": "a", "from": "pass", "to": "fail"}]


def test_diff_detects_improvement():
    old = {"deliverables": [{"id": "a", "status": "fail"}]}
    new = {"deliverables": [{"id": "a", "status": "pass"}]}
    d = diff_reports(old, new)
    assert d["improvements"] == [{"id": "a", "from": "fail", "to": "pass"}]


def test_run_check_timeout_is_fail():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = {"deliverables": [{"id": "slow", "required": True, "checks": [
            {"type": "run", "cmd": "sleep 5", "timeout_sec": 1},
        ]}]}
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec))
        report = run_governance(spec_path, tmp)
        assert report.to_dict()["overall_status"] == "fail"


def test_git_freshness_detects_stale_file():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        (tmp / "a.txt").write_text("v1")
        subprocess.run(["git", "add", "."], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=tmp, check=True)
        (tmp / "b.txt").write_text("v1")
        subprocess.run(["git", "add", "."], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=tmp, check=True)
        # b.txt was added in the most recent commit -> fresh as-of HEAD~1
        spec = {"deliverables": [{"id": "fresh", "required": True, "checks": [
            {"type": "git_freshness", "path": "b.txt", "since": "HEAD~1"},
        ]}]}
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec))
        report = run_governance(spec_path, tmp)
        assert report.to_dict()["overall_status"] == "pass"

        # a.txt was NOT touched since HEAD~1 (only created in the first
        # commit) -> should fail the "fresh since HEAD~1" requirement when
        # we require it to be at least as new as HEAD (i.e. touched in the
        # latest commit specifically)
        spec2 = {"deliverables": [{"id": "stale", "required": True, "checks": [
            {"type": "git_freshness", "path": "a.txt", "since": "HEAD"},
        ]}]}
        spec_path2 = tmp / "spec2.json"
        spec_path2.write_text(json.dumps(spec2))
        report2 = run_governance(spec_path2, tmp)
        assert report2.to_dict()["overall_status"] == "fail"


def _run_all():
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
