# Local patches

Applied by `scripts/sync_sources.py` right after the upstream packages are fetched, so the
tree pushed to mywrt-feed-sources is already patched. `<name>` is the package name from
`sources.toml`.

- `feed/<name>/*.patch` patches the package directory itself (`-p1`), such as `Makefile` metadata.
- `source/<name>/*.patch` is copied to `<category>/<name>/patches/` so OpenWrt applies it to the
  upstream source tree.

Patches are applied in file name order; prefix them with numbers. A patch that no longer applies,
including one upstream has already merged, fails the sync: update or delete it.
