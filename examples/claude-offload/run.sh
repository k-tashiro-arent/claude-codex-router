#!/usr/bin/env bash
# claude-offload: hands part of a request to Claude Code (claude -p) once, and observes the result and the changed files
# usage: run.sh <read|write> <cwd> <outdir> [--add-dir DIR]... [-m MODEL] [REQUEST_FILE]   (the request comes from REQUEST_FILE, else stdin)
#   read  = --permission-mode default: reading and searching only (in -p, tools that need permission are refused)
#   write = --permission-mode bypassPermissions: editing files and running commands (tests, builds)
#   leaves prompt.md / result.json / stderr.txt / last.txt / summary.txt in outdir. Only the summary goes to stdout.
#   It needs the network and ~/.claude, so under Codex it must run outside the sandbox (the installer adds a Codex rule for this file).
set -u
MODE="${1:?read|write}"; CWD="${2:?cwd}"; OUT="${3:?outdir}"; shift 3
EXTRA=(); REQ=""
while [ $# -gt 0 ]; do
  case "$1" in
    --add-dir) EXTRA+=(--add-dir "$2"); shift 2;;
    -m) EXTRA+=(--model "$2"); shift 2;;
    -*) echo "unknown arg: $1" >&2; exit 2;;
    *) REQ="$1"; shift;;
  esac
done
case "$MODE" in
  read) PERM=(--permission-mode default --disallowedTools "Edit Write NotebookEdit");;
  write) PERM=(--permission-mode bypassPermissions);;
  *) echo "mode must be read|write" >&2; exit 2;;
esac
if ! command -v claude >/dev/null 2>&1; then
  echo "UNAVAILABLE: claude (Claude Code) not found"
  exit 127
fi
mkdir -p "$OUT"
HERE="$(cd "$(dirname "$0")" && pwd)"
{ cat "$HERE/preamble.md"; echo; echo "## Work mode: $MODE (${PERM[1]}, working directory=$CWD)"; echo; if [ -n "$REQ" ]; then cat "$REQ"; else cat; fi; } > "$OUT/prompt.md"
ISGIT=0; git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 && ISGIT=1
[ $ISGIT = 1 ] && git -C "$CWD" status --porcelain > "$OUT/status.before"
START=$(date +%s)
(cd "$CWD" && claude -p --output-format json "${PERM[@]}" "${EXTRA[@]}" < "$OUT/prompt.md") > "$OUT/result.json" 2> "$OUT/stderr.txt"
RC=$?
# last.txt = the final message; is_error / api_error_status tell whether Claude Code could not answer
ERR="$(python3 - "$OUT/result.json" "$OUT/last.txt" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(0)
open(sys.argv[2], 'w').write('' if d.get('is_error') else (d.get('result') or ''))
if d.get('is_error'):
    print(f"{d.get('subtype')} {d.get('api_error_status') or ''} {(d.get('result') or '')[:200]}".strip())
PY
)"
{
  echo "rc=$RC mode=$MODE secs=$(( $(date +%s) - START ))"
  # signs that Claude Code is unavailable (usage limit, rate limit, authentication, connection)
  if [ ! -s "$OUT/last.txt" ] && printf '%s\n' "$ERR" | cat - "$OUT/stderr.txt" | grep -Eiq 'usage limit|limit reached|rate limit|rate_limit|too many requests|\b429\b|overloaded|\b529\b|unauthorized|\b401\b|\b403\b|not logged in|/login|invalid api key|credit balance|can.t reach|econnrefused|enotfound|eai_again|connection|network|\b50[234]\b|service unavailable'; then
    echo "UNAVAILABLE: $(printf '%s\n' "$ERR" | cat - "$OUT/stderr.txt" | grep -v '^$' | head -1 | cut -c1-200)"
  elif [ ! -s "$OUT/last.txt" ]; then
    echo "NO-RESULT: the last message is empty (${ERR:-end of stderr: $(tail -c 300 "$OUT/stderr.txt" | tr '\n' ' ')})"
  fi
  if [ $ISGIT = 1 ]; then
    git -C "$CWD" status --porcelain > "$OUT/status.after"
    CH=$(diff "$OUT/status.before" "$OUT/status.after" | grep -E '^[<>]' | sed 's/^[<>] //' | sort -u)
    echo "changed-files(status diff):"
    if [ -n "$CH" ]; then printf '%s\n' "$CH" | while IFS= read -r l; do echo "  $l"; done; else echo "  (none)"; fi
    if [ "$MODE" = read ] && [ -n "$CH" ]; then echo "WARNING: the working tree changed in read mode"; fi
  fi
  echo "result: $OUT/last.txt"
} > "$OUT/summary.txt"
cat "$OUT/summary.txt"
exit $RC
