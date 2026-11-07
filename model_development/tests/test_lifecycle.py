from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from model_development.lifecycle import Session, PreparedModel
from model_development.phase_2.model import parser, run_model
from utils.engine.adapters.paper_execution import Config

CORE=Path('C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/work/binance_futures_executor')


def signal(sid='one', eligible=1000, force=9000):
    return dict(sample_id=sid,session_day='2026-08-01',direction='long',stop=95.,entry_reference=100.,
        feature_as_of_ms=eligible,feature_available_at_ms=eligible,decision_timestamp_ms=eligible,
        entry_eligible_timestamp_ms=eligible,entry_deadline_timestamp_ms=eligible+1000,force_exit_timestamp_ms=force)


def tick(ts, ident, price):
    return dict(timestamp_ms=ts,agg_trade_id=ident,price=price)


class Lifecycle(unittest.TestCase):
    def test_fill_partial_exit_feedback_and_copy_isolation(self):
        m=PreparedModel([signal()]); events=[]
        def callback(event):
            events.append(deepcopy(event))
            if 'position' in event: event['position']['quantity']=-100
        m.on_execution=callback
        s=Session(m,Config(tp1_r=1.,tp1_fraction=.1))
        s.decide(lambda:m.evaluate_at(1000,1000),1000)
        self.assertEqual(events,[])
        s.tick(tick(1000,1,100.))
        self.assertEqual(s.portfolios['prepared'].position['quantity'],1.)
        s.tick(tick(2000,2,105.));self.assertEqual(len(events),1)
        s.tick(tick(2000,3,106.))
        self.assertEqual(events[-1]['type'],'partial_fill')
        self.assertTrue(m.positions)
        s.tick(tick(9000,4,110.))
        self.assertFalse(m.positions)
        self.assertEqual([e['type'] for e in events],['entry_fill','partial_fill','exit_fill'])
        trade=s.portfolios['prepared'].trades[0]
        self.assertAlmostEqual(trade['pnl'],trade['gross_pnl_before_costs']-trade['fees'])

    def test_state_mismatch_and_invalid_position_block_fail(self):
        m=PreparedModel([signal()]);s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000);s.tick(tick(1000,1,100.))
        m.positions.clear()
        with self.assertRaisesRegex(ValueError,'diverged'): s.tick(tick(2000,2,101.))
        m=PreparedModel([]);s=Session(m,Config())
        def false_block():
            m.position_blocks.append(dict(signal(),setup_id='x',submodel_key='prepared',blocking_setup_id='missing'))
        with self.assertRaisesRegex(ValueError,'block'):s.decide(false_block,1000)

    def test_future_availability_and_backdated_cancel_fail(self):
        m=PreparedModel([signal()]);s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000)
        with self.assertRaisesRegex(ValueError,'Cancellation'):s.cancel_pending('prepared',999)
        m.pending['prepared']['feature_available_at_ms']=2000
        with self.assertRaisesRegex(ValueError,'causal'):s.tick(tick(1000,1,100.))

    def test_original_entry_delay_not_reset_by_late_decision(self):
        a=signal();a['entry_deadline_timestamp_ms']=9000
        m=PreparedModel([a]);s=Session(m,Config(max_entry_delay_ms=100))
        s.decide(lambda:m.evaluate_at(1000,2000),2000);s.tick(tick(2000,1,100.))
        self.assertFalse(m.pending);self.assertFalse(m.positions)
        self.assertEqual(s.event_counts,{'entry_rejected':1})

    def test_price_rejection_and_no_same_print_exit(self):
        m=PreparedModel([signal()]);s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000);s.tick(tick(1000,1,94.))
        self.assertFalse(m.positions)
        m=PreparedModel([signal()]);s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000);s.tick(tick(1000,1,100.))
        s.tick(tick(1000,2,94.));self.assertEqual(s.portfolios['prepared'].trades,[])
        s.tick(tick(1000,3,93.));self.assertEqual(len(s.portfolios['prepared'].trades),1)
        with self.assertRaisesRegex(ValueError,'print'):s.tick(tick(1000,3,93.))

    def test_duplicate_intentions_and_insolvency_stop_new_risk(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):PreparedModel([signal(),signal()])
        m=PreparedModel([signal()]);s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000)
        s.portfolios['prepared'].equity=0
        s.tick(tick(1000,1,100.))
        self.assertFalse(m.positions);self.assertIn('prepared',s.insolvent)

    def test_cancellation_feedback(self):
        m=PreparedModel([signal()]);events=[];m.on_execution=events.append
        s=Session(m,Config());s.decide(lambda:m.evaluate_at(1000,1000),1000)
        s.cancel_pending('prepared',1000)
        self.assertFalse(m.pending);self.assertEqual(events[0]['reason'],'cancelled')

    def test_expired_intentions_between_sparse_prints_and_model_cancellation(self):
        m=PreparedModel([signal(),signal('two',5000,12000)])
        events=[];m.on_execution=events.append;s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,10000),10000)
        s.decide(lambda:m.evaluate_at(5000,10000),10000)
        s.tick(tick(10000,1,100.))
        self.assertFalse(m.positions);self.assertFalse(m.pending)
        self.assertEqual(len(events),2)
        m=PreparedModel([signal()]);events=[];m.on_execution=events.append;s=Session(m,Config())
        s.decide(lambda:m.evaluate_at(1000,1000),1000)
        s.decide(lambda:m.pending.clear(),2000)
        self.assertEqual(events[-1]['reason'],'model_cancelled_or_replaced')

    def test_real_vwap_candle_driver_accounts_artifacts_and_chart_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            dates=pd.date_range('2025-01-01','2025-03-01',freq='1D',tz='UTC')
            prices=[100.+(i%7)*5 for i in range(len(dates))]
            raw=base/'UNITUSDT-aggTrades-synthetic.parquet'
            pq.write_table(pa.table(dict(timestamp=dates.as_unit('ms').asi8,agg_trade_id=list(range(len(dates))),
                price=prices,qty=[1.]*len(dates))),raw)
            def args(output):
                return parser().parse_args(['--model',str(ROOT/'model_development/models/mean_reversion_by_vwap/legacy/monthly_sd_reversion.py'),
                    '--input',str(raw),'--output',str(base/output),'--shared-core',str(CORE),'--cache-dir',str(base/'cache'),
                    '--venue','synthetic','--product','linear_quote_margined','--quantity-unit','UNIT','--pnl-unit','USDT',
                    '--iteration','ITER-TEST','--inspection-status','development'])
            result=run_model(args('run'))
            self.assertEqual(result['status'],'complete');self.assertEqual(len(result['jobs']),3)
            self.assertGreater(result['closed_trades'],0);self.assertGreater(result['position_blocks'],0)
            rows=pq.read_table(base/'run/trades.parquet').to_pylist()
            for job in result['jobs']:
                trades=[r for r in rows if r['record_type']=='closed_trade' and r['portfolio_id']==job['portfolio_id']]
                self.assertAlmostEqual(job['metrics']['final_equity'],1000+sum(r['net_pnl'] for r in trades))
                self.assertTrue(all(r['entry_timestamp_ms']>=r['decision_timestamp_ms']>=r['feature_available_at_ms'] for r in trades))
            self.assertTrue(all(r['net_pnl'] is None for r in rows if r['record_type']=='position_block'))
            self.assertEqual({p.name for p in (base/'run').iterdir()},{'run.json','trades.parquet','equity_curve.png'})
            cutoff=int(pd.Timestamp('2025-02-01',tz='UTC').timestamp()*1000)
            # Mutating later raw prices must not change earlier decisions/fills.
            altered=[p if d<pd.Timestamp('2025-02-02',tz='UTC') else p*2 for d,p in zip(dates,prices)]
            pq.write_table(pa.table(dict(timestamp=dates.as_unit('ms').asi8,agg_trade_id=list(range(len(dates))),
                price=altered,qty=[1.]*len(dates))),raw)
            run_model(args('future_mutation'))
            changed=pq.read_table(base/'future_mutation/trades.parquet').to_pylist()
            names=('record_type','setup_id','entry_timestamp_ms','exit_timestamp_ms','entry_price','exit_price',
                   'quantity','gross_pnl','fees','net_pnl','blocking_setup_id')
            def prior(table):
                return [{n:r.get(n) for n in names} for r in table
                        if (r.get('exit_timestamp_ms') or r['decision_timestamp_ms'])<cutoff]
            self.assertEqual(prior(rows),prior(changed))
            with patch('model_development.phase_2.lifecycle_output.plot_accounts',side_effect=RuntimeError('chart failed')):
                with self.assertRaisesRegex(RuntimeError,'chart failed'):run_model(args('failed'))
            self.assertEqual(json.loads((base/'failed/run.json').read_text())['status'],'failed')
            with self.assertRaises(FileExistsError):run_model(args('failed'))

    def test_five_minute_flow_bars_and_ten_minute_confirmation_are_causal(self):
        import numpy as np
        from model_development.models.burst_breakout.strategy import (
            assess_breakout_flow, confirm_flow_entries, _flow_cache_location,
        )
        start = 24 * 3_600_000
        prices = 100 + np.arange(1500) * .01
        prices[1440:] = prices[1439] + np.arange(1, 61) * .1
        minutes = pd.DataFrame(dict(
            timestamp_ms=np.arange(1500, dtype='int64') * 60_000,
            open=prices, high=prices + .03, low=prices - .01, close=prices + .02,
            buy_volume=np.r_[np.ones(1440) * 2, np.ones(60) * 8],
            sell_volume=np.ones(1500) * 3,
        ))
        item = signal(eligible=start, force=start + 24 * 3_600_000)
        item.update(stop=110., entry_reference=float(minutes.close.iloc[1439]))
        trace = assess_breakout_flow(minutes, [item], step_minutes=5, window_minutes=10)
        self.assertEqual(trace.elapsed_minutes.tolist(), list(range(10, 61, 5)))
        self.assertEqual(trace.assessment_timestamp_ms.iloc[0], start + 10 * 60_000)
        first = trace.iloc[0]
        self.assertEqual(first.baseline_expected_samples, 288)
        self.assertEqual(first.favorable_aggressive_qty, 80)
        self.assertEqual(first.opposing_aggressive_qty, 30)
        self.assertAlmostEqual(first.progress_5m_pct, 100 * (
            minutes.close.iloc[1449] / minutes.open.iloc[1445] - 1))
        self.assertAlmostEqual(first.progress_10m_pct, 100 * (
            minutes.close.iloc[1449] / minutes.open.iloc[1440] - 1))
        entries, decisions = confirm_flow_entries(trace, [item], start + 3_600_001)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + 10 * 60_000)
        cutoff = start + 20 * 60_000
        prefix = assess_breakout_flow(minutes[minutes.timestamp_ms < cutoff], [item],
                                     step_minutes=5, window_minutes=10)
        pd.testing.assert_frame_equal(
            trace[trace.assessment_timestamp_ms <= cutoff].reset_index(drop=True),
            prefix[prefix.assessment_timestamp_ms <= cutoff].reset_index(drop=True),
        )
        bad = minutes.copy()
        bad.loc[bad.timestamp_ms.eq(start + 60_000), 'low'] = 109
        touched = assess_breakout_flow(bad, [item], step_minutes=5, window_minutes=10)
        self.assertFalse(confirm_flow_entries(touched, [item], start + 3_600_001)[0])
        missing = minutes[minutes.timestamp_ms != start + 60_000]
        incomplete = assess_breakout_flow(missing, [item], step_minutes=5, window_minutes=10)
        self.assertFalse(incomplete.window_complete.iloc[0])
        self.assertFalse(confirm_flow_entries(incomplete, [item], start + 3_600_001)[0])
        expired = trace.copy()
        expired['urgency_percentile'] = 75.
        entries, decisions = confirm_flow_entries(expired, [item], start + 3_600_001)
        self.assertFalse(entries)
        self.assertEqual(decisions.decision.iloc[-1], 'expired')
        old_key, _ = _flow_cache_location(Path('cache'), 'source', [item], 60, 1, 5)
        new_key, _ = _flow_cache_location(Path('cache'), 'source', [item], 60, 5, 10)
        self.assertNotEqual(old_key, new_key)

    def test_three_minute_flow_bars_and_twelve_minute_window(self):
        import numpy as np
        from model_development.models.burst_breakout.strategy import (
            assess_breakout_flow, confirm_flow_entries, _flow_cache_location,
        )
        start = 24 * 3_600_000
        prices = 100 + np.arange(1500) * .01
        prices[1440:] = prices[1439] + np.arange(1, 61) * .1
        minutes = pd.DataFrame(dict(
            timestamp_ms=np.arange(1500, dtype='int64') * 60_000,
            open=prices, high=prices + .03, low=prices - .01, close=prices + .02,
            buy_volume=np.r_[np.ones(1440) * 2, np.ones(60) * 8],
            sell_volume=np.ones(1500) * 3,
        ))
        item = signal(eligible=start, force=start + 24 * 3_600_000)
        item.update(stop=110., entry_reference=float(minutes.close.iloc[1439]))
        trace = assess_breakout_flow(minutes, [item], step_minutes=3, window_minutes=12)
        self.assertEqual(trace.elapsed_minutes.tolist(), list(range(12, 61, 3)))
        self.assertEqual(trace.baseline_expected_samples.iloc[0], 480)
        first = trace.iloc[0]
        self.assertEqual(first.favorable_aggressive_qty, 96)
        self.assertEqual(first.opposing_aggressive_qty, 36)
        self.assertEqual(first.bar_favorable_aggressive_qty, 24)
        self.assertAlmostEqual(first.progress_3m_pct, 100 * (
            minutes.close.iloc[1451] / minutes.open.iloc[1449] - 1))
        self.assertAlmostEqual(first.progress_12m_pct, 100 * (
            minutes.close.iloc[1451] / minutes.open.iloc[1440] - 1))
        entries, _ = confirm_flow_entries(trace, [item], start + 3_600_001)
        self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + 12 * 60_000)
        cutoff = start + 18 * 60_000
        prefix = assess_breakout_flow(minutes[minutes.timestamp_ms < cutoff], [item],
                                     step_minutes=3, window_minutes=12)
        pd.testing.assert_frame_equal(
            trace[trace.assessment_timestamp_ms <= cutoff].reset_index(drop=True),
            prefix[prefix.assessment_timestamp_ms <= cutoff].reset_index(drop=True),
        )
        missing = minutes[minutes.timestamp_ms != start + 60_000]
        gap = assess_breakout_flow(missing, [item], step_minutes=3, window_minutes=12)
        self.assertFalse(confirm_flow_entries(gap, [item], start + 3_600_001)[0])
        bad = minutes.copy()
        bad.loc[bad.timestamp_ms.eq(start + 60_000), 'low'] = 109
        touched = assess_breakout_flow(bad, [item], step_minutes=3, window_minutes=12)
        self.assertFalse(confirm_flow_entries(touched, [item], start + 3_600_001)[0])
        trace['urgency_percentile'] = 75.
        entries, decisions = confirm_flow_entries(trace, [item], start + 3_600_001)
        self.assertFalse(entries)
        self.assertEqual(decisions.decision.iloc[-1], 'expired')
        a, _ = _flow_cache_location(Path('cache'), 'source', [item], 60, 3, 12)
        b, _ = _flow_cache_location(Path('cache'), 'source', [item], 60, 5, 10)
        self.assertNotEqual(a, b)

    def test_flow_entry_gate_requires_strict_next_boundary_and_current_support(self):
        from model_development.models.burst_breakout.strategy import confirm_flow_entries
        start = 24 * 3_600_000
        item = signal(eligible=start, force=start + 24 * 3_600_000)

        def trace_at(step, window, ready):
            return pd.DataFrame([dict(
                sample_id=item['sample_id'], assessment_timestamp_ms=start + minute * 60_000,
                elapsed_minutes=minute, path_complete=True, poc_touched=False,
                window_complete=True, close=101., progress_from_anchor_pct=1.,
                progress_window_pct=1., progress_bar_pct=1.,
                urgency_percentile=90. if minute in ready else 75.,
                initiative_candidate=True, absorption_candidate=False,
                exhaustion_candidate=False, low_volume_expansion=False,
            ) for minute in range(window, 61, step)])

        for step, window, first, expected in (
            (1, 5, 5, 10), (1, 5, 6, 10), (1, 5, 10, 15),
            (3, 12, 12, 24), (3, 12, 15, 24),
            (5, 10, 10, 20), (5, 10, 15, 20),
        ):
            with self.subTest(step=step, first=first):
                trace = trace_at(step, window, {first, expected})
                entries, decisions = confirm_flow_entries(
                    trace, [item], start + 3_600_001, entry_gate_minutes=window)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + expected * 60_000)
                self.assertEqual(entries[0]['provisional_confirmation_timestamp_ms'], start + first * 60_000)
                self.assertEqual(decisions[decisions.elapsed_minutes.eq(first)].decision.iloc[0], 'buffering')
                # Intervening weak bars do not prevent a fresh positive judgement at the gate.
                immediate, _ = confirm_flow_entries(trace, [item], start + 3_600_001)
                self.assertEqual(immediate[0]['entry_eligible_timestamp_ms'], start + first * 60_000)
                prefix = trace[trace.elapsed_minutes <= expected]
                self.assertEqual(entries, confirm_flow_entries(
                    prefix, [item], start + 3_600_001, entry_gate_minutes=window)[0])

        trace = trace_at(1, 5, {5, 11, 15})
        entries, decisions = confirm_flow_entries(trace, [item], start + 3_600_001, entry_gate_minutes=5)
        self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + 15 * 60_000)
        self.assertEqual(entries[0]['provisional_confirmation_timestamp_ms'], start + 11 * 60_000)
        self.assertEqual(decisions[decisions.elapsed_minutes.eq(10)].decision_reason.iloc[0],
                         'gate_revalidation_failed')

    def test_flow_entry_gate_preserves_cancellation_and_sixty_minute_expiry(self):
        from model_development.models.burst_breakout.strategy import confirm_flow_entries
        start = 24 * 3_600_000
        item = signal(eligible=start, force=start + 24 * 3_600_000)
        trace = pd.DataFrame([dict(
            sample_id=item['sample_id'], assessment_timestamp_ms=start + minute * 60_000,
            elapsed_minutes=minute, path_complete=True, poc_touched=False,
            window_complete=True, close=101., progress_from_anchor_pct=1.,
            progress_window_pct=1., progress_bar_pct=1., urgency_percentile=90.,
            initiative_candidate=True, absorption_candidate=False,
            exhaustion_candidate=False, low_volume_expansion=False,
        ) for minute in range(5, 61)])
        for column, value, reason in (
            ('poc_touched', True, 'poc_touched_before_confirmation'),
            ('path_complete', False, 'missing_minute_in_setup_path'),
        ):
            bad = trace.copy()
            bad.loc[bad.elapsed_minutes.eq(8), column] = value
            entries, decisions = confirm_flow_entries(bad, [item], start + 3_600_001, entry_gate_minutes=5)
            self.assertFalse(entries)
            self.assertEqual(decisions.decision.iloc[-1], 'cancelled')
            self.assertEqual(decisions.decision_reason.iloc[-1], reason)
        for first, expected in ((55, 60), (60, None)):
            late = trace.copy()
            late.loc[late.elapsed_minutes.lt(first), 'urgency_percentile'] = 75.
            entries, decisions = confirm_flow_entries(late, [item], start + 3_600_001, entry_gate_minutes=5)
            if expected:
                self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + expected * 60_000)
            else:
                self.assertFalse(entries)
                self.assertEqual(decisions.decision.iloc[-1], 'expired')
        entries, decisions = confirm_flow_entries(trace, [item], start + 10 * 60_000, entry_gate_minutes=5)
        self.assertFalse(entries)
        self.assertEqual(decisions.decision.iloc[-1], 'unavailable')

    def test_breakout_daily_cap_option_keeps_default_and_rejects_non_boolean(self):
        from types import SimpleNamespace
        from model_development.models.burst_breakout.strategy import prepare_backtest
        for value in ('default', True, False, 0, 1, None, 'false'):
            with self.subTest(value=value):
                config = dict(mode='profile_burst', start_date='2026-10-01', end_date='2026-10-31')
                if value != 'default':
                    config['one_entry_per_utc_day'] = value
                request = SimpleNamespace(model_config=config, input=Path('raw.parquet'),
                    output_dir=Path('unused'), cache_dir=Path('cache'), id_policy='record',
                    initial_equity=1000., risk_fraction=.01, fee_bps=4., max_leverage=5.)
                if value != 'default' and type(value) is not bool:
                    with self.assertRaisesRegex(ValueError, 'one_entry_per_utc_day must be boolean'):
                        prepare_backtest(request)
                    continue
                cache = dict(source_start_ms=0, source_end_ms=2_000_000_000_000)
                with patch('model_development.observation_cache.candle_cache', return_value=(pd.DataFrame(), cache)), \
                     patch('model_development.models.burst_breakout.strategy._profile_signals', return_value=[]):
                    prepared = prepare_backtest(request)
                expected = True if value == 'default' else value
                self.assertIs(prepared['one_entry_per_utc_day'], expected)
                self.assertIs(prepared['metadata']['one_entry_per_utc_day'], expected)
                self.assertEqual(prepared['metadata']['max_entries_per_utc_day'], 1 if expected else None)
                self.assertEqual(prepared['metadata']['max_open_positions'], 1)

    def test_no_daily_cap_allows_reentry_after_exit_but_blocks_overlap(self):
        day = 86_400_000
        start = int(pd.Timestamp('2026-08-01 09:00', tz='UTC').value // 1_000_000)
        for side in (1, -1):
            for daily_limit in (True, False):
                with self.subTest(side=side, daily_limit=daily_limit):
                    intents = [signal(name, start + offset, start + offset + 100_000)
                               for name, offset in (('first', 0), ('overlap', 1000),
                                                    ('second', 4000), ('next_day', day))]
                    for item in intents:
                        item.update(direction='long' if side == 1 else 'short', stop=100-side*5)
                    model = PreparedModel(intents)
                    events = [];model.on_execution = events.append
                    session = Session(model, Config(1000, .01, 4), one_entry_per_utc_day=daily_limit)
                    ident = 0
                    def consume(offset, price, decide=False):
                        nonlocal ident
                        stamp = start + offset
                        if decide:
                            session.decide(lambda: model.evaluate_at(stamp, stamp), stamp)
                        ident += 1
                        session.tick(tick(stamp, ident, price))
                    consume(0, 100., True)
                    consume(1000, 100., True)
                    self.assertEqual(len(session.blocks), 1)
                    self.assertEqual(session.portfolios['prepared'].position['sample_id'], 'first')
                    consume(2000, 100-side*5)
                    consume(3000, 100-side*5.01)
                    self.assertFalse(model.positions)
                    equity = session.portfolios['prepared'].equity
                    consume(4000, 100., True)
                    if daily_limit:
                        self.assertFalse(model.positions)
                        self.assertTrue(any(e.get('reason') == 'daily_entry_limit' for e in events))
                    else:
                        position = session.portfolios['prepared'].position
                        self.assertEqual(position['sample_id'], 'second')
                        self.assertAlmostEqual(position['actual_initial_risk'], equity * .01)
                        consume(5000, 100-side*5)
                        consume(6000, 100-side*5.01)
                    consume(day, 100., True)
                    self.assertEqual(session.portfolios['prepared'].position['sample_id'], 'next_day')
                    self.assertNotIn(('prepared', 'overlap'), session.entries)
                    self.assertEqual(session.event_counts['entry_fill'], 2 if daily_limit else 3)

    def test_dynamic_flow_compares_both_sides_and_keeps_price_primary(self):
        import numpy as np
        from model_development.models.burst_breakout.strategy import interpret_dynamic_flow, confirm_flow_entries
        start = 24 * 3_600_000
        item = signal(eligible=start, force=start + 24 * 3_600_000)

        def row(favorable, opposing, before_favorable, before_opposing, *, minute=5,
                progress=.2, extreme=True, volume_rank=80.):
            return dict(
                sample_id=item['sample_id'], direction='long',
                assessment_timestamp_ms=start + minute * 60_000, elapsed_minutes=minute,
                path_complete=True, poc_touched=False, window_complete=True,
                close=101., progress_from_anchor_pct=1., progress_window_pct=progress,
                progress_bar_pct=.1, urgency_percentile=90., volume_percentile=volume_rank,
                favorable_aggressive_qty=float(favorable), opposing_aggressive_qty=float(opposing),
                favorable_qty_change=float(favorable-before_favorable),
                opposing_qty_change=float(opposing-before_opposing),
                delta_aligned_fraction=(favorable-opposing)/(favorable+opposing) if favorable+opposing else np.nan,
                new_favorable_extreme=extreme,
                initiative_candidate=False, absorption_candidate=False,
                exhaustion_candidate=opposing<before_opposing, low_volume_expansion=False,
            )

        withdrawal = pd.DataFrame([row(13989.52, 15112.94, 24501.46, 22406.03)])
        derived = interpret_dynamic_flow(withdrawal)
        self.assertTrue(derived.dynamic_support_withdrawing.iloc[0])
        self.assertLess(derived.favorable_qty_change_pct.iloc[0], derived.opposing_qty_change_pct.iloc[0])
        self.assertLess(derived.favorable_share_change_pp.iloc[0], 0)
        self.assertTrue(confirm_flow_entries(withdrawal, [item], start + 3_600_001)[0])
        self.assertFalse(confirm_flow_entries(withdrawal, [item], start + 3_600_001,
                                            flow_interpretation='dynamic')[0])
        for quantities, label in (
            ((90, 50, 60, 40), 'dynamic_initiative_candidate'),
            ((90, 50, 90, 50), 'dynamic_initiative_candidate'),
            ((40, 60, 100, 200), 'dynamic_exhaustion_candidate'),
            ((40, 80, 40, 60), 'dynamic_absorption_candidate'),
            ((10, 90, 0, 100), 'dynamic_exhaustion_candidate'),
        ):
            with self.subTest(quantities=quantities):
                trace = pd.DataFrame([row(*quantities)])
                entries, trace = confirm_flow_entries(trace, [item], start + 3_600_001,
                                                      flow_interpretation='dynamic')
                self.assertTrue(trace[label].iloc[0])
                self.assertEqual(entries[0]['confirmation_mechanisms'], [label])
                self.assertFalse(np.isinf(trace.select_dtypes(include='number').to_numpy()).any())
        for record in (row(70, 60, 60, 40), row(40, 80, 40, 60, extreme=False), row(10, 10, 0, 0)):
            self.assertFalse(confirm_flow_entries(pd.DataFrame([record]), [item], start + 3_600_001,
                                                 flow_interpretation='dynamic')[0])
        quiet = pd.DataFrame([
            row(200, 150, 300, 200, volume_rank=20.),
            row(80, 100, 200, 150, minute=10, progress=.4, volume_rank=20.),
        ])
        entries, trace = confirm_flow_entries(quiet, [item], start + 3_600_001, flow_interpretation='dynamic')
        self.assertFalse(trace.dynamic_low_volume_expansion.iloc[0])
        self.assertTrue(trace.dynamic_low_volume_expansion.iloc[1])
        self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + 10 * 60_000)
        # Absorption and low-volume candidates still need the common price gates.
        quiet['progress_bar_pct'] = -.1
        self.assertFalse(confirm_flow_entries(quiet, [item], start + 3_600_001,
                                             flow_interpretation='dynamic')[0])

    def test_dynamic_flow_is_causal_and_symmetric_with_all_timeframes_and_gates(self):
        import numpy as np
        from model_development.models.burst_breakout.strategy import assess_breakout_flow, confirm_flow_entries
        start = 24 * 3_600_000
        for side in (1, -1):
            prices = 100 + side * np.arange(1500) * .01
            prices[1440:] = prices[1439] + side * np.arange(1, 61) * .1
            favorable = np.r_[np.ones(1440) * 2, np.ones(60) * 8]
            opposing = np.ones(1500) * 3
            minutes = pd.DataFrame(dict(
                timestamp_ms=np.arange(1500, dtype='int64') * 60_000,
                open=prices, high=prices + .03, low=prices - .03, close=prices + side * .02,
                buy_volume=favorable if side == 1 else opposing,
                sell_volume=opposing if side == 1 else favorable,
            ))
            item = signal(eligible=start, force=start + 24 * 3_600_000)
            item.update(direction='long' if side == 1 else 'short',
                        stop=80. if side == 1 else 100., entry_reference=float(minutes.close.iloc[1439]))
            for step, window in ((1, 5), (3, 12), (5, 10)):
                with self.subTest(side=side, step=step):
                    raw = assess_breakout_flow(minutes, [item], step_minutes=step, window_minutes=window)
                    entries, decisions = confirm_flow_entries(raw, [item], start + 3_600_001,
                        flow_interpretation='dynamic', window_minutes=window)
                    self.assertEqual(entries[0]['entry_eligible_timestamp_ms'], start + window * 60_000)
                    gated, gate_trace = confirm_flow_entries(raw, [item], start + 3_600_001,
                        flow_interpretation='dynamic', window_minutes=window, entry_gate_minutes=window)
                    self.assertEqual(gated[0]['entry_eligible_timestamp_ms'], start + 2 * window * 60_000)
                    cutoff = start + 2 * window * 60_000
                    prefix = assess_breakout_flow(minutes[minutes.timestamp_ms < cutoff], [item],
                        step_minutes=step, window_minutes=window)
                    prefix_entries, prefix_trace = confirm_flow_entries(prefix, [item], start + 3_600_001,
                        flow_interpretation='dynamic', window_minutes=window, entry_gate_minutes=window)
                    self.assertEqual(gated, prefix_entries)
                    pd.testing.assert_frame_equal(
                        gate_trace[gate_trace.assessment_timestamp_ms <= cutoff].reset_index(drop=True),
                        prefix_trace[prefix_trace.assessment_timestamp_ms <= cutoff].reset_index(drop=True))
                    for column, value in (('poc_touched', True), ('path_complete', False)):
                        bad = raw.copy();bad.loc[bad.index[0], column] = value
                        self.assertFalse(confirm_flow_entries(bad, [item], start + 3_600_001,
                            flow_interpretation='dynamic', window_minutes=window)[0])

    def test_rm_a_ladder_and_next_print_stop_in_both_directions(self):
        from model_development.models.burst_breakout.strategy import rm_a_stop
        for side in (1, -1):
            for top_r, expected_lock in ((1, 0), (2, 1), (4, 2), (8, 4), (5, 2)):
                with self.subTest(side=side, top_r=top_r):
                    item = signal(force=9000)
                    item.update(direction='long' if side == 1 else 'short',
                                stop=100 - side * 5)
                    model = PreparedModel([item])
                    session = Session(model, Config(1000, .01, 4), risk_manager=rm_a_stop)
                    session.decide(lambda: model.evaluate_at(1000, 1000), 1000)
                    session.tick(tick(1000, 1, 100))
                    session.tick(tick(2000, 2, 100 + side * top_r * 5))
                    position = session.portfolios['prepared'].position
                    self.assertEqual(position['stop'], 100 + side * expected_lock * 5)
                    self.assertEqual(position['actual_initial_risk'], 10)
                    self.assertTrue(model.positions)
                    stop = position['stop']
                    session.tick(tick(3000, 3, stop))
                    self.assertTrue(model.positions)
                    session.tick(tick(3000, 4, stop - side * .01))
                    trade = session.portfolios['prepared'].trades[0]
                    self.assertEqual(trade['exit_reason'], 'risk_lock_stop_next_print')
                    self.assertEqual(trade['initial_stop'], item['stop'])
                    self.assertEqual(trade['final_stop'], stop)
                    self.assertEqual(trade['risk_lock_r'], expected_lock)
                    self.assertAlmostEqual(trade['actual_initial_risk'], 10)
                    if top_r == 1:
                        self.assertLess(trade['pnl'], 0)

    def test_rm_c_keeps_full_quantity_to_48h_or_trailing_stop(self):
        from model_development.models.burst_breakout.strategy import rm_a_stop
        hour = 3_600_000
        for side in (1, -1):
            for stopped in (False, True):
                with self.subTest(side=side, stopped=stopped):
                    item = signal(force=1000 + 48 * hour)
                    item.update(direction='long' if side == 1 else 'short',
                                stop=100 - side * 5, exit_after_fill_ms=48 * hour)
                    model = PreparedModel([item])
                    session = Session(model, Config(1000, .01, 4), risk_manager=rm_a_stop)
                    session.decide(lambda: model.evaluate_at(1000, 1010), 1010)
                    session.tick(tick(1010, 1, 100))
                    session.tick(tick(2010, 2, 100 + side * 5))
                    self.assertEqual(session.portfolios['prepared'].position['stop'], 100)
                    session.tick(tick(1010 + 24 * hour, 3, 100 + side * 10))
                    position = session.portfolios['prepared'].position
                    self.assertEqual(position['quantity'], 2)
                    self.assertNotIn('remaining_quantity', position)
                    self.assertNotIn('partial_fill', session.event_counts)
                    self.assertEqual(position['stop'], 100 + side * 5)
                    session.tick(tick(1010 + 25 * hour, 4, 100 + side * 20))
                    self.assertEqual(position['stop'], 100 + side * 10)
                    if stopped:
                        session.tick(tick(1010 + 26 * hour, 5, position['stop']))
                        session.tick(tick(1011 + 26 * hour, 6, position['stop'] - side * .01))
                    else:
                        session.tick(tick(1009 + 48 * hour, 5, 100 + side * 21))
                        self.assertTrue(session.model.positions)
                        session.tick(tick(1010 + 48 * hour, 6, 100 + side * 21))
                    trade = session.portfolios['prepared'].trades[0]
                    self.assertEqual(trade['quantity'], 2)
                    self.assertNotIn('partial_fill', session.event_counts)
                    self.assertEqual(trade['exit_reason'],
                                     'risk_lock_stop_next_print' if stopped else 'time_exit_next_print')
                    self.assertAlmostEqual(trade['gross_pnl_before_costs'], 19.98 if stopped else 42)
                    self.assertAlmostEqual(trade['fees'],
                        (trade['entry_price'] + trade['exit_price']) * 2 * .0004)
                    self.assertAlmostEqual(trade['pnl'], trade['gross_pnl_before_costs'] - trade['fees'])

    def test_rm_d_e_partial_ladder_and_remaining_time_exit(self):
        from model_development.models.burst_breakout.strategy import rm_a_stop, rm_e_stop
        hour = 3_600_000
        for side in (1, -1):
            for hours in (24, 48):
                for manager, first_stop_r in ((rm_a_stop, 0), (rm_e_stop, -1)):
                    with self.subTest(side=side, hours=hours, manager=manager.__name__):
                        item=signal(force=1000+hours*hour)
                        item.update(direction='long' if side==1 else 'short',stop=100-side*5,
                                    exit_after_fill_ms=hours*hour)
                        model=PreparedModel([item]);events=[];model.on_execution=events.append
                        session=Session(model,Config(1000,.01,4,tp1_r=1.,tp1_fraction=.1),risk_manager=manager)
                        session.decide(lambda:model.evaluate_at(1000,1010),1010)
                        session.tick(tick(1010,1,100.))
                        session.tick(tick(2000,2,100+side*5))
                        position=session.portfolios['prepared'].position
                        self.assertEqual(position['stop'],100+side*first_stop_r*5)
                        self.assertNotIn('remaining_quantity',position)
                        session.tick(tick(2001,3,100+side*5))
                        self.assertAlmostEqual(position['remaining_quantity'],position['quantity']*.9)
                        self.assertEqual(position['tp1_timestamp_ms'],2001)
                        self.assertEqual(position['actual_initial_risk'],10.)
                        session.tick(tick(3000,4,100+side*10))
                        self.assertEqual(position['stop'],100+side*5)
                        session.tick(tick(4000,5,100+side*20))
                        self.assertEqual(position['stop'],100+side*10)
                        session.tick(tick(1009+hours*hour,6,100+side*21))
                        self.assertTrue(model.positions)
                        session.tick(tick(1010+hours*hour,7,100+side*21))
                        trade=session.portfolios['prepared'].trades[0]
                        self.assertFalse(model.positions)
                        self.assertEqual(session.event_counts['partial_fill'],1)
                        self.assertEqual(trade['exit_reason'],'time_exit_next_print')
                        self.assertEqual(trade['tp1_fraction'],.1)
                        self.assertEqual(trade['tp1_r'],1.)
                        self.assertEqual(trade['initial_stop'],item['stop'])
                        self.assertAlmostEqual(trade['gross_pnl_before_costs'],.2*5+1.8*21)
                        expected_fee=(100*2+(100+side*5)*.2+(100+side*21)*1.8)*.0004
                        self.assertAlmostEqual(trade['fees'],expected_fee)
                        self.assertAlmostEqual(trade['pnl'],trade['gross_pnl_before_costs']-expected_fee)

    def test_rm_d_e_one_r_reversal_keeps_partial_and_distinct_stops(self):
        from model_development.models.burst_breakout.strategy import rm_a_stop, rm_e_stop
        for side in (1,-1):
            outcomes=[]
            for manager, lock_r in ((rm_a_stop,0),(rm_e_stop,-1)):
                item=dict(signal(),direction='long' if side==1 else 'short',stop=100-side*5)
                model=PreparedModel([item]);session=Session(model,Config(1000,.01,4,tp1_r=1.,tp1_fraction=.1),risk_manager=manager)
                session.decide(lambda:model.evaluate_at(1000,1000),1000)
                for stamp,ident,price in [(1000,1,100),(2000,2,100+side*5),(2001,3,100+side*5),
                                          (3000,4,100+side*lock_r*5),(3001,5,100+side*(lock_r*5-.01))]:
                    session.tick(tick(stamp,ident,price))
                trade=session.portfolios['prepared'].trades[0]
                self.assertEqual(trade['risk_lock_r'],lock_r)
                self.assertEqual(trade['exit_reason'],'risk_lock_stop_next_print' if lock_r==0 else 'initial_stop_next_print')
                self.assertAlmostEqual(trade['gross_pnl_before_costs'],1+1.8*(lock_r*5-.01))
                self.assertEqual(session.event_counts['partial_fill'],1)
                outcomes.append(trade['pnl'])
            self.assertGreater(outcomes[0],outcomes[1])

    def test_profile_windows_and_matching_p85_are_measured_and_cached_separately(self):
        import numpy as np
        from indicator.volume_profile import build_basic_volume_profile
        from model_development.models.burst_breakout.strategy import _profile_signals
        hour=3_600_000
        stamps=np.array([h*hour+offset for h in range(45) for offset in (0,1000)],dtype='int64')
        opens=np.array([100+h*.2 for h in range(45)])
        closes=opens+np.array([(h%7+1)*.1 for h in range(45)])
        prices=np.column_stack([opens,closes]).reshape(-1)
        qty=np.array([h+1. for h in range(45) for _ in range(2)])
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);raw=base/'UNIT-aggTrades-test.parquet'
            pq.write_table(pa.table(dict(timestamp=stamps,price=prices,qty=qty,agg_trade_id=np.arange(len(stamps)))),raw,row_group_size=11)
            cache_paths=[]
            for window in (12,16,24):
                info={};start=35*hour;end=40*hour
                entries=_profile_signals(raw,pd.DataFrame(),start,end,burst=True,cache_dir=base/'cache',cache_info=info,lookback_hours=window)
                features=pd.read_parquet(Path(info['path'])/'profile_features.parquet')
                row=features.iloc[0];opening=int(row.closing)-hour
                hours=np.arange(opening//hour-window,opening//hour)
                expected=build_basic_volume_profile(pd.DataFrame(dict(price=prices[(stamps>=opening-window*hour)&(stamps<opening)],qty=qty[(stamps>=opening-window*hour)&(stamps<opening)])),n_bins=50,value_area_pct=.70)
                self.assertAlmostEqual(row.body_p85,np.quantile(abs(closes[hours]-opens[hours]),.85))
                self.assertAlmostEqual(row.volume_p85,np.quantile(2*(hours+1),.85))
                for name,key in [('val','val'),('vah','vah'),('poc','poc_price')]:self.assertAlmostEqual(row[name],expected[key])
                self.assertFalse(info['reused']);cache_paths.append(info['path'])
                reused={}
                with patch('indicator.volume_profile.build_basic_volume_profile',side_effect=AssertionError('Profile cache must be reused')):
                    again=_profile_signals(raw,pd.DataFrame(),start,end,burst=True,cache_dir=base/'cache',cache_info=reused,lookback_hours=window)
                self.assertTrue(reused['reused']);self.assertEqual(entries,again)
            self.assertEqual(len(set(cache_paths)),3)

    def test_new_profile_rm_configurations_select_correct_clock_and_partial(self):
        from types import SimpleNamespace
        from model_development.models.burst_breakout.strategy import prepare_backtest,rm_a_stop,rm_e_stop
        for window in (12,16,24):
            for rm,hours in [('rm-a',24),('rm-b',48),('rm-c',48),('rm-d',24),('rm-d',48),('rm-e',24),('rm-e',48)]:
                config=dict(mode='profile_burst',setup_variant='b',lookback_hours=window,risk_management=rm,risk_exit_hours=hours,start_date='2026-10-01',end_date='2026-10-31')
                request=SimpleNamespace(model_config=config,input=Path('raw.parquet'),output_dir=Path('unused'),cache_dir=Path('cache'),id_policy='record',initial_equity=1000.,risk_fraction=.01,fee_bps=4.,max_leverage=5.)
                with self.subTest(window=window,rm=rm,hours=hours),patch('model_development.observation_cache.candle_cache',return_value=(pd.DataFrame(),dict(source_start_ms=0,source_end_ms=2_000_000_000_000))),patch('model_development.models.burst_breakout.strategy._profile_signals',return_value=[]) as profiles:
                    result=prepare_backtest(request)
                self.assertEqual(profiles.call_args.kwargs['lookback_hours'],window)
                self.assertEqual(result['metadata']['percentile_lookback_hours'],window)
                self.assertEqual(result['metadata']['final_exit_hours'],hours)
                partial=rm in ('rm-d','rm-e')
                self.assertEqual(result['config'].tp1_fraction,.1 if partial else None)
                self.assertEqual(result['config'].tp1_r,1. if partial else None)
                self.assertIs(result['risk_manager'],rm_e_stop if rm=='rm-e' else rm_a_stop)
                self.assertEqual(result['timed_scale_out'] is not None,rm=='rm-b')

    def test_rm_d_e_prepared_runner_records_partial_fees_and_exit_horizon(self):
        from types import SimpleNamespace
        hour=3_600_000;start=int(pd.Timestamp('2026-08-02',tz='UTC').value//1_000_000)
        end=start+3*24*hour
        for rm in ('rm-d','rm-e'):
            for hours in (24,48):
                with self.subTest(rm=rm,hours=hours),tempfile.TemporaryDirectory() as directory:
                    base=Path(directory);raw=base/'UNITUSDT-aggTrades-test.parquet'
                    stamps=[start-25*hour,start,start+1000,start+1001,start+2000,start+24*hour,start+48*hour,end-1]
                    pq.write_table(pa.table(dict(timestamp=stamps,price=[100.,100.,105.,105.,110.,115.,120.,120.],
                        qty=[1.]*8,agg_trade_id=list(range(8)))),raw)
                    config=dict(mode='profile_burst',setup_variant='b',lookback_hours=12,
                                risk_management=rm,risk_exit_hours=hours,start_date='2026-08-02',end_date='2026-08-04')
                    args=parser().parse_args(['--model',str(ROOT/'model_development/models/burst_breakout/strategy.py'),
                        '--input',str(raw),'--output-dir',str(base/'run'),'--cache-dir',str(base/'cache'),'--shared-core',str(CORE),
                        '--venue','synthetic','--product','linear_quote_margined','--quantity-unit','UNIT','--pnl-unit','USDT',
                        '--initial-equity','1000','--risk-fraction','.01','--fee-bps','4',
                        '--iteration','synthetic','--inspection-status','development','--model-config',json.dumps(config)])
                    item=signal(eligible=start,force=start+24*hour)
                    item.update(session_day='2026-08-02',exit_after_fill_ms=24*hour)
                    with patch('model_development.models.burst_breakout.strategy._profile_signals',return_value=[item]):
                        result=run_model(args)
                    self.assertEqual(result['closed_trades'],1)
                    trade=pq.read_table(base/'run/trades.parquet').to_pylist()[0]
                    self.assertEqual(trade['exit_timestamp_ms']-trade['entry_timestamp_ms'],hours*hour)
                    self.assertTrue(trade['tp1_filled']);self.assertEqual(trade['tp1_fraction'],.1)
                    self.assertAlmostEqual(trade['remaining_exit_quantity'],trade['quantity']*.9)
                    self.assertAlmostEqual(trade['net_pnl'],trade['gross_pnl']-trade['fees'])
                    self.assertEqual(result['loss_reward']['net_winning_trades'],1)
                    self.assertEqual(result['loss_reward']['total_net_wins'],trade['net_pnl'])
                    self.assertAlmostEqual(result['loss_reward']['average_initial_stop_distance_pct'],5.)

    def test_rm_b_partial_then_trailing_stop_and_stop_priority(self):
        from model_development.models.burst_breakout.strategy import rm_a_stop
        hour = 3_600_000
        for side in (1, -1):
            with self.subTest(side=side):
                item = signal(force=1000 + 48 * hour)
                item.update(direction='long' if side == 1 else 'short', stop=100 - side * 5)
                model = PreparedModel([item])
                session = Session(model, Config(1000, .01, 4),
                                  timed_scale_out=(24 * hour, 48 * hour, .5),
                                  risk_manager=rm_a_stop)
                session.decide(lambda: model.evaluate_at(1000, 1000), 1000)
                session.tick(tick(1000, 1, 100))
                session.tick(tick(2000, 2, 100 + side * 10))
                session.tick(tick(1000 + 24 * hour, 3, 100 + side * 15))
                position = session.portfolios['prepared'].position
                self.assertEqual(position['remaining_quantity'], position['quantity'] * .5)
                self.assertEqual(position['tp1_timestamp_ms'], 1000 + 24 * hour)
                self.assertEqual(position['actual_initial_risk'], 10)
                session.tick(tick(1000 + 25 * hour, 4, 100 + side * 20))
                self.assertEqual(position['stop'], 100 + side * 10)
                session.tick(tick(1000 + 26 * hour, 5, position['stop']))
                session.tick(tick(1001 + 26 * hour, 6, position['stop'] - side * .01))
                trade = session.portfolios['prepared'].trades[0]
                self.assertEqual(trade['remaining_exit_quantity'], 1)
                self.assertEqual(trade['exit_reason'], 'risk_lock_stop_next_print')
                self.assertEqual(trade['risk_lock_r'], 2)
                self.assertAlmostEqual(trade['gross_pnl_before_costs'], 24.99)
                self.assertAlmostEqual(trade['pnl'], 24.99 - trade['fees'])
                self.assertEqual(session.event_counts['partial_fill'], 1)

        model = PreparedModel([signal(force=1000 + 48 * hour)])
        session = Session(model, Config(1000, .01, 4),
                          timed_scale_out=(24 * hour, 48 * hour, .5), risk_manager=rm_a_stop)
        session.decide(lambda: model.evaluate_at(1000, 1000), 1000)
        session.tick(tick(1000, 1, 100))
        session.tick(tick(2000, 2, 105))
        session.tick(tick(1000 + 24 * hour, 3, 100))
        self.assertNotIn('partial_fill', session.event_counts)
        session.tick(tick(1001 + 24 * hour, 4, 99.99))
        self.assertFalse(session.portfolios['prepared'].trades[0]['tp1_filled'])
        self.assertEqual(session.portfolios['prepared'].trades[0]['remaining_exit_quantity'], 2)

    def test_rm_b_fast_replay_matches_every_print_for_stop_and_time_exit(self):
        import numpy as np
        from model_development.models.burst_breakout.strategy import rm_a_stop
        from model_development.observation_cache import RawPrints
        from model_development.phase_2.stateful import drive
        hour = 3_600_000
        stamps = 1000 + np.arange(48 * 60 + 1) * 60_000
        for side in (1, -1):
            for stopped in (False, True):
                with self.subTest(side=side, stopped=stopped), tempfile.TemporaryDirectory() as directory:
                    rewards = np.interp(stamps - 1000,
                        [0, hour, 2 * hour, 24 * hour, 25 * hour, 26 * hour, 48 * hour],
                        [0, 1, 2, 3, 4, 1 if stopped else 4.1, 1 if stopped else 4.2])
                    raw = Path(directory) / 'prints.parquet'
                    pq.write_table(pa.table(dict(
                        timestamp=stamps, price=100 + side * rewards * 5,
                        agg_trade_id=np.arange(len(stamps)),
                    )), raw, row_group_size=64)
                    sessions = []
                    for fast in (False, True):
                        item = signal(force=1000 + 48 * hour)
                        item.update(direction='long' if side == 1 else 'short', stop=100 - side * 5)
                        model = PreparedModel([item])
                        session = Session(model, Config(1000, .01, 4),
                            timed_scale_out=(24 * hour, 48 * hour, .5), risk_manager=rm_a_stop)
                        reader = RawPrints([raw])
                        try:
                            stats = drive(session, reader, [(1000, 1000)], 1000, int(stamps[-1]) + 1,
                                lambda opening, closing, ts: model.evaluate_at(closing, ts),
                                skip_inert_prints=fast)
                        finally:
                            reader.close()
                        if fast:
                            self.assertTrue(stats['skip_inert_prints'])
                            self.assertLess(stats['processed_prints'], len(stamps))
                        sessions.append(session)
                    for field in ('trades', 'orders', 'equity', 'position'):
                        self.assertEqual(getattr(sessions[0].portfolios['prepared'], field),
                                         getattr(sessions[1].portfolios['prepared'], field))
                    self.assertEqual(sessions[0].event_counts, sessions[1].event_counts)
                    trade = sessions[1].portfolios['prepared'].trades[0]
                    self.assertTrue(trade['tp1_filled'])
                    self.assertEqual(trade['remaining_exit_quantity'], trade['quantity'] * .5)
                    self.assertEqual(trade['exit_reason'],
                                     'risk_lock_stop_next_print' if stopped else 'time_exit_next_print')

    def test_skipped_prints_preserve_exact_replay_ledger(self):
        import numpy as np
        from model_development.observation_cache import RawPrints
        from model_development.phase_2.stateful import drive

        generator = np.random.default_rng(19)
        stamps = 1000 + np.arange(4000) // 2 * 10
        prices = 100 + generator.normal(0, 0.15, len(stamps)).cumsum()
        intentions = []
        for index in range(0, 3800, 190):
            eligible = int(stamps[index]) - 1
            side = 1 if index % 380 == 0 else -1
            item = signal(str(index), eligible, eligible + 700)
            item.update(direction='long' if side > 0 else 'short',
                        stop=float(prices[index] - side * 0.7),
                        entry_reference=float(prices[index]))
            if index % 570 == 0:
                item['target'] = float(prices[index] + side * 1.0)
            intentions.append(item)
        schedule = [(item['entry_eligible_timestamp_ms'], item['entry_eligible_timestamp_ms'])
                    for item in intentions]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prints.parquet'
            pq.write_table(pa.table(dict(
                timestamp=stamps, price=prices, agg_trade_id=np.arange(len(stamps)),
            )), path, row_group_size=64)
            from model_development.models.burst_breakout.strategy import rm_a_stop, rm_e_stop
            cases = ((Config(), False, None), (Config(), True, None),
                     (Config(), False, rm_a_stop),
                     (Config(tp1_r=0.5, tp1_fraction=0.5), False, None),
                     (Config(tp1_r=1., tp1_fraction=.1), False, rm_a_stop),
                     (Config(tp1_r=1., tp1_fraction=.1), False, rm_e_stop))
            for config, daily_limit, risk_manager in cases:
                sessions = []
                for optimized in (False, True):
                    model = PreparedModel(deepcopy(intentions))
                    session = Session(model, config, one_entry_per_utc_day=daily_limit, risk_manager=risk_manager)
                    reader = RawPrints([path])
                    try:
                        stats = drive(
                            session, reader, schedule, 999, int(stamps[-1]) + 1,
                            lambda opening, closing, ts: model.evaluate_at(closing, ts),
                            skip_inert_prints=optimized,
                        )
                    finally:
                        reader.close()
                    sessions.append(session)
                    if optimized:
                        self.assertTrue(stats['skip_inert_prints'])
                        self.assertLess(stats['processed_prints'], len(stamps))
                before, after = sessions
                for name in ('entries', 'blocks', 'event_counts', 'last_entry_day'):
                    self.assertEqual(getattr(before, name), getattr(after, name))
                for name in ('trades', 'orders', 'decisions', 'position', 'equity', 'equity_curve'):
                    self.assertEqual(getattr(before.portfolios['prepared'], name),
                                     getattr(after.portfolios['prepared'], name))

    def test_delayed_decision_preserves_availability_without_historical_fill(self):
        from model_development.phase_2.stateful import drive
        class Reader:
            groups=[None]
            def first(self,ts): return (0,0) if ts<=1005 else (1,0)
            def tick(self,ref): return tick(1005,1,100.)
            def next(self,ref): return (1,0)
        m=PreparedModel([signal()]);s=Session(m,Config())
        drive(s,Reader(),[(1000,1000)],1000,2000,lambda a,b,ts:m.evaluate_at(b,ts))
        entry=s.entries['prepared','one']
        self.assertEqual(entry['feature_available_at_ms'],1000)
        self.assertEqual(entry['decision_timestamp_ms'],1005)
        self.assertEqual(s.portfolios['prepared'].position['entry_timestamp_ms'],1005)


if __name__=='__main__':unittest.main()
