#!/usr/bin/env python3
"""Plan builds and maintain the published package feed.

Each target (OpenWrt version x package architecture) has one state file in the
state store, `<storage.state>/packages-<version>/<arch>.json`:

    packages: source package -> {fingerprint, status, updated_at[, attempts]}
        status is built | unsupported | failed | download_failed.
        A package is rebuilt when its fingerprint (content hash of
        <category>/<name> in the sources repo + build recipe hash) differs, or
        when its source download failed fewer than DOWNLOAD_ATTEMPTS runs in a
        row. `failed` packages are not retried until the fingerprint changes
        or a manual run names them; failure_issues.py reports them.
    files: archive filename -> {name, version, source, published_at, latest,
                                superseded_at, size, sha256, meta}
        `latest` files are exactly the ones listed in the feed index. Older
        files stay downloadable until the retention policy drops them. `meta`
        holds the control fields normalized for the catalog (opkg_meta / apk_meta).

Storage locations are rclone paths from [storage] in targets.toml, overridable
with the FEED_STORE / STATE_STORE / CATALOG_STORE environment variables.
Publishing writes the archives, then the index (feed store), then the state
(state store): the state is the commit point, so an interrupted publish only
causes a rebuild. The catalog (docs/catalog.md) is regenerated from all states
at the end of every run.

Subcommands are called in this order by .github/workflows/build.yml:
    storage -> plan -> target-env -> merge -> carry -> verify -> gc -> catalog
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import email.parser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

STATE_SCHEMA = 1
CATALOG_SCHEMA = 1
SUMMARY_KEYS = ("description", "license", "url", "maintainer", "section")
INDEX_FILES = {"apk": "packages.adb", "ipk": "Packages"}
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
ARCHIVE_SUFFIXES = (".ipk", ".apk")
DOWNLOAD_ATTEMPTS = 3
LOG_TAIL_LINES = 150
LOG_TAIL_CHARS = 15000
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class FeedError(Exception):
    pass


# --- time ---------------------------------------------------------------


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def now_arg(value: str | None) -> dt.datetime:
    return parse_time(value) if value else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# --- config & sources -----------------------------------------------------


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    for key in ("feed_name", "sdk_image", "epoch", "arches", "versions", "retention", "storage"):
        if key not in config:
            raise FeedError(f"{path}: missing {key}")
    for table, keys in (("retention", ("days", "min_versions")), ("storage", ("feed", "state", "catalog"))):
        for key in keys:
            if key not in config[table]:
                raise FeedError(f"{path}: missing {table}.{key}")
    for entry in config["versions"]:
        golang = entry.get("golang")
        if golang is not None and not (golang.get("repo") and golang.get("branch")):
            raise FeedError(f"{path}: version {entry.get('version')}: golang needs repo and branch")
    return config


def _contains(outer: str, inner: str) -> bool:
    """True if rclone path `inner` is `outer` or lies below it."""
    if outer.endswith(":"):
        return inner.startswith(outer)
    return inner == outer or inner.startswith(outer + "/")


def storage(config: dict) -> tuple[str, str, str]:
    """(feed, state, catalog) rclone paths, environment overrides first.

    The catalog is published with `rclone sync`, and the whole state store is
    downloaded by every run, so the catalog may neither contain the feed or
    state store nor lie inside the state store."""
    feed, state, catalog = (
        (os.environ.get(f"{key.upper()}_STORE") or config["storage"][key]).rstrip("/")
        for key in ("feed", "state", "catalog")
    )
    if _contains(catalog, feed) or _contains(catalog, state) or _contains(state, catalog):
        raise FeedError(
            f"storage.catalog ({catalog}) must not contain the feed ({feed}) or state ({state}) store, "
            "nor lie inside the state store"
        )
    return feed, state, catalog


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
    return entry is None or entry["fingerprint"] != fingerprint or entry["status"] == "download_failed"


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
    held = sorted(
        name
        for name, fingerprint in fps.items()
        if name not in build and packages.get(name, {}).get("status") == "failed"
    )
    remove = sorted(set(packages) - set(fps))
    stale_latest = sorted(
        name for name, meta in state["files"].items() if meta["latest"] and meta["source"] not in fps
    )
    expired = sorted(
        set(state["files"]) - retention_keep(state["files"], now, retention["days"], retention["min_versions"])
    )
    return {"build": build, "held": held, "remove": remove, "stale_latest": stale_latest, "expired": expired}


def source_name(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def split_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def unfold(value: str | None) -> str | None:
    """Join a folded control field (continuation lines start with a space)."""
    if not value:
        return None
    return "\n".join(line.strip() for line in value.splitlines()).strip() or None


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def opkg_meta(fields) -> dict:
    return {
        "arch": fields.get("Architecture"),
        "description": unfold(fields.get("Description")),
        "depends": split_list(fields.get("Depends")),
        "provides": split_list(fields.get("Provides")),
        "license": fields.get("License"),
        "section": fields.get("Section"),
        "maintainer": fields.get("Maintainer"),
        "url": fields.get("URL"),
        "installed_size": to_int(fields.get("Installed-Size")),
        "abi_version": fields.get("ABIVersion"),
    }


def apk_meta(package: dict) -> dict:
    abi = next(
        (tag.split("=", 1)[1] for tag in package.get("tags", []) if tag.startswith("openwrt:abiversion=")),
        None,
    )
    return {
        "arch": package.get("arch"),
        "description": unfold(package.get("description")),
        "depends": [str(item) for item in package.get("depends", [])],
        "provides": [str(item) for item in package.get("provides", [])],
        "license": package.get("license"),
        "section": None,
        "maintainer": package.get("maintainer"),
        "url": package.get("url"),
        "installed_size": to_int(package.get("installed-size")),
        "abi_version": abi,
    }


def read_index_metadata(directory: Path) -> list[dict]:
    """Archive metadata from an index dump written by sdk/entry.sh."""
    manifest = directory / "Packages.manifest"
    adb_dump = directory / "packages.adb.json"
    entries = []
    if manifest.exists():
        parser = email.parser.Parser()
        for chunk in manifest.read_text(encoding="utf-8").strip().split("\n\n"):
            if not chunk.strip():
                continue
            fields = parser.parsestr(chunk, headersonly=True)
            for key in ("Package", "Version", "Source", "Filename"):
                if not fields.get(key):
                    raise FeedError(f"{manifest}: entry without {key}: {chunk.splitlines()[0]}")
            entries.append(
                {
                    "filename": fields["Filename"],
                    "name": fields["Package"],
                    "version": fields["Version"],
                    "source": source_name(fields["Source"]),
                    "meta": opkg_meta(fields),
                }
            )
    elif adb_dump.exists():
        data = load_json(adb_dump)
        packages = data.get("packages", []) if isinstance(data, dict) else data
        for package in packages:
            for key in ("name", "version", "origin"):
                if not package.get(key):
                    raise FeedError(f"{adb_dump}: entry without {key}: {package.get('name')}")
            entries.append(
                {
                    "filename": f"{package['name']}-{package['version']}.apk",
                    "name": package["name"],
                    "version": package["version"],
                    "source": source_name(package["origin"]),
                    "meta": apk_meta(package),
                }
            )
    return entries


def merge_state(
    state: dict,
    fps: dict[str, str],
    result: dict,
    new_files: list[dict],
    now: dt.datetime,
    retention: dict,
) -> tuple[dict, list[str], dict]:
    """Apply one build result. Returns (new state, accepted filenames, report)."""
    state = copy.deepcopy(state)
    packages, files = state["packages"], state["files"]
    stamp = format_time(now)
    succeeded = set(result.get("succeeded", []))
    failed = set(result.get("failed", []))
    download_failed = set(result.get("download_failed", []))
    unsupported = set(result.get("unsupported", []))

    unknown = (succeeded | failed | download_failed | unsupported) - set(fps)
    if unknown:
        raise FeedError(f"build result names packages missing from sources: {sorted(unknown)}")

    removed = sorted(set(packages) - set(fps))
    for name in removed:
        del packages[name]
    for name in sorted(succeeded):
        packages[name] = {"fingerprint": fps[name], "status": "built", "updated_at": stamp}
    for name in sorted(unsupported):
        packages[name] = {"fingerprint": fps[name], "status": "unsupported", "updated_at": stamp}
    for name in sorted(failed):
        packages[name] = {"fingerprint": fps[name], "status": "failed", "updated_at": stamp}

    exhausted, retrying = [], []
    for name in sorted(download_failed):
        previous = packages.get(name, {})
        attempts = 1
        if previous.get("status") == "download_failed" and previous.get("fingerprint") == fps[name]:
            attempts = previous.get("attempts", 0) + 1
        if attempts >= DOWNLOAD_ATTEMPTS:
            packages[name] = {"fingerprint": fps[name], "status": "failed", "updated_at": stamp}
            exhausted.append(name)
        else:
            packages[name] = {
                "fingerprint": fps[name],
                "status": "download_failed",
                "attempts": attempts,
                "updated_at": stamp,
            }
            retrying.append(name)

    replaced_sources = succeeded | unsupported
    retired = []
    for filename, meta in files.items():
        if meta["latest"] and (meta["source"] in replaced_sources or meta["source"] not in fps):
            meta["latest"] = False
            meta["superseded_at"] = stamp
            retired.append(filename)

    accepted = []
    produced = set()
    for entry in new_files:
        if entry["source"] not in succeeded:
            continue
        record = {
            "name": entry["name"],
            "version": entry["version"],
            "source": entry["source"],
            "published_at": stamp,
            "latest": True,
        }
        for key in ("size", "sha256", "meta"):
            if key in entry:
                record[key] = entry[key]
        files[entry["filename"]] = record
        accepted.append(entry["filename"])
        produced.add(entry["source"])

    names = defaultdict(list)
    for filename, meta in files.items():
        if meta["latest"]:
            names[meta["name"]].append(filename)
    duplicates = {name: sorted(found) for name, found in names.items() if len(found) > 1}
    if duplicates:
        raise FeedError(f"more than one indexed file per package: {duplicates}")

    keep = retention_keep(files, now, retention["days"], retention["min_versions"])
    deleted = sorted(set(files) - keep)
    for filename in deleted:
        del files[filename]

    state["updated_at"] = stamp
    report = {
        "succeeded": sorted(succeeded),
        "failed": sorted(failed),
        "download_exhausted": exhausted,
        "download_retry": retrying,
        "unsupported": sorted(unsupported),
        "removed": removed,
        "empty": sorted(succeeded - produced),
        "accepted": sorted(accepted),
        "retired": sorted(name for name in retired if name not in accepted),
        "expired": deleted,
    }
    return state, sorted(accepted), report


def apply_remote_listing(state: dict, remote: set[str]) -> tuple[dict, list[str], list[str]]:
    """Drop indexed files missing remotely and force their sources to rebuild.
    Returns (state, carry filenames, missing filenames)."""
    state = copy.deepcopy(state)
    missing = sorted(
        name for name, meta in state["files"].items() if meta["latest"] and name not in remote
    )
    for name in missing:
        source = state["files"].pop(name)["source"]
        state["packages"].pop(source, None)
    carry = sorted(name for name, meta in state["files"].items() if meta["latest"])
    return state, carry, missing


def gc_candidates(state: dict, index_files: set[str], remote: set[str]) -> list[str]:
    return sorted(remote - set(state["files"]) - index_files)


def log_tail(path: Path) -> str:
    if not path.is_file():
        return "(no log captured)"
    text = ANSI_ESCAPE.sub("", path.read_text(encoding="utf-8", errors="replace"))
    tail = "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
    return tail[-LOG_TAIL_CHARS:]


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
    lines += ["| Target | Build | Held (failed) | Remove | Expire |", "|---|---|---|---|---|"]
    for entry in config["versions"]:
        version = entry["version"]
        fps = fingerprints(config, packages, version)
        for arch in config["arches"]:
            state = load_state(state_path(args.state_dir, version, arch), version, arch)
            plan = plan_target(state, fps, forced, args.full, now, config["retention"])
            busy = plan["build"] or plan["remove"] or plan["stale_latest"] or plan["expired"]
            lines.append(
                f"| {version} {arch} | {' '.join(plan['build']) or '-'} | {' '.join(plan['held']) or '-'} | "
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
    feed, state, catalog = storage(load_config(args.config))
    values = {"FEED_STORE": feed, "STATE_STORE": state, "CATALOG_STORE": catalog}
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
    feed, state, _catalog = storage(config)
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


def cmd_merge(args) -> int:
    config = load_config(args.config)
    now = now_arg(args.now)
    packages = source_packages(args.sources, config.get("exclude", []))
    fps = fingerprints(config, packages, args.version)
    state = load_state(args.state, args.version, args.arch)

    result_path = args.build_dir / "result.json"
    result = load_json(result_path) if result_path.exists() else {}
    new_files = read_index_metadata(args.build_dir)
    for entry in new_files:
        archive = args.build_dir / "packages" / entry["filename"]
        if not archive.is_file():
            raise FeedError(f"indexed archive missing from build output: {entry['filename']}")
        entry["size"] = archive.stat().st_size
        with archive.open("rb") as handle:
            entry["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()

    state, accepted, report = merge_state(state, fps, result, new_files, now, config["retention"])

    stage = args.out_dir / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    for filename in accepted:
        shutil.copy2(args.build_dir / "packages" / filename, stage / filename)
    write_json(args.out_dir / "state.json", state)
    (args.out_dir / "accepted.txt").write_text("".join(f"{name}\n" for name in accepted), encoding="utf-8")
    write_json(args.out_dir / "report.json", report)

    commit = git(args.sources, "rev-parse", "HEAD").strip()
    failures = []
    for kind, names, suffix in (
        ("build", report["failed"], ".log"),
        ("download", report["download_exhausted"], ".download.log"),
    ):
        for name in names:
            failures.append(
                {
                    "package": name,
                    "path": packages[name].path,
                    "version": args.version,
                    "arch": args.arch,
                    "kind": kind,
                    "sources_commit": commit,
                    "fingerprint": fps[name],
                    "log_tail": log_tail(args.build_dir / "failures" / f"{name}{suffix}"),
                }
            )
    write_json(args.out_dir / "failures.json", failures)

    lines = [f"### {args.version} {args.arch}", ""]
    for key in ("succeeded", "failed", "download_exhausted", "download_retry", "unsupported", "removed", "empty"):
        if report[key]:
            lines.append(f"- {key}: `{' '.join(report[key])}`")
    lines.append(
        f"- archives: {len(report['accepted'])} new, {len(report['retired'])} retired, "
        f"{len(report['expired'])} expired"
    )
    append_summary(args.summary, "\n".join(lines) + "\n\n")
    for name in report["download_retry"]:
        print(f"::warning::{name}: source download failed; it is retried on the next run")
    for name in report["empty"]:
        print(f"::warning::{name} built successfully but produced no {config['feed_name']} archives")
    for failure in failures:
        cause = "build failed" if failure["kind"] == "build" else f"source download failed {DOWNLOAD_ATTEMPTS} runs in a row"
        print(f"::warning::{failure['package']}: {cause}; the feed keeps its previous build (see the build-failure issue)")
    print(json.dumps(report, indent=2))
    return 0


def load_upstreams(path: Path) -> dict[str, dict]:
    """Package name -> upstream {repo, branch, path} from sources.toml."""
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {
        package["name"]: {"repo": package["repo"], "branch": package.get("branch"), "path": package.get("path", ".")}
        for package in data.get("package", [])
    }


def archive_format(files: dict) -> str | None:
    suffixes = {Path(name).suffix for name in files}
    if ".apk" in suffixes:
        return "apk"
    if ".ipk" in suffixes:
        return "ipk"
    return None


def build_catalog(
    config: dict,
    states: dict[tuple[str, str], dict],
    packages: dict[str, SourcePackage],
    upstreams: dict[str, dict],
    sources_repo: str,
    commit: str,
    now: dt.datetime,
) -> dict[str, dict]:
    """Relative path -> JSON document for the published catalog (see docs/catalog.md).
    `branch` is the OpenWrt release line (a [[versions]] entry)."""
    feed = config["feed_name"]
    generated_at = format_time(now)
    branches = [entry["version"] for entry in config["versions"]]
    arches = list(config["arches"])
    targets = []
    details: dict[str, dict] = {}

    for branch in branches:
        for arch in arches:
            state = states.get((branch, arch))
            if state is None:
                continue
            fmt = archive_format(state["files"])
            path = f"packages-{branch}/{arch}/{feed}"
            by_name: dict[str, list[tuple[str, dict]]] = defaultdict(list)
            for filename, record in state["files"].items():
                by_name[record["name"]].append((filename, record))

            indexed = 0
            for name, entries in sorted(by_name.items()):
                current = [(filename, record) for filename, record in entries if record["latest"]]
                if not current:
                    continue
                if not PACKAGE_NAME.match(name):
                    raise FeedError(f"package name {name!r} cannot be used as a catalog file name")
                indexed += 1
                filename, record = current[0]
                meta = record.get("meta", {})
                detail = details.setdefault(
                    name,
                    {"name": name, "source": record["source"], **dict.fromkeys(SUMMARY_KEYS), "targets": []},
                )
                for key in SUMMARY_KEYS:
                    detail[key] = detail[key] or meta.get(key)
                previous = sorted(
                    (item for item in entries if not item[1]["latest"]),
                    key=lambda item: item[1]["published_at"],
                    reverse=True,
                )
                detail["targets"].append(
                    {
                        "branch": branch,
                        "arch": arch,
                        "format": fmt,
                        "path": path,
                        "filename": filename,
                        "version": record["version"],
                        "package_arch": meta.get("arch"),
                        "size": record.get("size"),
                        "installed_size": meta.get("installed_size"),
                        "sha256": record.get("sha256"),
                        "depends": meta.get("depends") or [],
                        "provides": meta.get("provides") or [],
                        "abi_version": meta.get("abi_version"),
                        "published_at": record["published_at"],
                        "previous": [
                            {
                                "filename": old_filename,
                                "version": old["version"],
                                "size": old.get("size"),
                                "sha256": old.get("sha256"),
                                "published_at": old["published_at"],
                                "superseded_at": old.get("superseded_at"),
                            }
                            for old_filename, old in previous
                        ],
                    }
                )
            targets.append(
                {
                    "branch": branch,
                    "arch": arch,
                    "format": fmt,
                    "path": path,
                    "index": INDEX_FILES.get(fmt),
                    "updated_at": state.get("updated_at"),
                    "packages": indexed,
                }
            )

    documents: dict[str, dict] = {}
    search = []
    for name in sorted(details):
        detail = details[name]
        source = packages.get(detail["source"])
        available = {(target["branch"], target["arch"]) for target in detail["targets"]}
        detail["source_path"] = source.path if source else None
        detail["category"] = source.path.split("/", 1)[0] if source else None
        detail["upstream"] = upstreams.get(detail["source"])
        detail["unavailable"] = [
            {
                "branch": branch,
                "arch": arch,
                "status": states.get((branch, arch), {}).get("packages", {}).get(detail["source"], {}).get("status"),
            }
            for branch in branches
            for arch in arches
            if (branch, arch) not in available
        ]
        documents[f"packages/{name}.json"] = {"schema": CATALOG_SCHEMA, "feed": feed, "generated_at": generated_at, **detail}
        search.append(
            {
                "name": name,
                "source": detail["source"],
                "category": detail["category"],
                "description": detail["description"],
                "license": detail["license"],
                "maintainer": detail["maintainer"],
                "targets": [
                    {key: target[key] for key in ("branch", "arch", "version", "published_at")}
                    for target in detail["targets"]
                ],
            }
        )

    documents["search.json"] = {
        "schema": CATALOG_SCHEMA,
        "feed": feed,
        "generated_at": generated_at,
        "packages": search,
    }
    documents["index.json"] = {
        "schema": CATALOG_SCHEMA,
        "feed": feed,
        "generated_at": generated_at,
        "sources": {"repository": sources_repo, "commit": commit},
        "branches": branches,
        "arches": arches,
        "targets": targets,
        "search": "search.json",
        "package": "packages/{name}.json",
    }
    return documents


def cmd_catalog(args) -> int:
    config = load_config(args.config)
    packages = source_packages(args.sources, config.get("exclude", []))
    states = {}
    for entry in config["versions"]:
        for arch in config["arches"]:
            path = state_path(args.state_dir, entry["version"], arch)
            if path.exists():
                states[(entry["version"], arch)] = load_state(path, entry["version"], arch)
    documents = build_catalog(
        config,
        states,
        packages,
        load_upstreams(args.sources_config),
        args.sources_repo,
        git(args.sources, "rev-parse", "HEAD").strip(),
        now_arg(args.now),
    )
    if args.out.exists():
        shutil.rmtree(args.out)
    for relative, document in documents.items():
        write_json(args.out / relative, document)
    print(f"catalog: {len(states)} targets, {len(documents['search.json']['packages'])} packages")
    return 0


def remote_files(path: Path) -> set[str]:
    """Top-level file names from `rclone lsjson --files-only` output."""
    return {item["Path"] for item in load_json(path) if not item.get("IsDir") and "/" not in item["Path"]}


def cmd_carry(args) -> int:
    state = load_json(args.state)
    accepted = set(filter(None, args.accepted.read_text(encoding="utf-8").splitlines()))
    remote = remote_files(args.remote_list) | accepted
    state, carry, missing = apply_remote_listing(state, remote)
    carry = [name for name in carry if name not in accepted]
    for name in missing:
        print(f"::warning::{name} is indexed but missing remotely; its source will be rebuilt next run")
    write_json(args.state, state)
    args.out.write_text("".join(f"{name}\n" for name in carry), encoding="utf-8")
    print(f"carry {len(carry)} archives, {len(missing)} missing")
    return 0


def cmd_verify(args) -> int:
    state = load_json(args.state)
    expected = {name for name, meta in state["files"].items() if meta["latest"]}
    indexed = {entry["filename"] for entry in read_index_metadata(args.index_meta)}
    staged = {path.name for path in args.stage.iterdir() if path.suffix in ARCHIVE_SUFFIXES}
    problems = []
    if indexed != expected:
        problems.append(
            f"index/state mismatch: only indexed {sorted(indexed - expected)}, "
            f"only in state {sorted(expected - indexed)}"
        )
    if staged != expected:
        problems.append(
            f"stage/state mismatch: only staged {sorted(staged - expected)}, "
            f"only in state {sorted(expected - staged)}"
        )
    if expected and not (args.index_dir / "Packages").exists() and not (args.index_dir / "packages.adb").exists():
        problems.append(f"no index file in {args.index_dir}")
    if problems:
        raise FeedError("; ".join(problems))
    print(f"verified {len(expected)} indexed archives")
    return 0


def cmd_gc(args) -> int:
    state = load_json(args.state)
    index_files = {path.name for path in args.index_dir.iterdir() if path.is_file()}
    delete = gc_candidates(state, index_files, remote_files(args.remote_list))
    args.out.write_text("".join(f"{name}\n" for name in delete), encoding="utf-8")
    print(f"{len(delete)} remote files to delete")
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

    merge = sub.add_parser("merge", help="apply a build result to the target state")
    merge.add_argument("--config", type=Path, required=True)
    merge.add_argument("--sources", type=Path, required=True)
    merge.add_argument("--version", required=True)
    merge.add_argument("--arch", required=True)
    merge.add_argument("--state", type=Path, help="current state file (absent = new target)")
    merge.add_argument("--build-dir", type=Path, required=True)
    merge.add_argument("--out-dir", type=Path, required=True)
    merge.add_argument("--now")
    merge.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    merge.set_defaults(func=cmd_merge)

    carry = sub.add_parser("carry", help="list indexed archives to download, dropping ones missing remotely")
    carry.add_argument("--state", type=Path, required=True)
    carry.add_argument("--accepted", type=Path, required=True)
    carry.add_argument("--remote-list", type=Path, required=True)
    carry.add_argument("--out", type=Path, required=True)
    carry.set_defaults(func=cmd_carry)

    verify = sub.add_parser("verify", help="check the signed index matches the state")
    verify.add_argument("--state", type=Path, required=True)
    verify.add_argument("--stage", type=Path, required=True)
    verify.add_argument("--index-dir", type=Path, required=True)
    verify.add_argument("--index-meta", type=Path, required=True)
    verify.set_defaults(func=cmd_verify)

    gc = sub.add_parser("gc", help="list remote files not referenced by the state or index")
    gc.add_argument("--state", type=Path, required=True)
    gc.add_argument("--index-dir", type=Path, required=True)
    gc.add_argument("--remote-list", type=Path, required=True)
    gc.add_argument("--out", type=Path, required=True)
    gc.set_defaults(func=cmd_gc)

    catalog = sub.add_parser("catalog", help="generate the public metadata catalog from all target states")
    catalog.add_argument("--config", type=Path, required=True)
    catalog.add_argument("--sources-config", type=Path, required=True, help="sources.toml")
    catalog.add_argument("--sources", type=Path, required=True)
    catalog.add_argument("--sources-repo", required=True)
    catalog.add_argument("--state-dir", type=Path, required=True)
    catalog.add_argument("--out", type=Path, required=True)
    catalog.add_argument("--now")
    catalog.set_defaults(func=cmd_catalog)
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
