# mywrt-feed-sources

[English](README.md) | **中文**

`mywrt` 的 OpenWrt 软件包 feed 源码，由
[mywrt-feed-builder](https://github.com/MyWRT/mywrt-feed-builder) 从上游仓库加上本地
补丁生成。**请勿在此提交**：每次同步都会整体覆盖这棵目录树。要改动，请到
[feed 构建器](https://github.com/MyWRT/mywrt-feed-builder) 提交 pull request 或 issue，
在其 `sources.toml` 或 `local-patches/` 上修改（本仓库是生成产物）。

最后更新：**${updated}**

软件包位于 `<category>/<name>/`（`luci`、`net`、`utils`、`telephony`……）。

## 作为源码 feed 使用

在 `feeds.conf` 中加入：

```
src-git mywrt https://github.com/MyWRT/mywrt-feed-sources.git
```

然后执行 `./scripts/feeds update mywrt && ./scripts/feeds install -p mywrt <package>`。

## 预编译软件包

```
https://mywrt.awayfrom.cn/packages-<version>/<arch>/mywrt/
```
