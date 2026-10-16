#!/usr/bin/env python3
"""Plan builds and maintain the published package feed.

Each target (OpenWrt version x package architecture) has one state file in the
state store, `<storage.state>/packages-<version>/<arch>.json`:

    packages: source package -> {fingerprint, status, updated_at}
        status is built | unsupported. A package is rebuilt when its
        fingerprint (content hash of <category>/<name> in the sources repo +
        build recipe hash) differs. Failed builds are not recorded, so they
        are retried on the next run.
    files: archive filename -> {name, version, source, published_at, latest,
                                superseded_at}
        `latest` files are exactly the ones listed in the feed index. Older
        files stay downloadable until the retention policy drops them.

Storage locations are rclone paths from [storage] in targets.toml, overridable
with the FEED_STORE / STATE_STORE environment variables.

Subcommands, in workflow order:
    storage -> plan -> target-env
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

STATE_SCHEMA = 1


class FeedError(Exception):
    pass


# --- time ---------------------------------------------------------------


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def now_arg(value: str | None) -> dt.datetime:
    return parse_time(value) if value else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# --- config & sources -----------------------------------------------------


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    for key in ("feed_name", "sdk_image", "epoch", "arches", "versions", "retention", "storage"):
        if key not in config:
            raise FeedError(f"{path}: missing {key}")
    for table, keys in (("retention", ("days", "min_versions")), ("storage", ("feed", "state"))):
        for key in keys:
            if key not in config[table]:
                raise FeedError(f"{path}: missing {table}.{key}")
    for entry in config["versions"]:
        golang = entry.get("golang")
        if golang is not None and not (golang.get("repo") and golang.get("branch")):
            raise FeedError(f"{path}: version {entry.get('version')}: golang needs repo and branch")
    return config


def storage(config: dict) -> tuple[str, str]:
    """(feed, state) rclone paths, environment overrides first."""
    feed, state = (
        (os.environ.get(f"{key.upper()}_STORE") or config["storage"][key]).rstrip("/")
        for key in ("feed", "state")
    )
    return feed, state


def version_entry(config: dict, version: str) -> dict:
    for entry in config["versions"]:
        if entry["version"] == version:
            return entry
    raise FeedError(f"version {version} is not configured")


def recipe_hash(config: dict, version: str) -> str:
    """Hash of every builder setting that changes build output for a version."""
    recipe = {
        "epoch": config["epoch"],
        "feed_name": config["feed_name"],
        "sdk_image": config["sdk_image"],
        "config": config.get("config", []),
        "version": version_entry(config, version),
    }
    encoded = json.dumps(recipe, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def git(sources: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(sources), *args], check=True, capture_output=True, text=True
    ).stdout


def ignored_for_build(relative: str) -> bool:
    """Files in a package directory that never affect build output.
    Only the package's top level is filtered; deeper files may be installed."""
    parts = relative.split("/")
    top = parts[0]
    return (
        ".github" in parts[:-1]
        or (len(parts) == 1 and (top.startswith(("README", "LICENSE")) or top == ".gitignore"))
        or (len(parts) > 1 and top == "LICENSES")
    )


@dataclass(frozen=True)
class SourcePackage:
    path: str  # <category>/<name>
    digest: str


def source_packages(sources: Path, exclude: list[str]) -> dict[str, SourcePackage]:
    """Packages at HEAD: every <category>/<name>/ with a Makefile directly inside."""
    entries: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in git(sources, "ls-tree", "-r", "-z", "HEAD").split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        parts = path.split("/", 2)
        if len(parts) < 3 or parts[0].startswith("."):
            continue
        category, name, relative = parts
        if not ignored_for_build(relative):
            entries[(category, name)].append(f"{meta}\t{relative}")

    packages: dict[str, SourcePackage] = {}
    for (category, name), lines in sorted(entries.items()):
        if not any(line.endswith("\tMakefile") for line in lines):
            continue
        if name in packages:
            raise FeedError(f"package {name} exists in both {packages[name].path} and {category}/{name}")
        digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:20]
        packages[name] = SourcePackage(f"{category}/{name}", digest)
    for name in exclude:
        packages.pop(name, None)
    return packages


def fingerprints(config: dict, packages: dict[str, SourcePackage], version: str) -> dict[str, str]:
    recipe = recipe_hash(config, version)
    return {name: f"{package.digest}-{recipe}" for name, package in packages.items()}


def state_path(state_dir: Path, version: str, arch: str) -> Path:
    return state_dir / f"packages-{version}" / f"{arch}.json"


def load_state(path: Path | None, version: str, arch: str) -> dict:
    if path is None or not path.exists():
        return {"schema": STATE_SCHEMA, "version": version, "arch": arch, "packages": {}, "files": {}}
    state = load_json(path)
    if state.get("schema") != STATE_SCHEMA:
        raise FeedError(f"{path}: unsupported state schema {state.get('schema')}")
    if (state.get("version"), state.get("arch")) != (version, arch):
        raise FeedError(f"{path}: state belongs to {state.get('version')}/{state.get('arch')}")
    return state


# --- core logic -------------------------------------------------------------


def retention_keep(files: dict, now: dt.datetime, days: int, min_versions: int) -> set[str]:
    """Files to keep: indexed files, files that were current within `days`,
    and the `min_versions` newest files of every package still being published."""
    cutoff = now - dt.timedelta(days=days)
    by_name: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for filename, meta in files.items():
        by_name[meta["name"]].append((filename, meta))

    keep = set()
    for entries in by_name.values():
        entries.sort(key=lambda item: item[1]["published_at"], reverse=True)
        active = any(meta["latest"] for _, meta in entries)
        for position, (filename, meta) in enumerate(entries):
            last_current = meta.get("superseded_at") or meta["published_at"]
            if (
                meta["latest"]
                or (active and position < min_versions)
                or parse_time(last_current) >= cutoff
            ):
                keep.add(filename)
    return keep


def needs_build(entry: dict | None, fingerprint: str) -> bool:
    return entry is None or entry["fingerprint"] != fingerprint


def plan_target(
    state: dict,
    fps: dict[str, str],
    forced: set[str],
    full: bool,
    now: dt.datetime,
    retention: dict,
) -> dict:
    packages = state["packages"]
    build = sorted(
        name
        for name, fingerprint in fps.items()
        if full or name in forced or needs_build(packages.get(name), fingerprint)
    )
    remove = sorted(set(packages) - set(fps))
    stale_latest = sorted(
        name for name, meta in state["files"].items() if meta["latest"] and meta["source"] not in fps
    )
    expired = sorted(
        set(state["files"]) - retention_keep(state["files"], now, retention["days"], retention["min_versions"])
    )
    return {"build": build, "remove": remove, "stale_latest": stale_latest, "expired": expired}


# --- commands ----------------------------------------------------------------


def parse_names(value: str) -> set[str]:
    return {item for item in value.replace(",", " ").split() if item}


def github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value:
                handle.write(f"{key}<<__FEEDCTL_EOF__\n{value}\n__FEEDCTL_EOF__\n")
            else:
                handle.write(f"{key}={value}\n")


def append_summary(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def cmd_plan(args) -> int:
    config = load_config(args.config)
    now = now_arg(args.now)
    packages = source_packages(args.sources, config.get("exclude", []))
    forced = parse_names(args.packages)
    unknown = forced - set(packages)
    if unknown:
        raise FeedError(f"requested packages are not buildable sources: {sorted(unknown)}")

    commit = git(args.sources, "rev-parse", "HEAD").strip()
    include = []
    lines = ["### Build plan", "", f"Sources: `{commit}`", ""]
    lines += ["| Target | Build | Remove | Expire |", "|---|---|---|---|"]
    for entry in config["versions"]:
        version = entry["version"]
        fps = fingerprints(config, packages, version)
        for arch in config["arches"]:
            state = load_state(state_path(args.state_dir, version, arch), version, arch)
            plan = plan_target(state, fps, forced, args.full, now, config["retention"])
            busy = plan["build"] or plan["remove"] or plan["stale_latest"] or plan["expired"]
            lines.append(
                f"| {version} {arch} | {' '.join(plan['build']) or '-'} | "
                f"{' '.join(plan['remove']) or '-'} | {len(plan['expired'])} |"
            )
            if busy:
                include.append({"version": version, "arch": arch, "packages": " ".join(plan["build"])})

    print(json.dumps({"include": include}, indent=2))
    github_output(
        args.github_output,
        {
            "matrix": json.dumps({"include": include}, separators=(",", ":")),
            "has_work": "true" if include else "false",
            "sources_commit": commit,
        },
    )
    append_summary(args.summary, "\n".join(lines) + "\n")
    return 0


def cmd_storage(args) -> int:
    feed, state = storage(load_config(args.config))
    values = {"FEED_STORE": feed, "STATE_STORE": state}
    github_output(args.github_env, values)
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


def cmd_target_env(args) -> int:
    config = load_config(args.config)
    entry = version_entry(config, args.version)
    if args.arch not in config["arches"]:
        raise FeedError(f"arch {args.arch} is not configured")
    golang = entry.get("golang") or {}
    feed, state = storage(config)
    target = f"packages-{args.version}"
    values = {
        "WORK": str(Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve() / ".work"),
        "REMOTE_FEED": f"{feed}/{target}/{args.arch}/{config['feed_name']}",
        "REMOTE_STATE": f"{state}/{target}/{args.arch}.json",
        "SDK_IMAGE": config["sdk_image"],
        "FEED_NAME": config["feed_name"],
        "REPLACE_GOLANG": "1" if golang else "0",
        "GOLANG_REPO": golang.get("repo", ""),
        "GOLANG_BRANCH": golang.get("branch", ""),
        "EXTRA_CONFIG": "\n".join(config.get("config", [])),
    }
    github_output(args.github_env, values)
    for key, value in values.items():
        print(f"{key}={value!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    store = sub.add_parser("storage", help="export FEED_STORE and STATE_STORE")
    store.add_argument("--config", type=Path, required=True)
    store.add_argument("--github-env", default=os.environ.get("GITHUB_ENV"))
    store.set_defaults(func=cmd_storage)

    plan = sub.add_parser("plan", help="decide which targets and packages need work")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--sources", type=Path, required=True)
    plan.add_argument("--state-dir", type=Path, required=True)
    plan.add_argument("--packages", default="", help="force-rebuild these packages (space/comma separated)")
    plan.add_argument("--full", action="store_true", help="rebuild every package")
    plan.add_argument("--now")
    plan.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    plan.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    plan.set_defaults(func=cmd_plan)

    env = sub.add_parser("target-env", help="export per-target build settings and paths")
    env.add_argument("--config", type=Path, required=True)
    env.add_argument("--version", required=True)
    env.add_argument("--arch", required=True)
    env.add_argument("--github-env", default=os.environ.get("GITHUB_ENV"))
    env.set_defaults(func=cmd_target_env)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except FeedError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
