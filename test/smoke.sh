#!/usr/bin/env bash
# インストールからアンインストールまでの通しの確認（偽の HOME・偽の codex・偽の Claude の使用量 API・偽の crontab・
# 偽の単価データ・偽の会話ログで、本物の環境に触れない）。CI と手元の両方で動く。
#   SMOKE_INSTALL_MODE=direct（install.sh を直接）| bootstrap（このリポジトリを clone する経路）
#   SMOKE_BOOTSTRAP_REF   bootstrap で clone するブランチ（既定 citest）
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
mkdir -p "$HOME/.claude" "$work/bin"
DIR="$CLAUDE_CODEX_ROUTER_DIR"

# 偽の crontab（$work/crontab.txt に読み書きする）
cat > "$work/bin/crontab" <<EOF
#!/usr/bin/env bash
f="$work/crontab.txt"
if [[ "\${1:-}" == "-l" ]]; then [[ -f "\$f" ]] && cat "\$f" || exit 1; else cat > "\$f"; fi
EOF
# 偽の codex（app-server の account/rateLimits/read だけ答える）
cat > "$work/bin/codex" <<'EOF'
#!/usr/bin/env python3
import json, sys, time
for line in iter(sys.stdin.readline, ''):   # for line in sys.stdin は先読みするので、入力が閉じるまで答えない
    m = json.loads(line)
    if m.get('id') == 1:
        print(json.dumps({'id': 1, 'result': {}}), flush=True)
    if m.get('id') == 2:
        r = int(time.time()) + 3 * 86400
        print(json.dumps({'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 30, 'windowDurationMins': 10080, 'resetsAt': r}}}}), flush=True)
EOF
chmod +x "$work/bin/crontab" "$work/bin/codex"
export PATH="$work/bin:$PATH"

# 偽の単価データ（LiteLLM の形。通信しない）と、偽の会話ログ（Claude・Codex それぞれ 1 応答）
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

# 偽の Claude の使用量 API（127.0.0.1 の空きポート）
cat > "$work/server.py" <<'EOF'
import datetime, http.server, json, os, sys
WORK = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(os.path.join(WORK, 'hits'), 'a') as f:
            f.write('.')
        if os.path.exists(os.path.join(WORK, '429')):   # このファイルがある間は 429 を返す
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
exp=$(( ($(date +%s) + 3600) * 1000 ))
printf '{"claudeAiOauth": {"accessToken": "test-token", "expiresAt": %s}}\n' "$exp" > "$CLAUDE_CONFIG_DIR/.credentials.json"

# 1) インストール
install_once() {
  if [[ "$MODE" == "bootstrap" ]]; then
    CLAUDE_CODEX_ROUTER_REPO="$SRC" CLAUDE_CODEX_ROUTER_REF="${SMOKE_BOOTSTRAP_REF:-citest}" bash "$SRC/bootstrap.sh" > "$work/install.log"
  else
    bash "$SRC/install.sh" > "$work/install.log"
  fi
}
install_once
for f in bin/router.py weights-default.json config.json uninstall.sh .version; do
  [[ -f "$DIR/$f" ]] || fail "インストール後に $f が無い"
done
[[ "$(grep -c '# claude-codex-router$' "$work/crontab.txt")" == 1 ]] || fail "crontab の行が 1 行でない"
pass "install（$MODE）：ファイルと crontab の行"

# 2) 再インストール：設定は保つ・crontab の行は増えない
echo '{"guard_risk": 0.7, "claude_min_interval": 0}' > "$DIR/config.json"   # 3)〜6) は毎回 API を呼ぶ
install_once
grep -q '0.7' "$DIR/config.json" || fail "再インストールで config.json が上書きされた"
[[ "$(grep -c '# claude-codex-router$' "$work/crontab.txt")" == 1 ]] || fail "再インストールで crontab の行が増えた"
pass "再インストール：設定を保ち、タイマーは重ならない"

# 3) 両方読める：判断して記録する
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
[[ $code == 0 ]] || fail "now --json の終了コード $code（$out）"
python3 - "$out" <<'EOF' || fail "now --json の中身"
import json, sys
d = json.loads(sys.argv[1])
w = d['windows']
assert d['pick'] in ('claude', 'codex') and d['main'] == 'claude', d
assert set(w) == {'claude-session', 'claude-weekly_all', 'claude-weekly_scoped:Fable', 'codex-7d'}, w.keys()
assert w['codex-7d']['u'] == 30 and w['claude-weekly_all']['u'] == 20 and w['claude-session']['win'] == 300
assert w['claude-weekly_scoped:Fable']['scoped'] and not w['claude-weekly_all']['scoped']
assert all(v['X'] is None for v in w.values())                      # 料金の記録が 1 日分に満たない（初めての環境）
assert d['claude_L'] == 0.5 and d['codex_L'] == 0.5 and not d['errors']
assert any('1 日分' in n for n in d['notes']), d['notes']
EOF
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq 4 ]] || fail "記録が 4 行でない"
out=$(python3 "$DIR/bin/router.py" now --json --main codex) || true
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["main"]=="codex", d' "$out" || fail "--main codex"
python3 "$DIR/bin/router.py" now > /dev/null || fail "now（人向けの表示）"
pass "now：両方を読んで判断し、記録した"

# 4) 片側・両側が読めない
set +e
out=$(CLAUDE_CODEX_ROUTER_CODEX=/nonexistent PATH="/usr/bin:/bin" python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 1 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"]=="claude" and d["errors"]' "$out"; then
  fail "Codex が読めないとき（終了コード $code、$out）"
fi
mv "$CLAUDE_CONFIG_DIR/.credentials.json" "$work/cred.bak"
set +e
out=$(CLAUDE_CODEX_ROUTER_CODEX=/nonexistent PATH="/usr/bin:/bin" python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 2 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"]=="codex" and len(d["errors"])==2' "$out"; then
  fail "どちらも読めないとき（終了コード $code、$out）"
fi
mv "$work/cred.bak" "$CLAUDE_CONFIG_DIR/.credentials.json"
pass "読めない側があっても止まらず、終了コードで知らせる"

# 5) sample（タイマーが呼ぶもの）
n0=$(wc -l < "$DIR/data/usage.jsonl")
python3 "$DIR/bin/router.py" sample > /dev/null || fail "sample"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 4)) ]] || fail "sample で 4 行増えない"
[[ "$(wc -l < "$DIR/data/cost-claude.jsonl")" -eq 1 && "$(wc -l < "$DIR/data/cost-codex.jsonl")" -eq 1 ]] || fail "sample で料金の記録が会話ログから足されない"
[[ -f "$DIR/data/litellm-prices.json" ]] || fail "sample で単価データの控えができない"
python3 "$DIR/bin/router.py" sample > /dev/null || fail "2 回目の sample"
[[ "$(wc -l < "$DIR/data/cost-claude.jsonl")" -eq 1 ]] || fail "2 回目の sample で料金の記録が重なった"
pass "sample：使用率と料金を記録に足した（料金は重ねない）"

# 6) Claude の API が 429：呼び直さず、記録の最新の値で判断する（借りた値は記録に足さない）
touch "$work/429"; : > "$work/hits"
n0=$(wc -l < "$DIR/data/usage.jsonl")
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 0 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["pick"] in ("claude","codex") and not d["errors"] and "429" in d["notes"][0] and d["windows"]["claude-weekly_all"]["u"]==20 and d["claude_L"] is not None' "$out"; then
  fail "429 のとき（終了コード $code、$out）"
fi
[[ "$(wc -c < "$work/hits")" -eq 1 ]] || fail "429 のあと呼び直した（$(wc -c < "$work/hits") 回）"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 1)) ]] || fail "429 のとき Codex の 1 行だけ足すはず"
python3 "$DIR/bin/router.py" sample > /dev/null || fail "429 のときの sample"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 2)) ]] || fail "429 のときの sample は Codex の 1 行だけ足すはず"
rm "$work/429"
pass "429：呼び直さず、直近の記録で判断した"

# 6b) 直近の記録があれば Claude の API を呼ばない（借りた値は記録に足さない）
echo '{"guard_risk": 0.7, "claude_min_interval": 60}' > "$DIR/config.json"
r7=$(( $(date +%s) + 4 * 86400 ))
printf '{"ts": %s, "side": "claude", "win": 10080, "u": 33.0, "r": %s, "src": "live"}\n' "$(date +%s)" "$r7" >> "$DIR/data/usage.jsonl"
: > "$work/hits"
n0=$(wc -l < "$DIR/data/usage.jsonl")
set +e
out=$(python3 "$DIR/bin/router.py" now --json); code=$?
set -e
if [[ $code != 0 ]] || ! python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert not d["errors"] and "呼ばない" in d["notes"][0] and d["windows"]["claude-weekly_all"]["u"]==33' "$out"; then
  fail "直近の記録があるとき（終了コード $code、$out）"
fi
python3 "$DIR/bin/router.py" sample > /dev/null || fail "直近の記録があるときの sample"
[[ ! -s "$work/hits" ]] || fail "直近の記録があるのに Claude の API を呼んだ（$(wc -c < "$work/hits") 回）"
[[ "$(wc -l < "$DIR/data/usage.jsonl")" -eq $((n0 + 2)) ]] || fail "直近の記録があるときは now・sample とも Codex の 1 行だけ足すはず"
pass "直近の記録があれば Claude の API を呼ばない"

# 7) 単体テスト（リポジトリの test/ を、入れた router.py ではなくリポジトリの bin/ で）
python3 -m unittest discover -s "$SRC/test" > "$work/unit.log" 2>&1 || { cat "$work/unit.log"; fail "単体テスト"; }
pass "単体テスト"

# 8) アンインストール：タイマーと本体を外し、記録と設定は残す
bash "$DIR/uninstall.sh" > /dev/null
grep -q '# claude-codex-router$' "$work/crontab.txt" && fail "アンインストール後も crontab の行が残る"
[[ -f "$DIR/bin/router.py" ]] && fail "アンインストール後も router.py が残る"
[[ -f "$DIR/data/usage.jsonl" && -f "$DIR/config.json" ]] || fail "アンインストールで記録・設定が消えた"
pass "uninstall：タイマーと本体を外し、記録と設定は残した"
echo "smoke: all passed"
