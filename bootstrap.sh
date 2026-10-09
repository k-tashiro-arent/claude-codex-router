#!/usr/bin/env bash
# claude-codex-router ワンライナー・インストーラ（curl -fsSL .../bootstrap.sh | bash）。
# リポジトリを一時ディレクトリに clone し、その中の install.sh を実行する。
#
# 環境変数:
#   CLAUDE_CODEX_ROUTER_REPO  clone 元 URL（既定: 公式リポジトリ）
#   CLAUDE_CODEX_ROUTER_REF   ブランチ/タグ（既定: main。例 v0.1.0 で固定版）
#   CLAUDE_CODEX_ROUTER_DIR   設置先（install.sh がそのまま尊重）
# 依存: git（clone 用）／python3 は install.sh が確認する。
set -euo pipefail

REPO_URL="${CLAUDE_CODEX_ROUTER_REPO:-https://github.com/k-tashiro-arent/claude-codex-router.git}"
REF="${CLAUDE_CODEX_ROUTER_REF:-main}"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: 'git' が必要です（clone に使用）。git を入れてから再実行してください。" >&2
  exit 1
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/claude-codex-router.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

echo "claude-codex-router bootstrap: cloning $REPO_URL ($REF) ..."
if ! git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$tmp/repo" 2>/dev/null; then
  # REF がコミット SHA のとき（--branch は SHA を受け付けない）。full clone して checkout する
  git clone --quiet "$REPO_URL" "$tmp/repo"
  if ! git -C "$tmp/repo" checkout --quiet "$REF" 2>/dev/null; then
    echo "WARNING: ref '$REF' が見つかりません。既定ブランチのまま install を続行します。" >&2
  fi
fi

if [[ ! -f "$tmp/repo/install.sh" ]]; then
  echo "ERROR: clone 先に install.sh がありません（URL/REF を確認してください）。" >&2
  exit 1
fi

bash "$tmp/repo/install.sh"
