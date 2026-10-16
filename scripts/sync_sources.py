#!/usr/bin/env python3
"""Regenerate the mywrt-feed-sources tree from sources.toml and local-patches/.

Output: <category>/<name>/ for every [[package]], plus README.md and README_ZH.md
rendered from templates/sources-README.md and templates/sources-README_ZH.md.
Packages are assembled in a temporary directory first; any clone, layout or patch
error aborts before the destination checkout is touched.

local-patches/feed/<name>/*.patch    applied to the package directory (-p1)
local-patches/source/<name>/*.patch  copied to <name>/patches/ for OpenWrt to apply
Patches are applied in file name order.

README placeholders (string.Template syntax; write `$$` for a literal `$`):
    ${updated}  when the packages last changed, e.g. "2026-10-16 03:15 UTC"
The time is now when any package changed, otherwise the author date of the
current commit, so re-running a sync without package changes reproduces the
same READMEs. Commit with `--date` set to the `updated` output to keep it so.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
PACKAGE_KEYS = {"name", "category", "repo", "branch", "path"}
CLONE_ATTEMPTS = 3


class SyncError(Exception):
    pass


def load_packages(path: Path) -> list[dict]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown = set(data) - {"package"}
    if unknown:
        raise SyncError(f"{path}: unknown top-level keys {sorted(unknown)}")
    packages = data.get("package", [])
    if not packages:
        raise SyncError(f"{path}: no [[package]] entries")

    seen = set()
    for package in packages:
        label = package.get("name", package)
        unknown = set(package) - PACKAGE_KEYS
        if unknown:
            raise SyncError(f"package {label}: unknown keys {sorted(unknown)}")
        for key in ("name", "category", "repo"):
            if not package.get(key):
                raise SyncError(f"package {label}: missing {key}")
        for key in ("name", "category"):
            if not NAME.match(package[key]):
                raise SyncError(f"package {label}: invalid {key} {package[key]!r}")
        if package["name"] in seen:
            raise SyncError(f"package {package['name']} is defined twice")
        seen.add(package["name"])
    return packages


def clone(url: str, branch: str | None, dest: Path) -> None:
    command = ["git", "clone", "--quiet", "--depth", "1", "--single-branch"]
    if branch:
        command += ["--branch", branch]
    command += [url, str(dest)]
    for attempt in range(1, CLONE_ATTEMPTS + 1):
        shutil.rmtree(dest, ignore_errors=True)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return
        print(f"clone {url} (attempt {attempt}) failed: {result.stderr.strip()}", file=sys.stderr)
        if attempt < CLONE_ATTEMPTS:
            time.sleep(10 * attempt)
    raise SyncError(f"could not clone {url}")


def fetch(packages: list[dict], workdir: Path, tree: Path) -> dict[str, str]:
    """Copy every package into tree/<category>/<name>. Returns name -> relative path."""
    groups: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for package in packages:
        groups[(package["repo"], package.get("branch"))].append(package)

    locations = {}
    for index, ((repo, branch), members) in enumerate(groups.items()):
        checkout = workdir / f"clone-{index}"
        print(f"Fetching {repo}{f' ({branch})' if branch else ''}")
        clone(repo, branch, checkout)
        root = checkout.resolve()
        for package in members:
            source = (checkout / package.get("path", ".")).resolve()
            if source != root and root not in source.parents:
                raise SyncError(f"package {package['name']}: path escapes the repository")
            if not (source / "Makefile").is_file():
                raise SyncError(f"package {package['name']}: no Makefile in {repo} {package.get('path', '.')}")
            relative = f"{package['category']}/{package['name']}"
            shutil.copytree(source, tree / relative, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            locations[package["name"]] = relative
        shutil.rmtree(checkout)
    return locations


def apply_patches(patch_root: Path, tree: Path, locations: dict[str, str]) -> None:
    for kind in ("feed", "source"):
        kind_dir = patch_root / kind
        if not kind_dir.is_dir():
            continue
        for package_dir in sorted(path for path in kind_dir.iterdir() if path.is_dir()):
            if package_dir.name not in locations:
                raise SyncError(f"{package_dir}: no package named {package_dir.name} in sources.toml")
            target = tree / locations[package_dir.name]
            patches = sorted(package_dir.glob("*.patch"))
            if kind == "source":
                if patches:
                    (target / "patches").mkdir(exist_ok=True)
                for patch in patches:
                    shutil.copy2(patch, target / "patches" / patch.name)
                continue
            for patch in patches:
                print(f"Applying {patch}")
                result = subprocess.run(
                    ["patch", "-d", str(target), "-p1", "--forward", "--no-backup-if-mismatch", "-i", str(patch)],
                    capture_output=True,
                    text=True,
                )
                print(result.stdout, end="")
                if result.returncode:
                    raise SyncError(f"{patch} does not apply: {result.stdout.strip()} {result.stderr.strip()}")


def replace_checkout(dest: Path, tree: Path) -> None:
    if not (dest / ".git").exists():
        raise SyncError(f"{dest} is not a git checkout")
    for entry in dest.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in tree.iterdir():
        shutil.move(str(entry), dest / entry.name)


def git(dest: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(dest), *args], capture_output=True, text=True)


def last_update(dest: Path, now: dt.datetime, generated: dict[str, Path]) -> dt.datetime:
    """`now` if the package tree differs from HEAD (or there is no HEAD), else HEAD's author date.

    `generated` maps the files sync writes back into the checkout to their templates.
    replace_checkout() deletes them first, so their absence must not count as a change.
    """
    if git(dest, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        return now
    excludes = [f":(exclude){name}" for name in generated]
    status = git(dest, "status", "--porcelain", "--untracked-files=all", "--", ".", *excludes)
    if status.returncode != 0:
        raise SyncError(f"git status failed in {dest}: {status.stderr.strip()}")
    if status.stdout.strip():
        return now
    authored = git(dest, "log", "-1", "--format=%aI").stdout.strip()
    return dt.datetime.fromisoformat(authored).astimezone(dt.timezone.utc)


def render_readme(template: Path, updated: dt.datetime) -> str:
    try:
        return string.Template(template.read_text(encoding="utf-8")).substitute(
            updated=updated.strftime("%Y-%m-%d %H:%M UTC"),
        )
    except (KeyError, ValueError) as error:
        raise SyncError(f"{template}: bad placeholder {error}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=Path, required=True, help="mywrt-feed-sources checkout to overwrite")
    parser.add_argument("--config", type=Path, default=ROOT / "sources.toml")
    parser.add_argument("--patches", type=Path, default=ROOT / "local-patches")
    parser.add_argument("--readme-template", type=Path, default=ROOT / "templates" / "sources-README.md")
    parser.add_argument(
        "--readme-zh-template",
        type=Path,
        default=ROOT / "templates" / "sources-README_ZH.md",
        help="template for README_ZH.md (default: templates/sources-README_ZH.md)",
    )
    parser.add_argument("--now", help="override the current time (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    now = (
        dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if args.now
        else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    )
    try:
        packages = load_packages(args.config)
        with tempfile.TemporaryDirectory(prefix="sync-sources-") as tmp:
            workdir = Path(tmp)
            tree = workdir / "tree"
            tree.mkdir()
            locations = fetch(packages, workdir, tree)
            apply_patches(args.patches, tree, locations)
            # Render before touching the checkout so template errors leave it intact.
            readmes = {"README.md": args.readme_template, "README_ZH.md": args.readme_zh_template}
            for template in readmes.values():
                render_readme(template, now)
            replace_checkout(args.dest, tree)
        updated = last_update(args.dest, now, readmes)
        for name, template in readmes.items():
            (args.dest / name).write_text(render_readme(template, updated), encoding="utf-8")
    except SyncError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    stamp = updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"updated={stamp}\n")
    print(f"Synced {len(packages)} packages into {args.dest} (updated {stamp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
