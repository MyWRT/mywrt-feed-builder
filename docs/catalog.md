# Catalog format (schema 1)

Read-only package metadata for web front ends, published by the `catalog` job of
`build.yml` to `storage.catalog` (default `r2:mywrt-dist/.feed-catalog`). It is
regenerated from every target state at the end of each build run, so it always
matches what the feeds contain. It is shaped for a static, client-side package
browser in the style of [pkgs.alpinelinux.org](https://pkgs.alpinelinux.org/packages):
a list deduplicated by package name and filtered by name, branch and
architecture, and a detail page per package.

```
<catalog>/
  index.json            entry point: branches, architectures, feeds
  search.json           list page: one record per package name
  packages/<name>.json  detail page: one document per package name
```

All files are UTF-8 JSON. Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`. Unknown
fields must be ignored; incompatible changes bump `schema`. `index.json` is
written after the documents it points to.

Terms: **branch** is the OpenWrt release line (`25.12`, `24.10`); **arch** is the
package architecture of a feed (`x86_64`, `aarch64_generic`, ...). A package
**name** is a binary package; one source package can produce several (for
example `luci-app-natmapt` and its `luci-i18n-natmapt-*` translations).

## index.json

```json
{
  "schema": 1,
  "feed": "mywrt",
  "generated_at": "2026-10-16T12:00:00Z",
  "sources": { "repository": "MyWRT/mywrt-feed-sources", "commit": "5f1c..." },
  "branches": ["25.12", "24.10"],
  "arches": ["x86_64", "aarch64_generic", "arm_cortex-a7_neon-vfpv4"],
  "targets": [
    {
      "branch": "25.12",
      "arch": "x86_64",
      "format": "apk",
      "path": "packages-25.12/x86_64/mywrt",
      "index": "packages.adb",
      "updated_at": "2026-10-16T11:58:03Z",
      "packages": 78
    }
  ],
  "search": "search.json",
  "package": "packages/{name}.json"
}
```

- `branches` / `arches`: the configured values, in display order; use them for
  the filter selects.
- `targets`: feeds that have been published at least once. `format` is `apk` or
  `ipk` (`null` for an empty feed); `path` is the feed directory under the
  package download host; `index` is the index file inside it; `packages` is the
  number of indexed packages.
- `package`: path template of the detail document.

## search.json

```json
{
  "schema": 1,
  "feed": "mywrt",
  "generated_at": "2026-10-16T12:00:00Z",
  "packages": [
    {
      "name": "mosdns",
      "source": "mosdns",
      "category": "net",
      "description": "A plug-in DNS forwarder/splitter",
      "license": "GPL-3.0",
      "maintainer": "Jane Doe <jane@example.org>",
      "targets": [
        { "branch": "25.12", "arch": "x86_64", "version": "5.3.4-r14", "published_at": "2026-10-16T11:58:03Z" },
        { "branch": "24.10", "arch": "x86_64", "version": "5.3.4-r14", "published_at": "2026-10-16T11:40:12Z" }
      ]
    }
  ]
}
```

One record per package name, sorted by name. `targets` lists every feed whose
index contains the package, in `branches` x `arches` order. `description`,
`license` and `maintainer` come from the first such feed that has them (`null`
when absent; apk packages carry no `section`). `category` is the top-level
directory in the sources repository (`null` for removed sources).

List page filtering, entirely in the browser:

```js
const rows = search.packages.filter((pkg) =>
  nameMatches(pkg.name, query) &&            // e.g. glob with * and ?
  pkg.targets.some((t) => (!branch || t.branch === branch) && (!arch || t.arch === arch)));
// Show per row: the matching targets' versions and newest published_at.
```

## packages/&lt;name&gt;.json

```json
{
  "schema": 1,
  "feed": "mywrt",
  "generated_at": "2026-10-16T12:00:00Z",
  "name": "mosdns",
  "source": "mosdns",
  "source_path": "net/mosdns",
  "category": "net",
  "upstream": { "repo": "https://github.com/sbwml/luci-app-mosdns.git", "branch": null, "path": "mosdns" },
  "description": "A plug-in DNS forwarder/splitter",
  "license": "GPL-3.0",
  "url": "https://github.com/IrineSistiana/mosdns",
  "maintainer": "Jane Doe <jane@example.org>",
  "section": "net",
  "targets": [
    {
      "branch": "24.10",
      "arch": "x86_64",
      "format": "ipk",
      "path": "packages-24.10/x86_64/mywrt",
      "filename": "mosdns_5.3.4-r14_x86_64.ipk",
      "version": "5.3.4-r14",
      "package_arch": "x86_64",
      "size": 6612465,
      "installed_size": 17131520,
      "sha256": "9f2c...",
      "depends": ["libc", "ca-bundle"],
      "provides": [],
      "abi_version": null,
      "published_at": "2026-10-16T11:40:12Z",
      "previous": [
        {
          "filename": "mosdns_5.3.4-r13_x86_64.ipk",
          "version": "5.3.4-r13",
          "size": 6603556,
          "sha256": "41aa...",
          "published_at": "2026-09-12T10:56:16Z",
          "superseded_at": "2026-10-16T11:40:12Z"
        }
      ]
    }
  ],
  "unavailable": [
    { "branch": "24.10", "arch": "arm_cortex-a7_neon-vfpv4", "status": "unsupported" }
  ]
}
```

- `targets`: one entry per feed that indexes the package (the supported
  branch/arch matrix). Download URL = `<host>/<path>/<filename>`.
  `package_arch` is the package's own architecture (`all` / `noarch` for
  architecture-independent packages). `size` and `sha256` describe the archive
  file. `depends` / `provides` are as written by the package (opkg:
  `libfoo (>= 1)`; apk: `libfoo>=1`).
- `previous`: older builds still downloadable under the retention policy,
  newest first.
- `unavailable`: configured branch/arch combinations without this package, with
  the latest build outcome of its source there: `unsupported` (architecture
  excluded by the package), `failed`, `download_failed`, `built` (the source
  built but produced no package of this name there), or `null` (never built on
  that feed).

## Serving notes

`search.json` is small (well under 1 MB for this feed); the browser can fetch
it once and filter locally. Serve the catalog from the same origin as the page
(for example a Pages route that reads the R2 binding) to avoid CORS. Revalidate
`index.json` and `search.json` on every request (`no-cache`) or cache them for
at most a minute; detail documents can rely on `ETag`.
