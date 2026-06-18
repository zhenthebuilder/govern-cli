from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import run_governance, diff_reports, GovernanceReport

RESET = "\033[0m"
COLORS = {"pass": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"}


def _colorize(status: str) -> str:
    if not sys.stdout.isatty():
        return status.upper()
    return f"{COLORS.get(status, '')}{status.upper()}{RESET}"


def cmd_check(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    workspace = Path(args.workspace)
    report = run_governance(spec_path, workspace)
    data = report.to_dict()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"govern check :: spec={spec_path} workspace={workspace}")
        print("-" * 72)
        for d in data["deliverables"]:
            req = "required" if d["required"] else "optional"
            print(f"[{_colorize(d['status'])}] {d['id']} ({req}) - {d['description']}")
            for c in d["checks"]:
                print(f"    - {c['name']}: {_colorize(c['status'])} :: {c['detail'][:140]}")
        print("-" * 72)
        s = data["summary"]
        print(f"Summary: {s['pass']} pass, {s['warn']} warn, {s['fail']} fail "
              f"(of {s['total']}); required_fail={s['required_fail']}")
        print(f"Overall: {_colorize(data['overall_status'])}")

    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2))

    if data["overall_status"] == "fail":
        return 1
    if data["overall_status"] == "warn" and args.strict:
        return 1
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old = json.loads(Path(args.old).read_text())
    new = json.loads(Path(args.new).read_text())
    d = diff_reports(old, new)
    print(json.dumps(d, indent=2))
    return 1 if d["regressions"] else 0


def cmd_init(args: argparse.Namespace) -> int:
    template = """\
# govern spec -- describe the deliverables an agent must produce.
# Run: govern check spec.yaml /path/to/workspace
deliverables:
  - id: example-doc
    description: "Example: a README must exist and be non-trivial"
    required: true
    checks:
      - type: exists
        path: "README.md"
      - type: min_size
        path: "README.md"
        min_bytes: 200
      - type: no_placeholder
        path: "README.md"
"""
    out = Path(args.out)
    out.write_text(template)
    print(f"wrote template spec to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="govern", description="Governance gate for long-horizon agent deliverables.")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("check", help="Run a deliverable spec against a workspace.")
    pc.add_argument("spec", help="Path to spec YAML/JSON.")
    pc.add_argument("workspace", help="Path to the workspace/repo to check.")
    pc.add_argument("--json", action="store_true", help="Print JSON report to stdout.")
    pc.add_argument("--out", help="Write JSON report to this path.")
    pc.add_argument("--strict", action="store_true", help="Treat warn as failure for exit code.")
    pc.set_defaults(func=cmd_check)

    pd = sub.add_parser("diff", help="Diff two JSON governance reports for regressions.")
    pd.add_argument("old")
    pd.add_argument("new")
    pd.set_defaults(func=cmd_diff)

    pi = sub.add_parser("init", help="Write a starter spec template.")
    pi.add_argument("--out", default="govern.spec.yaml")
    pi.set_defaults(func=cmd_init)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
