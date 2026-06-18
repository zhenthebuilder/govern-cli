# govern-cli

**Independently verify that an agent's claimed deliverables are real.**

Long-horizon agent tasks (multi-hour coding/research/build runs) routinely end
with the agent declaring "done" — but the deliverables are missing, stubbed,
broken, or have silently regressed since the last check. `govern-cli` is a
small, dependency-light governance layer that re-checks claims against the
actual filesystem/git state/executable behavior of the workspace, instead of
trusting the agent's self-report.

It is meant to be dropped into:
- a pre-merge / pre-"done" CI gate for agent harnesses,
- a nightly drift check across long agent sessions,
- a human reviewer's first-pass triage tool.

## Install (under 5 minutes, one command)

```bash
pip install "git+https://github.com/govern-cli/govern-cli.git#subdirectory=govern"
```

No network/PyPI access? Use the single-file fallback (zero dependencies
beyond a stdlib-only YAML-lite reader for simple specs, or pass `--json`
specs which need no YAML at all):

```bash
curl -fsSL https://raw.githubusercontent.com/govern-cli/govern-cli/main/govern/govern_single_file.py -o govern.py
python3 govern.py check spec.json /path/to/workspace
```

Or just run it from this checkout directly:

```bash
cd govern && pip install -e . && govern init && govern check govern.spec.yaml /path/to/workspace
```

## Quick start

```bash
govern init                       # writes a starter govern.spec.yaml
govern check govern.spec.yaml .   # checks the spec against the current dir
```

Example spec:

```yaml
deliverables:
  - id: paper
    description: "NeurIPS-style paper with grounded results"
    required: true
    checks:
      - type: exists
        path: "paper/*.pdf"
      - type: no_placeholder
        path: "paper/*.tex"
      - type: numeric_grounding
        path: "paper/results.tex"
        evidence: ["benchmark/runs/*.json"]
```

## Check types

| type | what it catches |
|---|---|
| `exists` | claimed file/dir missing or empty |
| `min_size` | stub / near-empty placeholder files |
| `contains` | required string/regex absent (e.g. install command on landing page) |
| `required_keys` | malformed/incomplete structured deliverable (JSON/YAML) |
| `run` | claimed-working command/build/test that doesn't actually pass |
| `no_placeholder` | TODO/TBD/"coming soon"/`NotImplementedError` left in "finished" docs |
| `numeric_grounding` | numbers in a results doc that don't trace to any evidence/log file (fabrication smell) |
| `git_freshness` | file claimed "updated" but git history shows it's stale |

## Exit codes

`0` = overall pass, `1` = at least one **required** deliverable failed (or
`--strict` and something warned). Designed to be used directly as a CI gate:

```bash
govern check govern.spec.yaml . || exit 1
```

## Why not just ask the agent if it's done?

Because that's exactly the failure mode this tool exists to catch. See the
paper in `../paper/` for a benchmark quantifying how often self-report
diverges from ground truth, and how much of that gap independent checks like
these close.

## License

MIT.
