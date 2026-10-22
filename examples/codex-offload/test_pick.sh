#!/usr/bin/env bash
# checks for pick.sh (with a fake claude-codex-router; the real usage windows are not read)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/pick-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
fails=0
check() {  # check <name> <expected first word> <expected phrase>
  local out
  out="$(CLAUDE_CODEX_ROUTER_DIR="$work/r" CODEX_HOME="${T_CODEX:-$work/codex}" CLAUDE_CONFIG_DIR="${T_CLAUDE:-$work/claude}" bash "$HERE/pick.sh")"
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

mkdir -p "$work/codex" "$work/claude"   # both logged in unless a check says otherwise
printf '{"tokens": {"access_token": "t"}}\n' > "$work/codex/auth.json"
echo '{}' > "$work/claude/.credentials.json"
rm -rf "$work/r"
check "no router" codex "not installed"
mkdir -p "$work/nocodex" "$work/apikey"
printf '{"OPENAI_API_KEY": "sk-x", "tokens": null}\n' > "$work/apikey/auth.json"
mkdir -p "$work/r"; echo '{"tie_default": "claude"}' > "$work/r/config.json"
check "no router, both logged in: tie_default in config.json" claude "claude (tie_default)"
rm -rf "$work/r"
T_CLAUDE="$work/nocodex" check "no router, Claude not logged in: Codex" codex "Claude is not logged in"
T_CODEX="$work/nocodex" check "no router, Codex not logged in: Claude, not Codex" claude "Codex is not logged in"
T_CODEX="$work/apikey" check "no router, Codex only with an API key: Claude" claude "Codex is not logged in"
if [ "$(uname -s)" != Darwin ]; then   # on macOS the Keychain may hold real Claude credentials
  T_CODEX="$work/nocodex" T_CLAUDE="$work/nocodex" check "no router, neither logged in: tie_default (codex when unset)" codex "codex (tie_default; neither is logged in)"
  mkdir -p "$work/r"; echo '{"tie_default": "claude"}' > "$work/r/config.json"
  T_CODEX="$work/nocodex" T_CLAUDE="$work/nocodex" check "no router, neither logged in: tie_default in config.json" claude "claude (tie_default; neither is logged in)"
  rm -rf "$work/r"
fi

fake 'import json; print(json.dumps({"pick": "claude", "reason": "leftover", "claude_L": 0.9, "codex_L": 0.5, "guard": [], "full": [], "errors": []}))'
check "router picks claude (probability, an older router or leftover_prob)" claude "leftover (leftover probability Claude 0.90 / Codex 0.50)"

fake 'import json; print(json.dumps({"pick": "codex", "reason": "leftover", "claude_left_usd": 30.4, "codex_left_usd": 120.0, "claude_L": 0.9, "codex_L": 0.5, "guard": [], "full": [], "errors": []}))'
check "router picks codex by the expected leftover cost" codex "leftover (expected leftover Claude \$30 / Codex \$120)"

fake 'import json; print(json.dumps({"pick": "claude", "reason": "tie", "tie": True, "claude_L": 0.6, "codex_L": None, "guard": [], "full": [], "errors": []}))'
check "follows the decision when one side has no leftover probability (tie)" claude "tie (leftover probability Claude 0.60 / Codex unknown)"

fake 'import json; print(json.dumps({"pick": "codex", "reason": "tie", "tie": True, "claude_L": None, "codex_L": 0.7, "guard": [], "full": [], "errors": []}))'
check "follows the decision when the other side has no leftover probability (tie)" codex "tie (leftover probability Claude unknown / Codex 0.70)"

fake 'import json, sys; print(json.dumps({"pick": "codex", "reason": "guard", "claude_L": 0.4, "codex_L": 0.5, "guard": ["claude-7d"], "full": [], "errors": []}))'
check "router picks codex (guard)" codex "guard (main side): claude-7d"

fake 'import json, sys; print(json.dumps({"pick": "codex", "reason": "full", "claude_L": 0.0, "codex_L": 0.5, "guard": [], "full": ["claude-session"], "errors": []}))'
check "full window" codex "full: claude-session"

fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": "codex_unreadable", "claude_L": None, "codex_L": None, "guard": [], "errors": ["Codex: x"]})); sys.exit(1)'
check "follows the decision even when only one side was read (exit code 1)" claude "errors: Codex: x"

# shellcheck disable=SC2016  # the backticks are part of the JSON text, not a command
fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": "codex_unreadable", "guard": [], "errors": ["Codex: expired"], "login": {"codex": {"state": "token_expired", "expired_at": 1, "last_read": 1791800000, "action": "start Codex once; log in again (`codex login`)"}}})); sys.exit(1)'
check "a login that stopped working is marked for the user" claude "ACTION FOR THE USER (codex, last read "
check "the marked part carries the router's action" claude "log in again (\`codex login\`)"
fake 'import json, sys, time; print(json.dumps({"pick": "claude", "reason": "codex_unreadable", "guard": [], "errors": ["Codex: 401"], "login": {"codex": {"state": "login_rejected", "http": 401, "last_read": int(time.time()) - 300, "action": "log in"}}})); sys.exit(1)'
out="$(CLAUDE_CODEX_ROUTER_DIR="$work/r" CODEX_HOME="$work/codex" CLAUDE_CONFIG_DIR="$work/claude" bash "$HERE/pick.sh")"
if [[ "$out" == *"ACTION FOR THE USER"* || "$out" != *"errors: Codex: 401"* ]]; then
  echo "FAIL: a rejected read 5 minutes after a good one is not marked for the user → \"$out\""; fails=$((fails + 1))
else
  echo "ok  : a rejected read 5 minutes after a good one is not marked for the user → $out"
fi
fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": "codex_not_logged_in", "guard": [], "errors": ["Codex: no login"], "login": {"codex": {"state": "not_logged_in", "last_read": None, "action": "log in"}}})); sys.exit(1)'
out="$(CLAUDE_CODEX_ROUTER_DIR="$work/r" CODEX_HOME="$work/codex" CLAUDE_CONFIG_DIR="$work/claude" bash "$HERE/pick.sh")"
if [[ "$out" == *"ACTION FOR THE USER"* ]]; then
  echo "FAIL: a side that is not logged in is not marked for the user → \"$out\""; fails=$((fails + 1))
else
  echo "ok  : a side that is not logged in is not marked for the user → $out"
fi

fake 'raise SystemExit("boom")'
check "router is broken" codex "gave no answer"
T_CODEX="$work/nocodex" check "router is broken, Codex not logged in: Claude" claude "gave no answer"

fake 'print("not json")'
check "answer unreadable" codex "gave no answer"

fake 'import json; print(json.dumps({"pick": "gemini"}))'
check "unknown delegation target" codex "gave no answer"

fake 'import time; time.sleep(30)'
CLAUDE_CODEX_ROUTER_PICK_TIMEOUT=1 check "router does not answer in time" codex "gave no answer (rc=124)"

fake 'import json; print(json.dumps({"pick": "claude", "reason": "leftover", "claude_L": 0.9, "codex_L": 0.5, "guard": [], "full": [], "errors": []}))'
mkdir -p "$work/onlypy"
for c in python3 bash grep; do ln -sf "$(command -v "$c")" "$work/onlypy/$c"; done   # no timeout command
PATH="$work/onlypy" check "no timeout command (macOS default)" claude "leftover"

fake 'import json; print(json.dumps({"pick": "codex", "reason": "leftover", "claude_L": 0.4, "codex_L": 0.6, "guard": [], "full": [], "errors": []}))'
echo claude > "$work/r/force"
check "the flag file fixes claude even when the router picks codex" claude "fixed by the flag file"
printf ' codex \n' > "$work/r/force"
check "the flag file fixes codex (spaces around the word allowed)" codex "fixed by the flag file"
: > "$work/r/force"
check "an empty flag file is ignored and said so" codex "leftover (leftover probability Claude 0.40 / Codex 0.60); flag file $work/r/force ignored"
rm -f "$work/r/force"
mkdir -p "$work/flag"; echo claude > "$work/flag/f"
CLAUDE_CODEX_ROUTER_FORCE_FILE="$work/flag/f" check "the flag file's place can be changed" claude "fixed by the flag file ($work/flag/f)"
check "without the flag file the router decides" codex "claude-codex-router: leftover"
rm -rf "$work/r"; echo claude > "$work/flag/f"
CLAUDE_CODEX_ROUTER_FORCE_FILE="$work/flag/f" check "the flag file works without the router installed" claude "fixed by the flag file"

fake 'import json, sys; print(json.dumps({"pick": "claude", "reason": " ".join(sys.argv[1:]), "guard": [], "full": [], "errors": []}))'
if CLAUDE_CODEX_ROUTER_DIR="$work/r" bash "$HERE/pick.sh" --main codex | grep -q 'now --json --main codex'; then
  echo "ok  : --main is passed to the router"
else
  echo "FAIL: --main is not passed to the router"; fails=$((fails + 1))
fi

if [[ $fails != 0 ]]; then echo "$fails failed"; exit 1; fi
echo "all passed"
