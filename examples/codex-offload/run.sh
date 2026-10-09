#!/usr/bin/env bash
# codex-offload: hands part of a request to codex exec once, and observes the result and the changed files
# usage: run.sh <read|write> <cwd> <outdir> [--add-dir DIR]... [-m MODEL] [-e EFFORT] < prompt.md
#   read  = -s read-only (investigation, review, triage). write = -s workspace-write (editing, running tests)
#   leaves prompt.md / events.jsonl / stderr.txt / last.txt / summary.txt in outdir. Only the summary goes to stdout.
set -u
MODE="${1:?read|write}"; CWD="${2:?cwd}"; OUT="${3:?outdir}"; shift 3
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --add-dir) EXTRA+=(--add-dir "$2"); shift 2;;
    -m) EXTRA+=(-m "$2"); shift 2;;
    -e) EXTRA+=(-c "model_reasoning_effort=\"$2\""); shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
case "$MODE" in read) SB=read-only;; write) SB=workspace-write;; *) echo "mode must be read|write" >&2; exit 2;; esac
mkdir -p "$OUT"
HERE="$(cd "$(dirname "$0")" && pwd)"
{ cat "$HERE/preamble.md"; echo; echo "## Work mode: $MODE (sandbox=$SB, working directory=$CWD)"; echo; cat; } > "$OUT/prompt.md"
ISGIT=0; git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 && ISGIT=1
[ $ISGIT = 1 ] && git -C "$CWD" status --porcelain > "$OUT/status.before"
START=$(date +%s)
codex exec --skip-git-repo-check --color never --json -s "$SB" -C "$CWD" "${EXTRA[@]}" -o "$OUT/last.txt" - \
  < "$OUT/prompt.md" > "$OUT/events.jsonl" 2> "$OUT/stderr.txt"
RC=$?
{
  echo "rc=$RC mode=$MODE secs=$(( $(date +%s) - START )) events=$(wc -l < "$OUT/events.jsonl")"
  # signs that Codex is unavailable (usage limit, rate limit, authentication, connection)
  if [ ! -s "$OUT/last.txt" ] && grep -Eiqs 'usage limit|usage_limit|hit your (usage|weekly|daily) limit|spend cap|out of credits|rate limit|rate_limit|too many requests|\b429\b|quota|unauthorized|\b401\b|not logged in|codex login|invalid api key|econnrefused|enotfound|connection refused|network error|stream disconnected|internal server error|\b50[234]\b|overloaded|service unavailable' "$OUT/stderr.txt" "$OUT/events.jsonl"; then
    echo "UNAVAILABLE: $(grep -Eiohs 'usage limit|rate limit|too many requests|quota|unauthorized|not logged in|connection refused|stream disconnected|service unavailable|overloaded|\b50[234]\b|\b429\b|\b401\b' "$OUT/stderr.txt" "$OUT/events.jsonl" | head -1)"
  elif [ ! -s "$OUT/last.txt" ]; then
    echo "NO-RESULT: the last message is empty (end of stderr: $(tail -c 300 "$OUT/stderr.txt" | tr '\n' ' '))"
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
