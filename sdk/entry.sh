#!/bin/bash
# Runs inside an ImmortalWrt SDK container (working directory = SDK root).
#
# MODE=build  Compile $PACKAGES from the feed mounted at /feed. Writes to /out:
#               packages/        archives produced by the feed (all of them; the
#                                host keeps only those whose source succeeded)
#               Packages.manifest | packages.adb.json   unsigned metadata of packages/
#               result.json      source packages by outcome: succeeded, failed,
#                                download_failed, unsupported
#               failures/<pkg>.log           verbose log of a failed build
#               failures/<pkg>.download.log  log of the last failed download attempt
#               logs/            OpenWrt BUILD_LOG output
# MODE=index  Index and sign the archives in /stage with the keys in /keys.
#             Writes the index files (no archives) to /out/index/ and the
#             metadata dump to /out/.
#
# Package build failures are reported in result.json and do not fail the
# script; any other failure exits non-zero.
set -euo pipefail

: "${MODE:?}" "${FEED_NAME:?}" "${ARCH_PACKAGES:?}"

FEED_OUT="bin/packages/$ARCH_PACKAGES/$FEED_NAME"
DOWNLOAD_ATTEMPTS="${DOWNLOAD_ATTEMPTS:-3}"

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

# Run a command, mirroring its output to a log file; returns the command's status.
run_logged() {
	local log="$1" rc
	shift
	set +e
	"$@" 2>&1 | tee "$log"
	rc="${PIPESTATUS[0]}"
	set -e
	return "$rc"
}

download() {
	local pkg="$1" log="/out/failures/$1.download.log" attempt
	for attempt in $(seq 1 "$DOWNLOAD_ATTEMPTS"); do
		if run_logged "$log" make "package/$pkg/download" V=s; then
			rm -f "$log"
			return 0
		fi
		[ "$attempt" = "$DOWNLOAD_ATTEMPTS" ] || sleep $((attempt * 20))
	done
	return 1
}

compile() {
	local pkg="$1" log="/out/failures/$1.log"
	make -j"$(nproc)" BUILD_LOG=1 CONFIG_AUTOREMOVE=y "package/$pkg/compile" && return 0
	# Rebuild serially for a readable log; this also absorbs parallel-build races.
	echo "Retrying $pkg with -j1 V=s"
	if run_logged "$log" make -j1 V=s BUILD_LOG=1 CONFIG_AUTOREMOVE=y "package/$pkg/compile"; then
		rm -f "$log"
		return 0
	fi
	return 1
}

json_array() {
	local first=1 item
	printf '['
	for item in "$@"; do
		[ "$first" = 1 ] || printf ','
		printf '"%s"' "$item"
		first=0
	done
	printf ']'
}

setup_sdk() {
	group "setup SDK"
	# Snapshot containers download the SDK on first start.
	[ ! -f setup.sh ] || bash setup.sh
	endgroup
}

# Copy the metadata needed to map archives to source packages.
dump_metadata() {
	local dir="$1" dest="$2"
	if [ -f "$dir/Packages.manifest" ]; then
		cp "$dir/Packages.manifest" "$dest/Packages.manifest"
	elif [ -f "$dir/packages.adb" ]; then
		staging_dir/host/bin/apk adbdump --format json "$dir/packages.adb" > "$dest/packages.adb.json"
	else
		echo "No package index found in $dir" >&2
		return 1
	fi
}

build() {
	local pkg
	local -a packages succeeded=() failed=() download_failed=() unsupported=() installed=()
	read -r -a packages <<< "${PACKAGES:-}"
	mkdir -p /out/packages /out/logs /out/failures

	sed \
		-e 's,https://git.openwrt.org/feed/,https://github.com/openwrt/,' \
		-e 's,https://git.openwrt.org/openwrt/,https://github.com/openwrt/,' \
		-e 's,https://git.openwrt.org/project/,https://github.com/openwrt/,' \
		feeds.conf.default > feeds.conf
	echo "src-link $FEED_NAME /feed" >> feeds.conf

	# luci.mk derives versions from `git log` inside /feed.
	git config --global --add safe.directory '*'

	group "feeds update"
	cat feeds.conf
	./scripts/feeds update -a
	endgroup

	if [ "${REPLACE_GOLANG:-0}" = 1 ]; then
		group "replace golang (${GOLANG_REPO:?} ${GOLANG_BRANCH:?})"
		rm -rf feeds/packages/lang/golang
		git clone --depth=1 --branch "$GOLANG_BRANCH" "$GOLANG_REPO" feeds/packages/lang/golang
		./scripts/feeds update -i packages
		endgroup
	fi

	for pkg in "${packages[@]}"; do
		group "feeds install $pkg"
		if run_logged "/out/failures/$pkg.log" ./scripts/feeds install -p "$FEED_NAME" -f "$pkg"; then
			rm -f "/out/failures/$pkg.log"
			installed+=("$pkg")
		else
			echo "::error::feeds install failed for $pkg"
			failed+=("$pkg")
		fi
		endgroup
	done

	group "make defconfig"
	{
		[ -z "${EXTRA_CONFIG:-}" ] || printf '%s\n' "$EXTRA_CONFIG"
		if command -v ccache > /dev/null && [ -d /ccache ]; then
			echo 'CONFIG_CCACHE=y'
			echo 'CONFIG_CCACHE_DIR="/ccache"'
		fi
	} >> .config
	if command -v ccache > /dev/null && [ -d /ccache ]; then
		export CCACHE_DIR=/ccache
		ccache -M "${CCACHE_MAXSIZE:-1G}"
	fi
	make defconfig
	endgroup

	# Packages whose DEPENDS exclude this architecture are not selectable.
	make \
		-f .config \
		-f tmp/.packagedeps \
		-f <(echo "\$(info \$(sort \$(package-y) \$(package-m)))"; echo -en "a:\n\t@:") \
		| tr ' ' '\n' > enabled-package-subdirs.txt

	for pkg in "${installed[@]}"; do
		if ! grep -m1 -qE "(^|/)${pkg}\$" enabled-package-subdirs.txt; then
			echo "::warning::$pkg is not supported on $ARCH_PACKAGES"
			unsupported+=("$pkg")
			continue
		fi

		group "download $pkg"
		if ! download "$pkg"; then
			endgroup
			echo "::warning::$pkg: source download failed $DOWNLOAD_ATTEMPTS times on $ARCH_PACKAGES"
			download_failed+=("$pkg")
			continue
		fi
		endgroup

		group "build $pkg"
		if compile "$pkg"; then
			succeeded+=("$pkg")
			endgroup
		else
			endgroup
			failed+=("$pkg")
			echo "::error::$pkg failed to build on $ARCH_PACKAGES"
		fi
	done

	if command -v ccache > /dev/null && [ -d /ccache ]; then
		ccache -s || true
	fi

	# Dependencies from other feeds are rebuilt as a side effect; they are not published.
	if [ -d "bin/packages/$ARCH_PACKAGES" ]; then
		find "bin/packages/$ARCH_PACKAGES" -mindepth 1 -maxdepth 1 ! -name "$FEED_NAME" -exec rm -rf {} +
	fi

	if compgen -G "$FEED_OUT/*.[ai]pk" > /dev/null; then
		group "index new packages"
		make package/index PACKAGE_SUBDIRS="$PWD/$FEED_OUT" CONFIG_SIGNED_PACKAGES= V=s
		cp "$FEED_OUT"/*.[ai]pk /out/packages/
		dump_metadata "$FEED_OUT" /out
		endgroup
	fi

	[ ! -d logs ] || cp -r logs/. /out/logs/

	{
		printf '{"succeeded":%s,' "$(json_array "${succeeded[@]}")"
		printf '"failed":%s,' "$(json_array "${failed[@]}")"
		printf '"download_failed":%s,' "$(json_array "${download_failed[@]}")"
		printf '"unsupported":%s}\n' "$(json_array "${unsupported[@]}")"
	} > /out/result.json
	cat /out/result.json
}

index() {
	mkdir -p /out/index "$FEED_OUT"

	group "make defconfig"
	make defconfig
	endgroup

	if [ -f /keys/key-build ]; then
		cp /keys/key-build key-build
	fi
	if [ -f /keys/private-key.pem ]; then
		cp /keys/private-key.pem private-key.pem
	fi

	if compgen -G "/stage/*.[ai]pk" > /dev/null; then
		cp /stage/*.[ai]pk "$FEED_OUT"/
	fi

	# package/index only walks feeds known to the SDK (package/feeds/*, feeds.conf);
	# none are installed here, so name the directory explicitly.
	group "make package/index"
	make package/index PACKAGE_SUBDIRS="$PWD/$FEED_OUT" CONFIG_SIGNED_PACKAGES=y V=s
	endgroup

	find "$FEED_OUT" -maxdepth 1 -type f ! -name '*.ipk' ! -name '*.apk' -exec cp {} /out/index/ \;
	if compgen -G "$FEED_OUT/*.[ai]pk" > /dev/null; then
		dump_metadata "$FEED_OUT" /out
	fi
	ls -l /out/index
}

setup_sdk
case "$MODE" in
	build) build ;;
	index) index ;;
	*)
		echo "Unknown MODE: $MODE" >&2
		exit 2
		;;
esac
