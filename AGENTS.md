# AGENTS.md

Builds and publishes the `mywrt` OpenWrt package feed. Two workflows: `sync.yml`
regenerates the sources repository (`<category>/<name>/` trees, usable as a
`src-git` feed) from `sources.toml` + `local-patches/`; `build.yml` rebuilds only
changed packages in ImmortalWrt SDK containers and publishes archives, signed
indexes, per-target state and a metadata catalog through rclone.

- Operator view (flow, retention, storage, deployment): `README.md`.
- Catalog consumed by the web front end: `docs/catalog.md`, a public contract.
- All build decisions live in `scripts/feedctl.py`; its module docstring documents
  the state file and the subcommand order.

## Invariants

Each one protects published feeds or data; keep them when changing code.

- **State is the commit point.** Publish order is archives → index (feed store)
  → state (state store) → gc. A failure before the state write only causes a
  rebuild next run. Any new publish step goes before the state write or is
  derived from state afterwards, like the catalog.
- **The state store is authoritative.** gc deletes every file in a target feed
  directory that neither the state nor the new index references. Point
  `FEED_STORE` / `STATE_STORE` / `CATALOG_STORE` at scratch prefixes for any
  experiment.
- **Catalog is replaced with `rclone sync`.** `feedctl.storage()` rejects layouts
  where the catalog contains the feed or state store or lies inside the state
  store; keep that check in step with any new store.
- **Fingerprint = package content + build recipe.** Content hash covers
  `<category>/<name>/` minus `ignored_for_build()`; the recipe hash covers
  `feed_name`, `sdk_image`, `epoch`, `config` and the `[[versions]]` entry. A
  new setting that changes build output joins `recipe_hash()`. Changing the
  fingerprint format rebuilds every package once.
- **Failure policy.** Build failures are recorded as `failed` and not retried
  until the fingerprint changes or a manual run names the package; the `report`
  job owns the `Build failure: <name>` issues. Download failures retry for
  `DOWNLOAD_ATTEMPTS` runs, then count as build failures.
- **Sync is all-or-nothing.** `sync_sources.py` assembles everything in a temp
  directory and touches the sources checkout only after every clone and patch
  succeeded; the sources repository is fully generated (everything but `.git`
  is replaced).
- **README time round-trips through git.** `${updated}` is the time packages
  last changed; `sync.yml` commits with `--date "$UPDATED"` and the next sync
  reads the author date back, so unchanged syncs produce no commit.
- **Package names are unique across categories**; both sync and `feedctl`
  enforce it.
- **Signing keys exist only in the index step**, in `$RUNNER_TEMP/keys`,
  removed right after; the build container never receives them.
- **Schemas.** Bump `STATE_SCHEMA` / `CATALOG_SCHEMA` on incompatible changes and
  update `docs/catalog.md` in the same change.

## Known pitfalls

- OpenWrt `make package/index` walks only feeds the SDK knows; `sdk/entry.sh`
  passes `PACKAGE_SUBDIRS=` explicitly.
- ruby (pulled in by OpenClash) builds Rust + LLVM from source (~2 h per target)
  unless `# CONFIG_RUBY_ENABLE_YJIT is not set` stays in `targets.toml`.
- OpenWrt stamp hashes include file mtimes, so cached SDK `build_dir` /
  `staging_dir` trees rebuild after every fresh checkout; incrementality comes
  from the fingerprint plan, with ccache as the only compiler cache.
- The sources repository is cloned shallow, so `luci.mk` versions LuCI packages
  by the HEAD commit; only rebuilt packages get new versions, which is intended.
- apk and opkg do not follow HTTP redirects; the feed store must be served
  without them.
- apk field names read from `apk adbdump` (`origin`, `installed-size`,
  `provides`, `tags`) come from the apk-tools v3 schema; confirm against a real
  25.12 build when touching `apk_meta()`.
- rclone exit codes 3/4 mean "not found" (first run); every other non-zero code
  must fail the step.
- `GITHUB_TOKEN`-triggered `workflow_dispatch` starts runs, which is how sync
  starts builds; a `GITHUB_TOKEN` push would not trigger workflows.
- The generated sources repository has no `.gitignore`: the old builder's
  `*.pem` ignore silently dropped files that packages install.

## Verification

Python 3.11+ standard library only, no third-party dependencies, no test suite.
After changes run:

```sh
actionlint .github/workflows/*.yml
shellcheck -s bash sdk/entry.sh scripts/sdk-run.sh
python3 -m py_compile scripts/*.py
```

Exercise logic with throwaway scripts driving the real CLIs: a temp git repo
with `<category>/<name>/Makefile` as `--sources`, fake build directories
(`result.json`, `Packages.manifest` or `packages.adb.json`, `packages/`), and
`--now` for deterministic time; `sync_sources.py` accepts `file://` upstreams.
Container steps (`sdk-run.sh`) need Docker and several GB of SDK images and are
verified on CI against scratch stores.

## Conventions

- Configuration is TOML (`targets.toml`, `sources.toml`, read with `tomllib`);
  machine state and catalog are JSON written by `write_json()`.
- Keep `README.md` tables (secrets, variables, storage) in step with
  `targets.toml` and the workflows.
- Secrets, keys and rclone configuration stay in GitHub secrets; docs use
  placeholders for personal or account data.
