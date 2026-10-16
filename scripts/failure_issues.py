#!/usr/bin/env python3
"""Open, update and close `build-failure` issues after a build run.

- One open issue per package, titled "Build failure: <name>". A new failure of
  a package that already has an open issue is added as a comment.
- An open issue is closed once no target state in the state store marks the package as
  `failed` (it built, became unsupported, or was removed from the sources).

Inputs are the `failures.json` files written by `feedctl.py merge` (one per
target, collected from artifacts) and the published target states. Requires
the gh CLI with GH_TOKEN and GH_REPO set.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

LABEL = "build-failure"
TITLE_PREFIX = "Build failure: "
BODY_LIMIT = 60000


def gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["gh", *args], input=stdin, check=True, capture_output=True, text=True).stdout


def load_failures(directory: Path) -> dict[str, list[dict]]:
    by_package: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(directory.rglob("failures.json")):
        for record in json.loads(path.read_text(encoding="utf-8")):
            by_package[record["package"]].append(record)
    for records in by_package.values():
        records.sort(key=lambda record: (record["version"], record["arch"]))
    return by_package


def failing_in_states(state_dir: Path) -> tuple[set[str], int]:
    names, count = set(), 0
    for path in state_dir.rglob("*.json"):
        count += 1
        state = json.loads(path.read_text(encoding="utf-8"))
        names.update(name for name, meta in state.get("packages", {}).items() if meta.get("status") == "failed")
    return names, count


def render(package: str, records: list[dict], run_url: str, sources_repo: str, tail_chars: int) -> str:
    first = records[0]
    commit = first["sources_commit"]
    lines = [
        f"`{package}` failed to build in {run_url}.",
        "",
        f"- Source: [`{first['path']}`](https://github.com/{sources_repo}/tree/{commit}/{first['path']}) "
        f"at `{commit[:12]}`",
        "- The feed keeps serving the previous build. The package is not retried until its sources or "
        "`targets.toml` change, or a manual run lists it in `packages`.",
        "",
        "| Target | Stage | Fingerprint |",
        "|---|---|---|",
    ]
    lines += [f"| {r['version']} {r['arch']} | {r['kind']} | `{r['fingerprint']}` |" for r in records]
    for record in records:
        lines += [
            "",
            f"<details><summary>{record['version']} {record['arch']}: log tail</summary>",
            "",
            "````text",
            record["log_tail"][-tail_chars:],
            "````",
            "",
            "</details>",
        ]
    lines += ["", "Full logs are in the run's `logs-<version>-<arch>` artifacts."]
    return "\n".join(lines) + "\n"


def fitted_body(package: str, records: list[dict], run_url: str, sources_repo: str) -> str:
    tail_chars = 15000
    body = render(package, records, run_url, sources_repo, tail_chars)
    while len(body) > BODY_LIMIT and tail_chars > 500:
        tail_chars //= 2
        body = render(package, records, run_url, sources_repo, tail_chars)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--failures-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--sources-repo", required=True)
    args = parser.parse_args()

    gh("label", "create", LABEL, "--color", "d73a4a", "--description", "Package failed in the feed builder", "--force")
    issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number,title", "--limit", "500"))
    open_issues = {
        issue["title"][len(TITLE_PREFIX):]: issue["number"]
        for issue in issues
        if issue["title"].startswith(TITLE_PREFIX)
    }

    failures = load_failures(args.failures_dir)
    for package, records in sorted(failures.items()):
        body = fitted_body(package, records, args.run_url, args.sources_repo)
        if package in open_issues:
            gh("issue", "comment", str(open_issues[package]), "--body-file", "-", stdin=body)
            print(f"Commented on #{open_issues[package]} ({package})")
        else:
            url = gh("issue", "create", "--title", TITLE_PREFIX + package, "--label", LABEL, "--body-file", "-", stdin=body)
            print(f"Opened {url.strip()} ({package})")

    failing, state_count = failing_in_states(args.state_dir)
    if state_count == 0:
        print("::warning::No target states found; not closing any issues")
        return 0
    for package, number in sorted(open_issues.items()):
        if package in failing or package in failures:
            continue
        gh(
            "issue",
            "close",
            str(number),
            "--comment",
            f"No target reports a build failure for `{package}` any more ({args.run_url}).",
        )
        print(f"Closed #{number} ({package})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
