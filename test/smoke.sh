#!/usr/bin/env bash
# End-to-end check from install to uninstall (with a fake HOME, fake codex, fake Claude usage API, fake crontab,
# fake price data and fake conversation logs; the real environment is not touched). Runs both in CI and locally.
#   SMOKE_INSTALL_MODE=direct (install.sh directly) | bootstrap (the path that clones this repository)
#   SMOKE_BOOTSTRAP_REF   branch that bootstrap clones (default citest)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${SMOKE_INSTALL_MODE:-direct}"
work="$(mktemp -d "${TMPDIR:-/tmp}/ccr-smoke.XXXXXX")"
server_pid=""
cleanup() { [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null; rm -rf "$work"; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok  : $*"; }

export HOME="$work/home"
export CLAUDE_CONFIG_DIR="$HOME/.claude"
export CLAUDE_CODEX_ROUTER_DIR="$HOME/.claude/codex-router"
export CLAUDE_CODEX_ROUTER_SCHEDULER=cron
export CLAUDE_CODEX_ROUTER_SKILLS=none   # the installer does not ask about the skills (section 11 sets it)
mkdir -p "$HOME/.claude" "$work/bin"
DIR="$CLAUDE_CODEX_ROUTER_DIR"

# fake crontab (reads and writes $work/crontab.txt)
cat > "$work/bin/crontab" <<EOF
#!/usr/bin/env bash
f="$work/crontab.txt"
if [[ "\${1:-}" == "-l" ]]; then [[ -f "\$f" ]] && cat "\$f" || exit 1; else cat > "\$f"; fi
EOF
# fake codex (answers only exec, for run.sh of the delegation example; usage is read without codex)
cat > "$work/bin/codex" <<'EOF'
#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ['exec']:   # read the request and write the report to the -o file
    sys.stdin.read()
    open(sys.argv[sys.argv.index('-o') + 1], 'w').write('done\n')
    sys.exit(0)
sys.exit(2)
EOF
# fake claude (answers only -p, for run.sh of the claude-offload example)
cat > "$work/bin/claude" <<'EOF'
#!/usr/bin/env python3
import json, sys
if '-p' in sys.argv:
    sys.stdin.read()
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'}))
    sys.exit(0)
sys.exit(2)
EOF
chmod +x "$work/bin/crontab" "$work/bin/codex" "$work/bin/claude"
export PATH="$work/bin:$PATH"

# fake price data (LiteLLM format; no network) and fake conversation logs (one response each for Claude and Codex)
python3 - "$work/prices.json" <<'EOF'
import json, sys
d = {f'filler-{i}': {'input_cost_per_token': 1e-6} for i in range(120)}
d['claude-opus-5-5'] = {'input_cost_per_token': 4e-06, 'output_cost_per_token': 2e-05, 'cache_read_input_token_cost': 2e-07}
d['gpt-6.1-sol'] = {'input_cost_per_token': 2e-06, 'output_cost_per_token': 1e-05, 'cache_read_input_token_cost': 1e-07}
json.dump(d, open(sys.argv[1], 'w'))
EOF
export CLAUDE_CODEX_ROUTER_PRICE_URL="file://$work/prices.json"
mkdir -p "$HOME/.claude/projects/p" "$HOME/.codex/sessions/2026/10/06"
now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"timestamp": "%s", "requestId": "r1", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 1000}}}\n' "$now_iso" > "$HOME/.claude/projects/p/s.jsonl"
{ printf '{"timestamp": "%s", "type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}\n' "$now_iso"
  printf '{"timestamp": "%s", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}}}}\n' "$now_iso"; } > "$HOME/.codex/sessions/2026/10/06/s.jsonl"

# fake Claude usage API and fake ChatGPT usage (a free port on 127.0.0.1)
cat > "$work/server.py" <<'EOF'
import datetime, http.server, json, os, sys
WORK = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/backend-api/wham/usage'):   # fake ChatGPT usage (Codex)
            if self.headers.get('Authorization') != 'Bearer codex-token' or self.headers.get('ChatGPT-Account-Id') != 'acct-1':
                self.send_response(401); self.end_headers(); return
            r = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 3 * 86400
            b = json.dumps({'rate_limit': {'primary_window': {'used_percent': 30, 'limit_window_seconds': 604800, 'reset_at': r},
                                           'secondary_window': None}}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
            return
        with open(os.path.join(WORK, 'hits'), 'a') as f:
            f.write('.')
        if os.path.exists(os.path.join(WORK, '429')):   # returns 429 while this file exists
            self.send_response(429); self.end_headers(); return
        if self.headers.get('Authorization') != 'Bearer test-token':
            self.send_response(401); self.end_headers(); return
        now = datetime.datetime.now(datetime.timezone.utc)
        r5, r7 = (now + datetime.timedelta(hours=3)).isoformat(), (now + datetime.timedelta(days=4)).isoformat()
        body = {'five_hour': {'utilization': 10.0, 'resets_at': r5}, 'seven_day': {'utilization': 20.0, 'resets_at': r7},
                'limits': [{'kind': 'session', 'group': 'session', 'percent': 10, 'resets_at': r5, 'scope': None},
                           {'kind': 'weekly_all', 'group': 'weekly', 'percent': 20, 'resets_at': r7, 'scope': None},
                           {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 0, 'resets_at': r7,
                            'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}}]}
        b = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
s = http.server.HTTPServer(('127.0.0.1', 0), H)
print(s.server_address[1], flush=True)
s.serve_forever()
EOF
python3 "$work/server.py" "$work" > "$work/port" &
server_pid=$!
for _ in $(seq 50); do [[ -s "$work/port" ]] && break; sleep 0.1; done
port="$(cat "$work/port")"
export CLAUDE_CODEX_ROUTER_CLAUDE_URL="http://127.0.0.1:$port/api/oauth/usage"
export CLAUDE_CODEX_ROUTER_CODEX_URL="http://127.0.0.1:$port/backend-api/wham/usage"
mkdir -p "$HOME/.codex"
printf '{"tokens": {"access_token": "codex-token", "account_id": "acct-1"}}\n' > "$HOME/.codex/auth.json"
exp=$(( ($(date +%s) + 3600) * 1000 ))
printf '{"claudeAiOauth": {"accessToken": "test-token", "expiresAt": %s}}\n' "$exp" > "$CLAUDE_CONFIG_DIR/.credentials.json"

# 1) install
install_once() {
  if [[ "$MODE" == "bootstrap" ]]; then
    CLAUDE_CODEX_ROUTER_REPO="$SRC" CLAUDE_CODEX_ROUTER_REF="${SMOKE_BOOTSTRAP_REF:-citest}" bash "$SRC/bootstrap.sh" > "$work/install.log"
  else
    bash "$SRC/install.sh" > "$work/install.log"
  fi
}
install_once
for f in bin/router.py config.json uninstall.sh .version; do
  [[ -f "$DIR/$f" ]] || fail "$f missing after install"
done
[[ "$(grep -c '# claude-codex-router$' "$work/crontab.txt")" == 1 ]] || fail "the crontab line is not exactly 1 line"
grep -q "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR " "$work/crontab.txt" || fail "the crontab line has no CLAUDE_CONFIG_DIR"
pass "install ($MODE): files and crontab line"

# 2) reinstall: settings are kept, crontab lines do not increase
echo '{"guard_risk": 0.7, "claude_min_interval": 0}' > "$DIR/config.json"   # 3) to 6) call the API every time
install_once
grep -q '0.7' "$DIR/config.json" || fail "reinstall overwrote config.json"
[[ "$(grep -c '# claude-codex-router$' "$work/crontab.txt")" == 1 ]] || fail "reinstall added a crontab line"
pass "reinstall: keeps the settings, timers do not stack"

# 3) both readable: decides and records
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
[[ $code == 0 ]] || fail "now --json exit code $code ($out)"
python3 - "$out" <<'EOF' || fail "now --json contents"
import json, sys
d = json.loads(sys.argv[1])
w = d['windows']
assert d['pick'] in ('claude', 'codex') and d['main'] == 'claude', d
assert set(w) == {'claude-session', 'claude-weekly_all', 'claude-weekly_scoped:Fable', 'codex-7d'}, w.keys()
assert w['codex-7d']['u'] == 30 and w['claude-weekly_all']['u'] == 20 and w['codex-7d']['win'] == 10080
# Claude's API gives no length: on the first read it is only a lower bound (until the reset), estimated from the reads
assert w['claude-session']['win_known'] is False and w['claude-session']['win'] <= 300, w['claude-session']
assert w['claude-weekly_scoped:Fable']['scoped'] and not w['claude-weekly_all']['scoped']
# there are conversation logs, but the cost in the current windows is still 0 (no limit in cost): X comes from usage growth
assert all(v['basis'] == 'usage' for k, v in w.items() if not v['scoped']) and w['codex-7d']['X'] is not None, w
# Claude: the length is only a lower bound on the first read, so its usage growth gives no X yet (claude_L null: nothing to compare)
assert (d['claude_L'] is None or 0 <= d['claude_L'] <= 1) and 0 <= d['codex_L'] <= 1 and not d['errors'] and 'sub_tight' not in d and d['full'] == []
# no limit in cost, so the expected leftover in cost is unknown and the comparison uses the leftover probability
assert d['claude_left_usd'] is None and d['codex_left_usd'] is None and d['reason'] in ('leftover_prob', 'tie', 'guard'), d
assert 'limit unknown in cost' in w['codex-7d']['note'], w['codex-7d']
EOF
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq 4 ]] || fail "the records are not 4 lines"
out=$(python3 "$DIR/bin/router.py" now --json --main codex) || true
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["main"]=="codex", d' "$out" || fail "--main codex"
python3 "$DIR/bin/router.py" now > /dev/null || fail "now (human-readable output)"
pass "now: read both, decided, and recorded"

# 4) one or both sides unreadable
set +e
out=$(CODEX_HOME="$work/no-codex" python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 1 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"]=="claude" and d["reason"]=="codex_unreadable" and d["errors"]' "$out"; then
  fail "when Codex is unreadable (exit code $code, $out)"
fi
mv "$CLAUDE_CONFIG_DIR/.credentials.json" "$work/cred.bak"
set +e
out=$(CODEX_HOME="$work/no-codex" python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 2 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"]=="codex" and d["reason"]=="both_unreadable" and len(d["errors"])==2' "$out"; then
  fail "when neither is readable (exit code $code, $out)"
fi
mv "$work/cred.bak" "$CLAUDE_CONFIG_DIR/.credentials.json"
pass "does not stop when a side is unreadable, and reports it in the exit code"

# 5) sample (what the timer calls)
n0=$(wc -l < "$DIR/data/usage.jsonl")
python3 "$DIR/bin/router.py" sample > /dev/null || fail "sample"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 4)) ]] || fail "sample does not add 4 lines"
[[ "$(wc -l < "$DIR/data/cost-claude.jsonl")" -eq 1 && "$(wc -l < "$DIR/data/cost-codex.jsonl")" -eq 1 ]] || fail "sample does not add cost records from the conversation logs"
[[ -f "$DIR/data/litellm-prices.json" ]] || fail "sample does not create the price data cache"
python3 "$DIR/bin/router.py" sample > /dev/null || fail "second sample"
[[ "$(wc -l < "$DIR/data/cost-claude.jsonl")" -eq 1 ]] || fail "second sample duplicated cost records"
pass "sample: added usage and cost to the records (cost not duplicated)"

# 6) Claude's API returns 429: no retry, decide from the latest recorded value (borrowed values are not added to the records)
touch "$work/429"; : > "$work/hits"
n0=$(wc -l < "$DIR/data/usage.jsonl")
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 0 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"] in ("claude","codex") and not d["errors"] and "429" in d["notes"][0] and d["windows"]["claude-weekly_all"]["u"]==20 and d["reason"] in ("tie", "leftover_prob", "leftover", "guard")' "$out"; then
  fail "on 429 (exit code $code, $out)"
fi
[[ "$(wc -c < "$work/hits")" -eq 1 ]] || fail "called again after 429 ($(wc -c < "$work/hits") times)"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 1)) ]] || fail "on 429, only the 1 Codex line should be added"
python3 "$DIR/bin/router.py" sample > /dev/null || fail "sample on 429"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 2)) ]] || fail "sample on 429 should add only the 1 Codex line"
rm "$work/429"
pass "429: no retry, decided from the recent record"

# 6b) with a recent record, Claude's API is not called (borrowed values are not added to the records)
echo '{"guard_risk": 0.7, "claude_min_interval": 60}' > "$DIR/config.json"
r7=$(( $(date +%s) + 4 * 86400 ))
printf '{"ts": %s, "side": "claude", "id": "weekly_all", "u": 33.0, "r": %s, "src": "live"}\n' "$(date +%s)" "$r7" >> "$DIR/data/usage.jsonl"
: > "$work/hits"
n0=$(wc -l < "$DIR/data/usage.jsonl")
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 0 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert not d["errors"] and "API not called" in d["notes"][0] and d["windows"]["claude-weekly_all"]["u"]==33' "$out"; then
  fail "with a recent record (exit code $code, $out)"
fi
python3 "$DIR/bin/router.py" sample > /dev/null || fail "sample with a recent record"
[[ ! -s "$work/hits" ]] || fail "called Claude's API despite a recent record ($(wc -c < "$work/hits") times)"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 2)) ]] || fail "with a recent record, now and sample should each add only the 1 Codex line"
pass "with a recent record, Claude's API is not called"

# 6c) delegation example (examples/codex-offload): pick.sh reads the installed router's decision, run.sh keeps the codex exec result
EX="$SRC/examples/codex-offload"
line=$(bash "$EX/pick.sh")
[[ "$line" =~ ^(claude|codex)\ claude-codex-router: ]] || fail "pick.sh cannot read the router's decision ($line)"
bash "$EX/test_pick.sh" > "$work/pick-test.log" || { cat "$work/pick-test.log"; fail "test_pick.sh"; }
mkdir -p "$work/repo" && git -C "$work/repo" init -q && echo x > "$work/repo/a.txt"
echo "read a.txt" | bash "$EX/run.sh" read "$work/repo" "$work/cx" > "$work/run.out" || fail "run.sh ($(cat "$work/run.out"))"
if ! grep -q '^rc=0 mode=read' "$work/run.out" || [[ "$(cat "$work/cx/last.txt")" != "done" ]] || grep -q 'WARNING' "$work/run.out"; then
  fail "run.sh summary ($(cat "$work/run.out"))"
fi
grep -q 'Standing rules' "$work/cx/prompt.md" || fail "run.sh did not prepend the preamble"
EX2="$SRC/examples/claude-offload"
{ cmp -s "$EX/pick.sh" "$EX2/pick.sh" && cmp -s "$EX/preamble.md" "$EX2/preamble.md"; } || fail "pick.sh / preamble.md differ between the two skills"
line=$(bash "$EX2/pick.sh" --main codex)
[[ "$line" =~ ^(claude|codex)\ claude-codex-router: ]] || fail "pick.sh --main codex cannot read the router's decision ($line)"
echo "read a.txt" | bash "$EX2/run.sh" read "$work/repo" "$work/cl" > "$work/run2.out" || fail "claude-offload run.sh ($(cat "$work/run2.out"))"
if ! grep -q '^rc=0 mode=read' "$work/run2.out" || [[ "$(cat "$work/cl/last.txt")" != "done" ]] || grep -q 'WARNING' "$work/run2.out"; then
  fail "claude-offload run.sh summary ($(cat "$work/run2.out"))"
fi
pass "delegation examples: pick.sh reads the router, run.sh keeps the codex exec / claude -p result"

# 7) unit tests (the repository's test/, with the repository's bin/ rather than the installed router.py)
python3 -m unittest discover -s "$SRC/test" > "$work/unit.log" 2>&1 || { cat "$work/unit.log"; fail "unit tests"; }
pass "unit tests"

# 8) uninstall: removes the timer and the program, keeps the records and settings
bash "$DIR/uninstall.sh" > /dev/null
grep -q '# claude-codex-router$' "$work/crontab.txt" && fail "the crontab line remains after uninstall"
[[ -f "$DIR/bin/router.py" ]] && fail "router.py remains after uninstall"
[[ -f "$DIR/data/usage.jsonl" && -f "$DIR/config.json" ]] || fail "uninstall deleted the records or settings"
pass "uninstall: removed the timer and the program, kept the records and settings"
# 9) systemd: enables linger, and finishes installing even when the user's systemd is unreachable (the state after a WSL restart)
cat > "$work/bin/loginctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$work/loginctl.log"
if [[ "\$1" == show-user ]]; then [[ -f "$work/lingering" ]] && echo yes || echo no; fi
if [[ "\$1" == enable-linger ]]; then : > "$work/lingering"; fi
if [[ "\$1" == disable-linger ]]; then rm -f "$work/lingering"; fi
exit 0
EOF
cat > "$work/bin/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$work/systemctl.log"
[[ -f "$work/systemd-up" ]]
EOF
chmod +x "$work/bin/loginctl" "$work/bin/systemctl"
CLAUDE_CODEX_ROUTER_SCHEDULER=systemd bash "$SRC/install.sh" > "$work/install-systemd.log" || fail "install stops when systemd is unreachable"
[[ -f "$DIR/.version" && -f "$DIR/uninstall.sh" ]] || fail "when systemd is unreachable, install does not finish"
grep -q '^enable-linger ' "$work/loginctl.log" && [[ -f "$DIR/.linger" ]] || fail "linger not enabled / no marker"
grep -q 'timer could not be registered' "$work/install-systemd.log" || fail "does not report that the timer could not be registered"
[[ -f "$HOME/.config/systemd/user/claude-codex-router-sample.timer" ]] || fail "no timer definition"
: > "$work/systemd-up"
: > "$work/loginctl.log"
CODEX_HOME="$HOME/.codex" TOKEN_PACE_DIR="$HOME/tp" CLAUDE_CODEX_ROUTER_SCHEDULER=systemd bash "$SRC/install.sh" > "$work/install-systemd.log" || fail "systemd reinstall"
grep -q "^Environment=CODEX_HOME=$HOME/.codex$" "$HOME/.config/systemd/user/claude-codex-router-sample.service" || fail "CODEX_HOME is not passed to the timer"
grep -q "^Environment=TOKEN_PACE_DIR=$HOME/tp$" "$HOME/.config/systemd/user/claude-codex-router-sample.service" || fail "TOKEN_PACE_DIR is not passed to the timer"
grep -q '^enable-linger ' "$work/loginctl.log" && fail "enabled linger again although it was already enabled"
grep -q 'enable --now claude-codex-router-sample.timer' "$work/systemctl.log" || fail "the timer is not enabled"
bash "$DIR/uninstall.sh" > /dev/null
grep -q '^disable-linger ' "$work/loginctl.log" && [[ ! -f "$DIR/.linger" ]] || fail "uninstall did not revert linger"
[[ ! -f "$HOME/.config/systemd/user/claude-codex-router-sample.timer" ]] || fail "the timer definition remains after uninstall"
pass "systemd: enables linger, finishes installing even when systemd is unreachable, reverted by uninstall"

# 10) now reports when periodic sampling has stopped
bash "$SRC/install.sh" > /dev/null
python3 "$DIR/bin/router.py" sample > /dev/null || true
touch -d '-2 hours' "$DIR/data/last-sample"
out=$(python3 "$DIR/bin/router.py" now --json) || true
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert any("stopped" in n for n in d["notes"]), d["notes"]' "$out" || fail "does not report that periodic sampling has stopped"
python3 "$DIR/bin/router.py" sample > /dev/null || true
out=$(python3 "$DIR/bin/router.py" now --json) || true
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert not any("stopped" in n for n in d["notes"]), d["notes"]' "$out" || fail "reported although periodic sampling is running"
bash "$DIR/uninstall.sh" > /dev/null
pass "now reports when periodic sampling has stopped"

# 11) delegation skills: installed when chosen, kept up to date, someone else's or edited ones are kept, removed by uninstall
CS="$CLAUDE_CONFIG_DIR/skills/codex-offload"; XS="$HOME/.agents/skills/claude-offload"; RULES="$HOME/.codex/rules/claude-codex-router.rules"
bash "$SRC/install.sh" > /dev/null
[[ ! -e "$CS" && ! -e "$XS" && ! -e "$RULES" ]] || fail "skills installed although CLAUDE_CODEX_ROUTER_SKILLS=none"
mkdir -p "$XS" && echo mine > "$XS/SKILL.md"                                  # someone else's skill under the same name
CLAUDE_CODEX_ROUTER_SKILLS=both CLAUDE_CODEX_ROUTER_SKILLS_REPLACE=no bash "$SRC/install.sh" > "$work/install-sk.log"
[[ -x "$CS/pick.sh" && -x "$CS/run.sh" ]] || fail "the Claude Code skill is not installed"
[[ "$(cat "$XS/SKILL.md")" == mine && ! -e "$RULES" ]] || fail "replaced someone else's skill without being asked, or wrote the rule for it"
grep -q "$CS/pick.sh --main claude" "$CS/SKILL.md" || fail "SKILL.md does not have the absolute path"
CLAUDE_CODEX_ROUTER_SKILLS=codex CLAUDE_CODEX_ROUTER_SKILLS_REPLACE=yes bash "$SRC/install.sh" > "$work/install-sk.log"
ls -d "$XS".bak-* > /dev/null 2>&1 || fail "the replaced skill was not moved aside"
{ [[ -x "$XS/run.sh" ]] && grep -q "$XS/pick.sh --main codex" "$XS/SKILL.md"; } || fail "the Codex skill is not installed with the absolute path"
grep -q "pattern=\[\"$XS/run.sh\"\], decision=\"allow\"" "$RULES" || fail "the Codex rule for run.sh is missing ($(cat "$RULES" 2>/dev/null))"
echo "# my note" >> "$CS/SKILL.md"                                           # the user edits the Claude Code skill
bash "$SRC/install.sh" > "$work/install-sk.log"
{ grep -q 'was edited after install; kept as is' "$work/install-sk.log" && grep -q '# my note' "$CS/SKILL.md"; } || fail "overwrote an edited skill"
bash "$DIR/uninstall.sh" > "$work/uninstall-sk.log"
[[ ! -e "$XS" && ! -e "$RULES" ]] || fail "uninstall did not remove the Codex skill or the rule"
{ [[ -f "$CS/SKILL.md" ]] && grep -q 'kept the skill' "$work/uninstall-sk.log"; } || fail "uninstall removed an edited skill"
pass "skills: installed when chosen, edited or someone else's kept, removed by uninstall"
echo "smoke: all passed"
