#!/bin/bash
# Install device_tests skills as symlinks into ~/.claude/skills/
# Run from any directory — resolves paths relative to this script.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_SRC="$SCRIPT_DIR/skills"
SKILLS_DST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

if [ ! -d "$SKILLS_SRC" ]; then
    echo "ERROR: skills/ directory not found at $SKILLS_SRC"
    exit 1
fi

installed=0
for src in "$SKILLS_SRC"/*.md; do
    [ -f "$src" ] || continue
    name="$(basename "$src" .md)"
    dst_dir="$SKILLS_DST/$name"
    dst_file="$dst_dir/SKILL.md"

    mkdir -p "$dst_dir"

    if [ -L "$dst_file" ]; then
        rm "$dst_file"
    elif [ -f "$dst_file" ]; then
        echo "  SKIP $name (non-symlink SKILL.md exists, back up manually)"
        continue
    fi

    ln -s "$src" "$dst_file"
    echo "  OK   $name -> $src"
    installed=$((installed + 1))
done

echo ""
echo "Installed $installed skill(s) into $SKILLS_DST"
