# claude-codex-router

*日本語* ・ バージョン **0.1.0**（SemVer）

Claude Code と Codex CLI の両方を使うときに、**次の仕事をどちらに回すか**を、両方の使用枠（レート制限）の余裕から決める小さな道具です。
「リセットまでに使い切れずに消える枠」を先に使い、「足りなくなりそうな枠」を避けます。

```
$ python3 ~/.claude/codex-router/bin/router.py now
判断: claude（使い残す確率。主側 claude）
  claude-session（5h）: 使用 12%  リセット 10-09 02:10（あと 3.0 時間）  上限 $131（今の枠の分）  残り $115・使い切る確率 0.00
  claude-weekly_all（7d）: 使用 30%  リセット 10-14 15:00（あと 135.8 時間）  上限 $1,027（今の枠の分）  残り $719・使い切る確率 0.48
  claude-weekly_scoped:Fable（7d）: 使用 0%  リセット 10-14 15:00（あと 135.8 時間）  モデルを限った枠（表示だけ、判定に使わない）
  codex-7d（7d）: 使用 38%  リセット 10-14 12:30（あと 133.4 時間）  上限 $241（今の枠の分）  残り $149・使い切る確率 0.72
  使い残す確率 Claude 0.52 / Codex 0.28  歯止め なし  満杯 なし
```

## 主側と移譲先

主に使う側（主側）を `--main claude|codex`（または設定の `main`、既定 `claude`）で決めます。もう片方が移譲先です。
Claude を主に使って Codex にサブ処理を渡す人も、Codex を主に使って Claude に渡す人も使えます。

## 枠

API が返す枠をすべて扱います。枠の数・長さ・上限は変わる前提です。

- Claude：使用量の API が返す枠の一覧（5 時間の `session`、7 日の `weekly_all` など）。知らない種類の枠は判断に使わず、注記に出します。
  モデルを限った枠（例：Fable だけを数える 7 日枠）は表示だけで、判断には使いません（その枠で作業が止まるかは、そのモデルを使っているかで決まり、この道具には分からないため）。
- Codex：`codex app-server` が返す枠（長さも API が返します）。

## 使い切る確率

使用量は**料金（USD）**で数えます：会話ログのトークン数 × モデル別の単価。

- **単価**：動作時に [LiteLLM の単価データ](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)から取り、手元に控えます（1 日 1 回取り直し、取れないときは控えを使います）。設定の `prices` でモデルごとに上書きできます。単価の無いモデルは 0 として数え、注記に出します。
- **上限の金額**（枠ごとに、動作時に推定します）：
  - 使用率 u が 10% 以上なら、今の枠の分：枠の始まり（リセット − 枠の長さ）から今までの料金 ÷ u。
  - それ未満なら、記録の分：今から 1 時間ずつさかのぼり、使用率の伸びがその枠 1 つ分（100%）になるまでの料金 ÷ 使用率の伸び。
  - 上限が変われば、その枠 1 つ分を使ううちに新しい値に移ります。
- **これから使う料金の分布**：過去の料金（15 分ごと）から、今からリセットまでに使う料金 S の分布を作ります。
  - 24 時間先まで：似た時点のその後・同じ時刻の実績・「使うか」×「いくら使うか」のシミュレーション・同じ時刻の実績を直前 7 日／3 日の水準で割り戻したもの・日の合計の見込みを時刻で割り振ったもの、を混ぜます。混ぜる割合は、その環境の記録から 1 日 1 回学び直します（学べるまでは同梱の割合）。
  - それより先：似た時点のその後・直前 7 日の水準で割り戻したもの・日の合計の見込み、を同じ割合で混ぜます。
  - 休日（土日と設定の `holidays`）は平日と分けて扱います。
- **使い切る確率 X**（次のリセットまでに上限に達する確率）＝ P(S ≥ 上限 × (1 − u/100))。**使い残す確率**＝ 1 − X。
- 料金の記録が 1 日分に満たない側は、X を出しません（判断ではその枠を見ません）。記録が先の時間より短いあいだは、その時間の実績が要る見込みを外して混ぜます。
  見込みの当たり具合を確かめたのは、料金の記録が 2 週間分以上あるときだけです。

## 判断のしかた

次の順に決めます。先の段で決まれば、後の段は見ません。

### 1. 上限

片方だけがどれかの枠で上限（`full`、既定 98%）に達していれば、もう片方に回します。両方が達しているときは次の段へ進みます。

### 2. 歯止め

主側のどれかの枠で使い切る確率 X が `guard_risk`（既定 0.5）を超えるなら、移譲先に回します。
主側が上限に達すると、主側で作業を回している側も止まるためです（移譲先が上限に達しても、主側に戻せば作業は続きます）。
移譲先の短い枠（1 日未満）の X が `guard_risk` を超えるなら、主側に回します。

### 3. 使い残す確率の比較

長い枠（1 日以上）の使い残す確率の、各側の最小のものを比べ、大きい側に回します。短い枠はこの比較に入れません。

- 読み方：使い残す確率が 0.9 なら、これまでと同じように使うと 9 割の見込みで使い残すので、その枠を先に使うほど消える枠が減ります。0.1 なら、9 割の見込みで使い切ります。
- 両方がちょうど等しければ `tie_default`（既定 codex）。X の出せる長い枠が無い側は 0.5 とみなします。
- 確率は標本で数えるので、±0.03 程度の揺れがあります（同じ入力なら同じ値です）。直前の使い方で見込みが変わるため、使い方にむらのある側は 15 分ごとに値が大きく動くことがあります。

## 動作環境

- **OS**：Linux／WSL2。macOS は試験的な対応です（下の「macOS について」）。Windows ネイティブは対象外。
- **依存**：`python3`（3.8 以上。標準ライブラリだけ）。
- **Claude**：使用量の上限があるサブスクリプション（Pro／Max／Team など）で Claude Code にログインしていること。
- **Codex**：Codex CLI（`codex`）が入っていること。無ければ Codex 側は読めません（判断は Claude を返します）。

## インストール

### 方法 A：ワンライナー
```bash
curl -fsSL https://raw.githubusercontent.com/k-tashiro-arent/claude-codex-router/main/bootstrap.sh | bash
```
固定版は `CLAUDE_CODEX_ROUTER_REF=v0.1.0` を付けます。

### 方法 B：git clone
```bash
git clone https://github.com/k-tashiro-arent/claude-codex-router.git
cd claude-codex-router
./install.sh
```

インストーラは次を行います。

1. `router.py` と、混ぜる割合の既定（`weights-default.json`）を `~/.claude/codex-router/` に置く（`CLAUDE_CODEX_ROUTER_DIR` で変更可）。
2. 既定の `config.json` を置く（既にあれば上書きしない）。
3. 5 分おきに使用量を記録するタイマーを置く。使えるものを次の順で 1 つ選びます（`CLAUDE_CODEX_ROUTER_SCHEDULER=systemd|launchd|cron|none` で指定可）。
   - systemd のユーザータイマー（`claude-codex-router-sample.timer`）
   - macOS の launchd（`~/Library/LaunchAgents/io.github.k-tashiro-arent.claude-codex-router.plist`）
   - crontab（`# claude-codex-router` の印の付いた 1 行）

タイマーから `codex` を見つけられるよう、インストール時の `codex` の場所を PATH に書き込みます。`codex` を入れ直した・node の版を変えたときは、`install.sh` を再実行してください。

過去の記録を取り込むと、すぐに自分の記録で判断できます。
```bash
python3 ~/.claude/codex-router/bin/router.py import   # 会話ログの料金・~/.codex/sessions のログの使用率・claude-token-pace の記録（在れば）
```
取り込まなくても、最初の `sample`（タイマー）が会話ログの料金を読み込みます。

## 使い方

| コマンド | すること |
|---|---|
| `router.py now [--main claude\|codex]` | 両方を読んで判断を表示し、記録にも足す |
| `router.py now --json [--main claude\|codex]` | 同じ判断を JSON で（他の道具から使う） |
| `router.py sample` | 両方を読んで記録に足し、会話ログの料金を料金の記録に足す（タイマーが呼ぶ） |
| `router.py calibrate` | 料金の記録から混ぜる割合を学び直す（`sample` が 1 日 1 回呼ぶ） |
| `router.py import` | この環境にある過去の記録を取り込む（重複は足さない） |

`now` の終了コード：`0` 両方読めた／`1` 片方だけ読めた（読めた側を返す）／`2` どちらも読めない（`tie_default` を返す）。

`now --json` の形（値は例。`windows` は枠ごと）：
```json
{"pick": "claude", "main": "claude", "reason": "使い残す確率", "tie": false, "guard": [], "full": [],
 "claude_L": 0.515, "codex_L": 0.28,
 "windows": {"claude-session": {"u": 12.0, "r": 1791479400, "win": 300, "scoped": false, "X": 0.0,
                                "limit_usd": 131.15, "limit_src": "now", "remain_usd": 115.41, "rem_h": 3.01}, "...": {}},
 "errors": [], "notes": []}
```
`reason` は `満杯`・`歯止め`・`差なし`・`使い残す確率`・`Codex が読めない`・`Claude が読めない`・`どちらも読めない（既定）` のどれかです。
`limit_src` は上限の出し方（`now` 今の枠の分／`hist` 記録の分）です。

## 設定

`~/.claude/codex-router/config.json`
| キー | 既定 | 意味 |
|---|---|---|
| `main` | `claude` | 主側（`--main` で上書き） |
| `guard_risk` | 0.5 | 主側のどれかの枠の「使い切る確率」がこれを超えたら主側に回さない |
| `tie_default` | `codex` | 両方の使い残す確率がちょうど等しいときの側 |
| `full` | 98 | これ以上の使用率の側は使えないとみなす |
| `claude_min_interval` | 60 | この秒数以内に読んだ Claude の記録があれば、Claude の使用量の API を呼ばずにそれを使う（0 で無効） |
| `holidays` | `[]` | 土日のほかに休日として扱う日（`"YYYY-MM-DD"` の一覧） |
| `prices` | `{}` | 単価の上書き。モデル名ごとに 100 万トークンあたりの USD：`in`（入力）・`out`（出力）・`cw5m`・`cw1h`（キャッシュ書き込み 5 分・1 時間）・`cr`（キャッシュ読み込み） |

## 記録とデータの扱い

- 記録は `~/.claude/codex-router/data/` に溜まります。
  - `usage.jsonl`：使用率（1 行＝1 つの枠を 1 回読んだ値。5 分おきで 1 日に 1,000 行強）。
  - `cost-claude.jsonl`・`cost-codex.jsonl`：会話ログから写したトークン数（料金は単価から都度計算します）。Claude Code の会話ログは既定で 30 日たつと消えるので、読んだ分をここに溜めます。
  - `litellm-prices.json`：単価データの控え。`weights.json`：学んだ混ぜる割合。
- 料金はこの環境の会話ログの分だけです。ほかの端末・claude.ai・Web で使った分は入りません（上限の金額を低く見積もる向きに効きます）。
- 複数の Claude Code のセッションが書いた記録（claude-token-pace からの取り込み）には、新しい値の間に古い値が混ざります。使用率が下がった値は古い値として除き、0 近くまで戻ってそのまま続くときだけ、枠の途中のリセットとみなします。
- 外へ出るのは次の読み取りだけです。使用量の読み取りはどちらもモデルを呼ばないので、使用枠は減りません。
  - Claude：`GET https://api.anthropic.com/api/oauth/usage`（Claude Code の `/usage` と同じ）。
  - Codex：`codex app-server` の `account/rateLimits/read`（`codex` 自身が通信します）。
  - 単価：LiteLLM の単価データ（GitHub から 1 日 1 回）。
- Claude の使用量の API は、短い間に続けて呼ぶと 429（呼び過ぎ）を返します。そのため次のようにします。どちらの場合も、そのことを `notes` に書き、`sample` はその回の Claude を記録しません。
  - `claude_min_interval` 秒（既定 60 秒）以内に読んだ記録があれば、API を呼ばずにそれを使います。
  - それでも 429 が返ったときは、呼び直さず、記録にある 10 分以内の最新の値を使います。10 分以内の記録が無ければ、Claude は読めなかった扱いです。
- Claude のトークンは `$CLAUDE_CONFIG_DIR`（既定 `~/.claude`）の `.credentials.json` を**読むだけで、更新しません**（更新すると Claude Code 本体の認証を壊しうるため）。

## 知っておくこと

- Claude の使用量の API は**公開文書にありません**。Claude Code の版が上がると形が変わるおそれがあります。
- `codex app-server` は Codex CLI で**試験中（experimental）**と表示される機能です。
- LiteLLM の単価データは、コミュニティが保守している第三者のものです。新しいモデルが載るまでに遅れることがあり、提供元の資料と違うこともあります（違うときは `prices` で上書きしてください）。また、サブスクリプションの枠が API の単価と同じ比で数えているとは限りません。
- Claude Code をしばらく起動しないとトークンの期限が切れ、その間は Claude の使用量を記録できません（Codex だけ記録されます）。
- 超過分の利用（extra usage）が有効なアカウントで上限に達したとき、作業が止まるのか有料で続くのかによって、歯止めの意味が変わります。
- 判断器に従って振り分けを変えたとき、どちらかが上限に達しないかは、過去の記録からは分かりません（記録は、そのとき実際に回した結果だからです）。使い始めは一部の仕事から回し、歯止めが働くかを見てください。

### macOS について

- Claude Code が資格情報を Keychain に置いている場合は、`security find-generic-password -s "Claude Code-credentials"` で読みます。
- タイマーは launchd に置きます。
- どちらも試験的な対応です。うまく動かないときは issue で知らせてください。

## アンインストール
```bash
bash ~/.claude/codex-router/uninstall.sh
```
タイマーと `router.py` を外します。記録と設定は残します（完全に消すには `rm -rf ~/.claude/codex-router`）。

## 開発
```bash
python3 -m unittest discover -s test   # 単体テスト
bash test/smoke.sh                     # インストールからアンインストールまで（偽の HOME・codex・使用量 API・crontab・単価・会話ログ）
```

## ライセンス
MIT
