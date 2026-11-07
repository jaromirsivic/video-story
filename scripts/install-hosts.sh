#!/usr/bin/env sh
# Register the Video Story skills (video-story, video-story-edit) with Claude Code and/or Codex (symlinks).
#   scripts/install-hosts.sh --claude [--codex] [--remove]
set -e
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SKILL_NAMES="video-story video-story-edit"
for name in $SKILL_NAMES; do
  [ -f "$ROOT/skills/$name/SKILL.md" ] || { echo "Skill directory not found: skills/$name" >&2; exit 1; }
done

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
  host_name=$1; skills_dir=$2; name=$3; SKILL="$ROOT/skills/$name"; link="$skills_dir/$name"
  if [ "$REMOVE" = 1 ]; then
    if [ -L "$link" ]; then rm "$link"; echo "$host_name: removed $link"; else echo "$host_name: nothing to remove at $link"; fi
    return
  fi
  mkdir -p "$skills_dir"
  if [ -e "$link" ] && [ ! -L "$link" ]; then echo "$link exists and is not a link; remove it first." >&2; exit 1; fi
  ln -sfn "$SKILL" "$link"
  echo "$host_name: $link -> $SKILL"
}

for name in $SKILL_NAMES; do
  [ "$CLAUDE" = 1 ] && link_skill "Claude Code" "$HOME/.claude/skills" "$name"
  [ "$CODEX" = 1 ] && link_skill "Codex" "$HOME/.codex/skills" "$name"
done

if [ "$REMOVE" = 0 ]; then
  cat <<EOF

Claude Desktop (optional) - add to claude_desktop_config.json:
{"mcpServers": {"video-story": {"command": "uv", "args": ["run", "--project", "$ROOT", "video-story", "mcp"]}}}

Start a new chat in the host and say: Start Video Story (or Edit Video Story)
EOF
fi
