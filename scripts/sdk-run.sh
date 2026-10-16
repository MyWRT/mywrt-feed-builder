#!/bin/bash
# Run sdk/entry.sh inside the ImmortalWrt SDK container for one target.
#
# Usage: sdk-run.sh build|index
#
# Common env:  SDK_IMAGE VERSION ARCH FEED_NAME OUT_DIR
# build env:   SOURCES_DIR PACKAGES EXTRA_CONFIG REPLACE_GOLANG GOLANG_REPO
#              GOLANG_BRANCH CCACHE_DIR CCACHE_MAXSIZE
# index env:   STAGE_DIR KEYS_DIR
# SDK_DOCKER_ARGS is appended to `docker run` (word-split), e.g.
# `--userns=keep-id:uid=1000,gid=1000` for rootless podman.
set -euo pipefail

mode="${1:?usage: sdk-run.sh build|index}"
root="$(cd "$(dirname "$0")/.." && pwd)"
image="${SDK_IMAGE:?}:${ARCH:?}-openwrt-${VERSION:?}"

args=(
	--rm
	--env "MODE=$mode"
	--env "FEED_NAME=${FEED_NAME:?}"
	--env "ARCH_PACKAGES=$ARCH"
	--volume "$root/sdk/entry.sh:/entry.sh:ro"
	--volume "${OUT_DIR:?}:/out"
)
mounts=("$OUT_DIR")

case "$mode" in
	build)
		args+=(
			--env PACKAGES
			--env EXTRA_CONFIG
			--env REPLACE_GOLANG
			--env GOLANG_REPO
			--env GOLANG_BRANCH
			--env CCACHE_MAXSIZE
			--volume "${SOURCES_DIR:?}:/feed"
		)
		mounts+=("$SOURCES_DIR")
		if [ -n "${CCACHE_DIR:-}" ]; then
			mkdir -p "$CCACHE_DIR"
			args+=(--volume "$CCACHE_DIR:/ccache")
			mounts+=("$CCACHE_DIR")
		fi
		;;
	index)
		args+=(
			--volume "${STAGE_DIR:?}:/stage:ro"
			--volume "${KEYS_DIR:?}:/keys:ro"
		)
		mounts+=("$STAGE_DIR" "$KEYS_DIR")
		;;
	*)
		echo "Unknown mode: $mode" >&2
		exit 2
		;;
esac

mkdir -p "$OUT_DIR"

# The SDK image runs as buildbot (uid 1000); GitHub runners use uid 1001.
if [ "${GITHUB_ACTIONS:-}" = true ]; then
	owner="$(id -u):$(id -g)"
	sudo chown -R 1000:1000 "${mounts[@]}"
	trap 'sudo chown -R "$owner" "${mounts[@]}"' EXIT
fi

# shellcheck disable=SC2086
docker run "${args[@]}" ${SDK_DOCKER_ARGS:-} --entrypoint /bin/bash "$image" /entry.sh
