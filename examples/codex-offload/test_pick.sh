#!/usr/bin/env bash
# checks for pick.sh (with a fake claude-codex-router; the real usage windows are not read)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/pick-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
fails=0
check() {  # check <name> <expected first word> <expected phrase>
  local out
  out="$(CLAUDE_CODEX_ROUTER_DIR="$work/r" bash "$HERE/pick.sh")"
  local rc=$?
  if [[ $rc != 0 || "${out%% *}" != "$2" || "$out" != *"$3"* ]]; then
    echo "FAIL: $1 → rc=$rc \"$out\""; fails=$((fails + 1))
  else
    echo "ok  : $1 → $out"
  fi
  if LC_ALL=C grep -q '[^ -~]' <<< "$out"; then
    echo "FAIL: $1 → non-ASCII characters in \"$out\""; fails=$((fails + 1))
  fi
}
fake() {  # fake <body of router.py>
  mkdir -p "$work/r/bin"
  printf '%s\n' "$1" > "$work/r/bin/router.py"
}

rm -rf "$work/r"
check "no router" codex "not installed"

fake 'import json; print(json.dumps({"pick": "claude", "reason": "leftover", "claude_L": 0.9, "codex_L": 0.5, "guard": [], "sub_tight": [], "full": [], "errors": []}))'
check "router picks claude" claude "leftover (leftover probability Claude 0.90 / Codex 0.50)"

fake 'import json, sys; print(json.dumps({"pick": "codex", "reason": "guard", "claude_L": 0.4, "codex_L": 0.5, "guard": ["claude-7d"], "sub_tight": [], "full": [], "errors": []}))'
check "router picks codex (guard)" codex "guard (main side): claude-7d"

fake 'import json, sys; print(json.dumps({"pick": "claude", "main": "claude", "reason": "guard", "claude_L": 0.4, "codex_L": 0.5, "guard": [], "sub_tight": ["codex-5h"], "full": [], "errors": []}))'
check "kept on the main side by a sub-side short window" claude "kept on main (sub side short window): codex-5h"

fake 'import json, sys; print(json.dumps({"pick": "codex", "reason": "full", "claude_L": 0.0, "codex_L": 0.5, "guard": [], "sub_tight": [], "full": ["claude-session"], "errors": []}))'
check "full window" codex "full: claude-session"

fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": "codex_unreadable", "claude_L": None, "codex_L": None, "guard": [], "errors": ["Codex: x"]})); sys.exit(1)'
check "follows the decision even when only one side was read (exit code 1)" claude "errors: Codex: x"

fake 'raise SystemExit("boom")'
check "router is broken" codex "gave no answer"

fake 'print("not json")'
check "answer unreadable" codex "gave no answer"

fake 'import json; print(json.dumps({"pick": "gemini"}))'
check "unknown delegation target" codex "gave no answer"

fake 'import time; time.sleep(30)'
CLAUDE_CODEX_ROUTER_PICK_TIMEOUT=1 check "router does not answer in time" codex "gave no answer (rc=124)"

fake 'import json; print(json.dumps({"pick": "claude", "reason": "leftover", "claude_L": 0.9, "codex_L": 0.5, "guard": [], "sub_tight": [], "full": [], "errors": []}))'
mkdir -p "$work/onlypy"
for c in python3 bash grep; do ln -sf "$(command -v "$c")" "$work/onlypy/$c"; done   # no timeout command
PATH="$work/onlypy" check "no timeout command (macOS default)" claude "leftover"

fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": " ".join(sys.argv[1:]), "guard": [], "sub_tight": [], "full": [], "errors": []}))'
if CLAUDE_CODEX_ROUTER_DIR="$work/r" bash "$HERE/pick.sh" --main codex | grep -q 'now --json --main codex'; then
  echo "ok  : --main is passed to the router"
else
  echo "FAIL: --main is not passed to the router"; fails=$((fails + 1))
fi

if [[ $fails != 0 ]]; then echo "$fails failed"; exit 1; fi
echo "all passed"
