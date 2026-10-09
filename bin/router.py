#!/usr/bin/env python3
"""claude-codex-router：仕事を Claude と Codex のどちらに回すかを、両方の使用枠の余裕から決める。

主側（main）と移譲先：主に使う側を --main claude|codex（または config の main、既定 claude）で決める。もう片側が移譲先。

枠：API が返す枠をすべて扱う（数・長さ・上限は変わる前提）。
  Claude：使用量 API の limits の各枠（session＝5 時間、weekly＝7 日。知らない種類は判定に使わず注記）。
          モデルを限った枠（scope のある枠）は表示だけで、判定には使わない。
  Codex ：rateLimits の primary・secondary（長さは API が返す）。

使用量は料金（USD）で数える：会話ログのトークン数 × モデル別単価。
  単価は動作時に LiteLLM の単価データから取り、<DIR>/data/litellm-prices.json に控える（1 日より古ければ sample が取り直す。
  取れないときは控え）。config の prices でモデルごとに上書きできる。単価の無いモデルは 0 として数え、注記する。
  料金の記録：<DIR>/data/cost-claude.jsonl・cost-codex.jsonl（会話ログは消えることがあるので、読んだ分を写して溜める）。
  ほかの端末・claude.ai で使った分は入らない。

上限の金額（枠ごとに実行時に推定）：
  使用率 u が LIMIT_NOW_U（10%）以上なら、今の枠の分：100 × 枠の始まり（リセット − 枠の長さ）から今までの料金 ÷ u。
  それ未満なら、記録の分：直前を 1 時間ずつさかのぼり、使用率の伸びがその枠 1 つ分（100%）になるまでの料金 ÷ 使用率の伸び
  （上限に達していた時間と、使用率の記録が始まる前は除く。28 日まで。伸びが 20% に満たなければ使わない）。それも無ければ今の枠の分。
使い切る確率 X（その枠の次のリセットまでに上限に達する確率）＝ P(S ≥ 上限 × (1 − u/100))。
  S は今からリセットまでに使う料金の分布。過去の料金の列（15 分ごと）から標本で作る：
    24 時間先までは wpool：似た時点のその後（knn）・同じ時刻の実績（最近ほど重く）・使うか × いくら（hurdle）・
      同じ時刻の実績を直前 7 日／3 日の水準で割り戻したもの・日の合計の指数平滑を時刻で割り振ったもの を、x ごとに学んだ重みで混ぜる。
      重みは calibrate が記録から学ぶ（<DIR>/data/weights.json）。無いあいだは同梱の重み（<DIR>/weights-default.json）。
    それより先は pool2：knn・7 日の水準で割り戻したもの・日の合計の指数平滑 を同じ割合で混ぜる。
  休日：土日と config の holidays（"YYYY-MM-DD" の一覧）。
  料金の記録が 1 日分に満たない側は X を出さない（判定ではその枠を見ない）。

判断（先の段で決まれば後の段は見ない）：
  1. 上限：片側だけがどれかの枠で上限（config の full、既定 98%）に達していれば、もう片側。両側なら次へ。
  2. 歯止め：主側のどれかの枠の X が config の guard_risk（既定 0.5）を超えるなら移譲先。
     主側が上限に達すると、それを使って作業を回している側も止まる。移譲先が上限でも主側に戻せば作業は続く。
     移譲先の短い枠（1 日未満）の X が guard_risk を超えるなら主側。
  3. 比較：長い枠（1 日以上）の、リセットまでに使い切らない確率 1 − X（使い残す確率）の、各側の最小の大きい側。
     等しければ config の tie_default（既定 codex）。

使用率の記録（各環境で自前で溜める）：
  <DIR>/data/usage.jsonl。1 行 = 1 枠の 1 回の読み {"ts", "side", "id": 枠の種類, "win": 枠の長さ(分), "u": 使用率, "r": リセット時刻,
  "scoped": モデルを限った枠か, "src"}。id の無い古い行は長さから決める（Claude 300→session・10080→weekly_all、Codex は長さ）。
  DIR は環境変数 CLAUDE_CODEX_ROUTER_DIR（既定 ~/.claude/codex-router）。
  読み方（どちらもモデルを呼ばないので枠は減らない）：
    Claude：GET https://api.anthropic.com/api/oauth/usage（Claude Code の /usage と同じ。公開文書に無い）。
            トークンは $CLAUDE_CONFIG_DIR（既定 ~/.claude）/.credentials.json を読むだけで更新しない。
            macOS で Keychain に保管されている場合は security コマンドで読む（試験的な対応）。
    Codex ：codex app-server（stdio の JSON-RPC。試験中の機能）の account/rateLimits/read。

使い方：
  router.py now [--json] [--main claude|codex]   今の判断（両方を読んで記録にも足す）。終了コード 0=両方読めた 1=片側だけ 2=どちらも読めない
  router.py sample      両方を読んで記録に足し、料金の記録を会話ログから足す（install.sh が置くタイマーが 5 分おきに呼ぶ）
  router.py calibrate   料金の記録から wpool の重みを学び直す（sample が 1 日 1 回呼ぶ）
  router.py import      この環境に在る過去の記録を取り込む（会話ログの料金・~/.codex/sessions の使用率・claude-token-pace の記録。重複は足さない）
"""
import bisect
import datetime
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULTS = os.path.join(os.path.dirname(HERE), 'defaults')   # リポジトリから直接動かすとき
DIR = os.path.expanduser(os.environ.get('CLAUDE_CODEX_ROUTER_DIR') or '~/.claude/codex-router')
DATA = os.path.join(DIR, 'data')
USAGE_LOG = os.path.join(DATA, 'usage.jsonl')
COST_LOG = {'claude': os.path.join(DATA, 'cost-claude.jsonl'), 'codex': os.path.join(DATA, 'cost-codex.jsonl')}
COST_STATE = os.path.join(DATA, 'cost-state.json')
PRICE_FILE = os.path.join(DATA, 'litellm-prices.json')
WEIGHTS_FILE = os.path.join(DATA, 'weights.json')
TP = os.path.expanduser(os.environ.get('TOKEN_PACE_DIR') or '~/.claude/token-pace') + '/'   # import の取り込み元（在れば）
CLAUDE_HOME = os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude')
CODEX_HOME = os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex')
PRICE_URL = (os.environ.get('CLAUDE_CODEX_ROUTER_PRICE_URL')   # テスト用（file:// も可）
             or 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json')


def _conf(name):
    """<DIR>/name、無ければリポジトリの defaults/name。"""
    for d in (DIR, REPO_DEFAULTS):
        f = os.path.join(d, name)
        if os.path.exists(f):
            return f
    return os.path.join(DIR, name)


def _load_json(name):
    try:
        return json.load(open(_conf(name)))
    except (OSError, ValueError):
        return {}


CONFIG = {'main': 'claude', 'guard_risk': 0.5, 'tie_default': 'codex', 'full': 98.0, 'claude_min_interval': 60,
          'holidays': [], 'prices': {}, **_load_json('config.json')}
MAIN = CONFIG['main'] if CONFIG['main'] in ('claude', 'codex') else 'claude'
GUARD_RISK = float(CONFIG['guard_risk'])     # 歯止め：主側の使い切る確率がこれを超えるなら移譲先に回す
FULL = float(CONFIG['full'])                 # これ以上はその側を使えないとみなす
try:   # この秒数以内に読んだ Claude の記録があれば API を呼ばずにそれを使う（続けて呼ぶと 429 になる）。0 以下で無効
    CLAUDE_MIN_INTERVAL = float(CONFIG['claude_min_interval'])
except (TypeError, ValueError):
    CLAUDE_MIN_INTERVAL = 60.0
RECENT_MAX_AGE = 600   # 429 のとき代わりに使う記録の古さの上限（秒）。記録は 5 分おきなので 2 回分
TIE_DEFAULT = CONFIG['tie_default'] if CONFIG['tie_default'] in ('claude', 'codex') else 'codex'
HOLIDAYS = frozenset(str(x) for x in (CONFIG.get('holidays') or []))
LONG_WIN = 1440      # この長さ（分）以上の枠を「長い枠」（比較に使う）、未満を「短い枠」とする
LIMIT_NOW_U = 10.0   # 使用率がこれ以上なら上限を今の枠の分で出す
CLAUDE_KIND_WIN = {'session': 300, 'weekly': 10080}   # Claude の枠の group → 長さ（分）


# ---- 単価（LiteLLM） ----
_prices = None
_price_src = None


def fetch_prices(timeout=30):
    """LiteLLM の単価データを取り、控えに書く。返り値は単価データ。"""
    import urllib.request
    with urllib.request.urlopen(PRICE_URL, timeout=timeout) as r:
        d = json.load(r)
    if not isinstance(d, dict) or len(d) < 100:
        raise ValueError('単価データの形が想定と違う')
    os.makedirs(DATA, exist_ok=True)
    tmp = PRICE_FILE + '.tmp'
    json.dump(d, open(tmp, 'w'))
    os.replace(tmp, PRICE_FILE)
    return d


def load_prices(refresh=False):
    """単価データ。refresh で 1 日より古い控えを取り直す。控えが無ければ取りに行く。取れなければ控え（無ければ空）。"""
    global _prices, _price_src
    if _prices is not None and not refresh:
        return _prices
    stale = not os.path.exists(PRICE_FILE) or time.time() - os.path.getmtime(PRICE_FILE) >= 86400
    if not os.path.exists(PRICE_FILE) or (refresh and stale):
        try:
            _prices, _price_src = fetch_prices(), 'fetched'
            return _prices
        except Exception as e:   # noqa: BLE001 — 取れなければ控えを使う
            _price_src = f'取れなかった（{e}）'
    try:
        _prices = json.load(open(PRICE_FILE))
        _price_src = _price_src or ('控え（1 日より古い）' if stale else '控え')
    except (OSError, ValueError):
        _prices = {}
    return _prices


def price(model):
    """100 万トークンあたり USD の dict（in・out・cw5m・cw1h・cr）。config の prices が先。見つからなければ None。"""
    ov = (CONFIG.get('prices') or {}).get(model or '')
    d = load_prices()
    e = None
    for k in (model, 'anthropic/' + (model or ''), 'openai/' + (model or ''), (model or '')[:-9] if (model or '')[-9:-8] == '-' else None):
        v = d.get(k) if k else None
        if isinstance(v, dict) and v.get('input_cost_per_token') is not None:
            e = v
            break
    if not e and not ov:
        return None
    out = {}
    if e:
        inp = e['input_cost_per_token']
        cw = e.get('cache_creation_input_token_cost', inp)
        g = lambda k, dflt: e[k] if e.get(k) is not None else dflt   # noqa: E731
        out = {'in': inp * 1e6, 'out': g('output_cost_per_token', 0.0) * 1e6, 'cw5m': g('cache_creation_input_token_cost', inp) * 1e6,
               'cw1h': g('cache_creation_input_token_cost_above_1hr', cw if cw is not None else inp) * 1e6,
               'cr': g('cache_read_input_token_cost', inp) * 1e6}
    out.update({k: float(v) for k, v in (ov or {}).items() if k in ('in', 'out', 'cw5m', 'cw1h', 'cr')})
    return out if 'in' in out else None


def cost_of(side, row):
    """料金の記録の 1 行の料金（USD）。単価が無ければ None。"""
    p = price(row.get('model'))
    if not p:
        return None
    if side == 'claude':
        return (row.get('in', 0) * p['in'] + row.get('out', 0) * p['out'] + row.get('cw5m', 0) * p['cw5m']
                + row.get('cw1h', 0) * p['cw1h'] + row.get('cr', 0) * p['cr']) / 1e6
    return ((row.get('in', 0) - row.get('cached', 0)) * p['in'] + row.get('cached', 0) * p['cr'] + row.get('out', 0) * p['out']) / 1e6


# ---- 料金の記録（会話ログから写して溜める） ----
def _walk_jsonl(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith('.jsonl'):
                yield os.path.join(d, f)


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def _claude_rows(path, start):
    """Claude Code の会話ログ（start バイト目から）の応答ごとのトークン数。返り値 (行の一覧, 読み終えた位置)。"""
    out = []
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b'\n') + 1          # 書きかけの最後の行は次に回す
    for line in data[:end].decode('utf-8', 'replace').splitlines():
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get('message')
        if not isinstance(m, dict) or not isinstance(m.get('usage'), dict) or not d.get('timestamp') or not m.get('model'):
            continue
        u = m['usage']
        cc = u.get('cache_creation') or {}
        w1h, w5m = cc.get('ephemeral_1h_input_tokens'), cc.get('ephemeral_5m_input_tokens')
        if w1h is None and w5m is None:
            w5m, w1h = u.get('cache_creation_input_tokens') or 0, 0
        try:
            ts = _ts(d['timestamp'])
        except ValueError:
            continue
        out.append({'k': f'{m.get("id")}|{d.get("requestId")}', 'ts': ts, 'model': m['model'], 'in': u.get('input_tokens') or 0,
                    'out': u.get('output_tokens') or 0, 'cw5m': w5m or 0, 'cw1h': w1h or 0, 'cr': u.get('cache_read_input_tokens') or 0})
    return out, start + end


def _codex_rows(path, start, st):
    """Codex の会話ログ（start バイト目から）の token_count の増えた分。st は {model, tot, n}（前回の続き）。"""
    out = []
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b'\n') + 1
    for line in data[:end].decode('utf-8', 'replace').splitlines():
        if '"turn_context"' not in line and '"token_count"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get('payload') or {}
        if d.get('type') == 'turn_context' or p.get('type') == 'turn_context':
            st['model'] = p.get('model') or st.get('model')
            continue
        if p.get('type') != 'token_count':
            continue
        tot = (p.get('info') or {}).get('total_token_usage')
        if not tot or not d.get('timestamp'):
            continue
        cur = [tot.get('input_tokens') or 0, tot.get('cached_input_tokens') or 0, tot.get('output_tokens') or 0]
        prev = st.get('tot')
        if prev and cur[0] < prev[0]:
            prev = None                      # 累計が戻った（数え直し）
        dl = [c - (prev[k] if prev else 0) for k, c in enumerate(cur)]
        st['tot'] = cur
        if sum(dl) <= 0:
            continue
        st['n'] = (st.get('n') or 0) + 1
        try:
            ts = _ts(d['timestamp'])
        except ValueError:
            continue
        out.append({'k': f'{os.path.basename(path)}#{st["n"]}', 'ts': ts, 'model': st.get('model'), 'in': dl[0], 'cached': dl[1], 'out': dl[2]})
    return out, start + end


def sync_costs(full=False):
    """会話ログの増えた分を料金の記録に足す。full でなければ、前回の位置から読む（初めての環境では import か sample が全部読む）。
    返り値 {side: 足した行数}。"""
    import fcntl
    try:
        state = json.load(open(COST_STATE))
    except (OSError, ValueError):
        state = {}
    if not state and not full:
        return {}
    added = {}
    for side, root in (('claude', os.path.join(CLAUDE_HOME, 'projects')), ('codex', os.path.join(CODEX_HOME, 'sessions'))):
        ss = state.setdefault(side, {})
        have = {}
        if side == 'claude':
            for x in read_jsonl(COST_LOG[side]):
                have[x.get('k')] = max(have.get(x.get('k'), -1), x.get('out', 0))
        new = []
        for path in _walk_jsonl(root):
            try:
                size, mtime = os.path.getsize(path), os.path.getmtime(path)
            except OSError:
                continue
            prev = ss.get(path) or {}
            if prev.get('size') == size and prev.get('mtime') == mtime:
                continue
            pos = prev.get('pos', 0) if prev.get('size', 0) <= size else 0
            if side == 'claude':
                rows, pos = _claude_rows(path, pos)
                for x in rows:
                    if x['out'] > have.get(x['k'], -1):
                        have[x['k']] = x['out']
                        new.append(x)
                ss[path] = {'size': size, 'mtime': mtime, 'pos': pos}
            else:
                cst = {k: prev.get(k) for k in ('model', 'tot', 'n')} if pos else {}
                rows, pos = _codex_rows(path, pos, cst)
                new += rows
                ss[path] = {'size': size, 'mtime': mtime, 'pos': pos, **cst}
        if new:
            os.makedirs(DATA, exist_ok=True)
            with open(COST_LOG[side], 'a') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                for x in sorted(new, key=lambda x: x['ts']):
                    f.write(json.dumps(x) + '\n')
        added[side] = len(new)
    os.makedirs(DATA, exist_ok=True)
    tmp = COST_STATE + '.tmp'
    json.dump(state, open(tmp, 'w'))
    os.replace(tmp, COST_STATE)
    return added


def read_jsonl(path):
    out = []
    try:
        f = open(path)
    except OSError:
        return out
    with f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def cost_events(side, scope_model=None):
    """[(時刻, 料金)]（時刻順）と、単価の無かったモデルの一覧。Claude は同じ応答を出力の最大の 1 行に。"""
    rows = read_jsonl(COST_LOG[side])
    if side == 'claude':
        best = {}
        for x in rows:
            k = x.get('k')
            if k not in best or x.get('out', 0) > best[k].get('out', 0):
                best[k] = x
        rows = list(best.values())
    else:
        rows = list({x.get('k'): x for x in rows}.values())
    ev, miss = [], set()
    for x in rows:
        if scope_model and scope_model.lower() not in (x.get('model') or '').lower():
            continue
        c = cost_of(side, x)
        if c is None:
            if x.get('model') != '<synthetic>':   # Claude Code が自分で書く応答（API を呼んでいない）
                miss.add(x.get('model') or '（モデルの記録なし）')
            c = 0.0
        ev.append((x['ts'], c))
    ev.sort()
    return ev, sorted(miss)


# ---- 料金の列（15 分ごと） ----
Q = 900


class CostSeries:
    """15 分ごとの料金の列。x[i]＝[T0 + i·Q, T0 + (i+1)·Q) の料金。T0 は最初の料金のある日のローカル 0 時。"""

    def __init__(self, ev, t_end):
        d0 = datetime.datetime.fromtimestamp(ev[0][0] if ev else t_end).date()
        self.T0 = time.mktime((d0.year, d0.month, d0.day, 0, 0, 0, 0, 0, -1))
        self.n = int((t_end - self.T0) // Q) + 1   # 最後は t_end を含む（途中までの）区切り
        self.x = [0.0] * self.n
        for t, c in ev:
            i = int((t - self.T0) // Q)
            if 0 <= i < self.n:
                self.x[i] += c
        self.cs = [0.0]
        for v in self.x:
            self.cs.append(self.cs[-1] + v)
        self._cal = {}
        self._feat = {}

    def S(self, i, n):
        return self.cs[min(i + n, self.n)] - self.cs[max(min(i, self.n), 0)]

    def cal(self, i):
        """区切り i の (時, 休日か, 日の番号)。i は列の外でもよい（時刻から決める）。"""
        v = self._cal.get(i)
        if v is None:
            lt = time.localtime(self.T0 + i * Q + 1)
            day = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
            v = (lt.tm_hour, int(lt.tm_wday >= 5 or day.isoformat() in HOLIDAYS), day.toordinal())
            self._cal[i] = v
        return v

    def cost_between(self, a, b):
        ia = int(min(max((a - self.T0) / Q, 0), self.n))
        ib = int(min(max((b - self.T0) / Q, 0), self.n))
        return self.cs[ib] - self.cs[ia]


def _wchoice(rng, vals, w, n):
    if not vals:
        return [0.0] * n
    acc, s = [], 0.0
    for v in w:
        s += v
        acc.append(s)
    return [vals[min(bisect.bisect_left(acc, rng.random() * s), len(vals) - 1)] for _ in range(n)]


def _hw(i, s, hl):
    return 0.5 ** ((i - s) / (96.0 * hl)) if hl else 1.0


def _seasonal(cs_, i, lo, hi):
    """[lo, hi] の開始のうち、i と同じ時刻（15 分単位）・同じ休日かのもの（3 つ未満なら全部）。"""
    h, od, _ = cs_.cal(i)
    q = i % 4
    s = [j for j in range(lo, hi + 1) if j % 4 == q and cs_.cal(j)[0] == h and cs_.cal(j)[1] == od]
    return s if len(s) >= 3 else list(range(lo, hi + 1))


class Empirical:
    """同じ時刻・同じ休日かの過去の実績 S(s, x)（最近ほど重く）。"""

    def __init__(self, halflife=7):
        self.hl = halflife

    def fit(self, cs_, i):
        pass

    def predict(self, cs_, i, xs, n, rng):
        out = []
        for xx in xs:
            s = _seasonal(cs_, i, 0, i - xx) if i - xx >= 0 else []
            out.append(_wchoice(rng, [cs_.S(j, xx) for j in s], [_hw(i, j, self.hl) for j in s], n) if s else [])
        return out


class LevelScaled:
    """同じ時刻の実績 S(s, x) を、s の直前 days 日の料金で割り、今の直前 days 日の料金をかける。"""

    def __init__(self, days):
        self.L = days * 96

    def fit(self, cs_, i):
        pass

    def predict(self, cs_, i, xs, n, rng):
        now = cs_.S(i - self.L, self.L) if i >= self.L else cs_.S(0, i)
        out = []
        for xx in xs:
            vals = []
            if i - xx >= self.L:
                for j in _seasonal(cs_, i, self.L, i - xx):
                    lv = cs_.S(j - self.L, self.L)
                    if lv > 1.0:
                        vals.append(cs_.S(j, xx) * now / lv)
            out.append(_wchoice(rng, vals, [1.0] * len(vals), n) if vals else [])
        return out


def _ess_weights(rec, dist, ess):
    """重み w = rec × exp(−(dist/h)²)。h は、似ている度合いだけの重み exp(−(dist/h)²) の実質の点数 (Σk)²/Σk² が ess になるよう決める。
    この実質の点数は h について単調なので h は 1 つに決まり、w は距離・新しさに連続に依存する
    （新しさを掛けた重みで合わせると解が複数あり、特徴が少し動くだけで別の解に跳ぶ）。"""
    if len(dist) <= ess:
        return list(rec)
    d0 = min(dist)
    d = [v - d0 for v in dist]                      # 最も近い点を 1 に（exp の下限で全部 0 にならないように）

    def e(h):
        k = [math.exp(-min((v / h) ** 2, 700.0)) for v in d]
        return k, sum(k) ** 2 / sum(v * v for v in k)
    sc = max(sorted(d)[len(d) // 2], 1e-9)
    lo, hi = math.log(sc) - 9.0, math.log(sc) + 14.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if e(math.exp(mid))[1] < ess:
            lo = mid
        else:
            hi = mid
    return [r * k for r, k in zip(rec, e(math.exp(hi))[0])]


def _sd(col, floor=1e-6):
    m = sum(col) / len(col)
    return max(math.sqrt(sum((v - m) ** 2 for v in col) / len(col)), floor)


class KNN:
    """似た過去の時点のその後。固定の K 点で切らず、重み＝最近ほど重く（halflife 日）× 似ているほど重く（ガウス）。
    ガウスの幅は、似ている度合いだけの重みの実質の点数が決めた数になるよう決めるので、開始がずれても重みは連続に動く。
    x が 1 日以上：毎日、今と同じ時刻の開始を 1 つ（開始がずれれば候補も同じだけずれる）。似ている＝直前 24 時間・7 日の料金と、
      予測する期間の休日の割合が近い（候補を休日かで切らない）。実質の日数 ESS_LONG。
    x が 1 日未満：同じ休日かの開始。x の未来が大きく重なる開始（4 時間以上は 4 時間、1 時間以上は 1 時間のまとまり）は、いちばん似た 1 つに数える。
      似ている＝時刻が近い・直前 1・4・24 時間・7 日の料金が近い。実質の点数 ESS_SHORT。"""
    ESS_SHORT, ESS_LONG = 20, 8

    def __init__(self, halflife=7):
        self.hl = halflife

    def fit(self, cs_, i):
        pass

    @staticmethod
    def _f(cs_, s):
        f = cs_._feat.get(s)
        if f is None:
            f = cs_._feat[s] = [math.log1p(cs_.S(max(s - k, 0), min(k, s))) for k in (4, 16, 96, 672)]
        return f

    @staticmethod
    def _block(xx):
        return 16 if xx >= 16 else 4 if xx >= 4 else 1

    @staticmethod
    def _offfrac(cs_, a, xx):
        """区切り a から x 区切りのうち休日の割合（暦から。列の外でもよい）。"""
        c = cs_.__dict__.setdefault('_offc', [0])
        while len(c) <= a + xx:
            c.append(c[-1] + cs_.cal(len(c) - 1)[1])
        return (c[a + xx] - c[a]) / xx

    def neighbours(self, cs_, i, xx):
        """(選んだ開始, 重み)。候補が無い（記録が x より短い）なら ([], [])。"""
        if xx >= 96:
            cand = [s for s in range(i - 96, 671, -96) if s + xx <= i]
            if not cand:
                return [], []
            fi = self._f(cs_, i)
            q = [self._f(cs_, s) for s in cand]
            F = [[f[2], f[3], self._offfrac(cs_, s, xx)] for s, f in zip(cand, q)]
            fi = [fi[2], fi[3], self._offfrac(cs_, i, xx)]
            # 休日の割合がほぼ同じ候補ばかりのとき、わずかな差を大きく数えない
            sd = [_sd([f[k] for f in F], fl) for k, fl in ((0, 1e-6), (1, 1e-6), (2, 0.05))]
            dist = [sum(abs(f[k] - fi[k]) / sd[k] for k in range(3)) for f in F]
            return cand, _ess_weights([_hw(i, s, self.hl) for s in cand], dist, self.ESS_LONG)
        fi, (hi, odi, _) = self._f(cs_, i), cs_.cal(i)
        cand = [s for s in range(672, i - xx + 1) if cs_.cal(s)[1] == odi] or list(range(0, max(i - xx + 1, 0)))
        if not cand:
            return [], []
        F = [self._f(cs_, s) for s in cand]
        sd = [_sd([f[k] for f in F]) for k in range(4)]
        b = self._block(xx)
        best = {}                                  # まとまり → (距離, 開始)。同じ距離なら新しいほう
        for s, f in zip(cand, F):
            dh = abs(cs_.cal(s)[0] - hi)
            dh = min(dh, 24 - dh) / 3.0
            d = dh + sum(abs(f[k] - fi[k]) / sd[k] for k in range(4))
            g = s // b
            if g not in best or (d, -s) < (best[g][0], -best[g][1]):
                best[g] = (d, s)
        nn = [s for _, s in best.values()]
        return nn, _ess_weights([_hw(i, s, self.hl) for s in nn], [d for d, _ in best.values()], self.ESS_SHORT)

    def predict(self, cs_, i, xs, n, rng):
        out = []
        for xx in xs:
            nn, w = self.neighbours(cs_, i, xx)
            # 候補が無い（記録が x より短い）：この部品は混ぜない
            out.append(_wchoice(rng, [cs_.S(s, xx) for s in nn], w, n) if nn else [])
        return out


class Hurdle:
    """15 分ごとに、使うか（直前に使ったか・休日か・時刻で決まる確率）× いくら（同じ休日かの標本）をシミュレーション。最近ほど重く。"""

    def __init__(self, halflife=7):
        self.hl = halflife

    def fit(self, cs_, i):
        num, den = [[[0.5] * 2 for _ in range(24)] for _ in range(2)], [[[1.0] * 2 for _ in range(24)] for _ in range(2)]
        sizes, sw = {0: [], 1: []}, {0: [], 1: []}
        for j in range(1, i):
            w = _hw(i, j, self.hl)
            h, od, _ = cs_.cal(j)
            pa = int(cs_.x[j - 1] > 0)
            a = cs_.x[j] > 0
            num[od][h][pa] += w * a
            den[od][h][pa] += w
            if a:
                sizes[od].append(cs_.x[j])
                sw[od].append(w)
        self.p = [[[num[o][h][k] / den[o][h][k] for k in range(2)] for h in range(24)] for o in range(2)]
        for od in (0, 1):
            if len(sizes[od]) < 30:
                sizes[od], sw[od] = sizes[0] + sizes[1], sw[0] + sw[1]
        self.sizes, self.sw = sizes, sw
        self.acc = {od: list(_accum(sw[od])) for od in (0, 1)}

    def predict(self, cs_, i, xs, n, rng):
        mx = max(xs)
        want = {xx: k for k, xx in enumerate(xs)}
        prev = [cs_.x[i - 1] > 0 if 0 < i <= cs_.n else False] * n
        tot = [0.0] * n
        out = [None] * len(xs)
        for step in range(mx):
            h, od, _ = cs_.cal(i + step)
            pr = self.p[od][h]
            acc, sizes = self.acc[od], self.sizes[od]
            for k in range(n):
                a = rng.random() < pr[int(prev[k])]
                if a and sizes:
                    tot[k] += sizes[min(bisect.bisect_left(acc, rng.random() * acc[-1]), len(sizes) - 1)]
                prev[k] = a
            if step + 1 in want:
                out[want[step + 1]] = tot[:]
        return out


def _accum(w):
    s = 0.0
    for v in w:
        s += v
        yield s


class DailyTopDown:
    """日の合計を予測して時刻の割合で割り振る。log1p(日の合計) ＝ 水準 ＋ 休日の差 ＋ ばらつき、水準は指数平滑（alpha）。"""

    def __init__(self, alpha=0.3, halflife=14):
        self.alpha, self.hl = alpha, halflife

    def fit(self, cs_, i):
        d_now = i // 96
        D = [cs_.S(d * 96, 96) for d in range(d_now)]
        od = [cs_.cal(d * 96)[1] for d in range(d_now)]
        y = [math.log1p(v) for v in D]
        m = {o: [v for v, oo in zip(y, od) if oo == o] for o in (0, 1)}
        base = sum(m[0]) / len(m[0]) if m[0] else 0.0
        self.seas = [0.0, (sum(m[1]) / len(m[1]) - base) if m[1] and m[0] else 0.0]
        lv = (y[0] - self.seas[od[0]]) if y else 0.0
        res = []
        for v, o in zip(y, od):
            e = v - (lv + self.seas[o])
            res.append(e)
            lv += self.alpha * e
        self.level, self.res = lv, (res[1:] or [0.0])
        prof = [[1e-9] * 96 for _ in range(2)]
        for j in range(0, d_now * 96):
            prof[cs_.cal(j)[1]][j % 96] += _hw(i, j, self.hl) * cs_.x[j]
        self.prof = [[v / sum(p) for v in p] for p in prof]

    def predict(self, cs_, i, xs, n, rng):
        mx = max(xs)
        d0, q0 = i // 96, i % 96
        nd = (q0 + mx) // 96 + 1
        so_far = cs_.S(d0 * 96, q0)
        out = []
        sims = []
        for _ in range(n):
            lv, Ds = self.level, []
            for k in range(nd):
                e = self.res[rng.randrange(len(self.res))]
                Ds.append(math.expm1(lv + self.seas[cs_.cal((d0 + k) * 96)[1]] + e))
                lv += self.alpha * e
            Ds[0] = max(Ds[0], so_far)
            sims.append(Ds)
        pref = []
        for k in range(nd):
            p = self.prof[cs_.cal((d0 + k) * 96)[1]]
            acc = [0.0]
            for v in p:
                acc.append(acc[-1] + v)
            pref.append(acc)
        rest0 = max(pref[0][96] - pref[0][q0], 1e-12)
        for xx in xs:
            end = q0 + xx
            frac = []
            for k in range(nd):
                a, b = max(q0, k * 96), min(end, (k + 1) * 96)
                if a >= b:
                    frac.append(0.0)
                    continue
                f = pref[k][b - k * 96] - pref[k][a - k * 96]
                frac.append(f / rest0 if k == 0 else f)
            vals = []
            for Ds in sims:
                vals.append((Ds[0] - so_far) * frac[0] + sum(Ds[k] * frac[k] for k in range(1, nd)))
            out.append(vals)
        return out


PARTS = ('knn_w7', 'seas_w7', 'hurdle', 'lvl7', 'lvl3', 'daily')   # wpool の部品（weights.json の並び）
POOL2 = ('knn_w7', 'lvl7', 'daily')
W_HOURS = (1, 2, 4, 8, 12, 24)    # wpool の重みを持つ x（時間）。x がこの範囲を越えれば pool2
N_SAMP = 400


def make_parts():
    return {'knn_w7': KNN(7), 'seas_w7': Empirical(7), 'hurdle': Hurdle(7), 'lvl7': LevelScaled(7), 'lvl3': LevelScaled(3),
            'daily': DailyTopDown()}


def load_weights():
    """{x（時間）: [PARTS の重み]}。学んだもの（data/weights.json）が無ければ同梱の既定。"""
    for f in (WEIGHTS_FILE, _conf('weights-default.json')):
        try:
            d = json.load(open(f))
            w = {int(k): [float(v) for v in vs] for k, vs in d['weights'].items()}
            if w:
                return w, f
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {h: [1.0 / len(PARTS)] * len(PARTS) for h in W_HOURS}, None


def _bucket(xx):
    """x（区切りの数）に重みを使う評価点（時間）。24 時間を越えれば None（pool2）。"""
    if xx > W_HOURS[-1] * 4:
        return None
    return min(W_HOURS, key=lambda h: abs(math.log(h * 4) - math.log(max(xx, 1))))


def forecast(cs_, i, xs, n=N_SAMP, seed=0, weights=None):
    """区切り i から x 区切りの間に使う料金の標本（xs の順の一覧）。"""
    rng = random.Random(seed)
    parts = make_parts()
    for p in parts.values():
        p.fit(cs_, i)
    smp = {k: dict(zip(xs, parts[k].predict(cs_, i, xs, n, rng))) for k in PARTS}
    weights = weights or load_weights()[0]
    out = []
    for xx in xs:
        b = _bucket(xx)
        w = weights.get(b) if b else None
        names, ws = (PARTS, w) if w else (POOL2, [1.0 / len(POOL2)] * len(POOL2))
        # 標本の無い部品（記録が x より短く、候補が無い）は外し、残りの割合を合わせ直す。どれも無ければ None
        have = [(nm, v) for nm, v in zip(names, ws) if v > 0 and smp[nm][xx]]
        tot = sum(v for _, v in have)
        if not have or tot <= 0:
            out.append(None)
            continue
        cnt = [int(v / tot * n) for _, v in have]
        cnt[max(range(len(have)), key=lambda k: have[k][1])] += n - sum(cnt)
        mix = []
        for (name, _), c in zip(have, cnt):
            s = smp[name][xx][:]
            rng.shuffle(s)
            mix += (s * (c // len(s) + 1))[:c]
        out.append(mix)
    return out


def _abs_mean(a, b):
    """E|A − B|（標本どうし）。並べ替えと累積和で O(n log n)。"""
    b = sorted(b)
    acc = [0.0]
    for v in b:
        acc.append(acc[-1] + v)
    tot, m = 0.0, len(b)
    for v in a:
        k = bisect.bisect_right(b, v)
        tot += v * k - acc[k] + (acc[m] - acc[k]) - v * (m - k)
    return tot / (len(a) * m)


def calibrate(quiet=False, every=8, sub=50, step=0.05):
    """料金の記録から wpool の重みを x（W_HOURS）ごとに学ぶ：結果が分かっている過去の開始 j（j + x ≤ 今）で、
    混ぜた分布の CRPS（Σ w_a E|X_a − y| − ½ Σ w_a w_b E|X_a − X_b|）の平均が最小の重み（step 刻み）。
    開始は直近 CALIBRATE_DAYS 日の 2 時間おき（every 区切り。古い会話ログは消えて料金が欠けていることがあり、使い方も変わるため）。
    最初の 14 日は学習だけ。開始が 20 未満の x は学ばない（既定の重み）。"""
    t = time.time()
    ev, _ = cost_events(MAIN_SIDE_FOR_WEIGHTS)
    if not ev:
        quiet or print('料金の記録が無い')
        return None
    cs_ = CostSeries(ev, t)
    xs = [h * 4 for h in W_HOURS]
    A = {h: [] for h in W_HOURS}
    B = {h: [] for h in W_HOURS}
    parts = make_parts()
    rng = random.Random(1)
    last_day = None
    first = max(14 * 96, cs_.n - CALIBRATE_DAYS * 96)
    first += (-first) % every
    for i in range(first, cs_.n, every):
        ok = [xx for xx in xs if i + xx <= cs_.n]
        if not ok:
            break
        if i // 96 != last_day:
            for p in parts.values():
                p.fit(cs_, i)
            last_day = i // 96
        smp = {k: parts[k].predict(cs_, i, ok, N_SAMP, rng) for k in PARTS}
        for kx, xx in enumerate(ok):
            y = cs_.S(i, xx)
            ss = [smp[k][kx] for k in PARTS]
            if not all(ss):
                continue
            subs = [rng.sample(s, min(sub, len(s))) for s in ss]
            A[xx // 4].append([sum(abs(v - y) for v in s) / len(s) for s in ss])
            B[xx // 4].append([[_abs_mean(subs[a], subs[b]) for b in range(len(PARTS))] for a in range(len(PARTS))])
    grid = _simplex(len(PARTS), step)
    out = {}
    for h in W_HOURS:
        if len(A[h]) < 20:
            continue
        k = len(PARTS)
        a = [sum(r[j] for r in A[h]) / len(A[h]) for j in range(k)]
        b = [[sum(r[x][y] for r in B[h]) / len(B[h]) for y in range(k)] for x in range(k)]
        best = min(grid, key=lambda w: sum(w[j] * a[j] for j in range(k)) - 0.5 * sum(w[x] * w[y] * b[x][y] for x in range(k) for y in range(k)))
        out[h] = best
        quiet or print(f'{h} 時間：開始 {len(A[h])}  重み ' + ' '.join(f'{nm} {v:.2f}' for nm, v in zip(PARTS, best)))
    if not out:
        quiet or print('学べる開始が足りない（既定の重みのまま）')
        return None
    os.makedirs(DATA, exist_ok=True)
    tmp = WEIGHTS_FILE + '.tmp'
    json.dump({'parts': PARTS, 'weights': {str(h): w for h, w in out.items()}, 'n_origins': {str(h): len(A[h]) for h in out}}, open(tmp, 'w'))
    os.replace(tmp, WEIGHTS_FILE)
    quiet or print('→', WEIGHTS_FILE)
    return out


CALIBRATE_DAYS = 28
MAIN_SIDE_FOR_WEIGHTS = 'claude'   # 重みは Claude の料金で学ぶ（評価したのは Claude だけ）。Codex も同じ重みを使う


def _simplex(k, step):
    n = int(round(1 / step))
    out = []

    def rec(pre, left, d):
        if d == 1:
            out.append([v / n for v in pre + [left]])
            return
        for a in range(left + 1):
            rec(pre + [a], left - a, d - 1)
    rec([], n, k)
    return out


def maybe_calibrate(t):
    """sample から：weights.json が 1 日より古い（または無い）とき、料金の記録が 15 日分以上あれば学び直す。"""
    try:
        if t - os.path.getmtime(WEIGHTS_FILE) < 86400:
            return False
    except OSError:
        pass
    ev, _ = cost_events(MAIN_SIDE_FOR_WEIGHTS)
    if not ev or ev[-1][0] - ev[0][0] < 15 * 86400:
        return False
    return calibrate(quiet=True) is not None


# ---- 使用率の記録 ----
CONFIRM = 1800   # 使用率の上がり・戻りを本物とみなすか、この秒数のあとの観測の多数決で決める
EPS = 0.5        # 使用率の比べで同じとみなす幅（%）
RESET_U = 5.0    # 窓の途中で使用率がこれ以下に戻ったときだけリセットとみなす（リセットは 0 から数え直す）


def clean_points(pts):
    """1 つの窓の観測 [(ts, u)]（時刻順）から、古い値を除き、途中のリセットで切る。返り値は区間の一覧（各区間は使用率が減らない）。
    複数のセッションが、それぞれ最後に受け取った使用率を書くので、新しい値の間に古い値が混ざる。
    上がった値は、あとの観測の多数が同じ以上なら採る（混ざった古い値で上がって見えるだけのものを除く。リセット後とみられる RESET_U 以下の値は数えない）。
    下がった値は、RESET_U 以下で、あとの観測の多数も今の値より下ならリセットとして新しい区間を始める。それ以外は古い値として捨てる。
    あとの観測：CONFIRM 秒以内のもの。無ければ次の 1 つ。"""
    segs, level, j = [[]], None, 0
    for i, (t, u) in enumerate(pts):
        j = max(j, i + 1)
        while j < len(pts) and pts[j][0] <= t + CONFIRM:
            j += 1
        later = [v for _, v in pts[i + 1:max(j, i + 2)]]
        if level is None:
            segs[-1].append((t, u))
            level = u
        elif u >= level - EPS:
            hi = sum(1 for v in later if v >= u - EPS)
            lo = sum(1 for v in later if v < u - EPS and not (v <= RESET_U < level))   # リセット後とみられる値は数えない
            if hi >= lo:
                level = max(level, u)
                segs[-1].append((t, level))
        else:
            lo = sum(1 for v in later if v < level - EPS)
            if u <= RESET_U and later and lo > len(later) - lo:
                segs.append([(t, u)])
                level = u
    return segs


def row_id(x):
    """記録の行の枠の種類。id の無い古い行は長さから（Claude 300→session・10080→weekly_all、Codex は長さ）。"""
    if x.get('id'):
        return x['id']
    if x.get('side') == 'claude':
        return {300: 'session', 10080: 'weekly_all'}.get(x.get('win'), f'{x.get("win")}m')
    return win_label(x.get('win'))


def win_label(win_min):
    try:
        w = int(win_min)
    except (TypeError, ValueError):
        return '?'
    return f'{w // 60}h' if w < 1440 else f'{w // 1440}d'


class Series:
    """1 つの枠の観測 [(ts, u, r)] を窓（リセットごと）に分けて持つ。"""

    def __init__(self, side, wid, win_min, samples, scoped=False):
        self.side, self.id, self.win_min, self.win, self.scoped = side, wid, win_min, win_min * 60, scoped
        self.name = f'{side}-{wid}'
        # リセット済みの窓を報告し続ける古いセッションの値（t >= r）は捨てる。窓はリセット時刻（1 時間以内のぶれは同じ）で分ける
        s = sorted((r, t, u) for t, u, r in samples if r and t < r)
        clusters = []
        for r, t, u in s:
            if clusters and r - clusters[-1]['r0'] < 3600:
                clusters[-1]['pts'].append((t, u))
                continue
            clusters.append({'r0': r, 'r': r, 'pts': [(t, u)]})
        groups = []
        for c in clusters:
            segs = clean_points(sorted(c['pts']))
            groups.append({'r': c['r'], 'pts': segs[0]})
            groups += [{'r': c['r'], 'pts': sg, 'start': sg[0][0]} for sg in segs[1:]]
        groups.sort(key=lambda c: c['pts'][0][0])
        for i, g in enumerate(groups):
            g.setdefault('start', min(g['r'] - self.win, g['pts'][0][0]))
            nxt = groups[i + 1]['pts'][0][0] if i + 1 < len(groups) else None
            g['end'] = min(g['r'], nxt) if nxt else g['r']
        self.groups = groups
        gs = [(g['pts'][0][0], g['end'], [p for p, _ in g['pts']], [u for _, u in g['pts']]) for g in groups]
        self._gs, self._firsts, self._base = gs, [g[0] for g in gs], []
        self.first = gs[0][0] if gs else float('inf')
        acc = 0.0
        for _, _, _, us in gs:
            self._base.append(acc)
            acc += us[-1]

    def total(self, t):
        """t までに使った量の累計（%。リセットをまたいで足す）。"""
        i = bisect.bisect_right(self._firsts, t) - 1
        if i < 0:
            return 0.0
        _, _, xs, us = self._gs[i]
        return self._base[i] + us[bisect.bisect_right(xs, t) - 1]

    def u_at(self, t):
        """t の使用率（窓が終わった後、次の窓の観測までは 0）。"""
        i = bisect.bisect_right(self._firsts, t) - 1
        if i < 0 or t >= self._gs[i][1]:
            return 0.0
        _, _, xs, us = self._gs[i]
        return us[bisect.bisect_right(xs, t) - 1]


def load_series(side, rows=None, until=None):
    """{枠の種類: Series}。"""
    by, meta = {}, {}
    for x in rows if rows is not None else read_log():
        if x.get('side') != side or not x.get('r') or not x.get('win') or (until is not None and x['ts'] > until):
            continue
        k = row_id(x)
        by.setdefault(k, []).append((x['ts'], float(x['u']), x['r']))
        meta[k] = (int(x['win']), bool(x.get('scoped')))
    return {k: Series(side, k, meta[k][0], s, meta[k][1]) for k, s in by.items()}


def record(rows, src='live'):
    """記録に足す（同時に書くプロセスがあっても行が混ざらないよう flock）。記録から借りた行（cached）は足さない。"""
    import fcntl
    rows = [x for x in rows if not x.get('cached')]
    if not rows:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(USAGE_LOG, 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        for x in rows:
            f.write(json.dumps({'ts': int(x.get('ts') or time.time()), 'side': x['side'], 'id': row_id(x), 'win': x['win'], 'u': x['u'],
                                'r': x['r'], **({'scoped': True} if x.get('scoped') else {}), 'src': x.get('src', src)}) + '\n')


def read_log():
    return read_jsonl(USAGE_LOG)


# ---- 上限の金額と使い切る確率 ----
def limit_hist(ser, cs_, t):
    """記録の分の上限（$）：t より前を 1 時間ずつさかのぼり、使用率の伸びが 100% に届くまでの料金 ÷ 伸び。伸びが 20% 未満なら None。
    さかのぼるのは 28 日・料金の記録の始まり・使用率の記録の始まりのうち最も近いところまで。"""
    c = du = 0.0
    b = t
    lo = max(cs_.T0, t - 28 * 86400, ser.first)    # 使用率の記録が始まる前の時間は数えない（料金だけ増えて伸びが 0 になる）
    while b - 3600 >= lo:
        a = b - 3600
        if max(ser.u_at(a), ser.u_at(b)) < FULL:
            c += cs_.cost_between(a, b)
            du += max(ser.total(b) - ser.total(a), 0.0)
            if du >= 100:
                break
        b = a
    return 100.0 * c / du if du >= 20 else None


def limit_usd(ser, cs_, u, t, r, win_min):
    """上限（$）と出し方。今の枠の分（u ≥ LIMIT_NOW_U）・記録の分・今の枠の分（記録が足りないとき）の順。"""
    start = r - win_min * 60
    now = 100.0 * cs_.cost_between(start, t) / u if u > 0 and start >= cs_.T0 else None
    if now is not None and u >= LIMIT_NOW_U:
        return now, 'now'
    h = limit_hist(ser, cs_, t) if ser else None
    if h:
        return h, 'hist'
    return (now, 'now') if now else (None, None)


def window_states(rows, t, log=None, n=N_SAMP):
    """今の読み取り rows から、枠ごとの状態 {名前: {...}}（側ごと）と注記。"""
    log = read_log() if log is None else log
    notes = []
    out = {'claude': {}, 'codex': {}}
    for side in ('claude', 'codex'):
        rs = [x for x in rows if x['side'] == side]
        if not rs:
            continue
        ev, miss = cost_events(side)
        if miss:
            notes.append(f'{side}: 単価が無いモデルは 0 として数えた（{", ".join(miss)}）')
        cs_ = CostSeries(ev, t) if ev else None
        enough = bool(ev) and t - ev[0][0] >= 86400
        if not enough:
            notes.append(f'{side}: 料金の記録が 1 日分に満たないので、使い切る確率は出さない（import か sample で溜まる）')
        sers = load_series(side, log)
        need = []
        for x in rs:
            name = f'{side}-{row_id(x)}'
            win, u = int(x['win']), float(x['u'])
            r = x['r'] if x.get('r') and x['r'] > t else t + win * 60
            st = {'u': u, 'r': r, 'win': win, 'scoped': bool(x.get('scoped')), 'X': None, 'limit_usd': None, 'limit_src': None,
                  'rem_h': (r - t) / 3600}
            out[side][name] = st
            if st['scoped']:
                st['note'] = 'モデルを限った枠（表示だけ、判定に使わない）'
                continue
            if not enough or u >= FULL:
                continue
            L, src = limit_usd(sers.get(row_id(x)), cs_, u, t, r, win)
            st['limit_usd'], st['limit_src'] = L, src
            if L:
                # 予測は今の区切りの始まりから（入力は終わった区切りだけ。評価・学習と同じ）。今の区切りの使った分は u にも入るので少し多めに見る
                need.append((st, max(int(math.ceil((r - (cs_.T0 + (cs_.n - 1) * Q)) / Q)), 1), L * (1 - u / 100)))
        if need:
            i = cs_.n - 1
            xs = sorted({xx for _, xx, _ in need})
            smp = dict(zip(xs, forecast(cs_, i, xs, n=n)))
            for st, xx, m in need:
                s = smp[xx]
                st['remain_usd'] = m
                if not s:
                    st['note'] = '料金の記録が短く、使い切る確率を出せない'
                    continue
                st['X'] = sum(1 for v in s if v >= m) / len(s)
    return out, notes


def decide_states(states, main=MAIN, guard=GUARD_RISK, default=TIE_DEFAULT):
    """states：{側: {枠の名前: state}}。返り値 (選んだ側, 根拠)。モデルを限った枠は使わない。X の無い枠は歯止め・比較に使わない。"""
    sub = 'codex' if main == 'claude' else 'claude'
    use = {s: {k: v for k, v in states.get(s, {}).items() if not v.get('scoped')} for s in ('claude', 'codex')}

    def leftover(d):
        v = [1 - x['X'] for x in d.values() if x['win'] >= LONG_WIN and x['X'] is not None]
        return min(v) if v else 0.5
    full = {s: [k for k, v in use[s].items() if v['u'] >= FULL] for s in use}
    over = [k for k, v in use[main].items() if v['X'] is not None and v['X'] > guard]
    tight = [k for k, v in use[sub].items() if v['win'] < LONG_WIN and v['X'] is not None and v['X'] > guard]
    L = {s: leftover(use[s]) for s in use}
    why = {'main': main, 'claude_L': L['claude'], 'codex_L': L['codex'], 'guard': over, 'sub_tight': tight,
           'full': full['claude'] + full['codex'], 'tie': False}
    if full[sub] and not full[main]:
        return main, why
    if full[main] and not full[sub]:
        return sub, why
    if over:
        return sub, why
    if tight:
        return main, why
    if L['claude'] == L['codex']:
        why['tie'] = True
        return default, why
    return ('claude' if L['claude'] > L['codex'] else 'codex'), why


# ---- 今の値を直接読む（どちらもモデルを呼ばない） ----
def claude_credentials():
    """Claude Code の OAuth の資格情報（読むだけ。更新しない：更新するとトークンの入れ替わりで Claude Code 本体の認証を壊しうる）。"""
    import subprocess
    f = os.path.join(CLAUDE_HOME, '.credentials.json')
    if os.path.exists(f):
        return json.load(open(f))['claudeAiOauth']
    if sys.platform == 'darwin':   # macOS は Keychain に保管される（試験的な対応）
        p = subprocess.run(['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                           capture_output=True, text=True, timeout=10)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout)['claudeAiOauth']
    raise RuntimeError(f'Claude Code の資格情報が無い（{f}）')


class RateLimited(RuntimeError):
    pass


def parse_claude(d, notes=None):
    """使用量 API の応答 → [{side, id, win, u, r, scoped}]。limits の配列が在ればそれ、無ければ five_hour・seven_day。"""
    def ts(s):
        return int(datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()) if s else None
    out = []
    for w in d.get('limits') or []:
        if not isinstance(w, dict) or w.get('percent') is None:
            continue
        win = CLAUDE_KIND_WIN.get(w.get('group'))
        scope = w.get('scope') or None
        wid = w.get('kind') or w.get('group') or '?'
        if scope:
            m = (scope.get('model') or {}) if isinstance(scope, dict) else {}
            wid += ':' + str(m.get('display_name') or m.get('id') or (scope.get('surface') if isinstance(scope, dict) else '') or 'scope')
        if not win:
            if notes is not None:
                notes.append(f'Claude: 知らない種類の枠 {wid}（group={w.get("group")}）は判定に使わない')
            continue
        out.append({'side': 'claude', 'id': wid, 'win': win, 'u': float(w['percent']), 'r': ts(w.get('resets_at')), 'scoped': bool(scope)})
    if not out:
        for key, wid, win in (('five_hour', 'session', 300), ('seven_day', 'weekly_all', 10080)):
            w = d.get(key)
            if w and w.get('utilization') is not None:
                out.append({'side': 'claude', 'id': wid, 'win': win, 'u': float(w['utilization']), 'r': ts(w.get('resets_at')), 'scoped': False})
    return out


def read_claude(t, notes=None):
    """Claude Code の /usage と同じ GET /api/oauth/usage。"""
    import urllib.error
    import urllib.request
    cred = claude_credentials()
    if cred.get('expiresAt') and cred['expiresAt'] / 1000 < t:
        raise RuntimeError('Claude のアクセストークンが期限切れ（Claude Code が更新するまで読めない。ここでは更新しない）')
    url = 'https://api.anthropic.com/api/oauth/usage'
    test_url = os.environ.get('CLAUDE_CODEX_ROUTER_CLAUDE_URL', '')   # テスト用。トークンを外へ送らないよう手元の偽サーバーだけ許す
    if test_url:
        if not test_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
            raise RuntimeError('CLAUDE_CODEX_ROUTER_CLAUDE_URL は http://127.0.0.1:… か http://localhost:… だけ')
        url = test_url
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + cred['accessToken'], 'anthropic-beta': 'oauth-2025-04-20'})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited('Claude の使用量の API が 429（呼び過ぎ）') from e
        raise
    out = parse_claude(d, notes)
    if not out:
        raise RuntimeError('Claude の使用枠が応答に無い')
    return out


def parse_codex(rl):
    """rateLimits → [{side, id, win, u, r}]（枠の種類は長さで呼ぶ）。"""
    out = []
    for k in ('primary', 'secondary'):
        w = rl.get(k)
        if w and w.get('resetsAt') and w.get('usedPercent') is not None and w.get('windowDurationMins'):
            win = int(w['windowDurationMins'])
            out.append({'side': 'codex', 'id': win_label(win), 'win': win, 'u': float(w['usedPercent']), 'r': int(w['resetsAt'])})
    return out


def read_codex(t, timeout=30, notes=None):
    """codex app-server（stdio の JSON-RPC）に initialize → account/rateLimits/read。"""
    import select
    import shutil
    import subprocess
    exe = os.environ.get('CLAUDE_CODEX_ROUTER_CODEX') or shutil.which('codex')
    if not exe:
        raise RuntimeError('codex が PATH に無い（CLAUDE_CODEX_ROUTER_CODEX で場所を渡せる）')
    p = subprocess.Popen([exe, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for m in ({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'claude-codex-router', 'version': '0'}}},
                  {'jsonrpc': '2.0', 'method': 'initialized'},
                  {'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read'}):
            p.stdin.write((json.dumps(m) + '\n').encode())
        p.stdin.flush()
        # 応答は fd から直接読み、行は自分で切る（バッファ付きの readline と select を混ぜると、読み込み済みの行を待ち続ける）
        fd, buf, end = p.stdout.fileno(), b'', time.time() + timeout
        while time.time() < end:
            if not select.select([fd], [], [], max(end - time.time(), 0))[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get('id') != 2:
                    continue
                if 'error' in msg:
                    raise RuntimeError(f'Codex の応答がエラー: {msg["error"]}')
                out = parse_codex((msg.get('result') or {}).get('rateLimits') or {})
                if not out:
                    raise RuntimeError('Codex の使用枠が応答に無い')
                return out
        raise RuntimeError(f'Codex の app-server が {timeout} 秒以内に答えなかった')
    finally:
        p.kill()
        p.wait()


def recent_rows(side, t, max_age=RECENT_MAX_AGE):
    """記録にある side の最新の読み取り（t から max_age 秒以内）。無ければ []。"""
    rows = [x for x in read_log() if x.get('side') == side and x.get('win') and x.get('src') == 'live' and t - max_age <= x['ts'] <= t]
    if not rows:
        return []
    last = max(x['ts'] for x in rows)
    return [{**x, 'cached': True} for x in rows if x['ts'] == last]


def read_both(t):
    """両方を読む。片方が読めなくてもう片方は返す。返り値 (rows, 失敗の一覧, 注記の一覧)。
    Claude は CLAUDE_MIN_INTERVAL 秒以内の記録があれば API を呼ばずにそれを使う。
    Claude の API が 429 を返したら、呼び直さずに記録の最新の値（RECENT_MAX_AGE 秒以内）を使う。"""
    rows, errs, notes = [], [], []
    for name, f in (('Claude', read_claude), ('Codex', read_codex)):
        if name == 'Claude' and CLAUDE_MIN_INTERVAL > 0:
            old = recent_rows('claude', t, CLAUDE_MIN_INTERVAL)
            if old:
                rows += old
                notes.append(f'{name}: {CLAUDE_MIN_INTERVAL:.0f} 秒以内の記録（{time.strftime("%H:%M:%S", time.localtime(old[0]["ts"]))}）を使った（API は呼ばない）')
                continue
        try:
            got = f(t, notes=notes)
            for x in got:
                x['ts'] = int(t)
            rows += got
        except RateLimited as e:
            old = recent_rows('claude', t)
            if old:
                rows += old
                notes.append(f'{name}: 429 のため {time.strftime("%H:%M:%S", time.localtime(old[0]["ts"]))} の記録を使った')
            else:
                errs.append(f'{name}: {e}（{RECENT_MAX_AGE // 60} 分以内の記録も無い）')
        except Exception as e:   # noqa: BLE001 — 読めない理由をそのまま出す
            errs.append(f'{name}: {e}')
    return rows, errs, notes


def import_history():
    """この環境に在る過去の記録を取り込む（重複は足さない）。"""
    import glob
    added = sync_costs(full=True)
    print(f'料金の記録に足した行: {added} → {COST_LOG["claude"]}・{COST_LOG["codex"]}')
    have = {(x['ts'], x['side'], row_id(x)) for x in read_log()}
    new = []

    def add(ts, side, win, u, r, src):
        try:
            x = {'ts': int(ts), 'side': side, 'win': int(win), 'u': float(u), 'r': int(r), 'src': src}
        except (TypeError, ValueError):
            return
        k = (x['ts'], side, row_id(x))
        if k not in have and r:
            have.add(k)
            new.append(x)
    # Codex のセッションのログ（応答ごとの token_count に使用率が載る）
    for f in sorted(glob.glob(os.path.join(CODEX_HOME, 'sessions', '*', '*', '*', '*.jsonl'))):
        for line in open(f, errors='replace'):
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
                ts = _ts(d['timestamp'])
            except (ValueError, KeyError):
                continue
            rl = (d.get('payload') or {}).get('rate_limits') or {}
            for k in ('primary', 'secondary'):
                w = rl.get(k)
                if w and w.get('window_minutes'):
                    add(ts, 'codex', w['window_minutes'], w.get('used_percent'), w.get('resets_at'), 'import:codex-sessions')
    # token-pace（在れば）
    for name, conv in (('pace.jsonl', lambda d: [(d['ts'], 'claude', 300, d.get('h5'), d.get('h5r')), (d['ts'], 'claude', 10080, d.get('d7'), d.get('d7r'))]),
                       ('codex.jsonl', lambda d: [(d['ts'], 'codex', d.get('w'), d.get('u'), d.get('r')), (d['ts'], 'codex', d.get('w2'), d.get('u2'), d.get('r2'))])):
        for d in read_jsonl(TP + name):
            for ts, side, win, u, r in conv(d):
                if win:
                    add(ts, side, win, u, r, 'import:token-pace')
    new.sort(key=lambda x: x['ts'])
    record(new)
    by = {}
    for x in new:
        by[x['src']] = by.get(x['src'], 0) + 1
    print(f'使用率の記録に足した行: {len(new)} {by} → {USAGE_LOG}')


def decide_now(t, main=MAIN):
    """両方を読んで記録に足し、判断する。返り値 (結果の dict, 終了コード)。"""
    rows, errs, notes = read_both(t)
    record(rows)
    try:
        sync_costs()
    except Exception as e:   # noqa: BLE001 — 料金の記録が足せなくても判断はする
        notes.append(f'料金の記録を足せなかった: {e}')
    states, n2 = window_states(rows, t)
    notes += n2
    if _price_src and _price_src.startswith('取れなかった'):
        notes.append(f'単価: {_price_src}')
    res = {'pick': None, 'main': main, 'reason': '', 'tie': False, 'guard': [], 'full': [], 'claude_L': None, 'codex_L': None,
           'windows': {}, 'errors': errs, 'notes': notes}
    for side in states:
        for k, v in states[side].items():
            res['windows'][k] = {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
    if states['claude'] and states['codex']:
        pick, why = decide_states(states, main)
        res.update(pick=pick, tie=why['tie'], guard=why['guard'], full=why['full'], claude_L=round(why['claude_L'], 4),
                   codex_L=round(why['codex_L'], 4),
                   reason=('満杯' if why['full'] else '歯止め' if why['guard'] or why['sub_tight'] else '差なし' if why['tie'] else '使い残す確率'))
        return res, 0
    if states['claude']:
        res.update(pick='claude', reason='Codex が読めない')
        return res, 1
    if states['codex']:
        res.update(pick='codex', reason='Claude が読めない')
        return res, 1
    res.update(pick=TIE_DEFAULT, reason='どちらも読めない（既定）')
    return res, 2


def print_human(res):
    def f(x):
        return time.strftime('%m-%d %H:%M', time.localtime(x))
    print(f'判断: {res["pick"]}（{res["reason"]}。主側 {res["main"]}）')
    for e in res['errors']:
        print('  読めなかった:', e)
    for e in res['notes']:
        print('  注記:', e)
    for k, v in res['windows'].items():
        extra = ''
        if v.get('limit_usd'):
            extra += f'  上限 ${v["limit_usd"]:,.0f}（{"今の枠の分" if v["limit_src"] == "now" else "記録の分"}）'
        if v.get('X') is not None:
            extra += f'  残り ${v["remain_usd"]:,.0f}・使い切る確率 {v["X"]:.2f}'
        if v.get('note'):
            extra += f'  {v["note"]}'
        print(f'  {k}（{win_label(v["win"])}）: 使用 {v["u"]:.0f}%  リセット {f(v["r"])}（あと {v["rem_h"]:.1f} 時間）{extra}')
    if res['claude_L'] is not None:
        print(f'  使い残す確率 Claude {res["claude_L"]:.2f} / Codex {res["codex_L"]:.2f}  歯止め {res["guard"] or "なし"}  満杯 {res["full"] or "なし"}')


def _arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'now':
        m = _arg('--main') or MAIN
        if m not in ('claude', 'codex'):
            print('--main は claude か codex', file=sys.stderr)
            sys.exit(2)
        res, code = decide_now(time.time(), m)
        if '--json' in sys.argv:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print_human(res)
        sys.exit(code)
    if cmd == 'sample':
        t = time.time()
        rows, errs, notes = read_both(t)
        rows = [x for x in rows if not x.get('cached')]
        record(rows)
        load_prices(refresh=True)
        try:
            added = sync_costs(full=not os.path.exists(COST_STATE))
        except Exception as e:   # noqa: BLE001
            added, notes = {}, notes + [f'料金の記録を足せなかった: {e}']
        re_ = maybe_calibrate(t)
        print(f'記録した枠 {len(rows)}  料金の記録 {added}' + (f'  読めなかった: {errs}' if errs else '') + (f'  注記: {notes}' if notes else '')
              + ('  重みを学び直した' if re_ else ''))
        sys.exit(0 if rows else 1)
    if cmd == 'calibrate':
        calibrate()
        return
    if cmd == 'import':
        import_history()
        return
    print(__doc__)
    sys.exit(0 if cmd in ('', '-h', '--help', 'help') else 2)


if __name__ == '__main__':
    main()
