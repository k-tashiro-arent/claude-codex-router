#!/usr/bin/env bash
# picks the delegation target (codex / claude). Shared by the codex-offload (Claude Code) and claude-offload (Codex) skills.
# usage: pick.sh [--main claude|codex]   (--main: the delegating side; default: the router's setting, claude unless changed)
#   If claude-codex-router ($CLAUDE_CODEX_ROUTER_DIR, default ~/.claude/codex-router) is installed, follows its decision (now --json).
#   When it is not installed, did not answer, or its answer is unreadable: codex (Codex first).
#   A flag file ($CLAUDE_CODEX_ROUTER_FORCE_FILE, default $CLAUDE_CODEX_ROUTER_DIR/force) holding claude or codex fixes the target to that side
#   without calling the router; with anything else in it, it is ignored (and the line says so).
# Output is one line in English: "<codex|claude> <reason>". The exit code is always 0 (failing to pick does not stop the delegation).
set -u
MAIN=()
if [ "${1:-}" = --main ]; then
  case "${2:-}" in claude|codex) MAIN=(--main "$2");; *) echo "codex pick.sh: --main must be claude|codex; defaulting to Codex"; exit 0;; esac
fi
DIR="${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude/codex-router}"
FORCE="${CLAUDE_CODEX_ROUTER_FORCE_FILE:-$DIR/force}"
EXTRA_NOTE=""
if [ -e "$FORCE" ]; then
  FIXED="$(head -n 1 "$FORCE" 2>/dev/null | tr -d '[:space:]')"
  case "$FIXED" in
    claude|codex) echo "$FIXED fixed by the flag file ($FORCE); remove it to let claude-codex-router choose"; exit 0;;
    *) EXTRA_NOTE="; flag file $FORCE ignored (write claude or codex in it)";;
  esac
fi
ROUTER="$DIR/bin/router.py"
if [ ! -f "$ROUTER" ]; then
  echo "codex claude-codex-router not installed ($ROUTER); defaulting to Codex$EXTRA_NOTE"
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "codex python3 not found; defaulting to Codex$EXTRA_NOTE"
  exit 0
fi
# the timeout is measured with python3 (the timeout command is not standard on macOS). Overriding the seconds is for tests
OUT="$(python3 -c '
import subprocess, sys
try:
    r = subprocess.run([sys.executable, sys.argv[1], "now", "--json"] + sys.argv[3:], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=float(sys.argv[2]))
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.stdout.buffer.write(r.stdout)
sys.exit(r.returncode)
' "$ROUTER" "${CLAUDE_CODEX_ROUTER_PICK_TIMEOUT:-90}" ${MAIN[@]+"${MAIN[@]}"} 2>/dev/null)"
RC=$?
LINE="$(printf '%s' "$OUT" | python3 -c '
import json, sys
d = json.load(sys.stdin)
p = d.get("pick")
if p not in ("claude", "codex"):
    sys.exit(1)
why = d.get("reason") or ""
if d.get("claude_left_usd") is not None and d.get("codex_left_usd") is not None:
    why += " (expected leftover Claude $%.0f / Codex $%.0f)" % (d["claude_left_usd"], d["codex_left_usd"])
elif d.get("claude_L") is not None or d.get("codex_L") is not None:   # one side may have none (no exhaust probability)
    why += " (leftover probability Claude %s / Codex %s)" % tuple("unknown" if d.get(k) is None else "%.2f" % d[k] for k in ("claude_L", "codex_L"))
for key, label in (("full", "full"), ("guard", "guard (main side)")):
    if d.get(key):
        why += "; %s: %s" % (label, ", ".join(d[key]))
if d.get("errors"):
    why += "; errors: " + " / ".join(d["errors"])
if d.get("notes"):
    why += "; notes: " + " / ".join(d["notes"])
print(p, "claude-codex-router:", why)
' 2>/dev/null)"
if [ -z "$LINE" ]; then
  echo "codex claude-codex-router gave no answer (rc=$RC); defaulting to Codex$EXTRA_NOTE"
  exit 0
fi
echo "$LINE$EXTRA_NOTE"
