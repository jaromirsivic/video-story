#!/usr/bin/env sh
# Register the Video Story skills (video-story, video-story-edit) with Claude Code, Codex
# and/or Cursor (symbolic links).
#   scripts/install-hosts.sh --claude [--codex] [--cursor] [--remove]
# Where each host looks for user skills (docs/installation.md, section 3, cites the pages):
#   --claude  ~/.claude/skills
#   --codex   ~/.agents/skills  (a link left in the legacy ~/.codex/skills is removed)
#   --cursor  ~/.cursor/skills, but only when Cursor does not already load the skill from
#             one of the folders it also reads: ~/.agents/skills, ~/.claude/skills, ~/.codex/skills
# Both skills are always handled together. Running it again changes nothing that is already
# right. --remove deletes the links of the named hosts (only links, never a real folder).
set -e
SCRIPT_DIR=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -P -- "$SCRIPT_DIR/.." && pwd -P)
SKILL_NAMES="video-story video-story-edit"
for name in $SKILL_NAMES; do
  [ -f "$ROOT/skills/$name/SKILL.md" ] || { echo "Skill directory not found: skills/$name" >&2; exit 1; }
done

CLAUDE=0; CODEX=0; CURSOR=0; REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --claude) CLAUDE=1 ;;
    --codex) CODEX=1 ;;
    --cursor) CURSOR=1 ;;
    --remove) REMOVE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
if [ "$CLAUDE" = 0 ] && [ "$CODEX" = 0 ] && [ "$CURSOR" = 0 ]; then
  echo "Pass --claude, --codex and/or --cursor (and --remove to unregister)." >&2; exit 2
fi

CLAUDE_DIR="$HOME/.claude/skills"
AGENTS_DIR="$HOME/.agents/skills"
LEGACY_CODEX_DIR="$HOME/.codex/skills"
CURSOR_DIR="$HOME/.cursor/skills"
# The other folders Cursor reads skills from (cursor.com/docs/skills, "Skill directories").
CURSOR_ALSO_READS="$AGENTS_DIR $CLAUDE_DIR $LEGACY_CODEX_DIR"

# own_link <path> <name>: true when <path> is a symlink to this checkout's skill <name>.
own_link() {
  [ -L "$1" ] || return 1
  target=$(CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P) || return 1
  [ "$target" = "$ROOT/skills/$2" ]
}

# remove_link <host> <link> <why>: prints and succeeds only when a link was removed.
remove_link() {
  if [ -L "$2" ]; then
    rm -f -- "$2"  # removes the link itself; its target stays untouched
    echo "$1: removed $2$3"
    return 0
  fi
  if [ -e "$2" ]; then echo "$1: left $2 alone (a real folder, not a link)"; fi
  return 1
}

create_link() {
  host_name=$1; skills_dir=$2; name=$3; skill="$ROOT/skills/$name"; link="$skills_dir/$name"
  if own_link "$link" "$name"; then echo "$host_name: unchanged $link -> $skill"; return; fi
  replaced=""
  if [ -L "$link" ]; then
    replaced=" (replaced a link to $(readlink "$link"))"
    rm -f -- "$link"
  elif [ -e "$link" ]; then
    echo "$link exists and is not a link; remove it first." >&2; exit 1
  fi
  mkdir -p "$skills_dir"
  ln -s "$skill" "$link"
  [ -f "$link/SKILL.md" ] || { echo "Link failed: $link" >&2; exit 1; }
  echo "$host_name: created $link -> $skill$replaced"
}

# cursor_source <name>: prints the first other folder's link Cursor already loads <name> from.
cursor_source() {
  for dir in $CURSOR_ALSO_READS; do
    if own_link "$dir/$1" "$1"; then echo "$dir/$1"; return 0; fi
  done
  return 1
}

cursor_duplicate_note() {
  [ "$CURSOR" = 1 ] && return 0  # the Cursor step of this run sorts it out
  if own_link "$CURSOR_DIR/$1" "$1" && cursor_source "$1" >/dev/null; then
    echo "  note: Cursor now finds $1 twice (also $CURSOR_DIR/$1); run with --cursor to drop that link."
  fi
}

cursor_lost_note() {
  { [ "$CURSOR" = 1 ] || [ ! -d "$HOME/.cursor" ]; } && return 0
  if ! cursor_source "$1" >/dev/null && ! own_link "$CURSOR_DIR/$1" "$1"; then
    echo "  note: Cursor no longer finds $1 either; run with --cursor if you use it there."
  fi
}

for name in $SKILL_NAMES; do
  if [ "$CLAUDE" = 1 ]; then
    link="$CLAUDE_DIR/$name"
    if [ "$REMOVE" = 1 ]; then
      if remove_link "Claude Code" "$link" ""; then cursor_lost_note "$name"
      elif [ ! -e "$link" ]; then echo "Claude Code: nothing to remove at $link"; fi
    else
      create_link "Claude Code" "$CLAUDE_DIR" "$name"
      cursor_duplicate_note "$name"
    fi
  fi
  if [ "$CODEX" = 1 ]; then
    link="$AGENTS_DIR/$name"; legacy="$LEGACY_CODEX_DIR/$name"
    if [ "$REMOVE" = 1 ]; then
      removed=0
      if remove_link "Codex" "$link" ""; then removed=1
      elif [ ! -e "$link" ]; then echo "Codex: nothing to remove at $link"; fi
      if remove_link "Codex" "$legacy" " (legacy location)"; then removed=1; fi
      if [ "$removed" = 1 ]; then cursor_lost_note "$name"; fi
    else
      create_link "Codex" "$AGENTS_DIR" "$name"
      # Codex lists two skills of the same name side by side; its current documentation
      # names only ~/.agents/skills, so an older link in ~/.codex/skills goes.
      remove_link "Codex" "$legacy" " (legacy location; Codex now reads ~/.agents/skills)" || true
      cursor_duplicate_note "$name"
    fi
  fi
  if [ "$CURSOR" = 1 ]; then
    link="$CURSOR_DIR/$name"
    if [ "$REMOVE" = 1 ]; then
      if ! remove_link "Cursor" "$link" "" && [ ! -e "$link" ]; then
        echo "Cursor: nothing to remove at $link"
      fi
      for dir in $CURSOR_ALSO_READS; do
        if own_link "$dir/$name" "$name"; then
          echo "  note: Cursor still loads $name from $dir/$name (registered for another host)"
        fi
      done
    elif source_link=$(cursor_source "$name"); then
      why="; Cursor already loads the skill from $source_link"
      if ! remove_link "Cursor" "$link" "$why"; then echo "Cursor: no link needed in $CURSOR_DIR$why"; fi
    else
      create_link "Cursor" "$CURSOR_DIR" "$name"
    fi
  fi
done

if [ "$REMOVE" = 0 ]; then
  echo ""
  echo "MCP server (optional; only for a host that cannot open the window from a command, see docs/installation.md):"
  if [ "$CLAUDE" = 1 ]; then
    cat <<EOF

Claude Desktop - add to claude_desktop_config.json:
{"mcpServers": {"video-story": {"command": "uv", "args": ["run", "--project", "$ROOT", "video-story", "mcp"]}}}
EOF
  fi
  if [ "$CODEX" = 1 ]; then
    cat <<EOF

Codex - add to ~/.codex/config.toml (or run: codex mcp add video-story -- uv run --project <path> video-story mcp):
[mcp_servers.video-story]
command = "uv"
args = ["run", "--project", "$ROOT", "video-story", "mcp"]
tool_timeout_sec = 120
EOF
  fi
  if [ "$CURSOR" = 1 ]; then
    cat <<EOF

Cursor - add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project):
{"mcpServers": {"video-story": {"type": "stdio", "command": "uv", "args": ["run", "--project", "$ROOT", "video-story", "mcp"]}}}
EOF
  fi
  echo ""
  echo "Start a new chat in the host and say: Start Video Story (or Edit Video Story)"
fi
