#!/usr/bin/env bash
# picks the delegation target (codex / claude). Shared by the codex-offload (Claude Code) and claude-offload (Codex) skills.
# usage: pick.sh [--main claude|codex]   (--main: the delegating side; default: the router's setting, claude unless changed)
#   If claude-codex-router ($CLAUDE_CODEX_ROUTER_DIR, default ~/.claude/codex-router) is installed, follows its decision (now --json).
#   When it is not installed, did not answer, or its answer is unreadable: codex (Codex first).
# Output is one line in English: "<codex|claude> <reason>". The exit code is always 0 (failing to pick does not stop the delegation).
set -u
MAIN=()
if [ "${1:-}" = --main ]; then
  case "${2:-}" in claude|codex) MAIN=(--main "$2");; *) echo "codex pick.sh: --main must be claude|codex; defaulting to Codex"; exit 0;; esac
fi
DIR="${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude/codex-router}"
ROUTER="$DIR/bin/router.py"
if [ ! -f "$ROUTER" ]; then
  echo "codex claude-codex-router not installed ($ROUTER); defaulting to Codex"
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "codex python3 not found; defaulting to Codex"
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
if d.get("claude_L") is not None:
    why += " (leftover probability Claude %.2f / Codex %.2f)" % (d["claude_L"], d["codex_L"])
for key, label in (("full", "full"), ("guard", "guard (main side)"), ("sub_tight", "kept on main (sub side)")):
    if d.get(key):
        why += "; %s: %s" % (label, ", ".join(d[key]))
if d.get("errors"):
    why += "; errors: " + " / ".join(d["errors"])
if d.get("notes"):
    why += "; notes: " + " / ".join(d["notes"])
print(p, "claude-codex-router:", why)
' 2>/dev/null)"
if [ -z "$LINE" ]; then
  echo "codex claude-codex-router gave no answer (rc=$RC); defaulting to Codex"
  exit 0
fi
echo "$LINE"
