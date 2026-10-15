#!/usr/bin/env bash
# picks the delegation target (codex / claude). Shared by the codex-offload (Claude Code) and claude-offload (Codex) skills.
# usage: pick.sh [--main claude|codex]   (--main: the delegating side; default: the router's setting, claude unless changed)
#   If claude-codex-router ($CLAUDE_CODEX_ROUTER_DIR, default ~/.claude-codex-router) is installed, follows its decision (now --json).
#   When it is not installed, did not answer, or its answer is unreadable: the side that is logged in when only one is; otherwise (both or neither)
#   tie_default in the router's config.json (codex if it cannot be read), as the router does.
#   A flag file ($CLAUDE_CODEX_ROUTER_FORCE_FILE, default $CLAUDE_CODEX_ROUTER_DIR/force) holding claude or codex fixes the target to that side
#   without calling the router; with anything else in it, it is ignored (and the line says so).
# Output is one line in English: "<codex|claude> <reason>". The exit code is always 0 (failing to pick does not stop the delegation).
set -u
# the side to fall back to without the router's answer: never one that is not logged in (work cannot be handed to it)
codex_in() {
  local f="${CODEX_HOME:-$HOME/.codex}/auth.json"
  [ -f "$f" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import json, sys; sys.exit(0 if (json.load(open(sys.argv[1])).get("tokens") or {}).get("access_token") else 1)' "$f" 2>/dev/null
}
claude_in() {
  [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json" ] && return 0
  [ "$(uname -s)" = Darwin ] && security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1
}
DIR="${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude-codex-router}"
tie_default() {   # tie_default in the router's settings (codex when it cannot be read)
  DEF="$(python3 -c 'import json, sys; v = json.load(open(sys.argv[1])).get("tie_default"); print(v if v in ("claude", "codex") else "codex")' "$DIR/config.json" 2>/dev/null)"
  [ "$DEF" = claude ] || DEF=codex
}
if codex_in && claude_in; then
  tie_default; DEF_NAME="$DEF (tie_default)"
elif codex_in; then
  DEF=codex DEF_NAME="Codex (Claude is not logged in)"
elif claude_in; then
  DEF=claude DEF_NAME="Claude (Codex is not logged in)"
else
  tie_default; DEF_NAME="$DEF (tie_default; neither is logged in)"
fi
MAIN=()
if [ "${1:-}" = --main ]; then
  case "${2:-}" in claude|codex) MAIN=(--main "$2");; *) echo "$DEF pick.sh: --main must be claude|codex; defaulting to $DEF_NAME"; exit 0;; esac
fi
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
  echo "$DEF claude-codex-router not installed ($ROUTER); defaulting to $DEF_NAME$EXTRA_NOTE"
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "$DEF python3 not found; defaulting to $DEF_NAME$EXTRA_NOTE"
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
import json, sys, time
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
# a login that has stopped working: the user has to act (not_logged_in is in the reason). Only when the side has not been read for over 30 minutes,
# so that a single rejected read between good ones does not become a request to log in
for side, v in sorted((d.get("login") or {}).items()):
    if isinstance(v, dict) and v.get("state") in ("token_expired", "login_rejected") and time.time() - (v.get("last_read") or 0) > 1800:
        last = time.strftime("%m-%d %H:%M", time.localtime(v["last_read"])) if v.get("last_read") else "never"
        why += "; ACTION FOR THE USER (%s, last read %s): %s" % (side, last, v.get("action") or v["state"])
if d.get("notes"):
    why += "; notes: " + " / ".join(d["notes"])
print(p, "claude-codex-router:", why)
' 2>/dev/null)"
if [ -z "$LINE" ]; then
  echo "$DEF claude-codex-router gave no answer (rc=$RC); defaulting to $DEF_NAME$EXTRA_NOTE"
  exit 0
fi
echo "$LINE$EXTRA_NOTE"
