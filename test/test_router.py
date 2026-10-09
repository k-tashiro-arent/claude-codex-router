"""router.py の判断・記録・料金・上限を作った状況で確かめる。時刻は Asia/Tokyo で作る（料金の列はローカル時刻の日で区切る）。"""
import datetime
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

os.environ['TZ'] = 'Asia/Tokyo'
time.tzset()
# 入っている環境の config.json を読まないよう、必ず「入っていない」場所にする（リポジトリの defaults/ が使われる）
os.environ['CLAUDE_CODEX_ROUTER_DIR'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_no_install')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin'))
import router as route  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
H, D = 3600, 86400
# 単価（LiteLLM の形）。通信しないよう、試験では常にこれを使う
PRICES = {
    'claude-opus-5-5': {'input_cost_per_token': 4e-06, 'output_cost_per_token': 2e-05, 'cache_creation_input_token_cost': 5e-06,
                        'cache_creation_input_token_cost_above_1hr': 8e-06, 'cache_read_input_token_cost': 2e-07},
    'claude-haiku-4-5': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 5e-06},
    'openai/gpt-6.1-sol': {'input_cost_per_token': 2e-06, 'output_cost_per_token': 1e-05, 'cache_read_input_token_cost': 1e-07},
}


def at(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=JST).timestamp()


class TempDirCase(unittest.TestCase):
    """記録の場所を空の一時ディレクトリにし、単価は PRICES にする。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = {k: getattr(route, k) for k in ('DATA', 'USAGE_LOG', 'COST_LOG', 'COST_STATE', 'PRICE_FILE', 'WEIGHTS_FILE',
                                                     'CLAUDE_HOME', 'CODEX_HOME', '_prices', 'CONFIG')}
        route.DATA = os.path.join(self.d, 'data')
        route.USAGE_LOG = os.path.join(route.DATA, 'usage.jsonl')
        route.COST_LOG = {s: os.path.join(route.DATA, f'cost-{s}.jsonl') for s in ('claude', 'codex')}
        route.COST_STATE = os.path.join(route.DATA, 'cost-state.json')
        route.PRICE_FILE = os.path.join(route.DATA, 'litellm-prices.json')
        route.WEIGHTS_FILE = os.path.join(route.DATA, 'weights.json')
        route.CLAUDE_HOME, route.CODEX_HOME = os.path.join(self.d, 'claude'), os.path.join(self.d, 'codex')
        route._prices = dict(PRICES)
        route.CONFIG = dict(route.CONFIG, prices={})

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(route, k, v)
        shutil.rmtree(self.d, ignore_errors=True)

    def write_costs(self, side, rows):
        os.makedirs(route.DATA, exist_ok=True)
        with open(route.COST_LOG[side], 'a') as f:
            for x in rows:
                f.write(json.dumps(x) + '\n')


class DefaultsTests(unittest.TestCase):
    def test_shipped_defaults_are_used(self):
        self.assertEqual((route.MAIN, route.GUARD_RISK, route.TIE_DEFAULT, route.FULL, route.CLAUDE_MIN_INTERVAL),
                         ('claude', 0.5, 'codex', 98.0, 60.0))
        w, src = route.load_weights()
        self.assertEqual(sorted(w), list(route.W_HOURS))
        self.assertTrue(src.endswith('weights-default.json'))
        for v in w.values():
            self.assertEqual(len(v), len(route.PARTS))
            self.assertAlmostEqual(sum(v), 1.0)


class PriceTests(TempDirCase):
    def test_lookup_bare_prefixed_and_dated_names(self):
        p = route.price('claude-opus-5-5')
        for k, v in zip(('in', 'out', 'cw5m', 'cw1h', 'cr'), (4.0, 20.0, 5.0, 8.0, 0.2)):
            self.assertAlmostEqual(p[k], v)
        self.assertAlmostEqual(route.price('gpt-6.1-sol')['out'], 10.0)            # openai/ を付けた名前
        self.assertAlmostEqual(route.price('claude-haiku-4-5-20251001')['in'], 1.0)  # 日付の付いた名前は日付を外して探す
        self.assertIsNone(route.price('codex-auto-review'))
        q = route.price('claude-haiku-4-5')
        self.assertEqual((q['cw5m'], q['cr']), (1.0, 1.0))                           # 欄が無ければ入力の単価

    def test_config_overrides_and_adds_models(self):
        route.CONFIG = dict(route.CONFIG, prices={'claude-opus-5-5': {'cr': 0.5}, 'my-model': {'in': 1, 'out': 2, 'cw5m': 1, 'cw1h': 1, 'cr': 0.1}})
        self.assertEqual(route.price('claude-opus-5-5')['cr'], 0.5)
        self.assertEqual(route.price('claude-opus-5-5')['out'], 20.0)
        self.assertEqual(route.price('my-model')['out'], 2.0)

    def test_cost_formulas(self):
        c = route.cost_of('claude', {'model': 'claude-opus-5-5', 'in': 1e6, 'out': 1e6, 'cw5m': 1e6, 'cw1h': 1e6, 'cr': 1e6})
        self.assertAlmostEqual(c, 4 + 20 + 5 + 8 + 0.2)
        x = route.cost_of('codex', {'model': 'gpt-6.1-sol', 'in': 3e6, 'cached': 2e6, 'out': 1e6})   # 入力はキャッシュを含む
        self.assertAlmostEqual(x, 1 * 2 + 2 * 0.1 + 1 * 10)
        self.assertIsNone(route.cost_of('codex', {'model': 'codex-auto-review', 'in': 1}))

    def test_cached_file_is_used_when_fetch_fails(self):
        route._prices = None
        os.makedirs(route.DATA)
        json.dump(PRICES, open(route.PRICE_FILE, 'w'))
        os.utime(route.PRICE_FILE, (time.time() - 2 * D, time.time() - 2 * D))
        saved = route.PRICE_URL
        route.PRICE_URL = 'file:///nonexistent/prices.json'
        try:
            d = route.load_prices(refresh=True)
        finally:
            route.PRICE_URL = saved
        self.assertIn('claude-opus-5-5', d)
        self.assertTrue(route._price_src.startswith('取れなかった'))

    def test_fetch_writes_the_cache(self):
        route._prices = None
        src = os.path.join(self.d, 'src.json')
        json.dump({f'm{i}': {'input_cost_per_token': 1e-6} for i in range(150)}, open(src, 'w'))
        saved = route.PRICE_URL
        route.PRICE_URL = 'file://' + src
        try:
            d = route.load_prices()
        finally:
            route.PRICE_URL = saved
        self.assertEqual(len(d), 150)
        self.assertTrue(os.path.exists(route.PRICE_FILE))


class CostLogTests(TempDirCase):
    def _claude_line(self, mid, rid, ts, out, model='claude-opus-5-5'):
        return json.dumps({'timestamp': ts, 'requestId': rid, 'message': {'id': mid, 'model': model, 'usage': {
            'input_tokens': 10, 'output_tokens': out, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 100,
            'cache_creation': {'ephemeral_5m_input_tokens': 60, 'ephemeral_1h_input_tokens': 40}}}}) + '\n'

    def test_claude_streamed_lines_count_once_with_max_output(self):
        p = os.path.join(route.CLAUDE_HOME, 'projects', 'proj', 'a.jsonl')
        os.makedirs(os.path.dirname(p))
        with open(p, 'w') as f:
            f.write(self._claude_line('m1', 'r1', '2026-10-06T01:00:00Z', 5))
            f.write(self._claude_line('m1', 'r1', '2026-10-06T01:00:01Z', 50))   # 同じ応答の続き
            f.write(self._claude_line('m2', 'r2', '2026-10-06T01:05:00Z', 7))
        self.assertEqual(route.sync_costs(full=True)['claude'], 3)
        ev, miss = route.cost_events('claude')
        self.assertEqual(len(ev), 2)
        one = (10 * 4 + 50 * 20 + 60 * 5 + 40 * 8 + 1000 * 0.2) / 1e6
        self.assertAlmostEqual(ev[0][1], one)
        self.assertEqual(miss, [])

    def test_incremental_sync_reads_only_new_lines(self):
        p = os.path.join(route.CLAUDE_HOME, 'projects', 'proj', 'a.jsonl')
        os.makedirs(os.path.dirname(p))
        with open(p, 'w') as f:
            f.write(self._claude_line('m1', 'r1', '2026-10-06T01:00:00Z', 5))
        self.assertEqual(route.sync_costs(full=True)['claude'], 1)
        self.assertEqual(route.sync_costs()['claude'], 0)             # 変わっていなければ読まない
        with open(p, 'a') as f:
            f.write(self._claude_line('m3', 'r3', '2026-10-06T02:00:00Z', 9, model='unknown-model'))
            f.write('{"partial": ')                                    # 書きかけの行は次に回す
        self.assertEqual(route.sync_costs()['claude'], 1)
        ev, miss = route.cost_events('claude')
        self.assertEqual(len(ev), 2)
        self.assertEqual(miss, ['unknown-model'])

    def test_sync_without_state_waits_for_full(self):
        self.assertEqual(route.sync_costs(), {})                      # 初めての環境は import か sample が全部読む

    def test_codex_counts_increments_of_the_running_total(self):
        p = os.path.join(route.CODEX_HOME, 'sessions', '2026', '10', '06', 's.jsonl')
        os.makedirs(os.path.dirname(p))

        def tc(ts, i, c, o):
            return json.dumps({'timestamp': ts, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o}}}}) + '\n'
        with open(p, 'w') as f:
            f.write(json.dumps({'timestamp': '2026-10-06T00:00:00Z', 'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}}) + '\n')
            f.write(tc('2026-10-06T00:01:00Z', 1000, 500, 10))
            f.write(tc('2026-10-06T00:01:01Z', 1000, 500, 10))   # 同じ累計の繰り返しは数えない
            f.write(tc('2026-10-06T00:02:00Z', 3000, 1500, 30))
        route.sync_costs(full=True)
        with open(p, 'a') as f:
            f.write(tc('2026-10-06T00:03:00Z', 4000, 2000, 40))
        route.sync_costs()
        rows = route.read_jsonl(route.COST_LOG['codex'])
        self.assertEqual([(x['in'], x['cached'], x['out']) for x in rows], [(1000, 500, 10), (2000, 1000, 20), (1000, 500, 10)])
        self.assertTrue(all(x['model'] == 'gpt-6.1-sol' for x in rows))


class ParseTests(unittest.TestCase):
    def test_claude_limits_array(self):
        d = {'limits': [
            {'kind': 'session', 'group': 'session', 'percent': 32, 'resets_at': '2026-10-07T19:59:59+00:00', 'scope': None},
            {'kind': 'weekly_all', 'group': 'weekly', 'percent': 18, 'resets_at': '2026-10-14T05:59:59+00:00', 'scope': None},
            {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 0, 'resets_at': '2026-10-14T06:00:00+00:00',
             'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}},
            {'kind': 'monthly_x', 'group': 'monthly', 'percent': 5, 'resets_at': '2026-11-01T00:00:00+00:00', 'scope': None}]}
        notes = []
        rows = route.parse_claude(d, notes)
        self.assertEqual([(x['id'], x['win'], x['u'], x['scoped']) for x in rows],
                         [('session', 300, 32.0, False), ('weekly_all', 10080, 18.0, False), ('weekly_scoped:Fable', 10080, 0.0, True)])
        self.assertIn('monthly_x', notes[0])

    def test_claude_old_shape(self):
        d = {'five_hour': {'utilization': 12.0, 'resets_at': '2026-10-07T19:59:59+00:00'},
             'seven_day': {'utilization': 40.0, 'resets_at': '2026-10-14T05:59:59+00:00'}}
        self.assertEqual([(x['id'], x['win']) for x in route.parse_claude(d)], [('session', 300), ('weekly_all', 10080)])

    def test_codex_windows_by_length(self):
        rl = {'primary': {'usedPercent': 35, 'windowDurationMins': 10080, 'resetsAt': 1791948629}, 'secondary': None}
        self.assertEqual(route.parse_codex(rl), [{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 35.0, 'r': 1791948629}])

    def test_old_rows_get_ids_from_length(self):
        self.assertEqual(route.row_id({'side': 'claude', 'win': 300}), 'session')
        self.assertEqual(route.row_id({'side': 'claude', 'win': 10080}), 'weekly_all')
        self.assertEqual(route.row_id({'side': 'codex', 'win': 300}), '5h')
        self.assertEqual(route.row_id({'side': 'claude', 'win': 10080, 'id': 'weekly_scoped:Fable'}), 'weekly_scoped:Fable')


class CleanPointsTests(unittest.TestCase):
    """複数のセッションが書いた記録に混ざる古い値と、窓の途中のリセット。"""

    def test_stale_lower_value_is_dropped(self):
        pts = [(0, 40.0), (60, 41.0), (90, 38.0), (120, 42.0), (180, 43.0)]
        self.assertEqual(route.clean_points(pts), [[(0, 40.0), (60, 41.0), (120, 42.0), (180, 43.0)]])

    def test_drop_to_near_zero_confirmed_is_a_reset(self):
        pts = [(0, 27.0), (60, 28.0), (120, 0.0), (150, 28.0), (180, 0.0), (240, 1.0), (300, 2.0)]
        segs = route.clean_points(pts)
        self.assertEqual(segs, [[(0, 27.0), (60, 28.0)], [(120, 0.0), (180, 0.0), (240, 1.0), (300, 2.0)]])   # 直後の古い 28 も捨てる

    def test_drop_not_near_zero_never_splits(self):
        pts = [(0, 21.0), (1200, 16.0), (2200, 17.0), (9000, 22.0)]   # 観測がまばらで、古い値が続けて来る
        self.assertEqual(len(route.clean_points(pts)), 1)

    def test_without_points_within_confirm_the_next_one_decides(self):
        far = route.CONFIRM + 600
        self.assertEqual(route.clean_points([(0, 63.0), (100, 2.0), (100 + far, 3.0)]), [[(0, 63.0)], [(100, 2.0), (100 + far, 3.0)]])
        self.assertEqual(route.clean_points([(0, 63.0), (100, 2.0), (100 + far, 63.0)]), [[(0, 63.0), (100 + far, 63.0)]])

    def test_series_total_adds_across_resets_without_negative_use(self):
        t0, r = at('2026-10-05 08:00'), at('2026-10-12 08:00')
        samples = [(t0 + i * 1800, (i * 0.2 if i < 100 else (i - 100) * 0.2), r) for i in range(200)]
        s = route.Series('claude', 'weekly_all', 10080, samples)
        self.assertEqual(len(s.groups), 2)
        self.assertAlmostEqual(s.total(t0 + 199 * 1800), 99 * 0.2 + 99 * 0.2)
        self.assertAlmostEqual(s.u_at(t0 + 150 * 1800), 50 * 0.2)


class LimitTests(TempDirCase):
    """上限の金額：使用率が大きければ今の枠の分、小さければ記録の分。"""

    def _flat(self, t_end, days, per_hour):
        """t_end までの days 日、毎時 per_hour ドル使った料金の記録。"""
        ev = [(t_end - days * D + h * H + 60, per_hour) for h in range(days * 24)]
        return route.CostSeries(ev, t_end)

    def test_now_estimate_when_usage_is_large(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 10, 5.0)          # 1 時間 $5
        r = t + 2 * D                          # 7 日枠の始まり＝t − 5 日 → 今の枠で $600
        L, src = route.limit_usd(None, cs_, 50.0, t, r, 10080)
        self.assertEqual(src, 'now')
        self.assertAlmostEqual(L, 1200.0, delta=15)

    def test_hist_estimate_when_usage_is_small(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 20, 5.0)
        # 記録：使用率は 1 時間に 0.5%（＝上限 $1,000）、10 時間前にリセット（u はまだ 5%）
        r0 = t - 10 * H
        r1 = r0 + 7 * D
        samples = [(r0 - 7 * D + k * H, 0.5 * k, r0) for k in range(1, 7 * 24)]
        samples += [(r0 + k * H, 0.5 * k, r1) for k in range(0, 11)]
        ser = route.Series('codex', '7d', 10080, samples)
        u = ser.u_at(t)
        self.assertLess(u, route.LIMIT_NOW_U)
        L, src = route.limit_usd(ser, cs_, u, t, r1, 10080)
        self.assertEqual(src, 'hist')
        self.assertAlmostEqual(L, 1000.0, delta=60)

    def test_no_estimate_without_cost_or_usage(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 2, 0.0)
        self.assertEqual(route.limit_usd(None, cs_, 0.0, t, t + D, 10080), (None, None))


class DecideTests(unittest.TestCase):
    def st(self, u, X, win=10080, scoped=False):
        return {'u': u, 'X': X, 'win': win, 'scoped': scoped}

    def test_compare_long_windows(self):
        s = {'claude': {'claude-session': self.st(10, 0.1, 300), 'claude-weekly_all': self.st(40, 0.3)},
             'codex': {'codex-7d': self.st(20, 0.6)}}
        pick, why = route.decide_states(s, 'claude', guard=0.9)
        self.assertEqual(pick, 'claude')
        self.assertAlmostEqual(why['claude_L'], 0.7)
        self.assertAlmostEqual(why['codex_L'], 0.4)

    def test_guard_on_main_goes_to_sub(self):
        s = {'claude': {'claude-session': self.st(80, 0.7, 300), 'claude-weekly_all': self.st(10, 0.0)},
             'codex': {'codex-7d': self.st(90, 0.95)}}
        self.assertEqual(route.decide_states(s, 'claude')[0], 'codex')
        self.assertEqual(route.decide_states(s, 'codex')[0], 'claude')      # 主側が Codex なら、Codex の歯止めで Claude へ

    def test_sub_short_window_tight_keeps_main(self):
        s = {'claude': {'claude-weekly_all': self.st(10, 0.0)},
             'codex': {'codex-5h': self.st(70, 0.8, 300), 'codex-7d': self.st(5, 0.0)}}
        pick, why = route.decide_states(s, 'claude')
        self.assertEqual((pick, why['sub_tight']), ('claude', ['codex-5h']))

    def test_full_side_is_never_chosen(self):
        s = {'claude': {'claude-weekly_all': self.st(99, None)}, 'codex': {'codex-7d': self.st(97, 0.99)}}
        self.assertEqual(route.decide_states(s, 'claude')[0], 'codex')
        self.assertEqual(route.decide_states(s, 'codex')[0], 'codex')

    def test_scoped_and_unknown_windows_are_ignored(self):
        s = {'claude': {'claude-weekly_all': self.st(10, 0.2), 'claude-weekly_scoped:Fable': self.st(99, 1.0, scoped=True)},
             'codex': {'codex-7d': self.st(10, None)}}
        pick, why = route.decide_states(s, 'claude')
        self.assertEqual(why['full'], [])
        self.assertEqual(why['guard'], [])
        self.assertAlmostEqual(why['codex_L'], 0.5)                          # X の無い枠しか無ければ 0.5
        self.assertEqual(pick, 'claude')

    def test_tie_follows_default(self):
        s = {'claude': {'claude-weekly_all': self.st(10, 0.2)}, 'codex': {'codex-7d': self.st(10, 0.2)}}
        self.assertEqual(route.decide_states(s, 'claude', default='codex')[0], 'codex')
        self.assertTrue(route.decide_states(s, 'claude')[1]['tie'])


class ForecastTests(unittest.TestCase):
    def _series(self, t_end, days, f):
        ev = []
        for k in range(days * 96):
            t = t_end - days * D + k * route.Q + 1
            c = f(datetime.datetime.fromtimestamp(t))
            if c:
                ev.append((t, c))
        return route.CostSeries(ev, t_end)

    def test_repeatable_and_follows_the_daily_pattern(self):
        t = at('2026-10-07 07:00')   # 水曜の朝
        cs_ = self._series(t, 28, lambda d: 2.0 if d.weekday() < 5 and 9 <= d.hour < 18 else 0.0)   # 平日 9〜18 時に 15 分 $2
        a = route.forecast(cs_, cs_.n, [4, 96])
        self.assertEqual(a, route.forecast(cs_, cs_.n, [4, 96]))           # 同じ入力なら同じ標本
        one_h, one_d = [sorted(v) for v in a]
        self.assertLess(one_h[len(one_h) // 2], 1.0)                       # 7〜8 時はほとんど使わない
        self.assertAlmostEqual(one_d[len(one_d) // 2], 72.0, delta=12)     # 1 日で 9 時間 × $8

    def test_long_horizon_uses_pool2_and_weekend_is_quiet(self):
        t = at('2026-10-10 00:00')   # 土曜 0 時
        cs_ = self._series(t, 28, lambda d: 2.0 if d.weekday() < 5 and 9 <= d.hour < 18 else 0.0)
        two_days, week = [sorted(v) for v in route.forecast(cs_, cs_.n, [192, 672])]
        self.assertIsNone(route._bucket(672))
        self.assertLess(two_days[len(two_days) // 2], 10.0)                # 土日
        self.assertAlmostEqual(week[len(week) // 2], 360.0, delta=60)      # 平日 5 日 × $72

    def test_short_history_drops_parts_without_candidates(self):
        t = at('2026-10-07 07:00')
        cs_ = self._series(t, 3, lambda d: 1.0)                               # 3 日分しかない（15 分 $1）
        week = route.forecast(cs_, cs_.n, [672])[0]
        self.assertEqual(len(week), route.N_SAMP)
        self.assertGreater(min(week), 100.0)                                 # 候補の無い部品の 0 を混ぜない（日の合計の見込みだけ）

    def test_ess_weights_reach_the_target(self):
        rec = [0.5 ** (k / 7) for k in range(60)]
        dist = [abs(math.sin(k)) * 3 for k in range(60)]
        w = route._ess_weights([1.0] * 60, dist, 4)
        self.assertAlmostEqual(sum(w) ** 2 / sum(v * v for v in w), 4, delta=0.05)  # 似ている度合いだけの実質の点数
        self.assertEqual(route._ess_weights(rec[:3], dist[:3], 4), rec[:3])        # 点が足りなければ新しさの重みだけ
        a, b = route._ess_weights(rec, dist, 8), route._ess_weights(rec, [v + 1e-6 for v in dist[:-1]] + [dist[-1]], 8)
        self.assertLess(max(abs(x / sum(a) - y / sum(b)) for x, y in zip(a, b)), 1e-3)   # 距離が少し動けば重みも少しだけ

    def test_knn_counts_a_day_once_and_moves_smoothly(self):
        rng = random.Random(3)
        amount = {}
        t = at('2026-10-07 23:00')

        def f(d):
            a = amount.setdefault(d.date(), rng.choice([0.5, 1.0, 2.0, 4.0]))
            return a if 9 <= d.hour < 22 and rng.random() < 0.6 else 0.0
        cs_ = self._series(t, 60, f)
        knn = route.KNN(7)
        i, xx = cs_.n - 2, 500
        nn, w = knn.neighbours(cs_, i, xx)
        self.assertEqual(len({cs_.cal(s)[2] for s in nn}), len(nn))            # 1 日に 1 つ
        nn2, w2 = knn.neighbours(cs_, i + 1, xx - 1)
        p = {cs_.cal(s)[2]: v / sum(w) for s, v in zip(nn, w)}
        q = {cs_.cal(s)[2]: v / sum(w2) for s, v in zip(nn2, w2)}
        tv = sum(abs(p.get(k, 0) - q.get(k, 0)) for k in set(p) | set(q)) / 2
        self.assertLess(tv, 0.2)                                              # 15 分ずらしても重みは入れ替わらない

    def test_abs_mean(self):
        self.assertAlmostEqual(route._abs_mean([0.0, 1.0], [0.0, 1.0]), 0.5)
        self.assertAlmostEqual(route._abs_mean([3.0], [1.0, 2.0]), 1.5)


class WindowStateTests(TempDirCase):
    def test_probability_from_cost_and_usage(self):
        t = at('2026-10-07 12:00')
        # 毎日 9〜18 時に 1 時間 $10（15 分 $2.5）を 21 日分。7 日枠は 1 時間 1%（上限 $1,000）
        rows = []
        for k in range(21 * 24):
            ts = t - 21 * D + k * H
            d = datetime.datetime.fromtimestamp(ts)
            if 9 <= d.hour < 18:
                for q in range(4):
                    rows.append({'k': f'{k}-{q}', 'ts': ts + q * 900 + 1, 'model': 'claude-opus-5-5', 'in': 0, 'out': 125000,
                                 'cw5m': 0, 'cw1h': 0, 'cr': 0})
        self.write_costs('claude', rows)
        r = t + 2 * D
        live = [{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 45.0, 'r': int(r)},
                {'side': 'claude', 'id': 'weekly_scoped:Fable', 'win': 10080, 'u': 0.0, 'r': int(r), 'scoped': True}]
        states, notes = route.window_states(live, t, log=[], n=200)
        w = states['claude']['claude-weekly_all']
        self.assertEqual(w['limit_src'], 'now')
        self.assertAlmostEqual(w['limit_usd'], 100 * 450 / 45, delta=60)     # 枠の始まり（5 日前）から $450
        self.assertAlmostEqual(w['remain_usd'], w['limit_usd'] * 0.55, delta=1)
        self.assertLess(w['X'], 0.5)                                         # 残り約 $550、2 日で約 $200 しか使わない
        self.assertIsNone(states['claude']['claude-weekly_scoped:Fable']['X'])
        self.assertEqual(notes, [])

    def test_no_probability_without_a_day_of_cost(self):
        t = at('2026-10-07 12:00')
        self.write_costs('claude', [{'k': 'a', 'ts': t - H, 'model': 'claude-opus-5-5', 'out': 1000}])
        states, notes = route.window_states([{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 45.0, 'r': int(t + D)}], t, log=[])
        self.assertIsNone(states['claude']['claude-weekly_all']['X'])
        self.assertIn('1 日分', notes[0])


class RecordTests(TempDirCase):
    def _read_both_counting(self, t):
        calls = []

        def live(t, notes=None):
            calls.append(t)
            return [{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 50.0, 'r': int(t + D)}]
        saved = route.read_claude, route.read_codex
        route.read_claude = live
        route.read_codex = lambda t, notes=None: [{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 30.0, 'r': int(t + 3 * D)}]
        try:
            return route.read_both(t) + (calls,)
        finally:
            route.read_claude, route.read_codex = saved

    def test_record_keeps_ids_and_scoped_flag(self):
        t = at('2026-10-06 10:00')
        route.record([{'ts': t, 'side': 'claude', 'id': 'weekly_scoped:Fable', 'win': 10080, 'u': 3.0, 'r': int(t + D), 'scoped': True},
                      {'ts': t, 'side': 'claude', 'win': 300, 'u': 10.0, 'r': int(t + 2 * H)}])
        sers = route.load_series('claude')
        self.assertEqual(sorted(sers), ['session', 'weekly_scoped:Fable'])
        self.assertTrue(sers['weekly_scoped:Fable'].scoped)
        self.assertFalse(sers['session'].scoped)

    def test_recent_claude_record_skips_the_api(self):
        t = at('2026-10-06 10:00')
        route.record([{'ts': t - 30, 'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 12.0, 'r': int(t + D)}])
        rows, errs, notes, calls = self._read_both_counting(t)
        self.assertEqual(calls, [])
        self.assertEqual([x['u'] for x in rows if x['side'] == 'claude'], [12.0])
        self.assertIn('呼ばない', notes[0])
        route.record(rows)                                   # 借りた行は記録に足さない
        self.assertEqual(sum(1 for x in route.read_log() if x['side'] == 'claude'), 1)

    def test_older_claude_record_calls_the_api(self):
        t = at('2026-10-06 10:00')
        route.record([{'ts': t - route.CLAUDE_MIN_INTERVAL - 1, 'side': 'claude', 'win': 10080, 'u': 12.0, 'r': int(t + D)}])
        rows, errs, notes, calls = self._read_both_counting(t)
        self.assertEqual(len(calls), 1)
        self.assertEqual([x['u'] for x in rows if x['side'] == 'claude'], [50.0])

    def test_429_uses_latest_recent_record(self):
        t = at('2026-10-06 10:00')
        route.record([{'ts': t - 900, 'side': 'claude', 'win': 10080, 'u': 10.0, 'r': int(t + D)},
                      {'ts': t - 300, 'side': 'claude', 'win': 10080, 'u': 12.0, 'r': int(t + D)},
                      {'ts': t - 300, 'side': 'claude', 'win': 300, 'u': 40.0, 'r': int(t + H)}])

        def limited(t, notes=None):
            raise route.RateLimited('429')
        saved = route.read_claude, route.read_codex
        route.read_claude = limited
        route.read_codex = lambda t, notes=None: []
        try:
            rows, errs, notes = route.read_both(t)
        finally:
            route.read_claude, route.read_codex = saved
        self.assertEqual(sorted((x['win'], x['u']) for x in rows), [(300, 40.0), (10080, 12.0)])
        self.assertIn('429', notes[0])

    def test_calibrate_learns_weights_from_cost(self):
        t = time.time()
        rows = []
        for k in range(20 * 24):
            ts = t - 20 * D + k * H
            d = datetime.datetime.fromtimestamp(ts)
            if d.weekday() < 5 and 9 <= d.hour < 18:
                rows.append({'k': str(k), 'ts': ts, 'model': 'claude-opus-5-5', 'out': 250000})
        self.write_costs('claude', rows)
        w = route.calibrate(quiet=True, every=16, step=0.25)
        self.assertTrue(w)
        for v in w.values():
            self.assertAlmostEqual(sum(v), 1.0)
        self.assertEqual(route.load_weights()[1], route.WEIGHTS_FILE)

    def test_calibrate_waits_for_enough_days(self):
        t = time.time()
        self.write_costs('claude', [{'k': 'a', 'ts': t - 3 * D, 'model': 'claude-opus-5-5', 'out': 1000}])
        self.assertFalse(route.maybe_calibrate(t))
        self.assertFalse(os.path.exists(route.WEIGHTS_FILE))


if __name__ == '__main__':
    unittest.main()
