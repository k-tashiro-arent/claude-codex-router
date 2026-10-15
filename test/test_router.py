"""Checks router.py's decisions, records, cost and limits in constructed situations. Times are built in Asia/Tokyo (the cost series is split by local-time days)."""
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
# always point to a "not installed" location, so the config.json of an installed environment is not read (the repository's defaults/ is used)
os.environ['CLAUDE_CODEX_ROUTER_DIR'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_no_install')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin'))
import router as route  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
H, D = 3600, 86400
# prices (LiteLLM format). The tests always use these, so nothing goes over the network
PRICES = {
    'claude-opus-5-5': {'input_cost_per_token': 4e-06, 'output_cost_per_token': 2e-05, 'cache_creation_input_token_cost': 5e-06,
                        'cache_creation_input_token_cost_above_1hr': 8e-06, 'cache_read_input_token_cost': 2e-07},
    'claude-haiku-4-5': {'input_cost_per_token': 1e-06, 'output_cost_per_token': 5e-06},
    'openai/gpt-6.1-sol': {'input_cost_per_token': 2e-06, 'output_cost_per_token': 1e-05, 'cache_read_input_token_cost': 1e-07},
}


def at(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=JST).timestamp()


class TempDirCase(unittest.TestCase):
    """Point the records to an empty temporary directory and use PRICES as the prices."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = {k: getattr(route, k) for k in ('DATA', 'USAGE_LOG', 'COST_LOG', 'COST_STATE', 'PRICE_FILE',
                                                     'CLAUDE_HOME', 'CODEX_HOME', 'TP', '_prices', 'CONFIG', 'SAMPLE_STAMP', 'VERSION_FILE',
                                                     'FORECAST_FILE')}
        route.DATA = os.path.join(self.d, 'data')
        route.USAGE_LOG = os.path.join(route.DATA, 'usage.jsonl')
        route.COST_LOG = {s: os.path.join(route.DATA, f'cost-{s}.jsonl') for s in ('claude', 'codex')}
        route.COST_STATE = os.path.join(route.DATA, 'cost-state.json')
        route.PRICE_FILE = os.path.join(route.DATA, 'litellm-prices.json')
        route.SAMPLE_STAMP = os.path.join(route.DATA, 'last-sample')
        route.FORECAST_FILE = os.path.join(route.DATA, 'forecast.json')
        route.VERSION_FILE = os.path.join(self.d, '.version')
        route.CLAUDE_HOME, route.CODEX_HOME = os.path.join(self.d, 'claude'), os.path.join(self.d, 'codex')
        route.TP = os.path.join(self.d, 'token-pace') + '/'
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


class PriceTests(TempDirCase):
    def test_lookup_bare_prefixed_and_dated_names(self):
        p = route.price('claude-opus-5-5')
        for k, v in zip(('in', 'out', 'cw5m', 'cw1h', 'cr'), (4.0, 20.0, 5.0, 8.0, 0.2)):
            self.assertAlmostEqual(p[k], v)
        self.assertAlmostEqual(route.price('gpt-6.1-sol')['out'], 10.0)            # name with openai/ prefixed
        self.assertAlmostEqual(route.price('claude-haiku-4-5-20251001')['in'], 1.0)  # a dated name is looked up without the date
        self.assertIsNone(route.price('codex-auto-review'))
        q = route.price('claude-haiku-4-5')
        self.assertEqual((q['cw5m'], q['cr']), (1.0, 1.0))                           # a missing field falls back to the input price

    def test_config_overrides_and_adds_models(self):
        route.CONFIG = dict(route.CONFIG, prices={'claude-opus-5-5': {'cr': 0.5}, 'my-model': {'in': 1, 'out': 2, 'cw5m': 1, 'cw1h': 1, 'cr': 0.1}})
        self.assertEqual(route.price('claude-opus-5-5')['cr'], 0.5)
        self.assertEqual(route.price('claude-opus-5-5')['out'], 20.0)
        self.assertEqual(route.price('my-model')['out'], 2.0)

    def test_partial_override_for_an_unlisted_model(self):
        route.CONFIG = dict(route.CONFIG, prices={'my-model': {'in': 3}})
        self.assertEqual(route.price('my-model'), {'in': 3.0, 'out': 3.0, 'cw5m': 3.0, 'cw1h': 3.0, 'cr': 3.0})   # keys not given are in
        self.assertAlmostEqual(route.cost_of('claude', {'model': 'my-model', 'in': 1e6, 'out': 1e6}), 6.0)

    def test_cost_formulas(self):
        c = route.cost_of('claude', {'model': 'claude-opus-5-5', 'in': 1e6, 'out': 1e6, 'cw5m': 1e6, 'cw1h': 1e6, 'cr': 1e6})
        self.assertAlmostEqual(c, 4 + 20 + 5 + 8 + 0.2)
        x = route.cost_of('codex', {'model': 'gpt-6.1-sol', 'in': 3e6, 'cached': 2e6, 'out': 1e6})   # input includes cached
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
        self.assertTrue(route._price_src.startswith('fetch failed'))

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
            f.write(self._claude_line('m1', 'r1', '2026-10-06T01:00:01Z', 50))   # continuation of the same response
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
        self.assertEqual(route.sync_costs()['claude'], 0)             # not read if unchanged
        with open(p, 'a') as f:
            f.write(self._claude_line('m3', 'r3', '2026-10-06T02:00:00Z', 9, model='unknown-model'))
            f.write('{"partial": ')                                    # a partially written line is left for next time
        self.assertEqual(route.sync_costs()['claude'], 1)
        ev, miss = route.cost_events('claude')
        self.assertEqual(len(ev), 2)
        self.assertEqual(miss, ['unknown-model'])

    def test_sync_without_state_waits_for_full(self):
        self.assertEqual(route.sync_costs(), {})                      # if there are no cost records yet, the first now or sample reads everything

    def test_codex_counts_increments_of_the_running_total(self):
        p = os.path.join(route.CODEX_HOME, 'sessions', '2026', '10', '06', 's.jsonl')
        os.makedirs(os.path.dirname(p))

        def tc(ts, i, c, o):
            return json.dumps({'timestamp': ts, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o}}}}) + '\n'
        with open(p, 'w') as f:
            f.write(json.dumps({'timestamp': '2026-10-06T00:00:00Z', 'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}}) + '\n')
            f.write(tc('2026-10-06T00:01:00Z', 1000, 500, 10))
            f.write(tc('2026-10-06T00:01:01Z', 1000, 500, 10))   # a repeated identical running total is not counted
            f.write(tc('2026-10-06T00:02:00Z', 3000, 1500, 30))
        route.sync_costs(full=True)
        with open(p, 'a') as f:
            f.write(tc('2026-10-06T00:03:00Z', 4000, 2000, 40))
        route.sync_costs()
        rows = route.read_jsonl(route.COST_LOG['codex'])
        self.assertEqual([(x['in'], x['cached'], x['out']) for x in rows], [(1000, 500, 10), (2000, 1000, 20), (1000, 500, 10)])
        self.assertTrue(all(x['model'] == 'gpt-6.1-sol' for x in rows))

    def test_usage_in_codex_logs_and_token_pace_is_added_once(self):
        p = os.path.join(route.CODEX_HOME, 'sessions', '2026', '10', '06', 's.jsonl')
        os.makedirs(os.path.dirname(p))
        r = 1791900000

        def tc(ts, u):
            return json.dumps({'timestamp': ts, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None, 'rate_limits': {
                'primary': {'used_percent': u, 'window_minutes': 10080, 'resets_at': r}}}}) + '\n'
        with open(p, 'w') as f:
            f.write(tc('2026-10-06T00:01:00Z', 10.0))
        os.makedirs(route.TP)
        with open(route.TP + 'pace.jsonl', 'w') as f:
            f.write(json.dumps({'ts': 1791000000, 'h5': 12, 'h5r': 1791010000, 'd7': 30, 'd7r': r}) + '\n')
        self.assertEqual(route.sync_costs(full=True)['usage'], 3)    # 1 Codex row, 2 claude-token-pace rows
        with open(p, 'a') as f:
            f.write(tc('2026-10-06T00:02:00Z', 11.0))
        self.assertEqual(route.sync_costs()['usage'], 1)              # only the new part
        os.remove(route.COST_STATE)
        self.assertEqual(route.sync_costs(full=True)['usage'], 0)    # even when everything is read again, overlapping rows are not added
        rows = route.read_log()
        self.assertEqual(sorted((x['side'], route.row_id(x), x.get('win'), x['u']) for x in rows),
                         [('claude', 'legacy:d7', None, 30.0), ('claude', 'legacy:h5', None, 12.0), ('codex', '7d', 10080, 10.0), ('codex', '7d', 10080, 11.0)])
        # the imported Claude rows carry the file's field names only; load_series joins them to the API's kinds by the reset times


class LengthTests(unittest.TestCase):
    def test_length_from_the_reads(self):
        # periods of 5 h starting at first use; reads every 5 min. Each start is bracketed by two reads; the middle of the brackets is about 300 min
        H5 = 300 * 60
        reads = [(0, None, 0), (300, None, 0), (540, 300 + H5, 3), (840, 300 + H5, 5),    # started at 300 (between the reads at 300 and 540)
                 (40000, None, 0), (40300, 40240 + H5, 2)]                                 # started at 40240
        e = route.estimate_length(reads)
        self.assertAlmostEqual(e['len'], 300.0, delta=5)
        self.assertTrue(e['known'])
        self.assertLessEqual(e['lo'], 300.0)
        self.assertGreaterEqual(e['hi'], 300.0)
        self.assertFalse(e['contiguous'])                    # the second period started long after the first one's reset

    def test_only_a_lower_bound_without_a_bracketed_start(self):
        e = route.estimate_length([(1000, 1000 + 3 * 86400, 10)])               # one read, 3 days before the reset (the period may be longer)
        self.assertEqual((e['len'], e['known'], e['hi']), (3 * 1440, False, math.inf))
        self.assertIsNone(route.estimate_length([]))

    def test_short_window_read_every_5_minutes_is_known(self):
        # a 30-minute window, periods one after another, read every 5 minutes: known, and contiguous
        reads = [(k * 300, (k * 300 // 1800 + 1) * 1800, 1.0) for k in range(1, 60)]
        e = route.estimate_length(reads)
        self.assertTrue(e['known'])
        self.assertAlmostEqual(e['len'], 30.0, delta=5)
        self.assertTrue(e['contiguous'])
        sers = route.load_series('codex', [{'ts': ts, 'side': 'codex', 'win': 30, 'u': 1.0, 'r': r} for ts, r, _ in reads])
        self.assertEqual(len(sers['30m'].groups), len({r for _, r, _ in reads}))       # each 30-minute period apart (no fixed span of time merges them)

    def test_length_follows_a_change(self):
        # 5 periods of 300 min, then 3 of 240 min, each start bracketed: the length follows to 240 (the median of the latest 5)
        reads, t = [], 0
        for length in [300] * 5 + [240] * 3:
            reads += [(t + 1000, None, 0), (t + 1060, t + 1060 + length * 60 - 30, 4.0)]
            t += length * 60 + 5000
        e = route.estimate_length(reads)
        self.assertAlmostEqual(e['len'], 240.0, delta=2)
        self.assertTrue(e['known'])

    def test_periods_with_no_use_are_not_evidence(self):
        # claude-token-pace recorded a placeholder reset (the next 10 minutes) while nothing was used: such periods do not make the length short
        reads, t = [], 0
        for _ in range(3):
            reads += [(t + 1000, None, 0), (t + 1060, t + 1060 + 300 * 60 - 30, 4.0)]
            t += 300 * 60 + 5000
        for k in range(6):
            reads += [(t + k * 600 + 100, t + (k + 1) * 600, 0.0), (t + k * 600 + 400, t + (k + 1) * 600, 0.0)]
        e = route.estimate_length(reads)
        self.assertAlmostEqual(e['len'], 300.0, delta=2)

    def test_claude_rows_get_the_estimated_length(self):
        t = 10 * 86400.0
        r_prev = t - 4 * 86400
        log = [{'ts': int(r_prev - 300), 'side': 'claude', 'id': 'weekly_all', 'u': 60.0, 'r': int(r_prev)},
               {'ts': int(r_prev + 60), 'side': 'claude', 'id': 'weekly_all', 'u': 1.0, 'r': int(r_prev + 7 * 86400)}]
        ser = route.load_series('claude', log)['weekly_all']
        self.assertAlmostEqual(ser.win_min, 7 * 1440, delta=5)              # the start is between the two reads (5 minutes before, 1 minute after)
        self.assertTrue(ser.known)

    def test_length_from_the_name_when_the_reads_agree_or_give_none(self):
        t = 10 * 86400.0
        one = [{'ts': int(t - 3600), 'side': 'claude', 'id': 'weekly_all', 'nwin': 10080, 'u': 10.0, 'r': int(t + 3 * 86400)}]   # a lower bound of 3 days 1 hour
        ser = route.load_series('claude', one)['weekly_all']
        self.assertEqual((ser.win_min, ser.known, ser.lo, ser.hi, ser.len_note), (10080, True, 10080, 10080, None))
        r_prev = t - 4 * 86400
        log = [{'ts': int(r_prev - 300), 'side': 'claude', 'id': 'weekly_all', 'nwin': 10080, 'u': 60.0, 'r': int(r_prev)},
               {'ts': int(r_prev + 60), 'side': 'claude', 'id': 'weekly_all', 'nwin': 10080, 'u': 1.0, 'r': int(r_prev + 7 * 86400)}]
        self.assertEqual(route.load_series('claude', log)['weekly_all'].win_min, 10080)     # within the bracket of the reads

    def test_reads_win_over_the_name_when_they_disagree(self):
        # the name still says five_hour, but the periods are 4 hours (bracketed by reads 5 minutes apart): the reads are used, and a note says so
        H4 = 240 * 60
        log, t = [], 0
        for _ in range(3):
            log += [{'ts': t + 1000, 'side': 'claude', 'id': 'session', 'nwin': 300, 'u': 0.0, 'r': None},
                    {'ts': t + 1300, 'side': 'claude', 'id': 'session', 'nwin': 300, 'u': 4.0, 'r': t + 1150 + H4}]
            t += H4 + 5000
        ser = route.load_series('claude', log)['session']
        self.assertAlmostEqual(ser.win_min, 240, delta=3)
        self.assertIn('the name states 5h', ser.len_note)
        self.assertIn('the reads are used', ser.len_note)
        states, notes, _ = route.window_states([{'side': 'claude', 'id': 'session', 'nwin': 300, 'u': 4.0, 'r': t + 600}], t, log=log)
        self.assertIn(ser.len_note, notes)
        # a lower bound above the stated length: the reads too
        one = [{'ts': 0, 'side': 'claude', 'id': 'session', 'nwin': 300, 'u': 10.0, 'r': 6 * 3600}]
        ser = route.load_series('claude', one)['session']
        self.assertEqual((ser.win_min, ser.known), (360, False))
        self.assertIn('at least 360 min', ser.len_note)

    def test_limit_not_from_the_current_window_while_the_start_is_unknown(self):
        t = at('2026-10-07 12:00')
        cs_ = route.CostSeries([(t - 3600, 50.0)], t)
        self.assertEqual(route.limit_usd(None, cs_, 50.0, t, t + 3600, 120, known=False), (None, None))
        self.assertEqual(route.limit_usd(None, cs_, 50.0, t, t + 3600, 120, known=True), (100.0, 'now'))


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
        # every kind is taken, unknown ones too; no length (the limits array gives none, and there is no top-level object with the same reset)
        self.assertEqual([(x['id'], x['u'], x['scoped']) for x in rows],
                         [('session', 32.0, False), ('weekly_all', 18.0, False), ('weekly_scoped:Fable', 0.0, True), ('monthly_x', 5.0, False)])
        self.assertTrue(all('win' not in x and 'nwin' not in x for x in rows))
        self.assertEqual(notes, [])

    def test_claude_old_shape(self):
        d = {'five_hour': {'utilization': 12.0, 'resets_at': '2026-10-07T19:59:59+00:00'},
             'seven_day': {'utilization': 40.0, 'resets_at': '2026-10-14T05:59:59+00:00'}}
        self.assertEqual([(x['id'], x.get('win'), x['nwin']) for x in route.parse_claude(d)],
                         [('five_hour', None, 300), ('seven_day', None, 10080)])   # the kind is the key, the length the one the key states

    def test_claude_length_from_the_name_with_the_same_reset(self):
        d = {'five_hour': {'utilization': 46.0, 'resets_at': '2026-10-15T03:00:00.274360+00:00'},
             'seven_day': {'utilization': 19.0, 'resets_at': '2026-10-21T06:00:00.274379+00:00'},
             'seven_day_opus': None, 'tangelo': {'utilization': 3.0, 'resets_at': '2026-11-01T00:00:00+00:00'},
             'limits': [{'kind': 'session', 'percent': 46, 'resets_at': '2026-10-15T03:00:00.274360+00:00', 'scope': None},
                        {'kind': 'weekly_all', 'percent': 19, 'resets_at': '2026-10-21T06:00:00.274379+00:00', 'scope': None},
                        {'kind': 'weekly_scoped', 'percent': 0, 'resets_at': '2026-10-21T06:00:00+00:00',
                         'scope': {'model': {'display_name': 'Fable'}}},
                        {'kind': 'other', 'percent': 3, 'resets_at': '2026-11-01T00:00:00+00:00', 'scope': None}]}
        self.assertEqual([(x['id'], x.get('nwin')) for x in route.parse_claude(d)],
                         [('session', 300), ('weekly_all', 10080), ('weekly_scoped:Fable', None),   # the scoped one resets 0.27 s apart: no match
                          ('other', None)])                                                      # tangelo states no length
        d['five_hour_x'] = {'utilization': 46.0, 'resets_at': '2026-10-15T03:00:00.274360+00:00'}
        d['seven_hour'] = {'utilization': 46.0, 'resets_at': '2026-10-15T03:00:00.274360+00:00'}
        self.assertNotIn('nwin', route.parse_claude(d)[0])                # two lengths with the same reset: none is taken

    def test_name_length(self):
        self.assertEqual([route.name_length(n) for n in ('five_hour', 'seven_day', 'seven_day_sonnet', '30_minutes', 'two_weeks', 'one_month',
                                                          'tangelo', 'extra_usage', 'session')],
                         [300, 10080, 10080, 30, 20160, None, None, None, None])

    def test_codex_windows_by_length(self):
        d = {'rate_limit': {'allowed': True, 'primary_window': {'used_percent': 35, 'limit_window_seconds': 604800, 'reset_after_seconds': 9,
                                                                'reset_at': 1791948629},
                            'secondary_window': {'used_percent': 5, 'limit_window_seconds': 18000, 'reset_after_seconds': 60}},
             'credits': {'has_credits': True}}
        rows = route.parse_codex(d)
        self.assertEqual(rows[0], {'side': 'codex', 'id': '7d', 'win': 10080, 'u': 35.0, 'r': 1791948629})
        self.assertEqual((rows[1]['id'], rows[1]['win'], rows[1]['u']), ('5h', 300, 5.0))   # without reset_at, now + reset_after_seconds
        self.assertAlmostEqual(rows[1]['r'], time.time() + 60, delta=5)
        self.assertEqual(route.parse_codex({'rate_limit': {'primary_window': None, 'secondary_window': None}}), [])

    def test_codex_any_number_of_windows(self):
        # every window is used, under any key (what a window applies to is not given); two as long are told apart by their place
        d = {'rate_limit': {'primary_window': {'used_percent': 1, 'limit_window_seconds': 604800, 'reset_at': 2000000000},
                            'tertiary_window': {'used_percent': 2, 'limit_window_seconds': 2592000, 'reset_at': 2000000000}},
             'additional_rate_limits': [{'limit_name': 'x', 'rate_limit': {'primary_window': {'used_percent': 3, 'limit_window_seconds': 18000,
                                                                                                'reset_at': 2000000000}}}],
             'code_review_rate_limit': None}
        d['code_review_rate_limit'] = {'primary_window': {'used_percent': 4, 'limit_window_seconds': 604800, 'reset_at': 2000000000}}
        rows = route.parse_codex(d)
        self.assertEqual([(x['id'], x['u'], bool(x.get('scoped'))) for x in rows],
                         [('7d@rate_limit.primary_window', 1.0, False), ('30d', 2.0, False), ('5h', 3.0, False),
                          ('7d@code_review_rate_limit.primary_window', 4.0, False)])

    def test_length_labels(self):
        self.assertEqual([route.win_label(w) for w in (300, 299.78, 10080, 10079.93, 90, 43200, None)], ['5h', '5h', '7d', '7d', '1.5h', '30d', '?'])

    def test_rows_without_id(self):
        # Claude: a provisional kind (joined by reset times in load_series), not a kind decoded from a length; Codex: by the length the API gave
        self.assertEqual(route.row_id({'side': 'claude', 'win': 300}), 'legacy:300')
        self.assertEqual(route.row_id({'side': 'claude', 'tp': 'x'}), 'legacy:x')
        self.assertEqual(route.row_id({'side': 'codex', 'win': 300}), '5h')
        self.assertEqual(route.row_id({'side': 'codex', 'win': 10080, 'id': 'x'}), '7d')             # by the length, whatever the id
        self.assertEqual([route.exact_label(w) for w in (10080, 300, 90, 43200, 1500)], ['7d', '5h', '90m', '30d', '25h'])
        self.assertEqual(route.row_id({'side': 'claude', 'win': 10080, 'id': 'weekly_scoped:Fable'}), 'weekly_scoped:Fable')

    def test_rows_without_id_join_the_kind_with_the_same_resets(self):
        t = 1_000_000.0
        log = [{'ts': t, 'side': 'claude', 'id': 'session', 'u': 10.0, 'r': t + 3600},
               {'ts': t - 600, 'side': 'claude', 'win': 300, 'u': 8.0, 'r': t + 3601},            # an earlier version's row, the same period
               {'ts': t - 900, 'side': 'claude', 'tp': 'k', 'u': 7.0, 'r': t + 3599},             # an imported row, the same period
               {'ts': t, 'side': 'claude', 'tp': 'other', 'u': 1.0, 'r': t + 9 * 86400}]          # matches no kind: stays apart
        sers = route.load_series('claude', log)
        self.assertEqual(sorted(sers), ['legacy:other', 'session'])
        self.assertEqual([u for _, u in sers['session'].groups[0]['pts']], [7.0, 8.0, 10.0])

    def test_rows_without_id_do_not_join_a_scoped_kind(self):
        # weekly_all and a model-scoped weekly window reset 1 s apart: the imported row joins weekly_all (earlier versions recorded no scoped windows)
        t = 1_000_000.0
        log = [{'ts': t, 'side': 'claude', 'id': 'weekly_scoped:Fable', 'u': 0.0, 'r': t + 86400, 'scoped': True},
               {'ts': t, 'side': 'claude', 'id': 'weekly_all', 'u': 40.0, 'r': t + 86399},
               {'ts': t - 600, 'side': 'claude', 'tp': 'k', 'u': 39.0, 'r': t + 86399}]
        sers = route.load_series('claude', log)
        self.assertEqual(sorted(sers), ['weekly_all', 'weekly_scoped:Fable'])
        self.assertEqual([u for _, u in sers['weekly_all'].groups[0]['pts']], [39.0, 40.0])


class CleanPointsTests(unittest.TestCase):
    """Stale values mixed into records written by several sessions, and resets in the middle of a period."""

    def test_stale_lower_value_is_dropped(self):
        pts = [(0, 40.0), (60, 41.0), (90, 38.0), (120, 42.0), (180, 43.0)]
        self.assertEqual(route.clean_points(pts), [[(0, 40.0), (60, 41.0), (120, 42.0), (180, 43.0)]])

    def test_drop_to_near_zero_confirmed_is_a_reset(self):
        pts = [(0, 27.0), (60, 28.0), (120, 0.0), (150, 28.0), (180, 0.0), (240, 1.0), (300, 2.0)]
        segs = route.clean_points(pts)
        self.assertEqual(segs, [[(0, 27.0), (60, 28.0)], [(120, 0.0), (180, 0.0), (240, 1.0), (300, 2.0)]])   # the stale 28 right after it is dropped too

    def test_drop_not_near_zero_never_splits(self):
        pts = [(0, 21.0), (1200, 16.0), (2200, 17.0), (9000, 22.0)]   # sparse observations, with stale values arriving in a row
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
    """Limit amount: from the current window when usage is large, from history when it is small."""

    def _flat(self, t_end, days, per_hour):
        """Cost records of per_hour dollars used every hour for days days up to t_end."""
        ev = [(t_end - days * D + h * H + 60, per_hour) for h in range(days * 24)]
        return route.CostSeries(ev, t_end)

    def test_now_estimate_when_usage_is_large(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 10, 5.0)          # $5 per hour
        r = t + 2 * D                          # 7-day window start = t − 5 days → $600 in the current window
        L, src = route.limit_usd(None, cs_, 50.0, t, r, 10080)
        self.assertEqual(src, 'now')
        self.assertAlmostEqual(L, 1200.0, delta=15)

    def test_hist_estimate_when_usage_is_small(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 20, 5.0)
        # history: usage grows 0.5% per hour (= limit $1,000), reset 10 hours ago (u is still 5%)
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

    def test_cost_record_shorter_than_the_window(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 3, 5.0)            # the cost records cover only 3 days (the 7-day window started 5 days ago)
        r = t + 2 * D
        # the usage records cover the same 3 days: 0.5% per hour (= limit $1,000)
        ser = route.Series('claude', 'weekly_all', 10080, [(t - 3 * D + k * H, 0.5 * k, r) for k in range(0, 73)])
        L, src = route.limit_usd(ser, cs_, 36.0, t, r, 10080)
        self.assertEqual(src, 'hist')                                   # history is used first
        self.assertAlmostEqual(L, 1000.0, delta=60)
        L, src = route.limit_usd(None, cs_, 36.0, t, r, 10080)
        self.assertEqual(src, 'now')                                    # without it, the cost before the records is taken as 0
        self.assertAlmostEqual(L, 100 * 360 / 36, delta=15)

    def test_no_cost_in_the_window_uses_history(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 20, 5.0)
        cs_.x[-96 * 6:] = [0.0] * (96 * 6)                         # the cost in the current 7-day window (from 5 days ago) is 0 (only use elsewhere)
        cs_.cs = [0.0]
        for v in cs_.x:
            cs_.cs.append(cs_.cs[-1] + v)
        r = t + 2 * D
        ser = route.Series('claude', 'weekly_all', 10080, [(t - 15 * D + k * H, 0.5 * k, t - 8 * D) for k in range(0, 7 * 24)])
        L, src = route.limit_usd(ser, cs_, 40.0, t, r, 10080)
        self.assertNotEqual(src, 'now')                                 # a cost of 0 does not give a limit of 0

    def test_no_estimate_without_cost_or_usage(self):
        t = at('2026-10-08 12:00')
        cs_ = self._flat(t, 2, 0.0)
        self.assertEqual(route.limit_usd(None, cs_, 0.0, t, t + D, 10080), (None, None))


class DecideTests(unittest.TestCase):
    def st(self, u, X, win=10080, scoped=False):
        return {'u': u, 'X': X, 'win': win, 'scoped': scoped}

    def test_compare_every_window(self):
        s = {'claude': {'claude-session': self.st(10, 0.1, 300), 'claude-weekly_all': self.st(40, 0.3)},
             'codex': {'codex-7d': self.st(20, 0.6)}}
        pick, why = route.decide_states(s, 'claude', guard=0.9)
        self.assertEqual(pick, 'claude')
        self.assertAlmostEqual(why['claude_L'], 0.7)
        self.assertAlmostEqual(why['codex_L'], 0.4)

    def test_guard_on_main_goes_to_sub(self):
        s = {'claude': {'claude-session': self.st(80, 0.7, 300), 'claude-weekly_all': self.st(10, 0.0)},
             'codex': {'codex-7d': self.st(50, 0.3)}}
        self.assertEqual(route.decide_states(s, 'claude')[0], 'codex')
        s = {'claude': {'claude-weekly_all': self.st(10, 0.0)}, 'codex': {'codex-7d': self.st(90, 0.95)}}
        self.assertEqual(route.decide_states(s, 'codex')[0], 'claude')      # if the main side is Codex, Codex's guard sends it to Claude

    def test_guard_on_main_goes_to_sub_whatever_the_sub_side(self):
        # the sub side has no guard: if it reaches its limit, work returns to the main side, so only the main side's X counts
        s = {'claude': {'claude-weekly_all': self.st(61, 0.595)}, 'codex': {'codex-7d': self.st(83, 0.618)}}
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 100.0, 'codex': 5.0})
        self.assertEqual((pick, why['stage'], why['guard']), ('codex', 'guard', ['claude-weekly_all']))
        s = {'claude': {'claude-session': self.st(80, 0.7, 300), 'claude-weekly_all': self.st(10, 0.0)},
             'codex': {'codex-7d': self.st(90, 0.95)}}
        self.assertEqual(route.decide_states(s, 'claude')[0], 'codex')

    def test_sub_side_over_guard_does_not_keep_main(self):
        # Codex (sub) is likely to reach its limit, Claude (main) is not: decided by the comparison, not kept on main
        s = {'claude': {'claude-weekly_all': self.st(10, 0.2)}, 'codex': {'codex-5h': self.st(70, 0.8, 300), 'codex-7d': self.st(80, 0.7)}}
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 20.0, 'codex': 60.0})
        self.assertEqual((pick, why['stage'], why['guard']), ('codex', 'leftover', []))
        self.assertNotIn('sub_tight', why)

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
        self.assertIsNone(why['codex_L'])                                   # no window with X: nothing to compare
        self.assertEqual((pick, why['stage']), ('codex', 'tie'))             # tie_default

    def test_lists_name_only_the_stage_that_decided(self):
        both_full = {'claude': {'claude-session': self.st(99, None, 300), 'claude-weekly_all': self.st(40, 0.7)},
                     'codex': {'codex-7d': self.st(99, None)}}
        pick, why = route.decide_states(both_full, 'claude')
        self.assertEqual((pick, why['stage'], why['guard']), ('codex', 'guard', ['claude-weekly_all']))
        self.assertEqual(why['full'], ['claude-session', 'codex-7d'])           # every full window, regardless of the stage that decided
        sub_full = {'claude': {'claude-weekly_all': self.st(40, 0.7)}, 'codex': {'codex-7d': self.st(99, None)}}
        pick, why = route.decide_states(sub_full, 'claude')
        self.assertEqual((pick, why['stage'], why['guard']), ('claude', 'full', []))
        no_guard = {'claude': {'claude-weekly_all': self.st(40, 0.3)},
                    'codex': {'codex-5h': self.st(70, 0.8, 300), 'codex-7d': self.st(5, 0.0)}}
        pick, why = route.decide_states(no_guard, 'claude')
        self.assertEqual((pick, why['stage'], why['guard']), ('codex', 'leftover_prob', []))   # the longest windows are compared: Codex 1.0 > Claude 0.7

    def test_tie_follows_default(self):
        s = {'claude': {'claude-weekly_all': self.st(10, 0.2)}, 'codex': {'codex-7d': self.st(10, 0.2)}}
        self.assertEqual(route.decide_states(s, 'claude', default='codex')[0], 'codex')
        self.assertTrue(route.decide_states(s, 'claude')[1]['tie'])

    def test_compare_by_expected_leftover_cost(self):
        # the comparison is by the expected leftover $, not by the probability: Claude is likelier to leave something, but Codex leaves more
        s = {'claude': {'claude-weekly_all': self.st(40, 0.1)}, 'codex': {'codex-7d': self.st(20, 0.4)}}
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 30.0, 'codex': 120.0})
        self.assertEqual((pick, why['stage'], why['claude_left_usd'], why['codex_left_usd']), ('codex', 'leftover', 30.0, 120.0))
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 50.0, 'codex': 50.0}, default='claude')
        self.assertEqual((pick, why['stage'], why['tie']), ('claude', 'tie', True))

    def test_gates_come_before_the_cost_comparison(self):
        s = {'claude': {'claude-weekly_all': self.st(80, 0.7)}, 'codex': {'codex-7d': self.st(20, 0.1)}}
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 500.0, 'codex': 10.0})
        self.assertEqual((pick, why['stage']), ('codex', 'guard'))
        s = {'claude': {'claude-weekly_all': self.st(99, 0.99)}, 'codex': {'codex-7d': self.st(20, 0.1)}}
        self.assertEqual(route.decide_states(s, 'codex', left_usd={'claude': 500.0, 'codex': 10.0})[1]['stage'], 'full')

    def test_probability_comparison_uses_the_longest_window(self):
        # without cost, the leftover probability of the longest window: Claude's 5-hour window (likely to be used up) does not count
        s = {'claude': {'claude-session': self.st(80, 0.45, 300), 'claude-weekly_all': self.st(20, 0.05)}, 'codex': {'codex-7d': self.st(20, 0.2)}}
        pick, why = route.decide_states(s, 'claude')
        self.assertEqual((pick, why['stage']), ('claude', 'leftover_prob'))
        self.assertAlmostEqual(why['claude_L'], 0.95)

    def test_side_without_cost_falls_back_to_probability(self):
        s = {'claude': {'claude-weekly_all': self.st(40, 0.1)}, 'codex': {'codex-7d': self.st(20, 0.4)}}
        pick, why = route.decide_states(s, 'claude', left_usd={'claude': 30.0, 'codex': None})
        self.assertEqual((pick, why['stage']), ('claude', 'leftover_prob'))
        self.assertEqual(route.decide_states(s, 'claude')[1]['stage'], 'leftover_prob')


class OneSideTests(unittest.TestCase):
    def st(self, u, X, win=10080):
        return {'u': u, 'X': X, 'win': win, 'scoped': False}

    def test_readable_side_is_chosen(self):
        self.assertEqual(route.decide_one_side({'claude': {'claude-weekly_all': self.st(20, 0.1)}, 'codex': {}}, 'claude'),
                         ('claude', 'codex_unreadable', []))
        self.assertEqual(route.decide_one_side({'claude': {}, 'codex': {'codex-7d': self.st(90, 0.9)}}, 'claude'),
                         ('codex', 'claude_unreadable', []))          # the sub side has no guard

    def test_main_over_guard_goes_to_the_unread_sub_side(self):
        s = {'claude': {'claude-session': self.st(80, 0.7, 300), 'claude-weekly_all': self.st(20, 0.1)}, 'codex': {}}
        self.assertEqual(route.decide_one_side(s, 'claude'), ('codex', 'guard', ['claude-session']))

    def test_a_side_not_logged_in_is_never_chosen(self):
        # only Claude is used (Codex not logged in): over guard it still stays on Claude, and the reason says Codex is not logged in
        s = {'claude': {'claude-session': self.st(80, 0.7, 300)}, 'codex': {}}
        self.assertEqual(route.decide_one_side(s, 'claude', other_logged_in=False), ('claude', 'codex_not_logged_in', []))
        s = {'claude': {}, 'codex': {'codex-7d': self.st(90, 0.9)}}
        self.assertEqual(route.decide_one_side(s, 'codex', other_logged_in=False), ('codex', 'claude_not_logged_in', []))


def W(m, L, r, length, lo=None, hi=None):
    return {'m': m, 'L': L, 'r': r, 'len': length, 'lo': length if lo is None else lo, 'hi': length if hi is None else hi}


class ContractLeftoverTests(unittest.TestCase):
    def test_single_window(self):
        # remaining $100; use until the reset 30 or 150 → leftover 70 or 0
        self.assertAlmostEqual(route.contract_leftover([W(100.0, 200.0, 1000.0, 10080)], [[30.0], [150.0]], 0.0), 35.0)

    def test_short_window_does_not_bind_when_its_periods_allow_more(self):
        # 5-hour window: $150 left, resets in 2 h, limit $170; weekly: $260 left, resets in 24 h; $100 used evenly. The 5-hour periods allow more than $260
        t = 0.0
        wins = [W(150.0, 170.0, t + 2 * H, 300), W(260.0, 1100.0, t + 24 * H, 10080)]
        pts = route.leftover_points(wins, t)
        self.assertEqual(pts, [2 * H, 7 * H, 12 * H, 17 * H, 22 * H, 24 * H])
        self.assertAlmostEqual(route.contract_leftover(wins, [[100.0 * p / (24 * H) for p in pts]], t), 160.0)

    def test_short_window_binds_when_it_allows_less(self):
        # the 5-hour window allows 10 now and 20 in its next period (which starts before the longer window's reset in 6 h)
        t = 0.0
        wins = [W(10.0, 20.0, t + 2 * H, 300), W(500.0, 1100.0, t + 6 * H, 10080)]
        self.assertAlmostEqual(route.contract_leftover(wins, [[0.0, 0.0], [3.0, 6.0]], t), (30.0 + 24.0) / 2)

    def test_use_beyond_a_period_limit_does_not_move_to_another_period(self):
        # $25 wanted in the second 5-hour period, which allows $20: $20 is used there, and only the $10 of the first period could still be used
        t = 0.0
        wins = [W(10.0, 20.0, t + 2 * H, 300), W(500.0, 1100.0, t + 6 * H, 10080)]
        self.assertAlmostEqual(route.contract_leftover(wins, [[0.0, 25.0]], t), 10.0)

    def test_a_started_period_counts_whole(self):
        # the 5-hour window resets in 1 minute, 2 minutes before the longer window's reset: the new period's whole $170 can be used
        t = 0.0
        wins = [W(10.0, 170.0, t + 60, 300), W(500.0, 1100.0, t + 180, 10080)]
        self.assertAlmostEqual(route.contract_leftover(wins, [[0.0, 0.0]], t), 180.0)

    def test_longest_window_resetting_first(self):
        # weekly: $200 left, resets in 1 h; 5-hour window: $100 left, resets in 5 h. The weekly amount is lost in 1 h; until then only $100 can be used
        # (the 5-hour window), so with $8 used, $92 could have been used but is not. The use after the weekly reset (new weekly amount) does not count
        t = 0.0
        wins = [W(200.0, 1090.0, t + H, 10080), W(100.0, 170.0, t + 5 * H, 300)]
        self.assertEqual(route.leftover_horizon(wins), t + H)
        self.assertEqual(route.leftover_points(wins, t), [H])
        self.assertAlmostEqual(route.contract_leftover(wins, [[4.0], [8.0], [12.0]], t), 92.0)

    def test_horizon_is_the_longest_window_not_the_latest_reset(self):
        self.assertEqual(route.leftover_horizon([W(1, 1, 50.0, 300), W(1, 1, 10.0, 10080), W(1, 1, 30.0, 10080)]), 10.0)

    def test_horizon_with_estimated_lengths(self):
        # a window whose length is only a lower bound (120 min or more) may be the longest: its reset counts as well
        wins = [W(1, 1, 50.0, 240), W(1, 1, 20.0, 120, lo=120, hi=math.inf)]
        self.assertEqual(route.leftover_horizon(wins), 20.0)
        # lengths equal within their estimates (10079.9 known within [10079, 10085], and 10080): either may be the longest, the earliest reset is taken
        wins = [W(1, 1, 90.0, 10079.9, lo=10079, hi=10085), W(1, 1, 70.0, 10080)]
        self.assertEqual(route.leftover_horizon(wins), 70.0)

    def test_no_windows_or_samples(self):
        self.assertIsNone(route.contract_leftover([], [[1.0]], 0.0))
        self.assertIsNone(route.contract_leftover([W(1.0, 2.0, 3.0, 300)], None, 0.0))


class ForecastTests(unittest.TestCase):
    def _events(self, t_end, days, f):
        ev = []
        for k in range(days * 96):
            t = t_end - days * D + k * route.Q + 1
            c = f(datetime.datetime.fromtimestamp(t))
            if c:
                ev.append((t, c))
        return ev

    def test_repeatable_and_follows_the_daily_pattern(self):
        t = at('2026-10-07 07:00')
        ev = self._events(t, 28, lambda d: 2.0 if 9 <= d.hour < 18 else 0.0)   # $2 per 15 minutes from 9:00 to 18:00
        a = route.forecast(ev, t, [H, D])
        self.assertEqual(a, route.forecast(ev, t, [H, D]))                  # the same input gives the same samples
        one_h, one_d = [sorted(v) for v in a]
        self.assertLess(one_h[len(one_h) // 2], 1.0)                       # almost no use from 7:00 to 8:00
        self.assertAlmostEqual(one_d[len(one_d) // 2], 72.0, delta=12)     # 9 hours × $8 in a day

    def test_any_horizon_down_to_minutes(self):
        t = at('2026-10-07 10:00')
        ev = self._events(t, 14, lambda d: 2.0 if 9 <= d.hour < 18 else 0.0)
        ten_min, ninety_min = [sorted(v) for v in route.forecast(ev, t, [600, 5400])]
        self.assertEqual(len(ten_min), route.N_SAMP)
        self.assertAlmostEqual(ten_min[len(ten_min) // 2], 2.0, delta=2.0)  # in use at 10:00: about $1.3 in 10 minutes
        self.assertAlmostEqual(ninety_min[len(ninety_min) // 2], 12.0, delta=2.5)

    def test_weekdays_are_not_used(self):
        # moving every record and now by one day changes only the weekday, and the forecast stays the same
        t = at('2026-10-07 07:00')
        rng = random.Random(4)
        ev = self._events(t, 21, lambda d: rng.choice([0.0, 0.0, 1.0, 3.0]) if 9 <= d.hour < 20 else 0.0)
        xs = [H, D, 3 * D]
        self.assertEqual(route.forecast(ev, t, xs), route.forecast([(a + D, c) for a, c in ev], t + D, xs))

    def test_horizon_longer_than_the_records_is_scaled(self):
        t = at('2026-10-07 07:00')
        ev = self._events(t, 3, lambda d: 1.0)                               # only 3 days ($1 per 15 minutes)
        week = route.forecast(ev, t, [7 * D])[0]
        self.assertEqual(len(week), route.N_SAMP)
        self.assertAlmostEqual(sum(week) / len(week), 4 * 24 * 7, delta=10)  # the pace of the records over 7 days

    def test_all_past_outcomes_are_mixed_in_when_the_records_hold_few(self):
        t = at('2026-10-07 07:00')
        rng = random.Random(6)
        ev = self._events(t, 4, lambda d: rng.choice([0.0, 1.0, 2.0]) if 9 <= d.hour < 18 else 0.0)
        a = route.Analog().fit(ev, t).predict([D], 400, 0)[0]
        b = route.Analog(plain=True).fit(ev, t).predict([D], 400, 1)[0]
        k = round(400 * route.FC_MIX / (route.FC_MIX + (t - ev[0][0]) / D))         # about 4 outcomes of 1 day in the records: a third from all past outcomes
        self.assertEqual(route.forecast(ev, t, [D])[0], a[:400 - k] + b[:k])
        self.assertEqual(len(route.forecast(ev, t, [600])[0]), route.N_SAMP)

    def test_no_records_no_forecast(self):
        t = at('2026-10-07 07:00')
        self.assertEqual(route.forecast([], t, [600, D]), [None, None])

    def test_kernel_width_reaches_the_target(self):
        t = at('2026-10-07 07:00')
        rng = random.Random(5)
        ev = self._events(t, 30, lambda d: rng.choice([0.0, 1.0, 2.0]) if 8 <= d.hour < 22 else 0.0)
        m = route.Analog().fit(ev, t)
        w = m._weights(len(m.grid))
        self.assertAlmostEqual(sum(w) ** 2 / sum(v * v for v in w), route.FC_ESS, delta=0.05 * route.FC_ESS)
        w = m._weights(1000)                                                 # short records: at most FC_FRAC of the starts
        self.assertAlmostEqual(sum(w) ** 2 / sum(v * v for v in w), route.FC_FRAC * 1000, delta=0.05 * route.FC_FRAC * 1000)

    def test_held_back_time_is_replaced_by_the_pace_before(self):
        # a weekly window reaches 95% 2 days after its start, and then almost nothing is used until the reset
        t0 = at('2026-10-01 00:00')
        ev = [(t0 + k * H + 1, 2.0) for k in range(48)]
        rows = [{'ts': t0 + k * H, 'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': min(2.0 * k, 99.0), 'r': int(t0 + 7 * D)} for k in range(0, 120, 6)]
        sers = route.load_series('claude', rows)
        held = route.held_back(sers.values(), t0 + 5 * D)
        self.assertEqual([(a, b) for a, b, _, _ in held], [(t0 + 48 * H, t0 + 5 * D)])
        un = route.unheld(ev, held)
        self.assertAlmostEqual(sum(c for a, c in un if a >= t0 + 48 * H), 2.0 * 72, delta=0.01)   # $2 an hour also for the 3 held-back days
        self.assertEqual([e for e in un if e[0] < t0 + 48 * H], ev)


def bracket(kind, r, length_min, side='claude'):
    """Reads just before and after the start of the period ending at r: the window's length is known from the records (the API gives none for Claude)."""
    start = r - length_min * 60
    return [{'ts': int(start - 120), 'side': side, 'id': kind, 'u': 50.0, 'r': int(start)},
            {'ts': int(start + 60), 'side': side, 'id': kind, 'u': 1.0, 'r': int(r)}]


class WindowStateTests(TempDirCase):
    def test_probability_from_cost_and_usage(self):
        t = at('2026-10-07 12:00')
        # $10 per hour ($2.5 per 15 minutes) from 9:00 to 18:00 every day for 21 days. The 7-day window grows 1% per hour (limit $1,000)
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
        states, notes, left = route.window_states(live, t, log=bracket('weekly_all', r, 10080), n=200)
        w = states['claude']['claude-weekly_all']
        self.assertEqual(w['limit_src'], 'now')
        self.assertAlmostEqual(w['limit_usd'], 100 * 450 / 45, delta=60)     # $450 since the window start (5 days ago)
        self.assertAlmostEqual(w['remain_usd'], w['limit_usd'] * 0.55, delta=1)
        self.assertLess(w['X'], 0.5)                                         # about $550 remaining, and only about $200 is used in 2 days
        self.assertIsNone(states['claude']['claude-weekly_scoped:Fable']['X'])
        self.assertEqual(notes, [])
        self.assertGreater(left['claude'], 0)                                # some of the $550 is expected to be left (the scoped window is not used)
        self.assertLess(left['claude'], w['remain_usd'])
        self.assertIsNone(left['codex'])

    def test_read_whose_reset_has_passed_starts_a_new_period(self):
        # a record read before the reset (e.g. used instead on 429): the window has reset since, so usage is 0 and the period ends one length later
        t = at('2026-10-07 12:00')
        self.write_costs('claude', [{'k': 'a', 'ts': t - 3 * H, 'model': 'claude-opus-5-5', 'out': 125000}])
        states, notes, _ = route.window_states([{'side': 'claude', 'id': 'session', 'u': 99.0, 'r': int(t - 60), 'ts': int(t - 300)}], t,
                                               log=bracket('session', t - 60, 300))
        w = states['claude']['claude-session']
        self.assertEqual(w['u'], 0.0)
        self.assertAlmostEqual(w['r'], int(t - 60) + 300 * 60, delta=120)      # the length is estimated from the reads (within the read interval)
        self.assertTrue(any('reset after the read' in x for x in notes))
        self.assertEqual(route.decide_states({'claude': states['claude'], 'codex': {'codex-7d': {'u': 10, 'X': 0.1, 'win': 10080}}}, 'claude')[1]['full'], [])

    def test_reset_passed_without_contiguous_periods(self):
        # periods start at the next use (the records show gaps between them): after the reset, a period starting now would end a length later
        t = at('2026-10-07 12:00')
        H5 = 300 * 60
        log = []
        for k in range(3):
            st = t - (k + 1) * 30 * H
            log += [{'ts': int(st - 120), 'side': 'claude', 'id': 'session', 'u': 0.0, 'r': None},
                    {'ts': int(st + 60), 'side': 'claude', 'id': 'session', 'u': 1.0, 'r': int(st + H5)}]
        states, _, _ = route.window_states([{'side': 'claude', 'id': 'session', 'u': 80.0, 'r': int(t - 60), 'ts': int(t - 300)}], t, log=log)
        w = states['claude']['claude-session']
        self.assertEqual(w['u'], 0.0)
        self.assertAlmostEqual(w['r'], t + w['win'] * 60, delta=1)

    def test_probability_from_less_than_a_day_of_cost(self):
        t = at('2026-10-07 12:00')
        rows = [{'k': f'a{q}', 'ts': t - 3 * H + q * 900 + 1, 'model': 'claude-opus-5-5', 'out': 125000} for q in range(12)]
        self.write_costs('claude', rows)                             # about $112 in 3 hours (the cost records cover only 3 hours)
        states, notes, left = route.window_states([{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 10.0, 'r': int(t + 3 * D)},
                                             {'side': 'claude', 'id': 'session', 'win': 300, 'u': 99.0, 'r': int(t + H)}], t,
                                            log=bracket('weekly_all', t + 3 * D, 10080) + bracket('session', t + H, 300))
        w = states['claude']['claude-weekly_all']
        self.assertEqual(w['limit_src'], 'now')                       # the cost before the records is taken as 0
        self.assertIsNotNone(w['X'])
        self.assertIsNotNone(states['claude']['claude-session']['X'])  # also given for a full window
        self.assertEqual(notes, [])

    def test_probability_from_usage_growth_without_any_cost_record(self):
        # no cost records on this machine (e.g. a new machine): X comes from the window's own usage growth, even when the current read is not in the records yet
        t = at('2026-10-07 12:00')
        r_prev = t - 4 * D                                   # Claude's length is known from reads around the last reset (the API gives none)
        log = [{'ts': int(r_prev - 300), 'side': 'claude', 'id': 'weekly_all', 'u': 50.0, 'r': int(r_prev)},
               {'ts': int(r_prev + 60), 'side': 'claude', 'id': 'weekly_all', 'u': 0.0, 'r': int(t + 3 * D)},
               {'ts': int(t - 2 * D), 'side': 'claude', 'id': 'weekly_all', 'u': 15.0, 'r': int(t + 3 * D)}]
        states, notes, left = route.window_states([{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 90.0, 'r': int(t + 3 * D)},
                                             {'side': 'claude', 'id': 'weekly_all', 'u': 30.0, 'r': int(t + 3 * D)}], t, log=log)
        hi, lo = states['codex']['codex-7d'], states['claude']['claude-weekly_all']
        self.assertEqual((hi['basis'], hi['remain_pct']), ('usage', 10.0))
        self.assertGreater(hi['X'], 0.5)                     # 90% in 4 days, 10% left for 3 days
        self.assertLess(lo['X'], 0.5)                        # 30% in 4 days, 70% left for 3 days
        self.assertIn('exhaust probability from usage growth', notes[0])
        self.assertEqual(left, {'claude': None, 'codex': None})            # no limit in cost
        pick, why = route.decide_states(states, 'claude', left_usd=left)
        self.assertEqual((pick, why['stage']), ('claude', 'leftover_prob'))   # Codex is likelier to be used up (the sub side has no guard; the probability decides)

    def test_usage_growth_uses_the_reads_in_the_records(self):
        # reads every hour: 1%/hour for the last day of the window so far. 60% used, 40 hours to the reset → about 40% more → X near the middle, not 0 or 1 only
        t = at('2026-10-07 12:00')
        r = int(t + 40 * H)
        log = [{'ts': int(t - k * H), 'side': 'codex', 'id': '7d', 'win': 10080, 'u': 60.0 - k, 'r': r, 'src': 'live'} for k in range(24, 0, -1)]
        states, _, left = route.window_states([{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 60.0, 'r': r}], t, log=log)
        w = states['codex']['codex-7d']
        self.assertEqual(w['basis'], 'usage')
        self.assertIsNotNone(w['X'])
        ev = route.usage_events(route.load_series('codex', log + [{'ts': int(t), 'side': 'codex', 'id': '7d', 'win': 10080, 'u': 60.0, 'r': r}])['7d'], t)
        self.assertAlmostEqual(sum(v for _, v in ev), 60.0)   # the 36% seen at the first read is spread back to the window start, then 1% per read

    def test_usage_growth_when_the_cost_gives_no_limit(self):
        # there are cost records, but none in this window and no usage history: no limit in cost, so X comes from usage growth
        t = at('2026-10-07 12:00')
        self.write_costs('claude', [{'k': 'a', 'ts': t - 20 * D, 'model': 'claude-opus-5-5', 'out': 1000}])
        r_prev = t - 6 * D
        log = [{'ts': int(r_prev - 300), 'side': 'claude', 'id': 'weekly_all', 'u': 50.0, 'r': int(r_prev)},
               {'ts': int(r_prev + 60), 'side': 'claude', 'id': 'weekly_all', 'u': 0.0, 'r': int(t + D)},
               {'ts': int(t - 3 * D), 'side': 'claude', 'id': 'weekly_all', 'u': 20.0, 'r': int(t + D)}]
        states, _, left = route.window_states([{'side': 'claude', 'id': 'weekly_all', 'u': 45.0, 'r': int(t + D)}], t, log=log)
        w = states['claude']['claude-weekly_all']
        self.assertIsNone(w['limit_usd'])
        self.assertEqual(w['basis'], 'usage')
        self.assertIn('limit unknown in cost', w['note'])
        self.assertLess(w['X'], 0.5)                          # 45% in 6 days, 55% left for 1 day


class ForecastDataTests(TempDirCase):
    def cost_rows(self, t):
        # $10 per hour from 9:00 to 18:00 every day for 21 days (as in WindowStateTests)
        rows = []
        for k in range(21 * 24):
            ts = t - 21 * D + k * H
            if 9 <= datetime.datetime.fromtimestamp(ts).hour < 18:
                rows += [{'k': f'{k}-{q}', 'ts': ts + q * 900 + 1, 'model': 'claude-opus-5-5', 'out': 125000} for q in range(4)]
        return rows

    def test_paths_of_every_forecast_window(self):
        t = at('2026-10-07 12:00')
        self.write_costs('claude', self.cost_rows(t))
        r = t + 2 * D
        live = [{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 45.0, 'r': int(r)},
                {'side': 'claude', 'id': 'weekly_scoped:Fable', 'win': 10080, 'u': 0.0, 'r': int(r), 'scoped': True}]
        d = route.forecast_data(live, t, log=bracket('weekly_all', r, 10080), n=50)
        self.assertEqual(list(d['windows']), ['claude-weekly_all'])        # the model-scoped window is not forecast
        w = d['windows']['claude-weekly_all']
        self.assertEqual(w['basis'], 'cost')
        self.assertAlmostEqual(w['step_s'], w['win'] * 60 / 100)          # Claude's length is estimated from the reads (about 7 days)
        self.assertEqual(len(w['ts']), 29)                                 # the steps of the period left (2 days of 7) after now
        self.assertEqual(w['ts'][-1], int(r))                              # the last step is the reset
        for a, b in zip(w['ts'], w['ts'][1:]):
            self.assertAlmostEqual(b - a, w['step_s'], delta=0.1)
        self.assertEqual(len(w['paths']), 50)
        for path in w['paths']:
            self.assertEqual(len(path), 29)
            self.assertGreaterEqual(path[0], 45.0)
            self.assertTrue(all(b >= a for a, b in zip(path, path[1:])))   # usage never falls along a path
        self.assertLess(sorted(p[-1] for p in w['paths'])[25], 100.0)      # about $550 remaining, and only about $200 is used in 2 days

    def test_window_forecast_on_usage_growth(self):
        t = at('2026-10-07 12:00')
        r = int(t + 40 * H)
        log = [{'ts': int(t - k * H), 'side': 'codex', 'id': '7d', 'win': 10080, 'u': 60.0 - k, 'r': r, 'src': 'live'} for k in range(24, 0, -1)]
        d = route.forecast_data([{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 60.0, 'r': r}], t, log=log, n=50)
        w = d['windows']['codex-7d']
        self.assertEqual((w['basis'], w['limit_usd']), ('usage', None))
        self.assertEqual(w['ts'][-1], r)
        self.assertTrue(any(p[-1] > 60.0 for p in w['paths']))

    def test_window_with_too_short_records(self):
        # the length is known, but there is no use at all to forecast from: the window is listed without paths
        t = at('2026-10-07 12:00')
        d = route.forecast_data([{'side': 'codex', 'id': '7d', 'win': 10080, 'u': 0.0, 'r': int(t + D)}], t, log=[], n=20)
        self.assertIsNone(d['windows']['codex-7d']['paths'])

    def test_written_at_once(self):
        t = at('2026-10-07 12:00')
        self.write_costs('claude', self.cost_rows(t))
        r = t + 2 * D
        self.assertEqual(route.write_forecast([{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 45.0, 'r': int(r)}], t,
                                              log=bracket('weekly_all', r, 10080), n=20), 1)
        with open(route.FORECAST_FILE) as f:
            d = json.load(f)
        self.assertEqual((d['t'], d['n'], len(d['windows']['claude-weekly_all']['paths'])), (int(t), 20, 20))
        self.assertEqual(os.listdir(route.DATA).count('forecast.json.tmp'), 0)


class CodexLoginTests(TempDirCase):
    def _auth(self, d):
        os.makedirs(route.CODEX_HOME, exist_ok=True)
        with open(os.path.join(route.CODEX_HOME, 'auth.json'), 'w') as f:
            json.dump(d, f)

    def test_unreadable_logins_say_why_without_calling_out(self):
        t = time.time()
        with self.assertRaisesRegex(route.NotLoggedIn, 'no Codex login'):
            route.read_codex(t)
        self.assertFalse(route.logged_in('codex'))
        self._auth({'OPENAI_API_KEY': 'sk-x', 'tokens': None})
        with self.assertRaisesRegex(route.NotLoggedIn, 'API-key login'):
            route.read_codex(t)
        self.assertFalse(route.logged_in('codex'))                        # an API-key login has no plan: work is not handed to it
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({'exp': int(t) - 60}).encode()).decode().rstrip('=')
        self._auth({'tokens': {'access_token': 'h.' + payload + '.s', 'account_id': 'a'}})
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            route.read_codex(t)                                            # an expired token is not sent (nor refreshed)
        self.assertTrue(route.logged_in('codex'))                         # but Codex is logged in (it refreshes the token when used)
        self.assertFalse(route.logged_in('claude'))                       # no Claude Code credentials in the test home

    def _serve(self, code):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        os.environ['CLAUDE_CODEX_ROUTER_CODEX_URL'] = f'http://127.0.0.1:{srv.server_address[1]}/x'
        self.addCleanup(os.environ.pop, 'CLAUDE_CODEX_ROUTER_CODEX_URL', None)

    def test_a_rejected_login_is_told_apart_from_other_failures(self):
        t = time.time()
        self._auth({'tokens': {'access_token': 'not-a-jwt', 'account_id': 'a'}})
        self._serve(401)
        with self.assertRaises(route.LoginRejected) as c:
            route.read_codex(t)
        self.assertEqual(c.exception.code, 401)
        self._serve(503)
        with self.assertRaises(Exception) as c:
            route.read_codex(t)
        self.assertNotIsInstance(c.exception, route.LoginRejected)        # the service failing is not about the login
        self.assertIsNone(route.login_state('codex', c.exception, t))

    def test_login_state_says_what_is_known_and_what_to_do(self):
        t = at('2026-10-06 10:00')
        log = [{'ts': t - 3 * D, 'side': 'codex', 'src': 'live'}, {'ts': t - D, 'side': 'codex', 'src': 'token-pace'},
               {'ts': t - 60, 'side': 'claude', 'src': 'live'}]
        d = route.login_state('codex', route.TokenExpired('x', t - 2 * D), t, log)
        self.assertEqual((d['state'], d['expired_at'], d['last_read']), ('token_expired', int(t - 2 * D), t - 3 * D))   # the latest read by this tool
        self.assertIn('codex login', d['action'])
        d = route.login_state('claude', route.LoginRejected('x', 403), t, log)
        self.assertEqual((d['state'], d['http'], d['last_read']), ('login_rejected', 403, t - 60))
        self.assertIn('claude auth login', d['action'])
        d = route.login_state('codex', route.NotLoggedIn('x'), t, [])
        self.assertEqual((d['state'], d['last_read']), ('not_logged_in', None))

    def test_read_both_reports_the_login_of_the_side_not_read(self):
        t = at('2026-10-06 10:00')

        def expired(t, notes=None):
            raise route.TokenExpired('Codex access token expired', t - H)
        saved = route.read_claude, route.read_codex
        route.read_claude = lambda t, notes=None: [{'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 50.0, 'r': int(t + D)}]
        route.read_codex = expired
        try:
            login = {}
            rows, errs, notes = route.read_both(t, login=login)
            self.assertEqual((sorted(login), login['codex']['state']), (['codex'], 'token_expired'))
            self.assertEqual(len(errs), 1)
            route.read_codex = lambda t, notes=None: (_ for _ in ()).throw(OSError('connection refused'))
            login = {}
            route.read_both(t, login=login)
            self.assertEqual(login, {})                                     # a network failure is not about the login
        finally:
            route.read_claude, route.read_codex = saved


class SampleNoteTests(TempDirCase):
    def test_tells_when_the_timer_stopped(self):
        t = time.time()
        self.assertIsNone(route.sample_note(t))                          # not an installed copy (no .version)
        open(route.VERSION_FILE, 'w').close()
        os.utime(route.VERSION_FILE, (t - 7200, t - 7200))
        self.assertIn('never run', route.sample_note(t))                    # 2 hours after install, sample has never run
        os.makedirs(route.DATA)
        open(route.SAMPLE_STAMP, 'w').close()
        os.utime(route.SAMPLE_STAMP, (t - 600, t - 600))
        self.assertIsNone(route.sample_note(t))                          # ran 10 minutes ago
        os.utime(route.SAMPLE_STAMP, (t - 3 * 3600, t - 3 * 3600))
        self.assertIn('stopped 3.0 h ago', route.sample_note(t))


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
        route.record([{'ts': t + 60, 'side': 'claude', 'id': 'session', 'u': 11.0, 'r': int(t + 2 * H)}])
        sers = route.load_series('claude')
        self.assertEqual(sorted(sers), ['session', 'weekly_scoped:Fable'])     # the row without id joins session (the same reset)
        self.assertTrue(sers['weekly_scoped:Fable'].scoped)
        self.assertFalse(sers['session'].scoped)

    def test_recent_claude_record_skips_the_api(self):
        t = at('2026-10-06 10:00')
        route.record([{'ts': t - 30, 'side': 'claude', 'id': 'weekly_all', 'win': 10080, 'u': 12.0, 'r': int(t + D)}])
        rows, errs, notes, calls = self._read_both_counting(t)
        self.assertEqual(calls, [])
        self.assertEqual([x['u'] for x in rows if x['side'] == 'claude'], [12.0])
        self.assertIn('API not called', notes[0])
        route.record(rows)                                   # borrowed rows are not added to the records
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


if __name__ == '__main__':
    unittest.main()
