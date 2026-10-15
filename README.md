# claude-codex-router

*日本語* ・ バージョン **0.1.0**

Claude のプラン(Pro・Max など)と ChatGPT のプラン(Plus・Pro など。Codex を含む)を両方使っている人のためのツールです。作業の一部をもう一方に任せるときに、Claude と Codex のどちらに任せるとよいかを判定します。

- **使い残しが減る。** どちらのプランにも、5 時間・1 週間などの期間ごとに使える量の上限(使用枠)があり、使わなかった分は期間が終わると消えます。余りそうな方に回すので、消える分が減ります。
- **止まりにくい。** 主に使う側(移譲元)が期間の終わりまでに上限に達しそうなら、もう一方(移譲先)に回します。
- **手間が無い。** 同梱の移譲のスキル(Claude Code・Codex に作業の任せ方を教える手順書)が、任せるたびに判定を呼び、選ばれた側に渡します。判定が移譲元と同じ側なら、その側の中で行います(Claude Code ならサブエージェント、Codex なら Codex がそのまま自分で行う)。

## 動作環境

- Linux/WSL2・macOS(Windows ネイティブは対象外)。`python3`(3.8 以上)と `git`。
- 使用枠のあるプランで Claude Code と Codex にログインしていること。API キーでの利用は対象外です(期間ごとの上限がありません)。
- 片方だけログインしているときは、判定はいつもその側です(ログインしていない側は選びません)。その間もその側の記録は溜まり、もう片方にログインすれば、溜めた記録で比べ始めます。

## インストール

```bash
curl -fsSL https://raw.githubusercontent.com/k-tashiro-arent/claude-codex-router/main/bootstrap.sh | bash
```
リポジトリを手元に残したいときは、`git clone https://github.com/k-tashiro-arent/claude-codex-router.git` してから `./install.sh` を実行します。更新は、同じことをもう一度行います(設定と記録は残ります)。

インストーラは次を行います。
1. `~/.claude-codex-router/` に判定器(`bin/router.py`)と設定(`config.json`)を置く。
2. 5 分おきに使用率と料金(使った量を API の単価で換算した額。請求額ではありません)を記録するタイマーを置く(systemd・launchd・cron のうち使えるもの)。
3. 移譲のスキルを入れるかを聞く。
4. 判定を 1 回実行して結果を表示し、両方の使用率を読めたかを知らせる。このとき Claude Code・Codex の利用履歴(PC に保存されたやり取りの記録)を全部読むので、数十秒かかることがあります。

入れたら、スキルを使わせる 1 行を指示のファイルに自分で書き足します(インストーラは書く行を表示します)。
- Claude Code:`~/.claude/CLAUDE.md` に `- When you split off work and delegate it, follow the codex-offload skill.`
- Codex:`~/.codex/AGENTS.md` に `- When you split off work and delegate it, follow the claude-offload skill.`

ネットワークに出るのは、使用率と単価の読み取りだけです(下の「数えるもの・外へ出るもの」)。記録が短いあいだは予測が外れやすく、溜まるにつれて当たりやすくなります。

## 使い方

普段は何もしません。判定を自分で見たいときは次を実行します。

```
$ python3 ~/.claude-codex-router/bin/router.py now
pick: codex (reason leftover, main claude)
  claude-session (5h): used 40%  resets 10-13 21:59 (in 3.4 h)  limit $150 (current window)  remaining $90, exhaust probability 0.05
  claude-weekly_all (7d): used 70%  resets 10-14 14:59 (in 20.4 h)  limit $1,000 (current window)  remaining $300, exhaust probability 0.30
  claude-weekly_scoped:Fable (7d): used 5%  resets 10-14 14:59 (in 20.4 h)  model-scoped window (shown only, not used for the decision)
  codex-7d: used 10%  resets 10-20 14:04 (in 163.5 h)  limit $200 (history)  remaining $180, exhaust probability 0.10
  expected leftover Claude $70 / Codex $90  (leftover probability Claude 0.70 / Codex 0.90)  guard none  full none
```
- 1 行目：任せる先(`pick`)、理由(`reason`。下の「判定のしかた」)、移譲元(`main`)。
- 使用枠ごと：使用率(`used`)、リセットの時刻、推定した上限の金額(`limit`。`current window` は今の使用枠から、`history` は過去の記録から推定)、上限までの残り(`remaining`)、リセットまでに上限に達する確率(`exhaust probability`)。Fable の行は、そのモデルだけを数える使用枠で、表示だけです。
- 最後の行：それぞれのプランで使われずに消えると見込まれる料金(`expected leftover`)と使い残す確率(いちばん長い使用枠で、リセットまでに上限に達しない確率)、歯止め(移譲元が上限に達しそうなときに移譲先へ回すこと)で回した使用枠(`guard`)、満杯の使用枠(`full`)。

この例では、移譲元(Claude)が上限に達しそうではないので、消える見込みの金額($70 と $90)を比べ、大きい Codex に回しています。消える見込みは、いちばん長い使用枠の残りから、そのリセットまでに使うと見込まれる分を引いた額です(この例の Claude なら $300 から)。確率で比べるのは、金額が出せないときだけです。表示は英語です。

**移譲先を固定する**:`echo codex > ~/.claude-codex-router/force`(`claude` も可)。このファイルがあるあいだ、移譲のスキルは判定を呼ばずにその側を選び、消すと判定に戻ります(`now` の表示は変わりません)。

**移譲元の設定**:設定の `main` が効くのは、自分で実行する `now` だけです。移譲のスキルは自分の側を移譲元として渡すので(`codex-offload` は Claude、`claude-offload` は Codex)、設定は要りません。

## 知っておくこと

- 使用率を読む API は、Claude・Codex とも**公開文書にありません**。サービスの側で形が変わると、読めなくなるおそれがあります。
- 料金は利用履歴のトークン数から出します。数えるのはこの PC の利用履歴だけなので、claude.ai やほかの PC で多く使っていると、上限の金額を低く見積もります。単価は LiteLLM の単価データ(コミュニティが保守)から取るので、新しいモデルでは遅れることがあります(設定の `prices` で上書きできます)。プランの使用枠が API の単価と同じ比で数えているとも限りません。
- 一方を読めないときは、読めた方を選びます。例外は、読めたのが移譲元で上限に達しそうなときです。そのときは、読めない側でもログインしていれば(通信の失敗やトークンの期限切れで、読めないだけのとき)、そちらに回します。
- 移譲のスキルの `write`(編集あり)は、利用者に確かめずに編集とコマンドの実行をさせます。`claude-offload` を入れると書かれる Codex の規則は、そのスキルの `pick.sh` と `run.sh` を、聞かずに Codex のサンドボックスの外で動かすことを許します。
- 上限を超えた分を有料で使える設定(extra usage)のアカウントでは、上限に達しても止まらず有料で続くので、歯止めの必要は薄れます。
- このツールで振り分けると使い方が変わりますが、予測は変わる前の記録から作るので、その変化を織り込めません。どちらかが上限に達しないかを確かめるため、使い始めは一部の仕事から回してください。

## 設定

`~/.claude-codex-router/config.json`
| キー | 既定 | 意味 |
|---|---|---|
| `main` | `claude` | 移譲元(`now --main` で上書き) |
| `guard_risk` | 0.5 | 移譲元のどれかの使用枠で、上限に達する確率がこれを超えたら移譲先に回す |
| `tie_default` | `codex` | 差が無いとき、比べられないときに選ぶ側。どちらも読めないときは、片方だけログインしていればその側、それ以外はこの側 |
| `full` | 98 | 使用率がこれ以上の使用枠を満杯とみなす |
| `claude_min_interval` | 60 | この秒数以内に読んだ Claude の記録があれば、API を呼ばずにそれを使う(0 で無効) |
| `prices` | `{}` | 単価の上書き。モデル名ごとに 100 万トークンあたりの USD:`in`・`out`・`cw5m`・`cw1h`(キャッシュ書き込み)・`cr`(キャッシュ読み込み)。書かなかったキーは LiteLLM の値、それも無ければ `in` と同じ |

```json
{"main": "claude", "prices": {"my-model": {"in": 3, "out": 15, "cw5m": 3.75, "cw1h": 6, "cr": 0.3}}}
```

### 環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `CLAUDE_CODEX_ROUTER_DIR` | `~/.claude-codex-router` | 置き場(変えたら、判定器・スキル・アンインストーラを動かすときにも同じ値を設定) |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code のログイン情報と利用履歴の場所 |
| `CODEX_HOME` | `~/.codex` | Codex のログイン情報と利用履歴の場所 |
| `CLAUDE_CODEX_ROUTER_FORCE_FILE` | `$CLAUDE_CODEX_ROUTER_DIR/force` | 移譲先を固定するファイル |
| `CLAUDE_CODEX_ROUTER_PICK_TIMEOUT` | 90 | `pick.sh` が判定を待つ秒数 |
| `CLAUDE_CODEX_ROUTER_SCHEDULER` | `auto` | インストーラが置くタイマー(`auto`・`systemd`・`launchd`・`cron`・`none`)。前に置いた別の種類のタイマーは外しません |
| `CLAUDE_CODEX_ROUTER_SKILLS` | (聞く) | インストーラが入れるスキル(`claude`=`codex-offload`・`codex`=`claude-offload`・`both`・`none`) |
| `CLAUDE_CODEX_ROUTER_SKILLS_REPLACE` | (聞く) | 同じ名前のスキルがあるとき置き換えるか(`yes`・`no`) |
| `CLAUDE_CODEX_ROUTER_REF` | `main` | ワンライナーで入れる版(ブランチ・タグ・コミット。`curl … \| CLAUDE_CODEX_ROUTER_REF=<版> bash` のように `bash` の前に付ける) |
| `CLAUDE_CODEX_ROUTER_REPO` | このリポジトリ | ワンライナーで取ってくるリポジトリ |
| `TOKEN_PACE_DIR` | `~/.claude/token-pace` | [claude-token-pace](https://github.com/k-tashiro-arent/claude-token-pace)(Claude の使用率を記録する別のツール)の記録の場所。あれば取り込みます |
| `XDG_CONFIG_HOME` | `~/.config` | systemd のタイマーの置き場 |

タイマーはシェルの環境変数を引き継がないので、インストーラは `CLAUDE_CODEX_ROUTER_DIR`・`CLAUDE_CONFIG_DIR`・`CODEX_HOME`・`TOKEN_PACE_DIR` の値をタイマーに書き込みます。変えたら、インストールをやり直してください。

## アンインストール
```bash
bash ~/.claude-codex-router/uninstall.sh
```
タイマー・判定器・インストーラが入れたスキルと Codex の規則を外します。記録と設定は残します(完全に消すには `rm -rf ~/.claude-codex-router`)。入れたあとに書き換えたスキルと、`CLAUDE.md`・`AGENTS.md` に書いた 1 行は残るので、自分で消してください。インストールのときに置き場や `XDG_CONFIG_HOME`・`CODEX_HOME` を変えていたら、同じ値を付けて実行します。

## 移譲のスキル

| スキル | 使うもの | 置き場 | 任せる先 |
|---|---|---|---|
| `codex-offload` | Claude Code | `~/.claude/skills/codex-offload/` | Codex(`codex exec`)。判定が Claude なら Claude のサブエージェント |
| `claude-offload` | Codex | `~/.agents/skills/claude-offload/` | Claude Code(`claude -p`)。判定が Codex なら Codex がそのまま自分で行う |

- `pick.sh`:任せるたびに呼ばれ、判定を英語の 1 行(`<codex|claude> <理由>`)で返します。判定器が入っていない・答えないときは、片方だけログインしていればその側、それ以外(両方ログインしているときと、どちらもしていないとき)は設定の `tie_default` を返します。
- `run.sh`:移譲先に 1 回渡し、依頼文・結果・変更したファイルを残します。`read` は読み取りだけ、`write` は編集とコマンドの実行あり(`codex-offload` は `-s workspace-write`、`claude-offload` は `--permission-mode bypassPermissions`)。
- `SKILL.md`(手順)・`preamble.md`(移譲先に渡す規則と報告の形)は、自分の使い方に合わせて書き換えて構いません。書き換えたスキルは、更新のときも上書きしません(新しい版にするには、そのスキルを消してからインストールし直します)。インストーラが入れたものではない同じ名前のスキルがあれば、置き換えるかを聞き、置き換えるときは元を `~/.claude-codex-router/skill-backups/` に移します。
- スキルを使うかは Claude Code・Codex が指示のファイルに従うかで決まり、強制はされません。
- 判定器も Claude Code も Codex のサンドボックスの中では動かないので、`claude-offload` を入れると、インストーラが Codex の規則(`~/.codex/rules/claude-codex-router.rules`)を書きます。読み込むには Codex を起動し直します。

手で入れるときは、`examples/` の 2 つを上の置き場に写し、`SKILL.md` の中のパスを絶対パスに書き換えます。Codex の規則は次の形です。
```
prefix_rule(pattern=["/home/you/.agents/skills/claude-offload/pick.sh"], decision="allow", justification="claude-codex-router: pick the delegation target")
prefix_rule(pattern=["/home/you/.agents/skills/claude-offload/run.sh"], decision="allow", justification="claude-offload: hand work to Claude Code")
```

## 他のツールから使う

### 判定の結果(`now --json`)

```json
{"pick": "codex", "main": "claude", "reason": "leftover", "tie": false, "guard": [], "full": [],
 "claude_left_usd": 70.12, "codex_left_usd": 89.6, "claude_L": 0.7, "codex_L": 0.9,
 "windows": {"claude-session": {"u": 40.0, "r": 1791896399, "win": 300.0, "win_known": true, "win_lo": 300.0, "win_hi": 300.0,
                                "scoped": false, "X": 0.05, "limit_usd": 150.0, "limit_src": "now", "rem_h": 3.4, "remain_usd": 90.0}, "...": {}},
 "errors": [], "notes": ["Claude: used the record from 18:35:12 (within 60 s; API not called)"]}
```
(値は例)

| 終了コード | 意味 |
|---|---|
| `0` | 両方読めた |
| `1` | 一方だけ読めた(判定は出ます) |
| `2` | どちらも読めない(選ぶのは、片方だけログインしていればその側、それ以外は `tie_default`) |
| `64` | `--main` の値が違う、または知らないコマンド |
| `70` | 予期しないエラーで止まった(不具合。詳細は標準エラー) |

- `reason`:`full`(満杯)・`guard`(歯止め)・`leftover`(消える見込みの料金を比べた)・`leftover_prob`(料金で比べられず、使い残す確率で比べた)・`tie`(差が無い、比べられない)・`codex_unreadable`・`claude_unreadable`(ログインしているが今は読めない)・`codex_not_logged_in`・`claude_not_logged_in`・`both_unreadable`。
- `guard`:歯止めで回したときの、移譲元の使用枠。`full`:満杯の使用枠。
- `claude_left_usd`・`codex_left_usd`:それぞれのプランで消えると見込まれる料金(USD)。`claude_L`・`codex_L`:いちばん長い使用枠の使い残す確率。出せなければ `null`。
- 使用枠ごと:`u` 使用率(%)・`r` リセット(Unix 時刻)・`rem_h` リセットまでの時間・`win` 長さ(分)・`win_known` 長さが分かっているか(`false` は下限だけ)・`win_lo`・`win_hi` 長さの範囲・`scoped` モデルを限った使用枠か・`X` 上限に達する確率・`limit_usd` 上限の金額・`limit_src`(`now` 今の使用枠から/`hist` 過去の記録から。表示の `current window`・`history`)・`remain_usd` 残りの金額。上限の金額が出せない使用枠は `basis` が `usage` で、`remain_pct` が残りの使用率(%)。`note` 注記。

### 使用率の予測(`forecast.json`)

`sample`(タイマーが 5 分おきに呼ぶ)は、使用枠ごとの、リセットまでの使用率の見込みを `~/.claude-codex-router/data/forecast.json` に書きます。グラフに予測の幅を描くなどに使えます。ファイルは丸ごと置き換えるので、書きかけを読むことはありません。書けなかったときは前のファイルが残るので、`t` で古さを確かめてください。

```json
{"t": 1791895140, "n": 400, "notes": [],
 "windows": {"claude-session": {"side": "claude", "u": 20.0, "r": 1791900000, "win": 300.0, "win_known": true, "win_lo": 300.0, "win_hi": 300.0,
                                "basis": "cost", "limit_usd": 150.0, "step_s": 180.0,
                                "ts": [1791895320.0, 1791895500.0, "...", 1791900000.0],
                                "paths": [[20.0, 20.4, "...", 41.5], [20.3, 21.0, "...", 63.2], "..."]}, "...": {}}}
```
(値は例。途中を省いています)
- `t` 予測した時刻、`n` 見込みの数(どの使用枠も同じ)。使用枠ごとの値は `now --json` と同じで、`basis` は料金から予測したら `cost`、使用率の伸びからなら `usage`。
- `ts`:刻みの時刻。使用枠の長さの 100 分の 1(`step_s` 秒)ごとにリセットからさかのぼって作り、`t` より後だけを入れます。最後はリセットの時刻です。
- `paths`:`n` 本の見込み。1 本は `ts` の各時刻の使用率(%)で、過去に実際に使った流れ 1 つに沿うので、時刻とともに下がりません。上限が無かった場合の量なので 100 を超えることがあり、100 以上の見込みはその時刻までに上限に達するものです。予測できない使用枠は `null`。モデルを限った使用枠と、長さがまだ分からない使用枠は入りません。

## 詳しい仕組み

### 判定のしかた

両方読めたときは、次の順に決めます。先の段で決まれば、後の段は見ません。

1. **満杯**:一方だけに、使用率が `full`(既定 98%)以上の使用枠があれば、もう一方(両方にあれば次の段へ)。
2. **歯止め**:移譲元のどれかの使用枠で、上限に達する確率が `guard_risk`(既定 0.5)を超えたら、移譲先。移譲元が止まると任せた作業も止まりますが、移譲先が止まっても移譲元に戻せば作業は続くので、移譲先には歯止めを置きません。
3. **使い残しの比較**:それぞれのプランで使われずに消えると見込まれる料金を比べ、大きい方。料金が消えるのは、いちばん長い使用枠のリセットのときです(短い使用枠で残った分は、長い使用枠でまだ使えます)。それまでの使い方の見込みを多数作り、どの使用枠の上限も超えない範囲で使えたのに使わなかった料金の平均を出します。料金で上限が出せない使用枠があれば、いちばん長い使用枠の使い残す確率で比べます。差が無ければ `tie_default`。

一方しか読めないときは、読めた方です(上の「知っておくこと」)。

### 料金と予測

- 使用率は使用枠ごとの割合なので、上限の大きさが違う Claude と Codex は、使用率のままでは比べられません。そこで使った量を料金に揃えます。
- **上限の金額**:使用率と使用枠の数・長さはサービスから読み(長さは、名前で示さなければ記録から推定)、上限の金額はこちらで推定します。今の使用枠で使った料金 ÷ 使用率から出し、使用率がまだ低いときは過去の記録(使用率の伸びとそのあいだの料金)から出します。
- **これから使う料金**:自分の過去の記録から作ります。過去の各時点から実際に使った料金を、今と状態(直前の使い方と時刻)が近い時点・新しい時点ほど重くして集めます。曜日や休みの日は決め打ちせず、ほかの人の記録も使いません。上限に近づいて使い控えた期間は、その前の速さで使い続けたとみなします。
- 見込みは乱数で作りますが、乱数の種を決めているので、同じ記録からはいつも同じ値になります(記録が増えると少し動きます)。
- 上限の金額が出せない使用枠(この PC にその側の利用履歴が無いときなど)は、使用率の伸びの記録から同じように予測します(表示に `(from usage growth)`)。

### 使用枠

- 使用枠は、そのプランのほかの使い方(claude.ai・ChatGPT など)とも共有されます。使用率はプラン全体の値なので、ほかで使った分も入ります。
- Claude の使用枠の長さは、サービスが名前(`five_hour`・`seven_day` など)で示すものを使います。名前に長さが無い使用枠(モデルを限った使用枠など)は、5 分おきの記録から推定します。記録がまだ足りないあいだは下限しか分からず、表示に `at least` が付きます。名前の長さが記録と食い違うときは記録を採り、`notes` に書きます。
- モデルを限った使用枠(例:Fable だけを数える使用枠)は表示だけで、判定には使いません(そのモデルを使うかは、このツールには分からないためです)。
- Codex は、サービスが返すすべての使用枠を使います。長さもサービスが返し、名前は長さです(`codex-7d`・`codex-5h`・`codex-90m`)。

### 定期の記録

- systemd では、ログインしていない間もタイマーが動くように、インストーラが linger を有効にします(`loginctl enable-linger`。インストーラが有効にしたものは、アンインストールで戻します)。
- PC(WSL なら WSL)が止まっている間は記録されません。`now` も呼ばれるたびに記録を足すので、判定は続けられます。タイマーが 30 分を超えて止まっていると、`now` の `notes` に書きます。
- 動いているかは、`ls -l ~/.claude-codex-router/data/last-sample` の時刻(最後に記録した時刻)で確かめられます。

### 数えるもの・外へ出るもの

- 料金に入るもの:この PC の Claude Code の利用履歴(`~/.claude/projects`)と Codex の利用履歴(`~/.codex/sessions`。CLI・`codex exec`・この PC の IDE 拡張の分)。
- 入らないもの:ほかの PC、claude.ai・デスクトップ版、Codex のクラウドのタスク、ChatGPT のほかの機能。
- 外へ出るのは次の読み取りだけです。どれもモデルを呼ばないので、使用枠は減りません。
  - Claude:`GET https://api.anthropic.com/api/oauth/usage`(Claude Code の `/usage` と同じ)。トークンは `~/.claude/.credentials.json`(macOS では Keychain の場合も)を**読むだけで、更新しません**。
  - Codex:`GET https://chatgpt.com/backend-api/wham/usage`。トークンは `~/.codex/auth.json` を**読むだけで、更新しません**。
  - 単価:LiteLLM の単価データ(GitHub から 1 日 1 回)。
- Claude の使用率の API は、続けて呼ぶと 429(呼び過ぎ)を返します。`claude_min_interval` 秒以内の記録があれば API を呼ばずにそれを使い、429 が返ったら 10 分以内の最新の記録を使います(`notes` に書きます)。

### 記録のファイル

`~/.claude-codex-router/data/` に溜まります。
- `usage.jsonl`:使用率(1 行=1 つの使用枠を 1 回読んだ値)。Codex の利用履歴に載っている使用率と、claude-token-pace の記録も取り込みます。
- `cost-claude.jsonl`・`cost-codex.jsonl`:利用履歴から写したトークン数(Claude Code の利用履歴は既定で 30 日たつと消えるので、ここに溜めます)。
- `cost-state.json`:利用履歴をどこまで読んだか(消すと全部読み直します)。
- `forecast.json`:使用率の予測。`last-sample`:最後に記録した時刻。`litellm-prices.json`:単価データの控え。

## 開発
```bash
python3 -m unittest discover -s test   # 単体テスト
bash test/smoke.sh                     # インストールからアンインストールまで
```

## ライセンス
MIT
