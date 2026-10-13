#!/usr/bin/env bash
# claude-codex-router installer (Linux / WSL2 / macOS; native Windows is not supported)
#
# What it does:
#   1) Dependency check (python3 is required; missing Codex or Claude Code login info only gives a warning)
#   2) Places router.py in $CLAUDE_CODEX_ROUTER_DIR (default ~/.claude/codex-router)
#   3) Places the default config.json (an existing one is not overwritten)
#   4) Sets up a timer that calls `router.py sample` every 5 minutes
#        the first usable one, in the order systemd user timer → macOS launchd → crontab
#        with systemd, enables linger (the user's systemd starts at boot and the timer runs even when not logged in.
#        It does not stop after a WSL restart either). If this installer enabled it, uninstall.sh reverts it
#        selectable with CLAUDE_CODEX_ROUTER_SCHEDULER=systemd|launchd|cron|none (none sets up nothing)
#   5) Optionally installs the delegation skills (asks; CLAUDE_CODEX_ROUTER_SKILLS=claude|codex|both|none answers without asking)
#        claude: codex-offload for Claude Code (${CLAUDE_CONFIG_DIR:-~/.claude}/skills/codex-offload)
#        codex : claude-offload for Codex (~/.agents/skills/claude-offload) and a Codex rule that lets its two scripts run outside the sandbox
#        skills this installer placed and nobody edited are updated on reinstall; edited or someone else's skills are kept
#   6) Places uninstall.sh and records the version
# The usage records (data/) are not overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude/codex-router}"
SCHED="${CLAUDE_CODEX_ROUTER_SCHEDULER:-auto}"
VERSION="$(cat "$SRC/VERSION" 2>/dev/null || echo "0.0.0")"
OLD_VER="$([[ -f "$DIR/.version" ]] && cat "$DIR/.version" 2>/dev/null || echo "")"
NAME="claude-codex-router"
LABEL="io.github.k-tashiro-arent.claude-codex-router"

echo "$NAME installer (v$VERSION)"
echo "  source : $SRC"
echo "  target : $DIR"

# 1) Dependencies
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "ERROR: python3 3.8 or later is required ($(python3 --version 2>&1))." >&2
  exit 1
fi
PY="$(command -v python3)"
CODEX_AUTH="${CODEX_HOME:-$HOME/.codex}/auth.json"
[[ -f "$CODEX_AUTH" ]] || echo "  WARNING: $CODEX_AUTH not found. The Codex usage windows cannot be read until you log in to Codex with a ChatGPT plan."
CRED="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
if [[ ! -f "$CRED" && "$(uname -s)" != "Darwin" ]]; then
  echo "  WARNING: $CRED not found. The Claude usage windows cannot be read until you log in to Claude Code."
fi

# 2) Place the files
mkdir -p "$DIR/bin" "$DIR/data"
cp "$SRC/bin/router.py" "$DIR/bin/router.py"
chmod +x "$DIR/bin/router.py"
rm -f "$DIR/sigma-default.json" "$DIR/weights-default.json" "$DIR/data/weights.json"   # tables of earlier versions (not used)

# 3) Default settings (the user's settings are not overwritten)
[[ -f "$DIR/config.json" ]] || cp "$SRC/defaults/config.json" "$DIR/config.json"

# 4) Timer
path_dirs="/usr/local/bin:/usr/bin:/bin"
# the timer does not inherit the login shell's environment variables, so if the login info locations were changed, write those values in
extra_env=()
[[ -n "${CODEX_HOME:-}" ]] && extra_env+=("CODEX_HOME=$CODEX_HOME")
[[ -n "${CLAUDE_CONFIG_DIR:-}" ]] && extra_env+=("CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR")
[[ -n "${TOKEN_PACE_DIR:-}" ]] && extra_env+=("TOKEN_PACE_DIR=$TOKEN_PACE_DIR")
[[ "$(uname -s)" == "Darwin" ]] && path_dirs="/opt/homebrew/bin:$path_dirs"

if [[ "$SCHED" == "auto" ]]; then
  # do not decide by whether the user's systemd is running now (without linger it is not running, e.g. after a WSL restart). Decide by whether PID 1 is systemd
  if command -v systemctl >/dev/null 2>&1 && [[ "$(cat /proc/1/comm 2>/dev/null)" == "systemd" ]]; then
    SCHED=systemd
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    SCHED=launchd
  elif command -v crontab >/dev/null 2>&1; then
    SCHED=cron
  else
    SCHED=none
    echo "  WARNING: none of systemd, launchd or crontab is available. Call router.py sample periodically yourself (otherwise the usage records get gaps, and updating the prices is delayed)."
  fi
fi

case "$SCHED" in
  systemd)
    me="$(id -un)"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if command -v loginctl >/dev/null 2>&1 && [[ "$(loginctl show-user "$me" -p Linger --value 2>/dev/null)" != "yes" ]]; then
      if loginctl enable-linger "$me" >/dev/null 2>&1; then
        : > "$DIR/.linger"   # this installer enabled it (uninstall.sh reverts it)
        echo "  linger : enabled (the user's systemd starts at boot)"
      else
        echo "  WARNING: could not enable linger. After a restart, the timer does not run until you log in."
        echo "           by hand: loginctl enable-linger $me   (if that fails, sudo loginctl enable-linger $me)"
      fi
    fi
    # right after linger is enabled, wait a little for the user's systemd to start
    for _ in $(seq 20); do
      systemctl --user show-environment >/dev/null 2>&1 && break
      sleep 0.5
    done
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/$NAME-sample.service" <<EOF
[Unit]
Description=$NAME: read and record the Claude and Codex usage windows

[Service]
Type=oneshot
Environment=PATH=$path_dirs
Environment=CLAUDE_CODEX_ROUTER_DIR=$DIR
$(for e in ${extra_env[@]+"${extra_env[@]}"}; do echo "Environment=$e"; done)
ExecStart=$PY $DIR/bin/router.py sample
EOF
    cat > "$unit_dir/$NAME-sample.timer" <<EOF
[Unit]
Description=$NAME sample every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    if systemctl --user daemon-reload >/dev/null 2>&1 && systemctl --user enable --now "$NAME-sample.timer" >/dev/null 2>&1; then
      echo "  timer  : systemd ($unit_dir/$NAME-sample.timer)"
    else
      echo "  WARNING: could not connect to the user's systemd, so the timer could not be registered (the definitions were placed in $unit_dir)."
      echo "           in a logged-in terminal: systemctl --user daemon-reload && systemctl --user enable --now $NAME-sample.timer"
    fi
    ;;
  launchd)
    agent_dir="$HOME/Library/LaunchAgents"
    plist="$agent_dir/$LABEL.plist"
    mkdir -p "$agent_dir"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/bin/router.py</string><string>sample</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$path_dirs</string>
$(for e in ${extra_env[@]+"${extra_env[@]}"}; do echo "    <key>${e%%=*}</key><string>${e#*=}</string>"; done)
    <key>CLAUDE_CODEX_ROUTER_DIR</key><string>$DIR</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/sample.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/sample.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "  timer  : launchd ($plist)"
    ;;
  cron)
    line="*/5 * * * * PATH=$path_dirs CLAUDE_CODEX_ROUTER_DIR=$DIR ${extra_env[*]+${extra_env[*]}} $PY $DIR/bin/router.py sample >/dev/null 2>&1 # $NAME"
    { crontab -l 2>/dev/null | grep -v "# $NAME\$" || true; echo "$line"; } | crontab -
    echo "  timer  : crontab (every 5 minutes)"
    ;;
  none)
    echo "  timer  : none (CLAUDE_CODEX_ROUTER_SCHEDULER=none)"
    ;;
  *)
    echo "ERROR: CLAUDE_CODEX_ROUTER_SCHEDULER must be auto|systemd|launchd|cron|none (got: $SCHED)" >&2
    exit 1
    ;;
esac
printf '%s\n' "$SCHED" > "$DIR/.scheduler"

# 5) Delegation skills (optional)
CLAUDE_SKILLS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
CODEX_SKILLS="$HOME/.agents/skills"
CODEX_RULES="${CODEX_HOME:-$HOME/.codex}/rules/$NAME.rules"
SKILLS_FILE="$DIR/.skills"   # "<path> <hash>" for each skill this installer placed
TTY=0; (exec 3</dev/tty) 2>/dev/null && TTY=1
TILDE='~'
tree_hash() { "$PY" - "$1" <<'PY'
import hashlib, os, sys
h = hashlib.sha256()
for d, dirs, files in sorted(os.walk(sys.argv[1])):
    dirs.sort()
    for f in sorted(files):
        p = os.path.join(d, f)
        h.update(os.path.relpath(p, sys.argv[1]).encode() + b'\0' + open(p, 'rb').read() + b'\0')
print(h.hexdigest())
PY
}
recorded_hash() { [[ -f "$SKILLS_FILE" ]] && awk -v p="$1" '$1 == p {print $2}' "$SKILLS_FILE" || true; }
record_hash() {
  { [[ -f "$SKILLS_FILE" ]] && awk -v p="$1" '$1 != p' "$SKILLS_FILE"; printf '%s %s\n' "$1" "$2"; } > "$SKILLS_FILE.tmp"
  mv "$SKILLS_FILE.tmp" "$SKILLS_FILE"
}
ask_yes() {  # ask_yes <question>: yes only for an explicit y on the terminal
  local a=""
  [[ $TTY == 1 ]] || return 1
  printf '%s [y/N] ' "$1" > /dev/tty
  read -r a < /dev/tty || return 1
  [[ "$a" == [yY]* ]]
}
want_skill() {  # want_skill <claude|codex> <question>
  local dst="$3"
  if [[ -n "$(recorded_hash "$dst")" && -d "$dst" ]]; then return 0; fi   # placed by this installer before: keep it up to date
  case "${CLAUDE_CODEX_ROUTER_SKILLS:-}" in
    both) return 0;; none) return 1;;
    claude|codex) [[ "$CLAUDE_CODEX_ROUTER_SKILLS" == "$1" ]]; return;;
    "") ask_yes "$2";;
    *) echo "  WARNING: CLAUDE_CODEX_ROUTER_SKILLS must be claude|codex|both|none (got: $CLAUDE_CODEX_ROUTER_SKILLS); no skill installed"; return 1;;
  esac
}
install_skill() {  # install_skill <src dir> <dst dir> <skills dir under ~ written in SKILL.md>; returns 0 when the skill from this repo is in place
  local src="$1" dst="$2" written rec now bak
  written="$TILDE/$3"
  rec="$(recorded_hash "$dst")"
  if [[ -d "$dst" ]]; then
    now="$(tree_hash "$dst")"
    if [[ -n "$rec" && "$now" != "$rec" ]]; then
      echo "  skill  : $dst was edited after install; kept as is (not updated)"
      return 1
    elif [[ -z "$rec" ]]; then
      if [[ "${CLAUDE_CODEX_ROUTER_SKILLS_REPLACE:-}" == yes ]] || { [[ -z "${CLAUDE_CODEX_ROUTER_SKILLS_REPLACE:-}" ]] && ask_yes "$dst already exists. Replace it (the old one is moved aside)?"; }; then
        bak="$dst.bak-$(date +%Y%m%d%H%M%S)"
        mv "$dst" "$bak"
        echo "  skill  : moved the existing $dst to $bak"
      else
        echo "  skill  : $dst already exists; kept as is"
        return 1
      fi
    fi
  fi
  rm -rf "$dst"
  mkdir -p "$(dirname "$dst")"
  cp -R "$src" "$dst"
  chmod +x "$dst"/*.sh
  # SKILL.md gets the absolute path (the Codex rule matches the command as written, and CLAUDE_CONFIG_DIR may move the skills dir)
  sed "s|$written/|$(dirname "$dst")/|g" "$dst/SKILL.md" > "$dst/SKILL.md.tmp" && mv "$dst/SKILL.md.tmp" "$dst/SKILL.md"
  record_hash "$dst" "$(tree_hash "$dst")"
  echo "  skill  : $dst"
}
if want_skill claude "Install the Claude Code skill codex-offload (Claude Code delegates to Codex or a Claude subagent, as the router picks)?" "$CLAUDE_SKILLS/codex-offload"; then
  if install_skill "$SRC/examples/codex-offload" "$CLAUDE_SKILLS/codex-offload" ".claude/skills"; then
    echo "           to have Claude Code use it, add to ${CLAUDE_CONFIG_DIR:-~/.claude}/CLAUDE.md: - When you split off work and delegate it, follow the codex-offload skill."
  fi
fi
if want_skill codex "Install the Codex skill claude-offload (Codex delegates to Claude Code, as the router picks; adds a Codex rule that lets its two scripts run outside the sandbox)?" "$CODEX_SKILLS/claude-offload"; then
  if install_skill "$SRC/examples/claude-offload" "$CODEX_SKILLS/claude-offload" ".agents/skills"; then
    mkdir -p "$(dirname "$CODEX_RULES")"
    S="$CODEX_SKILLS/claude-offload"
    cat > "$CODEX_RULES" <<EOF
# written by the $NAME installer (removed by uninstall.sh): the claude-offload skill's two scripts run outside the Codex sandbox
prefix_rule(pattern=["$S/pick.sh"], decision="allow", justification="$NAME: read both usage windows to pick the delegation target (needs the network and $DIR)")
prefix_rule(pattern=["$S/run.sh"], decision="allow", justification="claude-offload: hand work to Claude Code (needs the network and ~/.claude)")
EOF
    echo "  rule   : $CODEX_RULES (restart Codex to load it)"
    echo "           to have Codex use it, add to ${CODEX_HOME:-~/.codex}/AGENTS.md: - When you split off work and delegate it, follow the claude-offload skill."
  fi
fi

# 6) uninstall and version
cp "$SRC/uninstall.sh" "$DIR/uninstall.sh"
chmod +x "$DIR/uninstall.sh"
printf '%s\n' "$VERSION" > "$DIR/.version"

echo ""
if [[ -z "$OLD_VER" ]]; then
  echo "Install complete: v$VERSION"
elif [[ "$OLD_VER" != "$VERSION" ]]; then
  echo "Update complete: v$OLD_VER → v$VERSION"
else
  echo "Reinstall complete: v$VERSION"
fi
echo "  decision now    : python3 \"$DIR/bin/router.py\" now        (now --json for machines)"
echo "  settings        : $DIR/config.json"
echo "  uninstall       : bash \"$DIR/uninstall.sh\" (keeps the records)"
