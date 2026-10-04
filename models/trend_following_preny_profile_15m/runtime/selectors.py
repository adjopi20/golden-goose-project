"""Existing outcome-free policy selection; thresholds are supplied, not fitted."""


def keep(row, policy):
    trend = row['trend_15m']
    state = row['effort_result_pre_entry']['setup_direction']
    labels = state['labels']
    initiative = 'initiative' in labels
    persistence = state['state'] == 'SUPPORT_PERSISTING'
    if policy == 'baseline': return True
    if policy in ('c1_delta', 'atr_activation'):
        plug = 'delta_without_result' if policy == 'c1_delta' else policy
        return row['research_gates'][plug]['gate'] is not False
    if policy == 'c2_initiative': return initiative
    if policy == 'c2_persistence': return persistence
    if policy == 'c2_union_i_p': return initiative or persistence
    if policy == 'c2_union_i_a_p': return initiative or persistence or 'possible_absorption' in labels
    if policy.startswith('ma_'): return trend['trade_alignment'] == policy[3:]
    if policy.startswith('ema_'):
        value = trend.get('price_vs_ema200_pct')
        sign = 1 if row['direction'] == 'long' else -1
        return value is None or sign*value >= float(policy[4:].replace('p', '.'))
    if policy.startswith('adx_'):
        value = trend.get('adx14')
        return value is None or value >= float(policy[4:])
    raise ValueError(policy)
