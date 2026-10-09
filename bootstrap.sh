#!/usr/bin/env bash
# claude-codex-router one-liner installer (curl -fsSL .../bootstrap.sh | bash).
# Clones the repository into a temporary directory and runs the install.sh in it.
#
# Environment variables:
#   CLAUDE_CODEX_ROUTER_REPO  URL to clone from (default: the official repository)
#   CLAUDE_CODEX_ROUTER_REF   branch/tag (default: main; e.g. v0.1.0 for a fixed version)
#   CLAUDE_CODEX_ROUTER_DIR   install location (install.sh honours it as is)
# Dependencies: git (for clone) / python3 is checked by install.sh.
set -euo pipefail

REPO_URL="${CLAUDE_CODEX_ROUTER_REPO:-https://github.com/k-tashiro-arent/claude-codex-router.git}"
REF="${CLAUDE_CODEX_ROUTER_REF:-main}"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: 'git' is required (used for clone). Install git and run this again." >&2
  exit 1
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/claude-codex-router.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

echo "claude-codex-router bootstrap: cloning $REPO_URL ($REF) ..."
if ! git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$tmp/repo" 2>/dev/null; then
  # when REF is a commit SHA (--branch does not accept a SHA). Do a full clone and check it out
  git clone --quiet "$REPO_URL" "$tmp/repo"
  if ! git -C "$tmp/repo" checkout --quiet "$REF" 2>/dev/null; then
    echo "WARNING: ref '$REF' not found. Continuing the install on the default branch." >&2
  fi
fi

if [[ ! -f "$tmp/repo/install.sh" ]]; then
  echo "ERROR: install.sh not found in the clone (check URL/REF)." >&2
  exit 1
fi

bash "$tmp/repo/install.sh"
