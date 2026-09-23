#!/usr/bin/env python3
"""Run pip-audit and fail only on a vulnerability with an available fix.

Milestone 6e (Phase 6 plan section 9.6): CI must fail on a known
vulnerability with a fix release, but not on one with no fix yet --
nothing actionable to do about the second kind, and failing on it
anyway would train everyone to ignore red CI. pip-audit 2.x has no
built-in flag for this distinction (its `--strict` means "fail if
dependency collection itself fails", not this), so this script runs
pip-audit with JSON output and applies the filter itself.

Usage: uv run python scripts/pip_audit_gate.py <requirements.txt>
Exit code: 0 if clean or every finding lacks a fix; 1 if any finding
has at least one fix_versions entry.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: pip_audit_gate.py <requirements.txt>", file=sys.stderr)
        return 2
    requirements_path = sys.argv[1]

    result = subprocess.run(
        ["uv", "run", "pip-audit", "-r", requirements_path, "-f", "json"],
        capture_output=True,
        text=True,
    )
    # pip-audit exits non-zero whenever *any* vulnerability is found,
    # fixable or not -- so its own exit code is not what this script
    # gates on. A genuinely broken invocation (bad path, no such tool)
    # still has no JSON on stdout, which the next line surfaces.
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        print("pip-audit did not produce JSON output", file=sys.stderr)
        return 2

    fixable = []
    unfixable = []
    for dependency in report.get("dependencies", []):
        for vuln in dependency.get("vulns", []) or []:
            entry = f"{dependency.get('name')} {dependency.get('version')}: {vuln.get('id')}"
            if vuln.get("fix_versions"):
                fixable.append(f"{entry} (fix: {', '.join(vuln['fix_versions'])})")
            else:
                unfixable.append(entry)

    if unfixable:
        print("Vulnerabilities with no fix release yet (not failing the build):")
        for line in unfixable:
            print(f"  - {line}")

    if fixable:
        print("Vulnerabilities WITH a fix release available:")
        for line in fixable:
            print(f"  - {line}")
        return 1

    print("pip-audit: no fixable vulnerabilities found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
