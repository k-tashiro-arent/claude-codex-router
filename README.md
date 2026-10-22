# claude-codex-router

バージョン **0.1.0**

Claude のプラン(Pro・Max など)と ChatGPT のプラン(Plus・Pro など。Codex はこのプランで使えます)を両方使っている人のためのツールです。Claude Code・Codex が作業の一部をほかに任せるとき(サブエージェントや、もう一方の CLI に渡すとき)に、任せる先を Claude と Codex のどちらにするとよいかを判定します。この README では、作業を任せる元の Claude Code・Codex を「任せる側」、作業を受け取るものを「任せる先」と呼びます。

どちらのプランにも使用枠があります。使用枠とは、5 時間・1 週間などの期間ごとに使える量の上限で、1 つのプランに長さの違う使用枠が同時にあります。使った割合を使用率と呼びます。期間はリセットから次のリセットまでで、使わなかった分は期間が終わると消えます。

- **使い残しが減る。** 余りそうな方に回すので、期間が終わって消える分が減ります。
- **止まりにくい。** 任せる側は、使用枠の上限に達すると作業が止まります。そこで、任せる側の使用枠が期間の終わりまでに上限に達しそうなら、もう一方に回します(歯止め)。
- **選ぶ手間が減る。** 同梱の移譲のスキル(Claude Code・Codex に作業の任せ方を教える手順書。そのまま使え、書き換えても構いません)を入れ、指示のファイル(`CLAUDE.md`・`AGENTS.md`)に 1 行足しておくと、Claude Code・Codex が任せるたびに判定を呼び、選ばれた側に渡します。

このツールの本体は判定器(`router.py`)で、どちらに任せるかを返すだけです。作業を実際に渡すのは移譲のスキルで、入れるかどうかは選べます。判定が見るのは使用枠と料金だけで、作業の中身(どちらのモデルが得意か)は見ません。料金とは、使った量を API の単価で換算した額です。上限の大きさが違う Claude と Codex を同じ物差しで比べるためのもので、請求額ではありません。

## 動作環境

- Linux/WSL2・macOS(Windows ネイティブは対象外)。`python3`(3.8 以上)。
- Claude Code・Codex には、使用枠のあるプランでログインして使うこと。API キーでの利用は、期間ごとの上限が無いので対象外です。
- 移譲のスキルで任せるには、任せる先の CLI も要ります(`codex-offload` は `codex`、`claude-offload` は `claude`)。
- 片方にしかログインしていなくても動きます。そのあいだはログインしている側が選ばれますが、使用率と料金の記録は溜まり、もう片方にログインすれば溜めた記録で比べ始めます。

## インストール

```bash
curl -fsSL https://raw.githubusercontent.com/k-tashiro-arent/claude-codex-router/main/bootstrap.sh | bash
```
ワンライナーは中で `git clone` を使うので、`git` コマンドが入っている必要があります。

リポジトリを手元に残したいときは、`git clone https://github.com/k-tashiro-arent/claude-codex-router.git` するか、[GitHub のページ](https://github.com/k-tashiro-arent/claude-codex-router)から ZIP をダウンロードして展開し、その中で `bash install.sh` を実行します。

更新のしかた(設定と記録は残ります):
- ワンライナーで入れたなら、もう一度実行します。
- clone したなら、`git pull` してから `bash install.sh` を実行します(判定器は `$CLAUDE_CODEX_ROUTER_DIR/` に写して動かすので、`git pull` だけでは変わりません)。
- ZIP で入れたなら、新しい ZIP を取り直して展開し、その中で `bash install.sh` を実行します。

インストーラは次を行います(この README のパスは環境変数で書いています。どれも普段は設定不要で、未設定のときの値は下の「環境変数」の表にあります)。
1. `$CLAUDE_CODEX_ROUTER_DIR/` に判定器(`bin/router.py`)と設定(`config.json`)を置く。
2. 5 分おきに使用率と料金を記録するタイマーを置く(systemd・launchd・cron のうち使えるもの)。
3. 移譲のスキルを入れるかを聞く。Claude Code から任せるなら `codex-offload`、Codex から任せるなら `claude-offload` です(両方も選べます。どちらも、判定に従って任せる先を選びます)。質問は端末から読むので、ワンライナーでも答えられます。スキルが何をするかと、入れたあとの準備は、下の「移譲のスキル」に書いています。
4. 判定を 1 回実行して結果を表示し、両方の使用率を読めたかを知らせる。初めて入れるとき(料金の記録がまだ無いとき)は、Claude Code・Codex の利用履歴(PC に保存されたやり取りの記録)を全部読むので、数十秒かかることがあります。

ネットワークに出るのは、使用率と単価の読み取りだけです(下の「数えるもの・外へ出るもの」)。

## 使い方

入れたあと、利用者は普段何もしません(タイマーが記録し、スキルを入れていれば任せるたびに判定を呼びます)。判定を自分で見たいときは次を実行します(表示は英語です)。`router.py` は `$CLAUDE_CODEX_ROUTER_DIR/bin/router.py` にあります(既定は `~/.claude-codex-router/bin/router.py`)。PATH には入らないので、その場所を付けて実行します。

```
$ router.py now
pick: codex (reason leftover, main claude)
  claude-session (5h): used 40%  resets 10-13 21:59 (in 3.4 h)  limit $150 (history + current window)  remaining $90, exhaust probability 0.05
  claude-weekly_all (7d): used 70%  resets 10-14 14:59 (in 20.4 h)  limit $1,000 (history + current window)  remaining $300, exhaust probability 0.30
  claude-weekly_scoped:Fable (7d): used 5%  resets 10-14 14:59 (in 20.4 h)  model-scoped window (shown only, not used for the decision)
  codex-7d: used 10%  resets 10-20 14:04 (in 163.5 h)  limit $200 (history + current window)  remaining $180, exhaust probability 0.10
  expected leftover Claude $70 / Codex $90  (leftover probability Claude 0.70 / Codex 0.90)  guard none  full none
```
- **1 行目**:`pick` 任せる先(任せる側と同じこともあります。そのときスキルは、Claude Code なら Claude のサブエージェントに渡し、Codex なら自分で行います。下の「移譲のスキル」)・`reason` そう決めた理由(下の「判定のしかた」)・`main` 任せる側。
- **使用枠の行**(1 つの使用枠につき 1 行。値は今の期間のもの):
  - 名前と長さ(Claude は括弧の中。Codex は名前が長さ)。
  - `used` 使用率・`resets` リセットの時刻。
  - `limit` 上限の金額の推定。括弧の中は出し方(下の「料金と予測」)。
  - `remaining` 上限までの残りの金額・`exhaust probability` リセットまでに上限に達する確率。
  - モデルを限った使用枠(例の Fable は Claude のモデルの一つ)は表示だけで、判定には使いません。
- **最後の行**(両方読めたときだけ出ます):
  - `expected leftover` 側ごとに、その側のいちばん長い使用枠のリセットのときに、使われずに消えると見込まれる料金。
  - `leftover probability` その使用枠を使い残す確率(出せなければ `unknown`)。判定に使うのは、上限の金額か上限に達する確率が出せない使用枠があって料金で比べられないときだけです(下の「判定のしかた」)。
  - `guard` 歯止めで回したとき、上限に達しそうだった任せる側の使用枠・`full` 満杯の使用枠(どちらも無ければ `none`)。

この例では、任せる側(Claude)のどの使用枠も `exhaust probability` が歯止めのしきい値(既定 0.5)以下なので、消える見込みの料金($70 と $90)を比べ、大きい Codex に回しています。ほかの表示(`at least` など)は下の「使用枠」「料金と予測」にあります。任せる先を固定するには、下の「移譲のスキル」の「任せる先を固定する」を使います。

タイマーが動いているかは、`$CLAUDE_CODEX_ROUTER_DIR/data/last-sample` の更新時刻(定期の記録を最後に実行した時刻)で確かめられます。

## 移譲のスキル

判定器の判定を使って、作業を任せる先に実際に渡す手順書です。インストーラで入れるか、手で写します。

| スキル | 使うもの | 置き場 | 判定がもう一方のとき | 判定が任せる側と同じとき |
|---|---|---|---|---|
| `codex-offload` | Claude Code | `$CLAUDE_CONFIG_DIR/skills/codex-offload/` | Codex に渡す(`codex exec`) | Claude のサブエージェントに渡す |
| `claude-offload` | Codex | `~/.agents/skills/claude-offload/`(`CODEX_HOME` を変えても変わりません) | Claude Code に渡す(`claude -p`) | Codex 自身が行う |

**入れたあとの準備**
- それを使わせる 1 行を、指示のファイルに自分で書き足します(インストーラは書く行を表示します)。スキルを使うかは Claude Code・Codex が指示のファイルに従うかで決まり、強制はされません。
  - Claude Code:`$CLAUDE_CONFIG_DIR/CLAUDE.md` に `- When you split off work and delegate it, follow the codex-offload skill.`
  - Codex:`$CODEX_HOME/AGENTS.md` に `- When you split off work and delegate it, follow the claude-offload skill.`
- `claude-offload` を入れたときは、Codex を起動し直します。インストーラが書いた Codex の規則(`$CODEX_HOME/rules/claude-codex-router.rules`)を読み込むためです。この規則は、このスキルの `pick.sh`・`run.sh` を、利用者に許可を求めずにサンドボックスの外で動かすことを許します。`pick.sh` は判定器を通してネットワークと `$CLAUDE_CODEX_ROUTER_DIR` を、`run.sh` は `claude -p` を通してネットワークと `$CLAUDE_CONFIG_DIR` を使うためです。

**中身**
- `pick.sh`:任せるたびに呼ばれ、判定器の判定を英語の 1 行(`<codex|claude> <理由>`)で返します。
  - 判定器が入っていない・答えないときは、ログイン情報が片方にだけあればその側、それ以外(両方にあるか、両方に無い)は設定の `tie_default`(設定が読めなければ `codex`)を返します。
  - ログインの期限切れか、サービスがログインを受け付けないことで、30 分を超えて読めない側があれば(一度も読めていなければすぐ)、1 行の後ろに `ACTION FOR THE USER` で始まる直し方を付けます。スキルの手順は、それを報告の先頭に書かせます。
  - **任せる先を固定する**:`echo codex > "${CLAUDE_CODEX_ROUTER_FORCE_FILE:-${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude-codex-router}/force}"`(`claude` も可。ファイルの場所は `$CLAUDE_CODEX_ROUTER_FORCE_FILE`)。このファイルがあるあいだ、`pick.sh` は判定器を呼ばずにその側を返し、消すと判定に戻ります(中身が `claude`・`codex` 以外なら無視して判定器を呼び、1 行の後ろにそのことを書きます)。判定器はこのファイルを見ないので、`now` は固定に関係なく判定を表示します。
- `run.sh`:もう一方に 1 回渡し、依頼文・結果・変更したファイルを、呼ぶときに指定した置き場に残します。`read`(読み取りだけ)か `write`(編集とコマンドの実行あり)かは、任せる側の Claude Code・Codex が作業に合わせて選びます。
  - `codex-offload`:`codex exec` を、`read` は `-s read-only`、`write` は `-s workspace-write`(作業ディレクトリの中は書き込めるサンドボックス)で起動します。
  - `claude-offload`:`claude -p` を、`read` は `--permission-mode default` で編集の道具(`Edit`・`Write`・`NotebookEdit`)を使わせずに、`write` は `--permission-mode bypassPermissions`(許可の確認をすべて飛ばす)で起動します。
- `SKILL.md`(手順)・`preamble.md`(任せる先に渡す規則と報告の形)は、自分の使い方に合わせて書き換えて構いません。書き換えたスキルは、更新のときも上書きしません(新しい版にするには、そのスキルを消してからインストールし直します)。インストーラが入れたものではない同じ名前のスキルがあれば、置き換えるかを聞き、置き換えるときは元を `$CLAUDE_CODEX_ROUTER_DIR/skill-backups/` に移します。

**手で入れるとき**:リポジトリの `examples/` にある `codex-offload` と `claude-offload` のディレクトリを上の置き場に写します。`CLAUDE_CONFIG_DIR` を変えていて `~/.claude/skills/` 以外に写したときは、`SKILL.md` の中の `~/.claude/skills/` で始まるパスを、写した先の絶対パスに書き換えます(既定の場所なら、そのままで動きます)。`claude-offload` には、Codex の規則を `$CODEX_HOME/rules/claude-codex-router.rules` に次の形で書きます。
```
prefix_rule(pattern=["/home/you/.agents/skills/claude-offload/pick.sh"], decision="allow", justification="claude-codex-router: read both usage windows to pick the delegation target (needs the network and /home/you/.claude-codex-router)")
prefix_rule(pattern=["/home/you/.agents/skills/claude-offload/run.sh"], decision="allow", justification="claude-offload: hand work to Claude Code (needs the network and ~/.claude)")
```
`pattern` の `/home/you` は自分のホームディレクトリに、1 行目の `justification` の `/home/you/.claude-codex-router` は置き場(`$CLAUDE_CODEX_ROUTER_DIR`)の絶対パスに置き換えます。2 行目の `justification` の `~/.claude` は説明の文で、インストーラもこの文字のまま書くので、置き換えは要りません。

## 知っておくこと

- 使用率を読む API は、Claude・Codex とも**公開文書にありません**。サービスの側で形が変わると、読めなくなるおそれがあります。
- 料金は利用履歴に残る使用量(トークン数)から出します。数えるのはこの PC の利用履歴だけなので、claude.ai やほかの PC で多く使っていると、上限の金額を低く見積もります。単価は LiteLLM の単価データ(コミュニティが保守)から取るので、新しいモデルでは遅れることがあります(設定の `prices` で上書きできます)。プランの使用枠が API の単価と同じ比で数えているとも限りません。
- 一方を読めないときは、原則として読めた方を選びます(例外は下の「判定のしかた」)。
- **ログインが切れたとき**:判定器はログインを更新しないので、使わない側のログインは切れたままになることがあります。読めなかった理由がログインにあれば、そのときすぐ、`now` の表示と `now --json` の `login` に、その側と直し方が出ます。ログインの期限切れ・拒否なら「Claude Code・Codex を一度起動し、それでも読めなければ `claude auth login`・`codex login`」、ログイン情報が無ければ「ログインする」です。移譲のスキルを入れていれば、報告の先頭にも書かれます(スキルの報告に書く条件は上の「移譲のスキル」)。
- 上限を超えた分を有料で使える設定(extra usage)のアカウントでは、上限に達しても止まらず有料で続くので、歯止めの必要は薄れます。
- 記録が短いあいだは予測が外れやすく、溜まるにつれて当たりやすくなります。このツールで振り分けると使い方が変わりますが、予測は変わる前の記録から作るので、その変化を織り込めません。使い始めは、`now` の表示(スキルを入れていれば、報告に出る任せた先と理由)を見て、どちらかが早く尽きていないか確かめてください。

## 設定

`$CLAUDE_CODEX_ROUTER_DIR/config.json`
| キー | 既定 | 意味 |
|---|---|---|
| `main` | `claude` | 自分で `now` を実行するときの任せる側(`now --main` で上書き)。移譲のスキルは、そのスキルを使う Claude Code・Codex の側を任せる側として渡すので、この設定は使いません |
| `guard_risk` | 0.5 | 任せる側のどれかの使用枠で、上限に達する確率がこれを超えたらもう一方に回す |
| `tie_default` | `codex` | 決め手が無いときに選ぶ側(消える見込みが同じ・比べられない・どちらも読めず、ログイン情報が両方にあるか両方に無い) |
| `full` | 98 | 使用率がこれ以上の使用枠を満杯とみなす |
| `claude_min_interval` | 60 | この秒数以内に読んだ Claude の記録があれば、API を呼ばずにそれを使う(0 で無効) |
| `prices` | `{}` | 単価の上書き。モデル名ごとに 100 万トークンあたりの USD:`in`・`out`・`cw5m`・`cw1h`(キャッシュ書き込み。保持が 5 分・1 時間)・`cr`(キャッシュ読み込み)。書かなかったキーは LiteLLM の値を使います。LiteLLM に無いモデルは、書かなかったキーを `in` の値で代わりにします。LiteLLM にも `prices` にも `in` が無いモデルは、料金を 0 と数え、`notes` に書きます |

```json
{"main": "claude", "prices": {"my-model": {"in": 3, "out": 15, "cw5m": 3.75, "cw1h": 6, "cr": 0.3}}}
```

### 環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `CLAUDE_CODEX_ROUTER_DIR` | `~/.claude-codex-router` | 置き場(変えたら、判定器・スキル・アンインストーラを動かすときにも同じ値を設定) |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code のログイン情報と利用履歴の場所 |
| `CODEX_HOME` | `~/.codex` | Codex のログイン情報と利用履歴の場所 |
| `CLAUDE_CODEX_ROUTER_FORCE_FILE` | `$CLAUDE_CODEX_ROUTER_DIR/force` | 任せる先を固定するファイル |
| `CLAUDE_CODEX_ROUTER_PICK_TIMEOUT` | 90 | `pick.sh` が判定を待つ秒数 |
| `CLAUDE_CODEX_ROUTER_SCHEDULER` | `auto` | インストーラが置くタイマー(`auto`・`systemd`・`launchd`・`cron`・`none`)。前に置いた別の種類のタイマーは外しません(種類を変えるときは、先にアンインストールしてから入れ直します。アンインストールはどの種類のタイマーも外します) |
| `CLAUDE_CODEX_ROUTER_SKILLS` | (聞く) | インストーラが入れるスキル(値は任せる側:`claude`=`codex-offload`(Claude Code から任せる)・`codex`=`claude-offload`(Codex から任せる)・`both`・`none`) |
| `CLAUDE_CODEX_ROUTER_SKILLS_REPLACE` | (聞く) | 同じ名前のスキルがあるとき置き換えるか(`yes`・`no`) |
| `CLAUDE_CODEX_ROUTER_REF` | `main` | ワンライナーで入れる版(ブランチ・タグ・コミット。`curl … \| CLAUDE_CODEX_ROUTER_REF=<版> bash` のように `bash` の前に付ける) |
| `CLAUDE_CODEX_ROUTER_REPO` | このリポジトリ | ワンライナーで取ってくるリポジトリ |
| `TOKEN_PACE_DIR` | `~/.claude/token-pace` | [claude-token-pace](https://github.com/k-tashiro-arent/claude-token-pace)(使用率を記録する別のツール)の記録の場所(`CLAUDE_CONFIG_DIR` を変えても変わりません)。あれば取り込みます |
| `XDG_CONFIG_HOME` | `~/.config` | systemd のタイマーの置き場 |

タイマーはシェルの環境変数を引き継がないので、インストーラは `CLAUDE_CODEX_ROUTER_DIR`・`CLAUDE_CONFIG_DIR`・`CODEX_HOME`・`TOKEN_PACE_DIR` の値をタイマーに書き込みます。変えたら、インストールをやり直してください。

## アンインストール
```bash
bash "${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude-codex-router}/uninstall.sh"
```
タイマー・判定器・インストーラが入れたスキルと Codex の規則を外します。記録と設定は残します(完全に消すには `rm -rf "${CLAUDE_CODEX_ROUTER_DIR:-$HOME/.claude-codex-router}"`)。入れたあとに書き換えたスキルと、`CLAUDE.md`・`AGENTS.md` に書いた 1 行は残るので、自分で消してください。インストールのときに置き場や `XDG_CONFIG_HOME`・`CODEX_HOME` を変えていたら、同じ値を付けて実行します(スキルは、入れたときに記録した場所から外します)。

## 他のツールから使う

### 判定の結果(`now --json`)

```json
{"pick": "codex", "main": "claude", "reason": "leftover", "tie": false, "guard": [], "full": [],
 "claude_left_usd": 70.12, "codex_left_usd": 89.6, "claude_L": 0.7, "codex_L": 0.9,
 "windows": {"claude-session": {"u": 40.0, "r": 1791896399, "win": 300.0, "win_known": true, "win_lo": 300.0, "win_hi": 300.0,
                                "scoped": false, "X": 0.05, "limit_usd": 150.0, "limit_src": "blend", "rem_h": 3.4, "remain_usd": 90.0}, "...": {}},
 "login": {}, "errors": [], "notes": ["Claude: used the record from 18:35:12 (within 60 s; API not called)"]}
```
(値は例)

`now` は、`--json` の有無にかかわらず、同じ終了コードを返します。

| 終了コード | 意味 |
|---|---|
| `0` | 両方読めた |
| `1` | 一方だけ読めた(判定は出ます) |
| `2` | どちらも読めない(選ぶのは、ログイン情報が片方にだけあればその側、それ以外は `tie_default`) |
| `64` | `--main` の値が違う、または知らないコマンド |
| `70` | 予期しないエラーで止まった(不具合。詳細は標準エラー) |

- `reason`:`full`(満杯)・`guard`(歯止め)・`leftover`(消える見込みの料金を比べた)・`leftover_prob`(料金で比べられず、使い残す確率で比べた)・`tie`(差が無い、比べられない)・`codex_unreadable`・`claude_unreadable`(その側にログイン情報はあるが、今は読めない。ログインの期限切れ・拒否も含む)・`codex_not_logged_in`・`claude_not_logged_in`(その側のログイン情報が無い)・`both_unreadable`。
- `guard`:歯止めで回したときの、任せる側の使用枠。`full`:満杯の使用枠。
- `login`:読めなかった理由がログインにある側ごとに、`state`(`not_logged_in` ログイン情報が無い/`token_expired` ログインの期限切れ/`login_rejected` サービスがログインを受け付けない)・`last_read`(最後に読めた時刻)・`action`(直し方)。期限切れなら `expired_at`、拒まれたなら `http`(コード)も。
- `claude_left_usd`・`codex_left_usd`:それぞれのプランで消えると見込まれる料金(USD)。`claude_L`・`codex_L`:いちばん長い使用枠の使い残す確率。出せなければ `null`。
- 使用枠ごと:`u` 使用率(%)・`r` リセット(Unix 時刻)・`rem_h` リセットまでの時間・`win` 長さ(分)・`win_known` 長さが分かっているか(`false` は下限だけ)・`win_lo`・`win_hi` 長さの範囲・`scoped` モデルを限った使用枠か・`X` 上限に達する確率・`limit_usd` 上限の金額・`limit_src`(`blend` 過去の期間からの値と今の期間からの値の、重みを付けた平均/`hist` 期間の区切りを見ず、今から 1 時間ずつさかのぼった記録から/`now` 今の期間だけから。表示はそれぞれ `history + current window`・`history`・`current window`)・`remain_usd` 残りの金額。上限の金額が出せない使用枠にだけ、`basis`(`usage`)と `remain_pct`(残りの使用率、%)が付きます。`note` 注記。

### 使用率の予測(`forecast.json`)

`router.py sample`(タイマーが 5 分おきに呼ぶ)は、使用枠ごとの、リセットまでの使用率の見込みを `$CLAUDE_CODEX_ROUTER_DIR/data/forecast.json` に書きます。グラフに予測の幅を描くなどに使えます。ファイルは丸ごと置き換えるので、書きかけを読むことはありません。書けなかったときは前のファイルが残るので、`t` で古さを確かめてください。

```json
{"t": 1791884160, "n": 400, "notes": [],
 "windows": {"claude-session": {"side": "claude", "u": 40.0, "r": 1791896399, "win": 300.0, "win_known": true, "win_lo": 300.0, "win_hi": 300.0,
                                "basis": "cost", "limit_usd": 150.0, "step_s": 180.0,
                                "ts": [1791884339.0, 1791884519.0, "...", 1791896399.0],
                                "paths": [[40.0, 40.4, "...", 61.5], [40.3, 41.0, "...", 83.2], "..."]}, "...": {}}}
```
(値は例。途中を省いています)
- `t` 予測した時刻、`n` 見込みの数(どの使用枠も同じ)。使用枠ごとの `u`・`r`・`win`・`win_known`・`win_lo`・`win_hi`・`limit_usd` は `now --json` と同じ意味です。`side` はどちらの側か、`basis` は料金から予測したら `cost`、使用率の伸びからなら `usage`(`now --json` と違い、`forecast.json` ではどの使用枠にも付きます)。
- `ts`:刻みの時刻。使用枠の長さの 100 分の 1(`step_s` 秒)ごとにリセットからさかのぼって作り、`t` より後だけを入れます。最後はリセットの時刻です。
- `paths`:`n` 本の見込み。1 本は `ts` の各時刻の使用率(%)で、過去に実際に使った流れに沿うので(予測する先までの時間が、記録のある期間の半分より長いときは、過去の記録から別々に選んだいくつかの区間をつないだもの)、時刻とともに下がりません。上限が無かった場合の量なので 100 を超えることがあり、100 以上の見込みはその時刻までに上限に達するものです。予測できない使用枠は `null`。モデルを限った使用枠と、長さが分からない使用枠は入りません。

## 詳しい仕組み

### 判定のしかた

両方読めたときは、次の順に決めます。先の段で決まれば、後の段は見ません。モデルを限った使用枠は、どの段でも使いません。

1. **満杯**:一方だけに、使用率が `full`(既定 98%。判定で満杯とみなす使用率)以上の使用枠があれば、もう一方(両方にあれば次の段へ)。
2. **歯止め**:任せる側のどれかの使用枠で、上限に達する確率が `guard_risk`(既定 0.5)を超えたら、もう一方。任せる側が止まると任せた作業も止まりますが、もう一方が止まっても任せる側に戻せば作業は続くので、もう一方には歯止めを置きません。
3. **使い残しの比較**:消えると見込まれる料金が大きい方を選びます。
   - 比べるのは、それぞれのプランで使われずに消えると見込まれる料金です。消えるのは、いちばん長い使用枠のリセットのときです(短い使用枠で残った分は、長い使用枠でまだ使えます)。消える時刻は側ごとに違いますが、それぞれのプランで消える額を比べます。消える額が大きい方に回すほど、両方を合わせて消える分が減るためです。
   - 出し方:これからの使い方の見込みを多数作ります。見込みの 1 つごとに、その使い方をしたうえで、どの使用枠の上限にも当たらずにさらに使えた料金を出し、その平均を取ります。
   - 料金で比べられないとき:どちらかの側に、上限の金額か上限に達する確率が出せない使用枠が 1 つでもあると(長さが分からない使用枠は除きます)、料金では比べられません。そのときは両方の側とも、いちばん長い使用枠の使い残す確率で比べます。使い残す確率は、1 から上限に達する確率を引いた値です。長さが推定で、どれがいちばん長いか決まらないときは、候補の確率のうち小さい方を使います。
   - 比べた値が同じか、使い残す確率がどちらかの側で出せなければ `tie_default`。

長さが分からない使用枠(下の「使用枠」)は、使用率は読めているので満杯の段には使い、歯止めと使い残しの比較には使いません。

一方しか読めないときは、読めた方を選びます。例外は、読めたのが任せる側で、上限に達する確率が `guard_risk` を超えたときです(`reason` は `guard`)。そのときは、読めない側に PC のログイン情報が残っていれば、そちらに回します(通信の失敗やログインの期限切れで、読めないだけと見なします)。ログイン情報が無ければ、作業を渡せないので、任せる側のままです。

### 料金と予測

- 使用率は使用枠ごとの割合なので、上限の大きさが違う Claude と Codex は、使用率のままでは比べられません。そこで使った量を料金に揃えます。
- 使用率と使用枠の数はサービスから読みます。長さは、Codex はサービスが返す値、Claude は同じリセットの時刻を持つ項目の名前が示す値で、そうした名前が無いときや記録と食い違うときは記録から推定します(下の「使用枠」)。上限の金額はサービスが示さないので、こちらで推定します。
- **上限の金額**:上限はめったに変わらないので、過去の期間の記録からも出します。どの場合も「使用率が伸びたあいだの料金 ÷ その伸び(%)× 100」で出し、場合ごとに次のとおりです。
  - 過去の期間と今の期間の両方から出せるとき(表示は `history + current window`):過去の期間から出した値に 20、今の期間から出した値に u(今の期間の使用率 %)の重みを付けて平均します。過去の期間の値は、今の期間の始まりから 1 時間ずつ、使用率の伸びが合わせて 100% になるか記録の始まりに着くまでさかのぼって出します。u が 20% のときに半々になり、使うほど今の期間の値に近づきます。同じ 1% に当たる料金は使い方(キャッシュの割合など)で期間ごとに少し変わるので、今の期間を使うほど(使用率が上がるほど)、今の期間の値を重く見ます。
  - 過去の記録が無いとき(`current window`):今の期間だけから出します。
  - 今の期間の料金が分からないとき(`history`):期間の区切りを見ず、今から同じようにさかのぼって出します。分からないのは次の 3 つのときです。
    1. この PC の料金の記録が期間の途中から始まる。
    2. 今の期間はほかの場所でしか使っていない。
    3. 使用枠の長さが下限しか分からない(表示に `at least` が付くとき)。
  - さかのぼっても出せないとき(さかのぼった分の使用率の伸びが 20% に満たないとき。上の重みの 20 とは別の値です):上の 1 だけに当たるなら、記録より前の料金を 0 とみなして今の期間だけから出します(`current window`)。2 か 3 に当たるなら上限の金額を出さず、使用率の伸びから予測します(下の「上限の金額が出せない使用枠」)。
- **これから使う料金**:自分の過去の記録から作ります。過去の各時点から実際に使った料金を、今と状態(直前の使い方と、1 日のうちの時刻)が近い時点・新しい時点ほど重くして集めます。曜日や休みの日は決め打ちせず、ほかの人の記録も使いません。使用率が 95% 以上になってからリセットまでの間は使い控えたとみなし(予測の入力だけに使う値で、判定の満杯 `full` とは別です)、その前の速さで使い続けたとして扱います。
- 見込みは乱数で作りますが、乱数の種を決めているので、同じ記録からはいつも同じ値になります(記録が増えると少し動きます)。
- 上限の金額が出せない使用枠(この PC にその側の利用履歴が無いときなど)は、使用率の伸びの記録から同じように予測します。表示の `remaining` は残りの使用率になり、`remaining 60%, exhaust probability 0.20 (from usage growth)` のように付きます。

### 使用枠

- 使用枠は、そのプランのほかの使い方(claude.ai・ChatGPT など)とも共有されます。使用率はプラン全体の値なので、ほかで使った分も入ります。
- Claude の使用枠の名前(`session`・`weekly_all` など)は、サービスが返す使用枠の種類です。長さは、同じリセットの時刻を持つ別の項目の名前(`five_hour`・`seven_day` など)が示すものを使います。同じリセットの時刻で長さを示す名前が無い(または 1 つに決まらない)使用枠は、5 分おきの記録から推定します。記録がまだ足りないあいだは下限しか分からず、表示に `(6d, at least)` のように `at least` が付きます。長さを示す名前が無く、記録からまだ下限も出せない使用枠(この README では「長さが分からない使用枠」)は、長さの括弧が付かず、行の後ろに `length unknown (no period has been read yet); used only for the full check` と出ます。名前の長さが記録と食い違うときは記録を採り、`notes` に書きます。
- モデルを限った使用枠(例:Claude のモデル Fable だけを数える使用枠)は表示だけで、判定には使いません(そのモデルを使うかは、このツールには分からないためです)。
- Codex は、サービスが返すすべての使用枠を使います。長さもサービスが返し、名前は長さです(`codex-7d`・`codex-5h`・`codex-90m`)。

### 定期の記録

- systemd では、ログインしていない間もタイマーが動くように、インストーラが linger を有効にします(`loginctl enable-linger`。インストーラが有効にしたものは、アンインストールで戻します)。
- PC(WSL なら WSL)が止まっている間は記録されません。`now` も呼ばれるたびに記録を足すので(移譲のスキルが判定を呼ぶときも、中で `now` を実行します)、判定は続けられます。タイマーが 30 分を超えて止まっていると、`now` の `notes` に書きます。

### 数えるもの・外へ出るもの

- 料金に入るもの:この PC の Claude Code の利用履歴(`$CLAUDE_CONFIG_DIR/projects`)と Codex の利用履歴(`$CODEX_HOME/sessions`。CLI・`codex exec`・この PC の IDE 拡張の分)。
- 入らないもの:ほかの PC、claude.ai・デスクトップ版、Codex のクラウドのタスク、ChatGPT のほかの機能。
- 外へ出るのは次の読み取りだけです。どれもモデルを呼ばないので、使用率は上がりません。
  - Claude:`GET https://api.anthropic.com/api/oauth/usage`(Claude Code の `/usage` と同じ)。ログインのトークンは `$CLAUDE_CONFIG_DIR/.credentials.json`(macOS では Keychain の場合も)を**読むだけで、更新しません**。
  - Codex:`GET https://chatgpt.com/backend-api/wham/usage`。ログインのトークンは `$CODEX_HOME/auth.json` を**読むだけで、更新しません**。
  - 単価:LiteLLM の単価データ(GitHub から 1 日 1 回)。
- Claude の使用率の API は、続けて呼ぶと 429(呼び過ぎ)を返します。`claude_min_interval` 秒以内の記録があれば API を呼ばずにそれを使い、429 が返ったら 10 分以内の最新の記録を使います(`notes` に書きます)。

### 記録のファイル

`$CLAUDE_CODEX_ROUTER_DIR/data/` に溜まります。
- `usage.jsonl`:使用率(1 行=1 つの使用枠を 1 回読んだ値)。Codex の利用履歴に載っている使用率と、claude-token-pace の記録も取り込みます。
- `cost-claude.jsonl`・`cost-codex.jsonl`:利用履歴から写したトークン数(Claude Code の利用履歴は既定で 30 日たつと消えるので、ここに溜めます)。
- `cost-state.json`:利用履歴をどこまで読んだか(消すと全部読み直します)。
- `forecast.json`:使用率の予測。`last-sample`:定期の記録(`router.py sample`)を最後に実行した時刻。`litellm-prices.json`:単価データの控え。

## 開発
```bash
python3 -m unittest discover -s test   # 単体テスト
bash test/smoke.sh                     # インストールからアンインストールまで
```

## ライセンス
MIT
