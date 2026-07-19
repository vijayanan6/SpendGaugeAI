#!/usr/bin/env bash
# Downloads the Tailwind standalone CLI (a single binary, not the npm package —
# see docs/DESIGN.md's "no Node anywhere" requirement) if not already present,
# then compiles src/spendgaugeai/static/src/input.css to the package's served
# static/app.css. Also vendors Alpine.js if it isn't already in static/ — same
# "download once, commit/ship the file, no CDN at runtime" pattern used for
# Tailwind itself (self-hosted means the dashboard must work offline).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC_DIR="$ROOT_DIR/src/spendgaugeai/static"
BIN_DIR="$ROOT_DIR/.tailwind-bin"
TAILWIND_VERSION="v3.4.13"

os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
  Linux)  plat="linux" ;;
  Darwin) plat="macos" ;;
  MINGW*|MSYS*|CYGWIN*) plat="windows" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac
case "$arch" in
  x86_64|amd64) tarch="x64" ;;
  arm64|aarch64) tarch="arm64" ;;
  *) echo "Unsupported arch: $arch" >&2; exit 1 ;;
esac

ext=""
[ "$plat" = "windows" ] && ext=".exe"
bin_name="tailwindcss-${plat}-${tarch}${ext}"
bin_path="$BIN_DIR/$bin_name"

mkdir -p "$BIN_DIR" "$STATIC_DIR/src"

if [ ! -x "$bin_path" ]; then
  echo "Downloading Tailwind standalone CLI ($TAILWIND_VERSION, $plat-$tarch)..."
  curl -sL -o "$bin_path" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${bin_name}"
  chmod +x "$bin_path"
fi

if [ ! -f "$STATIC_DIR/alpine.min.js" ]; then
  echo "Vendoring Alpine.js..."
  curl -sL -o "$STATIC_DIR/alpine.min.js" \
    "https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js"
fi

echo "Compiling CSS..."
"$bin_path" -i "$STATIC_DIR/src/input.css" -o "$STATIC_DIR/app.css" --minify

echo "Done: $STATIC_DIR/app.css"
