#!/usr/bin/env python3
"""claude-codex-router: decides whether to send work to Claude or Codex, from the headroom of both usage windows.

Main side and sub side: the side used mainly is set by --main claude|codex (or main in config, default claude). The other side is the sub side.

Windows: every window the API returns is handled (their number, length and limit are assumed to change).
  Claude: each window in limits of the usage API (session = 5 hours, weekly = 7 days; unknown kinds are not used for the decision and get a note).
          Model-scoped windows (windows with a scope) are shown only, not used for the decision.
  Codex : primary and secondary of rateLimits (the API returns the length).

Usage is counted in cost (USD): token counts in the conversation logs × per-model price.
  Prices are taken at run time from LiteLLM's price data and cached in <DIR>/data/litellm-prices.json (sample refetches it when older than 1 day;
  when it cannot be fetched, the cache is used). prices in config can override them per model. Models without a price count as 0, with a note.
  Cost records: <DIR>/data/cost-claude.jsonl and cost-codex.jsonl (conversation logs can disappear, so what has been read is copied and accumulated).
  Use on other machines or on claude.ai is not included.

Limit amount (estimated per window at run time):
  If usage u is LIMIT_NOW_U (10%) or more, from the current window: 100 × cost from the window start (reset − window length) until now ÷ u.
  Below that, from history: going back 1 hour at a time from just before now, cost until the usage growth reaches one window's worth (100%) ÷ usage growth
  (excluding time spent at the limit and time before the usage records start; up to 28 days; not used if the growth is under 20%). If that is unavailable too, from the current window.
Exhaust probability X (probability of reaching the limit before the window's next reset) = P(S ≥ limit × (1 − u/100)).
  S is the distribution of the cost used from now until the reset. It is built as samples from the past cost series (every 15 minutes):
    Up to 24 hours ahead, wpool: what followed similar points in time (knn), actuals at the same time of day (more recent weighs more), whether used × how much (hurdle),
      actuals at the same time of day rescaled by the level of the previous 7 days / 3 days, and exponential smoothing of daily totals spread by time of day, mixed with weights learned per x.
      calibrate learns the weights from the records (<DIR>/data/weights.json). Until then, the bundled weights (<DIR>/weights-default.json).
    Beyond that, pool2: knn, the rescaling by the 7-day level, and exponential smoothing of daily totals, mixed in equal shares.
  Holidays: Saturdays, Sundays and holidays in config (a list of "YYYY-MM-DD").
  A side with no cost records at all gets no X (the decision does not look at its windows). Even if the records are short, the forecast uses what there is.

Decision (once a stage decides, later stages are not looked at):
  1. Full: if only one side has reached the limit (full in config, default 98%) in some window, the other side. If both have, go on.
  2. Guard: if X of any main-side window exceeds guard_risk in config (default 0.5) and no sub-side window does, the sub side.
     When the main side reaches its limit, the side that drives the work with it stops too. If the sub side is at its limit, work continues by returning to the main side.
     If X of any sub-side window exceeds guard_risk and no main-side window does, the main side. If both sides have such a window, go on.
  3. Compare: for long windows (1 day or more), the probability of not using the window up before the reset, 1 − X (leftover probability); the side whose minimum is larger.
     If equal, tie_default in config (default codex).

Usage records (each environment accumulates its own):
  <DIR>/data/usage.jsonl. 1 line = 1 read of 1 window {"ts", "side", "id": window kind, "win": window length (min), "u": usage, "r": reset time,
  "scoped": whether the window is model-scoped, "src"}. For old lines without id, it is derived from the length (Claude 300→session, 10080→weekly_all; Codex by the length).
  DIR is the environment variable CLAUDE_CODEX_ROUTER_DIR (default ~/.claude/codex-router).
  How they are read (neither calls a model, so the windows are not used up):
    Claude: GET https://api.anthropic.com/api/oauth/usage (same as /usage in Claude Code; not publicly documented).
            The token is only read from $CLAUDE_CONFIG_DIR (default ~/.claude)/.credentials.json and never refreshed.
            On macOS, when it is stored in the Keychain, it is read with the security command (experimental support).
    Codex : GET https://chatgpt.com/backend-api/wham/usage (the endpoint written in the Codex CLI executable; not publicly documented).
            The token is only read from $CODEX_HOME (default ~/.codex)/auth.json and never refreshed.

Usage:
  router.py now [--json] [--main claude|codex]   decide now (reads both and also adds them to the records). Exit code 0=both read 1=one side only 2=neither read
  router.py sample      reads both and adds them to the records; adds cost records from the conversation logs (the timer install.sh sets up calls it every 5 minutes)
  router.py calibrate   relearns the wpool weights from the cost records (sample calls it once a day)
"""
import bisect
import datetime
import json
import math
import os
import random
import sys
import time

USAGE = """claude-codex-router: pick Claude or Codex for the next task from the headroom of both usage limits.

usage:
  router.py now [--json] [--main claude|codex]  decide now (reads both, also records). exit 0=both read 1=one side 2=neither
  router.py sample      read both and record; add cost rows from the conversation logs (the timer calls this every 5 min)
  router.py calibrate [--if-needed]  relearn the mixing weights from the cost records (sample calls this once a day;
                        --if-needed: only when the weights are missing, older than a day, or learned with other parts)

now --json fields: pick, main, reason (full|guard|tie|leftover|codex_unreadable|claude_unreadable|both_unreadable), tie,
  guard (main-side windows over guard_risk, when they sent the pick to the sub side),
  sub_tight (sub-side windows over guard_risk, when they kept the pick on main), full (all windows at full),
  claude_L, codex_L, windows, errors, notes. Data and settings: $CLAUDE_CODEX_ROUTER_DIR (default ~/.claude/codex-router)."""

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULTS = os.path.join(os.path.dirname(HERE), 'defaults')   # when run directly from the repository
DIR = os.path.expanduser(os.environ.get('CLAUDE_CODEX_ROUTER_DIR') or '~/.claude/codex-router')
DATA = os.path.join(DIR, 'data')
USAGE_LOG = os.path.join(DATA, 'usage.jsonl')
COST_LOG = {'claude': os.path.join(DATA, 'cost-claude.jsonl'), 'codex': os.path.join(DATA, 'cost-codex.jsonl')}
COST_STATE = os.path.join(DATA, 'cost-state.json')
PRICE_FILE = os.path.join(DATA, 'litellm-prices.json')
WEIGHTS_FILE = os.path.join(DATA, 'weights.json')
SAMPLE_STAMP = os.path.join(DATA, 'last-sample')   # when sample last ran (to check that periodic sampling has not stopped)
VERSION_FILE = os.path.join(DIR, '.version')       # stands in for the install time (when sample has never run yet)
TP = os.path.expanduser(os.environ.get('TOKEN_PACE_DIR') or '~/.claude/token-pace') + '/'   # claude-token-pace records (added to the usage records if found)
CLAUDE_HOME = os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude')
CODEX_HOME = os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex')
PRICE_URL = (os.environ.get('CLAUDE_CODEX_ROUTER_PRICE_URL')   # for tests (file:// also works)
             or 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json')


def _conf(name):
    """<DIR>/name, or the repository's defaults/name if it is missing."""
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
GUARD_RISK = float(CONFIG['guard_risk'])     # guard: if the main side's exhaust probability exceeds this, send to the sub side
FULL = float(CONFIG['full'])                 # at or above this, the side is treated as unusable
try:   # if a Claude record read within this many seconds exists, use it without calling the API (calling repeatedly gives 429). 0 or less disables this
    CLAUDE_MIN_INTERVAL = float(CONFIG['claude_min_interval'])
except (TypeError, ValueError):
    CLAUDE_MIN_INTERVAL = 60.0
RECENT_MAX_AGE = 600   # max age (s) of the record used instead on 429. Records are every 5 minutes, so this is 2 records' worth
TIE_DEFAULT = CONFIG['tie_default'] if CONFIG['tie_default'] in ('claude', 'codex') else 'codex'
HOLIDAYS = frozenset(str(x) for x in (CONFIG.get('holidays') or []))
LONG_WIN = 1440      # windows of this length (min) or more are "long windows" (used for the comparison), shorter ones are "short windows"
LIMIT_NOW_U = 10.0   # if usage is at least this, the limit is estimated from the current window
CLAUDE_KIND_WIN = {'session': 300, 'weekly': 10080}   # Claude window group → length (min)


# ---- prices (LiteLLM) ----
_prices = None
_price_src = None


def fetch_prices(timeout=30):
    """Fetch LiteLLM's price data and write the cache. Returns the price data."""
    import urllib.request
    with urllib.request.urlopen(PRICE_URL, timeout=timeout) as r:
        d = json.load(r)
    if not isinstance(d, dict) or len(d) < 100:
        raise ValueError('unexpected price data format')
    os.makedirs(DATA, exist_ok=True)
    tmp = PRICE_FILE + '.tmp'
    json.dump(d, open(tmp, 'w'))
    os.replace(tmp, PRICE_FILE)
    return d


def load_prices(refresh=False):
    """Price data. With refresh, refetch a cache older than 1 day. Fetch it if there is no cache. If fetching fails, the cache (empty if none)."""
    global _prices, _price_src
    if _prices is not None and not refresh:
        return _prices
    stale = not os.path.exists(PRICE_FILE) or time.time() - os.path.getmtime(PRICE_FILE) >= 86400
    if not os.path.exists(PRICE_FILE) or (refresh and stale):
        try:
            _prices, _price_src = fetch_prices(), 'fetched'
            return _prices
        except Exception as e:   # noqa: BLE001 — use the cache if fetching fails
            _price_src = f'fetch failed ({e})'
    try:
        _prices = json.load(open(PRICE_FILE))
        _price_src = _price_src or ('cache (older than 1 day)' if stale else 'cache')
    except (OSError, ValueError):
        _prices = {}
    return _prices


def price(model):
    """dict of USD per 1M tokens (in, out, cw5m, cw1h, cr). prices in config take precedence. None if not found."""
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
    if 'in' not in out:
        return None
    for k in ('out', 'cw5m', 'cw1h', 'cr'):   # keys not given in the override (models not in LiteLLM) are treated as equal to in
        out.setdefault(k, out['in'])
    return out


def cost_of(side, row):
    """Cost (USD) of one row of the cost records. None if there is no price."""
    p = price(row.get('model'))
    if not p:
        return None
    if side == 'claude':
        return (row.get('in', 0) * p['in'] + row.get('out', 0) * p['out'] + row.get('cw5m', 0) * p['cw5m']
                + row.get('cw1h', 0) * p['cw1h'] + row.get('cr', 0) * p['cr']) / 1e6
    return ((row.get('in', 0) - row.get('cached', 0)) * p['in'] + row.get('cached', 0) * p['cr'] + row.get('out', 0) * p['out']) / 1e6


# ---- cost records (copied from the conversation logs and accumulated) ----
def _walk_jsonl(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith('.jsonl'):
                yield os.path.join(d, f)


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def _claude_rows(path, start):
    """Token counts per response in a Claude Code conversation log (from byte start). Returns (list of rows, position read up to)."""
    out = []
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b'\n') + 1          # a partially written last line is left for next time
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


def _codex_rows(path, start, st, usage=None):
    """Increments of token_count in a Codex conversation log (from byte start). st is {model, tot, n} (continued from last time).
    If usage (a list) is given, the usage in token_count (rate_limits) is also added to it as usage record rows."""
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
        if usage is not None and d.get('timestamp'):
            _codex_usage_rows(p.get('rate_limits') or {}, d['timestamp'], usage)
        tot = (p.get('info') or {}).get('total_token_usage')
        if not tot or not d.get('timestamp'):
            continue
        cur = [tot.get('input_tokens') or 0, tot.get('cached_input_tokens') or 0, tot.get('output_tokens') or 0]
        prev = st.get('tot')
        if prev and cur[0] < prev[0]:
            prev = None                      # the running total went back (counting restarted)
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


def _codex_usage_rows(rl, stamp, out):
    """rate_limits {primary, secondary} in a Codex conversation log → usage record rows."""
    try:
        ts = int(_ts(stamp))
    except ValueError:
        return
    for k in ('primary', 'secondary'):
        w = rl.get(k)
        try:
            if w and w.get('window_minutes') and w.get('resets_at'):
                out.append({'ts': ts, 'side': 'codex', 'win': int(w['window_minutes']), 'u': float(w['used_percent']),
                            'r': int(w['resets_at']), 'src': 'codex-sessions'})
        except (TypeError, ValueError, KeyError):
            continue


def _token_pace_rows(path, start, name):
    """claude-token-pace records (from byte start) → usage record rows. Returns (list of rows, position read up to)."""
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b'\n') + 1
    out = []
    for line in data[:end].decode('utf-8', 'replace').splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if name == 'pace.jsonl':
            vals = [('claude', 300, d.get('h5'), d.get('h5r')), ('claude', 10080, d.get('d7'), d.get('d7r'))]
        else:
            vals = [('codex', d.get('w'), d.get('u'), d.get('r')), ('codex', d.get('w2'), d.get('u2'), d.get('r2'))]
        for side, win, u, r in vals:
            try:
                if win and r and u is not None:
                    out.append({'ts': int(d['ts']), 'side': side, 'win': int(win), 'u': float(u), 'r': int(r), 'src': 'token-pace'})
            except (TypeError, ValueError, KeyError):
                continue
    return out, start + end


def sync_costs(full=False):
    """Add the new part of the conversation logs to the cost records. Unless full, read from the previous position (if there are no cost records yet, the first now or sample reads everything).
    In the same read, add the usage in the Codex conversation logs and the claude-token-pace records (if found) to the usage records (overlapping rows are not added).
    Returns {side: rows added, 'usage': usage rows added}."""
    import fcntl
    try:
        state = json.load(open(COST_STATE))
    except (OSError, ValueError):
        state = {}
    if not state and not full:
        return {}
    added = {}
    usage, reread = [], bool(full)   # reread: a part read before was read again (usage rows may overlap)
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
            reread = reread or (bool(prev) and pos == 0)
            if side == 'claude':
                rows, pos = _claude_rows(path, pos)
                for x in rows:
                    if x['out'] > have.get(x['k'], -1):
                        have[x['k']] = x['out']
                        new.append(x)
                ss[path] = {'size': size, 'mtime': mtime, 'pos': pos}
            else:
                cst = {k: prev.get(k) for k in ('model', 'tot', 'n')} if pos else {}
                rows, pos = _codex_rows(path, pos, cst, usage)
                new += rows
                ss[path] = {'size': size, 'mtime': mtime, 'pos': pos, **cst}
        if new:
            os.makedirs(DATA, exist_ok=True)
            with open(COST_LOG[side], 'a') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                for x in sorted(new, key=lambda x: x['ts']):
                    f.write(json.dumps(x) + '\n')
        added[side] = len(new)
    tp = state.setdefault('token-pace', {})
    for name in ('pace.jsonl', 'codex.jsonl'):
        path = TP + name
        try:
            size, mtime = os.path.getsize(path), os.path.getmtime(path)
        except OSError:
            continue
        prev = tp.get(name) or {}
        if prev.get('size') == size and prev.get('mtime') == mtime:
            continue
        pos = prev.get('pos', 0) if prev.get('size', 0) <= size else 0
        reread = reread or (bool(prev) and pos == 0)
        rows, pos = _token_pace_rows(path, pos, name)
        usage += rows
        tp[name] = {'size': size, 'mtime': mtime, 'pos': pos}
    if usage and reread:
        have = {(x['ts'], x['side'], row_id(x)) for x in read_log()}
        uniq = []
        for x in usage:
            k = (x['ts'], x['side'], row_id(x))
            if k not in have:
                have.add(k)
                uniq.append(x)
        usage = uniq
    record(sorted(usage, key=lambda x: x['ts']))
    added['usage'] = len(usage)
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
    """[(time, cost)] (in time order) and the list of models without a price. For Claude, each response becomes its one row with the largest output."""
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
            if x.get('model') != '<synthetic>':   # responses Claude Code writes itself (the API was not called)
                miss.add(x.get('model') or '(no model recorded)')
            c = 0.0
        ev.append((x['ts'], c))
    ev.sort()
    return ev, sorted(miss)


# ---- cost series (every 15 minutes) ----
Q = 900


class CostSeries:
    """Cost series every 15 minutes. x[i] = cost in [T0 + i·Q, T0 + (i+1)·Q). T0 is local midnight of the first day with a cost."""

    def __init__(self, ev, t_end):
        d0 = datetime.datetime.fromtimestamp(ev[0][0] if ev else t_end).date()
        self.T0 = time.mktime((d0.year, d0.month, d0.day, 0, 0, 0, 0, 0, -1))
        self.n = int((t_end - self.T0) // Q) + 1   # the last slot is the (partial) one containing t_end
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
        """(hour, whether a holiday, day number) of slot i. i may be outside the series (derived from the time)."""
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
    """Starts in [lo, hi] with the same time of day as i (in 15-minute units) and the same holiday-or-not (all of them if fewer than 3)."""
    h, od, _ = cs_.cal(i)
    q = i % 4
    s = [j for j in range(lo, hi + 1) if j % 4 == q and cs_.cal(j)[0] == h and cs_.cal(j)[1] == od]
    return s if len(s) >= 3 else list(range(lo, hi + 1))


class Empirical:
    """Past actuals S(s, x) at the same time of day and the same holiday-or-not (more recent weighs more)."""

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
    """Actuals S(s, x) at the same time of day, divided by the cost of the days days before s and multiplied by the cost of the days days before now."""

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
    """Weight w = rec × exp(−(dist/h)²). h is chosen so that the effective number of points (Σk)²/Σk² of the similarity-only weight exp(−(dist/h)²) equals ess.
    This effective number is monotonic in h, so h is unique and w depends continuously on distance and recency
    (matching with the recency-multiplied weights has several solutions and jumps to another one when the features move slightly)."""
    if len(dist) <= ess:
        return list(rec)
    d0 = min(dist)
    d = [v - d0 for v in dist]                      # make the nearest point 1 (so that the exp floor does not make them all 0)

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
    """What followed similar past points in time. Not cut at a fixed K points; weight = more recent weighs more (halflife days) × more similar weighs more (Gaussian).
    The Gaussian width is chosen so that the effective number of points of the similarity-only weight is a fixed number, so the weights move continuously when the start shifts.
    x of 1 day or more: one start per day at the same time of day as now (if the start shifts, the candidates shift by the same amount). Similar = close in the cost of the previous 24 hours and 7 days, and in
      the holiday share of the forecast period (candidates are not split by holiday-or-not). Effective number of days ESS_LONG.
    x under 1 day: starts with the same holiday-or-not. Starts whose futures of length x largely overlap (blocks of 4 hours for 4 hours or more, 1 hour for 1 hour or more) count as the single most similar one.
      Similar = close in time of day and in the cost of the previous 1, 4 and 24 hours and 7 days. Effective number of points ESS_SHORT."""
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
        """Share of holiday slots among the x slots from slot a (from the calendar; may be outside the series)."""
        c = cs_.__dict__.setdefault('_offc', [0])
        while len(c) <= a + xx:
            c.append(c[-1] + cs_.cal(len(c) - 1)[1])
        return (c[a + xx] - c[a]) / xx

    def neighbours(self, cs_, i, xx):
        """(chosen starts, weights). ([], []) if there are no candidates (the records are shorter than x)."""
        if xx >= 96:
            cand = [s for s in range(i - 96, 671, -96) if s + xx <= i]
            if not cand:
                return [], []
            fi = self._f(cs_, i)
            q = [self._f(cs_, s) for s in cand]
            F = [[f[2], f[3], self._offfrac(cs_, s, xx)] for s, f in zip(cand, q)]
            fi = [fi[2], fi[3], self._offfrac(cs_, i, xx)]
            # when the candidates all have nearly the same holiday share, do not count tiny differences as large
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
        best = {}                                  # block → (distance, start). On equal distance, the newer one
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
            # no candidates (the records are shorter than x): this part is not mixed in
            out.append(_wchoice(rng, [cs_.S(s, xx) for s in nn], w, n) if nn else [])
        return out


class Hurdle:
    """Simulates, every 15 minutes, whether used (a probability set by whether used just before, holiday-or-not and time of day) × how much (samples with the same holiday-or-not). More recent weighs more."""

    def __init__(self, halflife=7):
        self.hl = halflife

    PRIOR = 2.0   # prior weight (count) of the per-hour "probability of use". The prior value is the overall share of use (hours with few records are not taken as 0.5)

    def fit(self, cs_, i):
        act = [[0.5, 1.0], [0.5, 1.0]]                   # whether used just before → [times used, count]
        for j in range(1, i):
            pa = int(cs_.x[j - 1] > 0)
            act[pa][0] += cs_.x[j] > 0
            act[pa][1] += 1
        r = [a / n for a, n in act]
        num = [[[r[k] * self.PRIOR for k in range(2)] for _ in range(24)] for _ in range(2)]
        den = [[[self.PRIOR] * 2 for _ in range(24)] for _ in range(2)]
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
    """Forecasts the daily total and spreads it by time-of-day shares. log1p(daily total) = level + holiday difference + noise; the level is exponentially smoothed (alpha)."""

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
        adj = [v - self.seas[o] for v, o in zip(y, od)]
        # the initial level is the mean of the first few days (up to 7 days). Residuals are shifted to mean 0 (so that with few days, a bias in the residuals does not pile up every day and diverge)
        lv = sum(adj[:7]) / len(adj[:7]) if adj else 0.0
        res = []
        for v in adj:
            e = v - lv
            res.append(e)
            lv += self.alpha * e
        mu = sum(res) / len(res) if res else 0.0
        self.level, self.res = lv, ([e - mu for e in res] or [0.0])
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


PARTS = ('knn_w7', 'seas_w7', 'hurdle', 'lvl7', 'lvl3', 'daily')   # wpool parts (in the order of weights.json)
POOL2 = ('knn_w7', 'lvl7', 'daily')
W_HOURS = (1, 2, 4, 8, 12, 24)    # x (hours) that have wpool weights. If x goes beyond this range, pool2
N_SAMP = 400


def make_parts():
    return {'knn_w7': KNN(7), 'seas_w7': Empirical(7), 'hurdle': Hurdle(7), 'lvl7': LevelScaled(7), 'lvl3': LevelScaled(3),
            'daily': DailyTopDown()}


# form of the parts the weights were learned with. Raise it when the parts (PARTS) or their contents (such as how knn chooses) change. Weights of a different form are not used
# (for data/weights.json, the next sample or install.sh relearns them; until then, the bundled default)
WEIGHTS_FORM = 3


def weights_form_ok(d):
    return d.get('form') == WEIGHTS_FORM and list(d.get('parts') or []) == list(PARTS)


def _read_weights(f):
    try:
        with open(f) as fh:
            d = json.load(fh)
        if not weights_form_ok(d):
            return {}
        return {int(k): [float(v) for v in vs] for k, vs in d['weights'].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def load_weights():
    """{x (hours): [weights of PARTS]}. Per x, the learned ones (data/weights.json; not used if the form differs),
    else the bundled default, else equal shares. The second value is weights.json if any learned weight is used, else the default file."""
    default_file = _conf('weights-default.json')
    learned, default = _read_weights(WEIGHTS_FILE), _read_weights(default_file)
    equal = [1.0 / len(PARTS)] * len(PARTS)
    w = {h: learned.get(h) or default.get(h) or equal for h in W_HOURS}
    return w, (WEIGHTS_FILE if learned else default_file if default else None)


def _bucket(xx):
    """Evaluation point (hours) whose weights are used for x (number of slots). None beyond 24 hours (pool2)."""
    if xx > W_HOURS[-1] * 4:
        return None
    return min(W_HOURS, key=lambda h: abs(math.log(h * 4) - math.log(max(xx, 1))))


def forecast(cs_, i, xs, n=N_SAMP, seed=0, weights=None):
    """Samples of the cost used over x slots from slot i (a list in the order of xs)."""
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
        if w:
            names, ws = PARTS, w
        else:
            # when the records are short and knn and lvl7 have no samples (beyond 1 day, with records shorter than 7 days + x), mix in hurdle too instead of daily alone
            names = POOL2 + (('hurdle',) if not smp['knn_w7'][xx] and not smp['lvl7'][xx] else ())
            ws = [1.0 / len(names)] * len(names)
        # drop parts without samples (records shorter than x, no candidates) and rescale the remaining shares. None if none are left
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
    """E|A − B| (between samples). O(n log n) by sorting and cumulative sums."""
    b = sorted(b)
    acc = [0.0]
    for v in b:
        acc.append(acc[-1] + v)
    tot, m = 0.0, len(b)
    for v in a:
        k = bisect.bisect_right(b, v)
        tot += v * k - acc[k] + (acc[m] - acc[k]) - v * (m - k)
    return tot / (len(a) * m)


def calibrate(quiet=False, every=None, sub=50, step=0.05):
    """Learn the wpool weights per x (W_HOURS) from the cost records: over past starts j whose outcome is known (j + x ≤ now),
    the weights (in steps of step) that minimise the mean CRPS of the mixed distribution (Σ w_a E|X_a − y| − ½ Σ w_a w_b E|X_a − X_b|).
    Starts are every 2 hours over the last CALIBRATE_DAYS days (every slots; old conversation logs disappear so costs may be missing, and the way of use also changes).
    Only starts where every part has samples are used (lvl7 needs 7 days + x of records before the start). x with fewer than 20 starts is not learned (default weights)."""
    every = every or CALIBRATE_EVERY
    t = time.time()
    ev, _ = cost_events(MAIN_SIDE_FOR_WEIGHTS)
    if not ev:
        quiet or print('no cost records')
        return None
    cs_ = CostSeries(ev, t)
    xs = [h * 4 for h in W_HOURS]
    A = {h: [] for h in W_HOURS}
    B = {h: [] for h in W_HOURS}
    parts = make_parts()
    rng = random.Random(1)
    last_day = None
    first = max(0, cs_.n - CALIBRATE_DAYS * 96)
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
        quiet or print(f'{h}h: origins {len(A[h])}  weights ' + ' '.join(f'{nm} {v:.2f}' for nm, v in zip(PARTS, best)))
    # write even when learning failed (even with no x learned). The write time limits relearning to once a day. x that were not learned use the bundled default
    os.makedirs(DATA, exist_ok=True)
    tmp = WEIGHTS_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump({'form': WEIGHTS_FORM, 'parts': PARTS, 'weights': {str(h): w for h, w in out.items()}, 'n_origins': {str(h): len(A[h]) for h in out}}, fh)
    os.replace(tmp, WEIGHTS_FILE)
    if not out:
        quiet or print('not enough origins to learn from (keeping default weights)')
        return None
    quiet or print('->', WEIGHTS_FILE)
    return out


CALIBRATE_DAYS = 28
CALIBRATE_EVERY = 8   # interval between learning starts (in slots; 2 hours)
MAIN_SIDE_FOR_WEIGHTS = 'claude'   # the weights are learned from Claude's cost (only Claude was evaluated). Codex uses the same weights


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
    """From sample: when weights.json is older than 1 day, missing, or of a different form, relearn if the records are long enough to learn from
    (long enough for 20 starts where every part has samples at the shortest x. weights.json is written even when learning fails, so it is tried once a day)."""
    try:
        with open(WEIGHTS_FILE) as fh:
            same = weights_form_ok(json.load(fh))
        if same and t - os.path.getmtime(WEIGHTS_FILE) < 86400:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    ev, _ = cost_events(MAIN_SIDE_FOR_WEIGHTS)
    need = (max(p.L for p in make_parts().values() if isinstance(p, LevelScaled)) + W_HOURS[0] * 4 + 20 * CALIBRATE_EVERY) * Q
    if not ev or ev[-1][0] - ev[0][0] < need:
        return False
    return calibrate(quiet=True) is not None


# ---- usage records ----
CONFIRM = 1800   # whether a rise or drop in usage is real is decided by majority vote of the observations within this many seconds after it
EPS = 0.5        # margin (%) within which usage values are treated as equal
RESET_U = 5.0    # only a drop to this or below in the middle of a period is treated as a reset (a reset counts again from 0)


def clean_points(pts):
    """From the observations [(ts, u)] of one period (in time order), drop stale values and cut at resets in the middle. Returns a list of segments (usage never decreases within a segment).
    Several sessions each write the last usage they received, so stale values mix in between new ones.
    A risen value is taken if most later observations are the same or higher (this drops values that only look risen because of mixed-in stale values; values at or below RESET_U that look post-reset are not counted).
    A dropped value starts a new segment as a reset if it is at or below RESET_U and most later observations are also below the current value. Otherwise it is discarded as a stale value.
    Later observations: those within CONFIRM seconds. If there are none, the next one."""
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
            lo = sum(1 for v in later if v < u - EPS and not (v <= RESET_U < level))   # values that look post-reset are not counted
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
    """Window kind of a record row. For old rows without id, from the length (Claude 300→session, 10080→weekly_all; Codex by the length)."""
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
    """Holds the observations [(ts, u, r)] of one window, split into periods (one per reset)."""

    def __init__(self, side, wid, win_min, samples, scoped=False):
        self.side, self.id, self.win_min, self.win, self.scoped = side, wid, win_min, win_min * 60, scoped
        self.name = f'{side}-{wid}'
        # drop values (t >= r) from old sessions that keep reporting a period already reset. Periods are split by reset time (differences within 1 hour count as the same)
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
        """Running total used up to t (%; summed across resets)."""
        i = bisect.bisect_right(self._firsts, t) - 1
        if i < 0:
            return 0.0
        _, _, xs, us = self._gs[i]
        return self._base[i] + us[bisect.bisect_right(xs, t) - 1]

    def u_at(self, t):
        """Usage at t (0 after a period ends, until the next period is observed)."""
        i = bisect.bisect_right(self._firsts, t) - 1
        if i < 0 or t >= self._gs[i][1]:
            return 0.0
        _, _, xs, us = self._gs[i]
        return us[bisect.bisect_right(xs, t) - 1]


def load_series(side, rows=None, until=None):
    """{window kind: Series}."""
    by, meta = {}, {}
    for x in rows if rows is not None else read_log():
        if x.get('side') != side or not x.get('r') or not x.get('win') or (until is not None and x['ts'] > until):
            continue
        k = row_id(x)
        by.setdefault(k, []).append((x['ts'], float(x['u']), x['r']))
        meta[k] = (int(x['win']), bool(x.get('scoped')))
    return {k: Series(side, k, meta[k][0], s, meta[k][1]) for k, s in by.items()}


def record(rows, src='live'):
    """Add to the records (flock, so lines do not interleave even if another process writes at the same time). Rows borrowed from the records (cached) are not added."""
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


# ---- limit amount and exhaust probability ----
def limit_hist(ser, cs_, t):
    """Limit ($) from history: going back from t 1 hour at a time, cost until the usage growth reaches 100% ÷ growth. None if the growth is under 20%.
    It goes back to the nearest of 28 days, the start of the cost records and the start of the usage records."""
    c = du = 0.0
    b = t
    lo = max(cs_.T0, t - 28 * 86400, ser.first)    # time before the usage records start is not counted (only the cost would grow, with no growth in usage)
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
    """Limit ($) and how it was obtained. In order: from the current window (u ≥ LIMIT_NOW_U), from history, from the current window (when history is not enough).
    When the cost records start after the window start, history is used first; if unavailable, the current window with the cost before the records taken as 0
    (the same assumption as the cost covering only this computer's conversation logs)."""
    start = r - win_min * 60
    c = cs_.cost_between(start, t)
    now = 100.0 * c / u if u > 0 and c > 0 else None   # if the cost is 0 (only use elsewhere), it cannot be estimated from the current window
    if now is not None and u >= LIMIT_NOW_U and start >= cs_.T0:
        return now, 'now'
    h = limit_hist(ser, cs_, t) if ser else None
    if h:
        return h, 'hist'
    return (now, 'now') if now else (None, None)


def window_states(rows, t, log=None, n=N_SAMP):
    """From the current reads rows, the state of each window {name: {...}} (per side) and notes."""
    log = read_log() if log is None else log
    notes = []
    out = {'claude': {}, 'codex': {}}
    for side in ('claude', 'codex'):
        rs = [x for x in rows if x['side'] == side]
        if not rs:
            continue
        ev, miss = cost_events(side)
        if miss:
            notes.append(f'{side}: models without a price counted as 0 ({", ".join(miss)})')
        cs_ = CostSeries(ev, t) if ev else None
        if cs_ is None:
            notes.append(f'{side}: no cost records on this machine; no exhaust probability')
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
                st['note'] = 'model-scoped window (shown only, not used for the decision)'
                continue
            if cs_ is None:
                continue
            L, src = limit_usd(sers.get(row_id(x)), cs_, u, t, r, win)
            st['limit_usd'], st['limit_src'] = (L, src) if L else (None, None)
            if not L:
                st['note'] = 'limit unknown (no cost or no usage in this window yet, and no usage history to estimate from)'
            else:
                # the forecast starts at the start of the current slot (inputs are finished slots only, same as evaluation and learning). Use in the current slot is also in u, so this errs slightly high
                need.append((st, max(int(math.ceil((r - (cs_.T0 + (cs_.n - 1) * Q)) / Q)), 1), L * (1 - u / 100)))
        if need:
            i = cs_.n - 1
            xs = sorted({xx for _, xx, _ in need})
            smp = dict(zip(xs, forecast(cs_, i, xs, n=n)))
            for st, xx, m in need:
                s = smp[xx]
                st['remain_usd'] = m
                if not s:
                    st['note'] = 'cost records too short for an exhaust probability'
                    continue
                st['X'] = sum(1 for v in s if v >= m) / len(s)
    return out, notes


def decide_states(states, main=MAIN, guard=GUARD_RISK, default=TIE_DEFAULT):
    """states: {side: {window name: state}}. Returns (chosen side, reasons). Model-scoped windows are not used. Windows without X are not used for the guard or the comparison."""
    sub = 'codex' if main == 'claude' else 'claude'
    use = {s: {k: v for k, v in states.get(s, {}).items() if not v.get('scoped')} for s in ('claude', 'codex')}

    def leftover(d):
        v = [1 - x['X'] for x in d.values() if x['win'] >= LONG_WIN and x['X'] is not None]
        return min(v) if v else 0.5
    full = {s: [k for k, v in use[s].items() if v['u'] >= FULL] for s in use}
    over = [k for k, v in use[main].items() if v['X'] is not None and v['X'] > guard]
    tight = [k for k, v in use[sub].items() if v['X'] is not None and v['X'] > guard]
    L = {s: leftover(use[s]) for s in use}
    # guard and sub_tight list windows only when the decision was made at that stage. full always lists every full window
    why = {'main': main, 'claude_L': L['claude'], 'codex_L': L['codex'], 'guard': [], 'sub_tight': [],
           'full': full['claude'] + full['codex'], 'tie': False, 'stage': ''}
    if full[sub] and not full[main]:
        return main, dict(why, stage='full')
    if full[main] and not full[sub]:
        return sub, dict(why, stage='full')
    # when both sides have a window over guard, the guard does not decide (sending to either side may use it up); go on to the comparison
    if over and not tight:
        return sub, dict(why, stage='guard', guard=over)
    if tight and not over:
        return main, dict(why, stage='guard', sub_tight=tight)
    if L['claude'] == L['codex']:
        return default, dict(why, stage='tie', tie=True)
    return ('claude' if L['claude'] > L['codex'] else 'codex'), dict(why, stage='leftover')


# ---- read the current values directly (neither calls a model) ----
def claude_credentials():
    """Claude Code's OAuth credentials (read only, never refreshed: refreshing rotates the token and can break the authentication of Claude Code itself)."""
    import subprocess
    f = os.path.join(CLAUDE_HOME, '.credentials.json')
    if os.path.exists(f):
        return json.load(open(f))['claudeAiOauth']
    if sys.platform == 'darwin':   # on macOS they are stored in the Keychain (experimental support)
        p = subprocess.run(['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                           capture_output=True, text=True, timeout=10)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout)['claudeAiOauth']
    raise RuntimeError(f'no Claude Code credentials ({f})')


class RateLimited(RuntimeError):
    pass


def parse_claude(d, notes=None):
    """Usage API response → [{side, id, win, u, r, scoped}]. The limits array if present, else five_hour and seven_day."""
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
                notes.append(f'Claude: unknown window kind {wid} (group={w.get("group")}), not used')
            continue
        out.append({'side': 'claude', 'id': wid, 'win': win, 'u': float(w['percent']), 'r': ts(w.get('resets_at')), 'scoped': bool(scope)})
    if not out:
        for key, wid, win in (('five_hour', 'session', 300), ('seven_day', 'weekly_all', 10080)):
            w = d.get(key)
            if w and w.get('utilization') is not None:
                out.append({'side': 'claude', 'id': wid, 'win': win, 'u': float(w['utilization']), 'r': ts(w.get('resets_at')), 'scoped': False})
    return out


def read_claude(t, notes=None):
    """GET /api/oauth/usage, the same as /usage in Claude Code."""
    import urllib.error
    import urllib.request
    cred = claude_credentials()
    if cred.get('expiresAt') and cred['expiresAt'] / 1000 < t:
        raise RuntimeError('Claude access token expired (unreadable until Claude Code refreshes it; not refreshed here)')
    url = 'https://api.anthropic.com/api/oauth/usage'
    test_url = os.environ.get('CLAUDE_CODEX_ROUTER_CLAUDE_URL', '')   # for tests. Only a local fake server is allowed, so the token is never sent out
    if test_url:
        if not test_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
            raise RuntimeError('CLAUDE_CODEX_ROUTER_CLAUDE_URL must be http://127.0.0.1:... or http://localhost:...')
        url = test_url
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + cred['accessToken'], 'anthropic-beta': 'oauth-2025-04-20'})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited('Claude usage API returned 429 (too many requests)') from e
        raise
    out = parse_claude(d, notes)
    if not out:
        raise RuntimeError('no Claude usage windows in the response')
    return out


def parse_codex(d):
    """ChatGPT usage response (/backend-api/wham/usage) → [{side, id, win, u, r}] (window kinds are named by their length).
    primary_window and secondary_window of rate_limit (the usage windows of the plan's agent features, including Codex)."""
    out = []
    rl = (d or {}).get('rate_limit') or {}
    for k in ('primary_window', 'secondary_window'):
        w = rl.get(k)
        if not w or w.get('used_percent') is None or not w.get('limit_window_seconds'):
            continue
        r = w.get('reset_at') or (time.time() + w['reset_after_seconds'] if w.get('reset_after_seconds') is not None else None)
        if not r:
            continue
        win = int(w['limit_window_seconds']) // 60
        out.append({'side': 'codex', 'id': win_label(win), 'win': win, 'u': float(w['used_percent']), 'r': int(r)})
    return out


def _jwt_exp(tok):
    import base64
    try:
        p = tok.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4))).get('exp')
    except (IndexError, ValueError, AttributeError):
        return None


def read_codex(t, timeout=15, notes=None):
    """ChatGPT usage (GET /backend-api/wham/usage; the endpoint written in the Codex CLI executable).
    The token is only read from Codex's login info ($CODEX_HOME/auth.json, default ~/.codex) and never refreshed (Codex refreshes it)."""
    import urllib.error
    import urllib.request
    f = os.path.join(CODEX_HOME, 'auth.json')
    try:
        with open(f) as fh:
            tokens = json.load(fh).get('tokens') or {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f'no Codex login ({f}: {e.__class__.__name__})') from e
    tok, acct = tokens.get('access_token'), tokens.get('account_id')
    if not tok:
        raise RuntimeError('Codex is not logged in with a ChatGPT plan (API-key login has no plan usage)')
    exp = _jwt_exp(tok)
    if exp and exp < t:
        raise RuntimeError('Codex access token expired (unreadable until Codex refreshes it; not refreshed here)')
    url = 'https://chatgpt.com/backend-api/wham/usage'
    test_url = os.environ.get('CLAUDE_CODEX_ROUTER_CODEX_URL', '')   # for tests. Only a local fake server is allowed, so the token is never sent out
    if test_url:
        if not test_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
            raise RuntimeError('CLAUDE_CODEX_ROUTER_CODEX_URL must be http://127.0.0.1:... or http://localhost:...')
        url = test_url
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'claude-codex-router'}
    if acct:
        h['ChatGPT-Account-Id'] = acct
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        d = json.load(r)
    out = parse_codex(d)
    if not out:
        raise RuntimeError('no Codex usage windows in the response')
    return out


def recent_rows(side, t, max_age=RECENT_MAX_AGE):
    """The latest read of side in the records (within max_age seconds of t). [] if there is none."""
    rows = [x for x in read_log() if x.get('side') == side and x.get('win') and x.get('src') == 'live' and t - max_age <= x['ts'] <= t]
    if not rows:
        return []
    last = max(x['ts'] for x in rows)
    return [{**x, 'cached': True} for x in rows if x['ts'] == last]


def read_both(t):
    """Read both. If one side is unreadable, the other is still returned. Returns (rows, list of failures, list of notes).
    For Claude, if there is a record within CLAUDE_MIN_INTERVAL seconds, it is used without calling the API.
    If Claude's API returns 429, the latest value in the records (within RECENT_MAX_AGE seconds) is used without calling again."""
    rows, errs, notes = [], [], []
    for name, f in (('Claude', read_claude), ('Codex', read_codex)):
        if name == 'Claude' and CLAUDE_MIN_INTERVAL > 0:
            old = recent_rows('claude', t, CLAUDE_MIN_INTERVAL)
            if old:
                rows += old
                notes.append(f'{name}: used the record from {time.strftime("%H:%M:%S", time.localtime(old[0]["ts"]))} (within {CLAUDE_MIN_INTERVAL:.0f} s; API not called)')
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
                notes.append(f'{name}: 429; used the record from {time.strftime("%H:%M:%S", time.localtime(old[0]["ts"]))}')
            else:
                errs.append(f'{name}: {e} (no record within {RECENT_MAX_AGE // 60} min either)')
        except Exception as e:   # noqa: BLE001 — report why it could not be read as is
            errs.append(f'{name}: {e}')
    return rows, errs, notes


STALE_SAMPLE = 1800   # report when periodic sampling (sample every 5 minutes) has stopped for longer than this


def sample_note(t):
    """A note if periodic sampling has stopped (decisions can continue because now adds cost and usage; when it stops, relearning the weights and updating the prices are delayed, and the records get gaps)."""
    try:
        last = os.path.getmtime(SAMPLE_STAMP)
    except OSError:
        try:
            if t - os.path.getmtime(VERSION_FILE) <= STALE_SAMPLE:
                return None
        except OSError:
            return None                  # not an installed copy (run directly from the repository)
        return 'periodic sample (every 5 min) has never run; check the timer (README: periodic sampling)'
    if t - last <= STALE_SAMPLE:
        return None
    return f'periodic sample (every 5 min) stopped {(t - last) / 3600:.1f} h ago (last {time.strftime("%m-%d %H:%M", time.localtime(last))}); check the timer (README: periodic sampling)'


def decide_now(t, main=MAIN):
    """Read both, add them to the records, and decide. Returns (result dict, exit code)."""
    rows, errs, notes = read_both(t)
    sn = sample_note(t)
    if sn:
        notes.append(sn)
    record(rows)
    try:
        sync_costs(full=not os.path.exists(COST_STATE))   # if there are no cost records yet (before sample), read all the conversation logs
    except Exception as e:   # noqa: BLE001 — decide even if the cost records could not be added
        notes.append(f'could not add cost records: {e}')
    states, n2 = window_states(rows, t)
    notes += n2
    if _price_src and _price_src.startswith('fetch failed'):
        notes.append(f'prices: {_price_src}')
    res = {'pick': None, 'main': main, 'reason': '', 'tie': False, 'guard': [], 'sub_tight': [], 'full': [], 'claude_L': None, 'codex_L': None,
           'windows': {}, 'errors': errs, 'notes': notes}
    for side in states:
        for k, v in states[side].items():
            res['windows'][k] = {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
    if states['claude'] and states['codex']:
        pick, why = decide_states(states, main)
        res.update(pick=pick, tie=why['tie'], guard=why['guard'], sub_tight=why['sub_tight'], full=why['full'], claude_L=round(why['claude_L'], 4),
                   codex_L=round(why['codex_L'], 4),
                   reason=why['stage'])
        return res, 0
    if states['claude']:
        res.update(pick='claude', reason='codex_unreadable')
        return res, 1
    if states['codex']:
        res.update(pick='codex', reason='claude_unreadable')
        return res, 1
    res.update(pick=TIE_DEFAULT, reason='both_unreadable')
    return res, 2


def print_human(res):
    def f(x):
        return time.strftime('%m-%d %H:%M', time.localtime(x))
    print(f'pick: {res["pick"]} (reason {res["reason"]}, main {res["main"]})')
    for e in res['errors']:
        print('  error:', e)
    for e in res['notes']:
        print('  note:', e)
    for k, v in res['windows'].items():
        extra = ''
        if v.get('limit_usd'):
            extra += f'  limit ${v["limit_usd"]:,.0f} ({"current window" if v["limit_src"] == "now" else "history"})'
        if v.get('X') is not None:
            extra += f'  remaining ${v["remain_usd"]:,.0f}, exhaust probability {v["X"]:.2f}'
        if v.get('note'):
            extra += f'  {v["note"]}'
        print(f'  {k} ({win_label(v["win"])}): used {v["u"]:.0f}%  resets {f(v["r"])} (in {v["rem_h"]:.1f} h){extra}')
    if res['claude_L'] is not None:
        print(f'  leftover probability Claude {res["claude_L"]:.2f} / Codex {res["codex_L"]:.2f}  guard {res["guard"] or "none"}'
              f'  sub_tight {res["sub_tight"] or "none"}  full {res["full"] or "none"}')


def _arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'now':
        m = _arg('--main') or MAIN
        if m not in ('claude', 'codex'):
            print('--main must be claude or codex', file=sys.stderr)
            sys.exit(2)
        res, code = decide_now(time.time(), m)
        if '--json' in sys.argv:
            print(json.dumps(res))
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
            added, notes = {}, notes + [f'could not add cost records: {e}']
        re_ = maybe_calibrate(t)
        os.makedirs(DATA, exist_ok=True)
        with open(SAMPLE_STAMP, 'w') as f:
            f.write(f'{int(t)}\n')
        usage_added = added.pop('usage', 0) if isinstance(added, dict) else 0
        print(f'windows recorded {len(rows)}  cost rows {added}  usage rows from logs {usage_added}' + (f'  errors: {errs}' if errs else '') + (f'  notes: {notes}' if notes else '')
              + ('  weights relearned' if re_ else ''))
        sys.exit(0 if rows else 1)
    if cmd == 'calibrate':
        if '--if-needed' in sys.argv:   # from install.sh: only when the weights are missing, old, or of a different form (if the records are long enough to learn from)
            print('weights relearned' if maybe_calibrate(time.time()) else 'weights kept')
            return
        calibrate()
        return
    print(USAGE)
    sys.exit(0 if cmd in ('', '-h', '--help', 'help') else 2)


if __name__ == '__main__':
    main()
