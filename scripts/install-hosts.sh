#!/usr/bin/env sh
# Register the Video Story skill with Claude Code and/or Codex (symlinks).
#   scripts/install-hosts.sh --claude [--codex] [--remove]
set -e
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SKILL="$ROOT/skills/video-story"
[ -f "$SKILL/SKILL.md" ] || { echo "Skill directory not found: $SKILL" >&2; exit 1; }

CLAUDE=0; CODEX=0; REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --claude) CLAUDE=1 ;;
    --codex) CODEX=1 ;;
    --remove) REMOVE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ "$CLAUDE" = 1 ] || [ "$CODEX" = 1 ] || { echo "Pass --claude and/or --codex." >&2; exit 2; }

link_skill() {
  host_name=$1; skills_dir=$2; link="$skills_dir/video-story"
  if [ "$REMOVE" = 1 ]; then
    if [ -L "$link" ]; then rm "$link"; echo "$host_name: removed $link"; else echo "$host_name: nothing to remove at $link"; fi
    return
  fi
  mkdir -p "$skills_dir"
  if [ -e "$link" ] && [ ! -L "$link" ]; then echo "$link exists and is not a link; remove it first." >&2; exit 1; fi
  ln -sfn "$SKILL" "$link"
  echo "$host_name: $link -> $SKILL"
}

[ "$CLAUDE" = 1 ] && link_skill "Claude Code" "$HOME/.claude/skills"
[ "$CODEX" = 1 ] && link_skill "Codex" "$HOME/.codex/skills"

if [ "$REMOVE" = 0 ]; then
  cat <<EOF

Claude Desktop (optional) - add to claude_desktop_config.json:
{"mcpServers": {"video-story": {"command": "uv", "args": ["run", "--project", "$ROOT", "video-story", "mcp"]}}}

Start a new chat in the host and say: Start Video Story
EOF
fi
