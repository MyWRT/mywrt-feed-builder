# 本地补丁

[English](README.md) | **中文** | [构建器 README](../README_ZH.md)

由 `scripts/sync_sources.py` 在上游软件包抓取完成后立即应用，因此推送到
mywrt-feed-sources 的目录树已经带上了补丁。`<name>` 是 `sources.toml` 里的软件包名称。

- `feed/<name>/*.patch` 直接对软件包目录本身打补丁（`-p1`），例如修改 `Makefile` 元数据。
- `source/<name>/*.patch` 会被复制到 `<category>/<name>/patches/`，由 OpenWrt 应用到上游
  源码树。

补丁按文件名顺序应用，请用数字前缀命名。已经无法应用的补丁（包括上游早已合并进去的）
会让同步失败：请更新或删除它。
