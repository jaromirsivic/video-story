#!/usr/bin/env sh
VS_HERE=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd -P) || exit 21
if [ "$#" -eq 0 ]; then
  set -- run
fi
exec sh "$VS_HERE/skills/video-story/scripts/video-story.sh" "$@" --detach
