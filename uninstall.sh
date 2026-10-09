#!/usr/bin/env bash
# claude-codex-router uninstaller.
#   Stops and removes the timer (whichever of systemd / launchd / crontab) and deletes router.py.
#   Removes the delegation skills the installer placed (unless edited since) and the Codex rule it wrote.
#   The usage records (data/) and settings (config.json) are kept (to delete everything, do it by hand as the final message says).
set -u

DIR="${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude/codex-router}"
NAME="claude-codex-router"
LABEL="io.github.k-tashiro-arent.claude-codex-router"

# systemd
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ -f "$unit_dir/$NAME-sample.timer" ]]; then
  systemctl --user disable --now "$NAME-sample.timer" >/dev/null 2>&1 || true
  rm -f "$unit_dir/$NAME-sample.timer" "$unit_dir/$NAME-sample.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  echo "  removed the systemd timer"
fi
# linger (reverted only when the installer enabled it; left alone if it was enabled for other uses)
if [[ -f "$DIR/.linger" ]]; then
  loginctl disable-linger "$(id -un)" >/dev/null 2>&1 && echo "  reverted linger (disabled)" || echo "  could not revert linger. By hand: loginctl disable-linger $(id -un)"
  rm -f "$DIR/.linger"
fi
# launchd
plist="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ -f "$plist" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  rm -f "$plist"
  echo "  removed the launchd agent"
fi
# crontab
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "# $NAME\$"; then
  crontab -l 2>/dev/null | grep -v "# $NAME\$" | crontab -
  echo "  removed the crontab line"
fi

# delegation skills: only the ones this installer placed (recorded in .skills), and only if nobody edited them
if [[ -f "$DIR/.skills" ]]; then
  while read -r path hash; do
    [[ -d "$path" ]] || continue
    now="$(python3 - "$path" <<'PY'
import hashlib, os, sys
h = hashlib.sha256()
for d, dirs, files in sorted(os.walk(sys.argv[1])):
    dirs.sort()
    for f in sorted(files):
        p = os.path.join(d, f)
        h.update(os.path.relpath(p, sys.argv[1]).encode() + b'\0' + open(p, 'rb').read() + b'\0')
print(h.hexdigest())
PY
)"
    if [[ "$now" == "$hash" ]]; then
      rm -rf "$path"
      echo "  removed the skill $path"
    else
      echo "  kept the skill $path (edited after install; delete it by hand if you do not need it)"
    fi
  done < "$DIR/.skills"
  rm -f "$DIR/.skills"
  echo "  if you added a line for the skills to CLAUDE.md or AGENTS.md, remove it by hand"
fi
rules="${CODEX_HOME:-$HOME/.codex}/rules/$NAME.rules"
if [[ -f "$rules" ]]; then
  rm -f "$rules"
  echo "  removed the Codex rule $rules"
fi
rm -f "$DIR/bin/router.py" "$DIR/weights-default.json" "$DIR/sigma-default.json" "$DIR/.version" "$DIR/.scheduler"
rmdir "$DIR/bin" 2>/dev/null || true
echo "Uninstall complete. The records and settings are kept: $DIR"
echo "  to delete everything: rm -rf \"$DIR\""
rm -f "$DIR/uninstall.sh"
