#!/usr/bin/env sh
# Video Story launcher (Linux, macOS). Runs the plugin through uv from the repository root.
set -e
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VS_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' '{"ok":false,"code":"UV_MISSING","message":"uv is required. Install it from https://docs.astral.sh/uv/ and run again."}' >&2
  exit 20
fi

exec uv run --quiet --project "$VS_ROOT" video-story "$@"
