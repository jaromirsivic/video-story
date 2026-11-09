#!/usr/bin/env sh
# Video Story starter (Linux, macOS). Run it to open the recording window;
# "./start-video-story.sh edit" opens the Video Story Editor with the stored recordings.
# The first start prepares FFmpeg and the Whisper model (about 550 MB).

VS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$VS_ROOT" || exit 1

# Started from a file manager the PATH is short: add where uv and Homebrew install.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

fail() {
  printf '\n%s\n' "$1" >&2
  if [ -t 0 ]; then
    printf 'Press Enter to close.'
    read -r _
  fi
  exit "${2:-1}"
}

case "${1:-run}" in
  run) VS_COMMAND=run ;;
  edit) VS_COMMAND=edit ;;
  *) fail "Usage: $0 [edit]" 2 ;;
esac

command -v uv >/dev/null 2>&1 || fail "uv is not installed. Install it with:
  curl -LsSf https://astral.sh/uv/install.sh | sh
or from https://docs.astral.sh/uv/ and start Video Story again." 20

MODEL=models/ggml-large-v3-turbo-q5_0.bin
need_setup=
[ -f "$MODEL" ] || need_setup=1
if ! command -v ffmpeg >/dev/null 2>&1 && ! ls bin/ffmpeg/*/ffmpeg >/dev/null 2>&1; then
  need_setup=1
fi
if [ -n "$need_setup" ]; then
  echo "First start: preparing FFmpeg and the Whisper model, about 550 MB. This takes a few minutes."
  uv run --project . video-story setup || fail "Video Story could not start; the message above says why."
  [ -f "$MODEL" ] || fail "Video Story could not start; the message above says why."
fi

echo "Starting Video Story..."
uv run --project . video-story --log-level WARNING "$VS_COMMAND" --detach >/dev/null \
  || fail "Video Story could not start; the message above says why."
