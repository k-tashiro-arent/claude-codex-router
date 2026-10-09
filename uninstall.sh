#!/usr/bin/env bash
# claude-codex-router アンインストーラ。
#   タイマー（systemd / launchd / crontab のどれでも）を止めて外し、router.py を消す。
#   使用量の記録（data/）と設定（config.json）は残す（完全に消すのは最後の案内どおり手で）。
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
  echo "  systemd のタイマーを外した"
fi
# linger（このインストーラが有効にしたときだけ戻す。ほかの用途で有効にしていたものは触らない）
if [[ -f "$DIR/.linger" ]]; then
  loginctl disable-linger "$(id -un)" >/dev/null 2>&1 && echo "  linger を戻した（無効に）" || echo "  linger を戻せなかった。手で: loginctl disable-linger $(id -un)"
  rm -f "$DIR/.linger"
fi
# launchd
plist="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ -f "$plist" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  rm -f "$plist"
  echo "  launchd のエージェントを外した"
fi
# crontab
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q "# $NAME\$"; then
  crontab -l 2>/dev/null | grep -v "# $NAME\$" | crontab -
  echo "  crontab の行を外した"
fi

rm -f "$DIR/bin/router.py" "$DIR/weights-default.json" "$DIR/sigma-default.json" "$DIR/.version" "$DIR/.scheduler"
rmdir "$DIR/bin" 2>/dev/null || true
echo "アンインストール完了。記録と設定は残しています: $DIR"
echo "  完全に消すには: rm -rf \"$DIR\""
rm -f "$DIR/uninstall.sh"
