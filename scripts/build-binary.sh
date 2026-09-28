#!/bin/sh
# Build a single-file talkub binary with Nuitka: dist/talkub-<version>-<os>-<arch>.
# Needs a C compiler (clang on macOS, gcc on Linux); Nuitka comes from the `binary` dependency group.
set -eu
cd "$(dirname "$0")/.."

version="$(uv version --short)"
build_id="${version}-$(git rev-parse --short=12 HEAD 2>/dev/null || echo local)"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$arch" in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
esac
name="talkub-${version}-${os}-${arch}"

# onefile unpacks into a per-build cache dir that is reused on the next start (fast start).
uv run --locked --group binary python -m nuitka \
  --mode=onefile \
  --onefile-tempdir-spec="{CACHE_DIR}/talkub/bin/${build_id}" \
  --include-package=talkub \
  --include-package-data=talkub \
  --include-package-data=yamllint \
  --output-dir=build/nuitka \
  --output-filename="$name" \
  --assume-yes-for-downloads \
  --quiet \
  scripts/talkub_bin.py

mkdir -p dist
mv "build/nuitka/$name" "dist/$name"
echo "dist/$name"
