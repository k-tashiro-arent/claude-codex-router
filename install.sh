#!/usr/bin/env bash
# claude-codex-router インストーラ（Linux / WSL2 / macOS。Windows ネイティブは対象外）
#
# やること:
#   1) 依存チェック（python3 は必須。codex と Claude Code の資格情報は無ければ警告だけ）
#   2) router.py と同梱の重み（weights-default.json）を $CLAUDE_CODEX_ROUTER_DIR（既定 ~/.claude/codex-router）に配置
#   3) 既定の config.json を配置（既存は上書きしない）
#   4) 5 分おきに `router.py sample` を呼ぶタイマーを置く
#        systemd のユーザータイマー → macOS の launchd → crontab の順に、使えるものを 1 つ
#        CLAUDE_CODEX_ROUTER_SCHEDULER=systemd|launchd|cron|none で選べる（none は置かない）
#   5) uninstall.sh を配置、版数を記録
# 使用量の記録（data/）は上書きしない。
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

# 1) 依存
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 が必要です。" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "ERROR: python3 は 3.8 以上が必要です（$(python3 --version 2>&1)）。" >&2
  exit 1
fi
PY="$(command -v python3)"
CODEX="$(command -v codex || true)"
[[ -n "$CODEX" ]] || echo "  WARNING: codex が PATH に無い。Codex の使用枠は読めません（入れた後に install.sh を再実行）。"
CRED="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
if [[ ! -f "$CRED" && "$(uname -s)" != "Darwin" ]]; then
  echo "  WARNING: $CRED が無い。Claude Code にログインするまで Claude の使用枠は読めません。"
fi

# 2) ファイル配置
mkdir -p "$DIR/bin" "$DIR/data"
cp "$SRC/bin/router.py" "$DIR/bin/router.py"
chmod +x "$DIR/bin/router.py"
cp "$SRC/defaults/weights-default.json" "$DIR/weights-default.json"
rm -f "$DIR/sigma-default.json"   # 前の版の表（使わない）

# 3) 既定設定（ユーザー設定は上書きしない）
[[ -f "$DIR/config.json" ]] || cp "$SRC/defaults/config.json" "$DIR/config.json"

# 4) タイマー
# タイマーから codex を見つけられるよう、今の codex の場所を PATH に入れる
path_dirs="/usr/local/bin:/usr/bin:/bin"
[[ "$(uname -s)" == "Darwin" ]] && path_dirs="/opt/homebrew/bin:$path_dirs"
[[ -n "$CODEX" ]] && path_dirs="$(dirname "$CODEX"):$path_dirs"

if [[ "$SCHED" == "auto" ]]; then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    SCHED=systemd
  elif [[ "$(uname -s)" == "Darwin" ]]; then
    SCHED=launchd
  elif command -v crontab >/dev/null 2>&1; then
    SCHED=cron
  else
    SCHED=none
    echo "  WARNING: systemd・launchd・crontab のどれも使えない。記録は router.py now を呼んだときだけ溜まります。"
  fi
fi

case "$SCHED" in
  systemd)
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/$NAME-sample.service" <<EOF
[Unit]
Description=$NAME: Claude と Codex の使用枠を読んで記録する

[Service]
Type=oneshot
Environment=PATH=$path_dirs
Environment=CLAUDE_CODEX_ROUTER_DIR=$DIR
ExecStart=$PY $DIR/bin/router.py sample
EOF
    cat > "$unit_dir/$NAME-sample.timer" <<EOF
[Unit]
Description=$NAME sample を 5 分おきに

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$NAME-sample.timer" >/dev/null
    echo "  timer  : systemd（$unit_dir/$NAME-sample.timer）"
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
    echo "  timer  : launchd（$plist）"
    ;;
  cron)
    line="*/5 * * * * PATH=$path_dirs CLAUDE_CODEX_ROUTER_DIR=$DIR $PY $DIR/bin/router.py sample >/dev/null 2>&1 # $NAME"
    { crontab -l 2>/dev/null | grep -v "# $NAME\$" || true; echo "$line"; } | crontab -
    echo "  timer  : crontab（5 分おき）"
    ;;
  none)
    echo "  timer  : 置かない（CLAUDE_CODEX_ROUTER_SCHEDULER=none）"
    ;;
  *)
    echo "ERROR: CLAUDE_CODEX_ROUTER_SCHEDULER は auto|systemd|launchd|cron|none（受け取った値: $SCHED）" >&2
    exit 1
    ;;
esac
printf '%s\n' "$SCHED" > "$DIR/.scheduler"

# 5) uninstall・版数
cp "$SRC/uninstall.sh" "$DIR/uninstall.sh"
chmod +x "$DIR/uninstall.sh"
printf '%s\n' "$VERSION" > "$DIR/.version"

echo ""
if [[ -z "$OLD_VER" ]]; then
  echo "インストール完了: v$VERSION"
elif [[ "$OLD_VER" != "$VERSION" ]]; then
  echo "更新完了: v$OLD_VER → v$VERSION"
else
  echo "再インストール完了: v$VERSION"
fi
echo "  今の判断       : python3 \"$DIR/bin/router.py\" now        （機械向けは now --json）"
echo "  過去の記録     : python3 \"$DIR/bin/router.py\" import     （会話ログの料金・Codex のログの使用率・claude-token-pace の記録を取り込む）"
echo "  設定           : $DIR/config.json"
echo "  アンインストール: bash \"$DIR/uninstall.sh\"（記録は残す）"
