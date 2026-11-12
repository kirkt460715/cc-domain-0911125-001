import datetime as dt
from collections import defaultdict

from registry_bridge.errors import InvalidMessage
from registry_bridge.feeds.message import integer
from registry_bridge.study.metrics import EPOCH, _day_floor

DEFAULT_OPTIONS = {
    'base_period': -1,
    'control_rule': 'not_yet_treated',
    'weight': 'treated_issuers',
    'confidence': 0.95,
    'bootstrap_replications': 500,
    'random_seed': 0,
    'min_group_issuers': 1,
    'min_inference_issuers': 6,
    'min_valid_replications': 100,
    'transition_week': 'include',
}


def _week_day(start_day, index):
    return start_day + 7 * index


def _day_timestamp(day):
    return (EPOCH + dt.timedelta(days=day)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def validate_options(options):
    if not isinstance(options, dict):
        raise InvalidMessage('Study options must be an object')
    unknown = set(options) - set(DEFAULT_OPTIONS)
    missing = set(DEFAULT_OPTIONS) - set(options)
    if unknown or missing:
        raise InvalidMessage('Study option fields do not match schema', context={
            'unknown': sorted(unknown), 'missing': sorted(missing),
        })
    base = options['base_period']
    if isinstance(base, bool) or not isinstance(base, int) or not -12 <= base <= -1:
        raise InvalidMessage('Base period must be a negative relative week', context={'field': 'base_period'})
    if options['control_rule'] not in {'not_yet_treated', 'never_treated'}:
        raise InvalidMessage('Unknown control rule', context={'field': 'control_rule'})
    if options['weight'] not in {'treated_issuers', 'treated_observed_days'}:
        raise InvalidMessage('Unknown aggregation weight', context={'field': 'weight'})
    confidence = options['confidence']
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.80 <= confidence <= 0.99:
        raise InvalidMessage('Confidence must be between 0.80 and 0.99', context={'field': 'confidence'})
    integer(options['bootstrap_replications'], 'bootstrap_replications', 50, 5000)
    integer(options['random_seed'], 'random_seed', 0, 2**31 - 1)
    integer(options['min_group_issuers'], 'min_group_issuers', 0, 100)
    integer(options['min_inference_issuers'], 'min_inference_issuers', 2, 10000)
    integer(options['min_valid_replications'], 'min_valid_replications', 10, 5000)
    if options['min_valid_replications'] > options['bootstrap_replications']:
        raise InvalidMessage('Minimum valid replications exceeds the bootstrap budget')
    if options['transition_week'] not in {'include', 'exclude'}:
        raise InvalidMessage('Unknown transition week policy', context={'field': 'transition_week'})
    return options


def week_grid(start, end):
    start_day = _day_floor(start)
    end_day = _day_floor(end)
    if end_day <= start_day:
        raise InvalidMessage('Study window must span at least one day')
    weeks = (end_day - start_day) // 7
    if weeks < 2:
        raise InvalidMessage('Study window must contain at least two whole weeks')
    if weeks > 520:
        raise InvalidMessage('Study window cannot exceed 520 weeks')
    if end_day - start_day - 7 * weeks:
        raise InvalidMessage('Study end must align to a whole-week boundary from start')
    return {
        'start_day': start_day,
        'weeks': weeks,
        'grid': [
            {'week': index, 'start': _day_timestamp(start_day + 7 * index),
             'end': _day_timestamp(start_day + 7 * (index + 1))}
            for index in range(weeks)
        ],
    }


def assign_launch(launch, start_day, weeks):
    """Classify an issuer by its operator launch week inside the business window."""
    if launch is None:
        return {'treatment_week': None, 'status': 'never', 'launch_week': None}
    launch_day = _day_floor(launch)
    relative_days = launch_day - start_day
    if relative_days < 0:
        return {'treatment_week': None, 'status': 'before_window', 'launch_week': None}
    week = relative_days // 7
    if week >= weeks:
        return {'treatment_week': weeks, 'status': 'after_window', 'launch_week': None}
    return {'treatment_week': week, 'status': 'treated', 'launch_week': week}


def _clean_float(value):
    if value is None:
        return None
    return round(value, 12)


def _ratio(numerator, denominator):
    if denominator == 0:
        return None
    return numerator / denominator


def _cell(issuer_records, members, weights, week, *, min_issuers):
    numerator = 0
    denominator = 0
    contributors = []
    for issuer in members:
        multiplicity = weights.get(issuer, 0)
        if multiplicity == 0:
            continue
        record = issuer_records.get(issuer, {}).get(week)
        if record is None or record['observed_days'] == 0:
            continue
        numerator += multiplicity * record['available_days']
        denominator += multiplicity * record['observed_days']
        contributors.append({
            'issuer': issuer,
            'multiplicity': multiplicity,
            'available_days': record['available_days'],
            'observed_days': record['observed_days'],
            'assets': record['assets'],
            'ratio': _clean_float(_ratio(record['available_days'], record['observed_days'])),
        })
    ratio = _clean_float(_ratio(numerator, denominator)) if denominator else None
    return {
        'issuers': len(contributors),
        'available_days': numerator,
        'observed_days': denominator,
        'ratio': ratio,
        'supported': len(contributors) >= min_issuers,
        'contributors': contributors,
    }


def _members(roster, cohort_week, weights, treatment_week):
    return [issuer for issuer in roster
            if weights.get(issuer, 0) and treatment_week.get(issuer) == cohort_week]


def _control_members(roster, launch_state, weights, comparison_week, base_week, cohort_week, rule):
    """Issuers untreated at both the comparison week and the base week.

    An issuer that launched before the comparison week (or at the base week) has
    left the comparison pool; earlier cohorts may not stand in as untreated
    controls for later ones.
    """
    last_checked = max(comparison_week, base_week)
    result = []
    for issuer in roster:
        if weights.get(issuer, 0) == 0:
            continue
        treatment_week = launch_state[issuer]['treatment_week']
        if treatment_week == cohort_week:
            continue
        if rule == 'never_treated':
            if treatment_week is not None:
                continue
        elif treatment_week is not None and treatment_week <= last_checked:
            continue
        result.append(issuer)
    return result


def compute_analysis(definition, issuer_records):
    options = definition['options']
    grid = definition['weeks']
    weeks = grid['weeks']
    start_day = grid['start_day']
    base_period = options['base_period']
    min_group = options['min_group_issuers']
    roster = sorted(
        issuer for issuer, state in definition['launch_state'].items()
        if state['status'] != 'before_window'
    )
    launch_state = {issuer: definition['launch_state'][issuer] for issuer in roster}
    treatment_week = {issuer: launch_state[issuer]['treatment_week'] for issuer in roster}
    cohorts = sorted({week for week in treatment_week.values() if week is not None and week < weeks})
    weights = {issuer: 1 for issuer in roster}

    def analyse(current_weights):
        cohort_rows = []
        for cohort in cohorts:
            treated_members = _members(roster, cohort, current_weights, treatment_week)
            comparisons = []
            lowest = max(-cohort, -6)
            highest = weeks - 1 - cohort
            for relative in range(lowest, highest + 1):
                comparison_week = cohort + relative
                base_week = cohort + base_period
                controls = _control_members(
                    roster, launch_state, current_weights, comparison_week, base_week, cohort,
                    options['control_rule'],
                )
                treated_cell = _cell(issuer_records, treated_members, current_weights, comparison_week, min_issuers=min_group)
                treated_base = _cell(issuer_records, treated_members, current_weights, base_week, min_issuers=min_group)
                control_cell = _cell(issuer_records, controls, current_weights, comparison_week, min_issuers=min_group)
                control_base = _cell(issuer_records, controls, current_weights, base_week, min_issuers=min_group)
                gaps = []
                if treated_cell['ratio'] is None:
                    gaps.append('treated_week_missing')
                elif treated_cell['issuers'] < min_group:
                    gaps.append('treated_week_too_few')
                if treated_base['ratio'] is None:
                    gaps.append('treated_base_missing')
                elif treated_base['issuers'] < min_group:
                    gaps.append('treated_base_too_few')
                if control_cell['ratio'] is None:
                    gaps.append('control_week_missing')
                elif control_cell['issuers'] < min_group:
                    gaps.append('control_week_too_few')
                if control_base['ratio'] is None:
                    gaps.append('control_base_missing')
                elif control_base['issuers'] < min_group:
                    gaps.append('control_base_too_few')
                did = None
                if not gaps:
                    did = (treated_cell['ratio'] - treated_base['ratio']) - (
                        control_cell['ratio'] - control_base['ratio'])
                weight = 0
                if not gaps:
                    weight = (treated_cell['issuers'] if options['weight'] == 'treated_issuers'
                              else treated_cell['observed_days'])
                comparisons.append({
                    'relative_week': relative,
                    'calendar_week': comparison_week,
                    'base_week': base_week,
                    'is_base_period': relative == base_period,
                    'treated': treated_cell,
                    'treated_base': treated_base,
                    'control': control_cell,
                    'control_base': control_base,
                    'control_rule': options['control_rule'],
                    'control_issuers_considered': len(controls),
                    'did': _clean_float(did),
                    'weight': weight,
                    'supported': not gaps,
                    'support_gaps': gaps,
                })
            cohort_rows.append({
                'cohort_week': cohort,
                'cohort_start': _day_timestamp(_week_day(start_day, cohort)),
                'treated_issuers': treated_members,
                'comparisons': comparisons,
            })
        overall = []
        overall_relative = sorted({row['relative_week'] for cohort in cohort_rows
                                   for row in cohort['comparisons'] if row['supported'] and not row['is_base_period']})
        overall_relative.append(base_period)
        overall_relative.sort()
        for relative in overall_relative:
            contributions = []
            excluded = []
            for cohort in cohort_rows:
                row = next((item for item in cohort['comparisons'] if item['relative_week'] == relative), None)
                if row is None:
                    excluded.append({
                        'cohort_week': cohort['cohort_week'],
                        'reason': 'relative_week_outside_sample',
                    })
                    continue
                if not row['supported']:
                    excluded.append({
                        'cohort_week': cohort['cohort_week'],
                        'reason': 'support_gap',
                        'support_gaps': row['support_gaps'],
                    })
                    continue
                contributions.append({
                    'cohort_week': cohort['cohort_week'],
                    'did': row['did'],
                    'weight': row['weight'],
                })
            total_weight = sum(row['weight'] for row in contributions)
            did = (sum(row['did'] * row['weight'] for row in contributions) / total_weight
                   if total_weight else None)
            overall.append({
                'relative_week': relative,
                'did': _clean_float(did),
                'is_base_period': relative == base_period,
                'cohorts': contributions,
                'excluded_cohorts': excluded,
                'weight_total': total_weight,
                'supported': did is not None,
            })
        return cohort_rows, overall

    cohort_rows, overall = analyse(weights)
    definition_checks = {
        'start': definition['start'],
        'end': definition['end'],
        'knowledge_cutoff': definition['knowledge_cutoff'],
        'window_start_day': _day_timestamp(start_day),
        'window_weeks': weeks,
        'roster_issuers': len(roster),
        'cohorts': cohorts,
        'relative_weeks': sorted({row['relative_week'] for row in overall}),
        'base_period': base_period,
        'control_rule': options['control_rule'],
        'weight': options['weight'],
        'excluded_launch_status': sorted(
            issuer for issuer, state in definition['launch_state'].items()
            if state['status'] == 'before_window'),
    }

    # Naive comparison: average level across all pre-launch weeks versus the
    # average after launch, with no comparison group. This is the operator
    # headline number and absorbs any common time trend.
    naive = []
    for cohort in cohorts:
        treated_members = _members(roster, cohort, weights, treatment_week)
        base_week = cohort + base_period
        pre_weeks = list(range(0, cohort))
        post_start = cohort + (1 if options['transition_week'] == 'exclude' else 0)
        post_weeks = list(range(post_start, weeks))

        def aggregate(weeks, members):
            rows = [issuer_records.get(issuer, {}).get(week) for issuer in members for week in weeks]
            return {'available_days': sum(row['available_days'] for row in rows if row),
                    'observed_days': sum(row['observed_days'] for row in rows if row)}

        pre = aggregate(pre_weeks, treated_members)
        post = aggregate(post_weeks, treated_members)
        pre_ratio = _clean_float(_ratio(pre['available_days'], pre['observed_days']))
        post_ratio = _clean_float(_ratio(post['available_days'], post['observed_days']))
        change = None if pre_ratio is None or post_ratio is None else post_ratio - pre_ratio
        per_issuer = []
        for issuer in treated_members:
            issuer_pre = aggregate(pre_weeks, [issuer])
            issuer_post = aggregate(post_weeks, [issuer])
            left_ratio = _clean_float(_ratio(issuer_pre['available_days'], issuer_pre['observed_days']))
            right_ratio = _clean_float(_ratio(issuer_post['available_days'], issuer_post['observed_days']))
            per_issuer.append({
                'issuer': issuer,
                'pre': {**issuer_pre, 'ratio': left_ratio},
                'post': {**issuer_post, 'ratio': right_ratio},
                'change': None if left_ratio is None or right_ratio is None else round(right_ratio - left_ratio, 12),
            })
        naive.append({
            'cohort_week': cohort,
            'treated_issuers': treated_members,
            'base_week_for_did': base_week,
            'pre_weeks': pre_weeks,
            'post_weeks': post_weeks,
            'pre': {**pre, 'ratio': pre_ratio},
            'post': {**post, 'ratio': post_ratio},
            'change': None if change is None else round(change, 12),
            'issuers': per_issuer,
        })

    # Inference: resample whole issuers so an issuer's week sequence stays intact.
    inference = {
        'method': 'issuer_cluster_bootstrap_sup_t',
        'resample_unit': 'issuer',
        'replications': options['bootstrap_replications'],
        'seed': options['random_seed'],
        'confidence': options['confidence'],
        'construction': {
            'description': (
                'Each replication draws issuers with replacement and recomputes every cohort and '
                'relative-week cell with expanded multiplicities. Pointwise bounds are percentile '
                'intervals of the bootstrap DiD; the simultaneous band uses the studentized '
                'sup-t critical value across the reported relative weeks.'
            ),
            'pointwise': 'percentile_interval',
            'simultaneous': 'sup_t_studentized_percentile',
        },
    }
    import random
    generator = random.Random(options['random_seed'])
    band_relatives = [row['relative_week'] for row in overall
                      if not row['is_base_period'] and row['supported']]
    boot_dids = {relative: [] for relative in band_relatives}
    boot_cohort_dids = {}
    for cohort in cohorts:
        for relative in band_relatives:
            boot_cohort_dids[(cohort, relative)] = []
    boot_rows = []
    boot_cohort_rows = []
    valid_replications = 0
    point_overall = {row['relative_week']: row['did'] for row in overall}
    for _ in range(options['bootstrap_replications']):
        if not roster:
            break
        sampled_weights = defaultdict(int)
        for _ in range(len(roster)):
            sampled_weights[roster[generator.randrange(len(roster))]] += 1
        try:
            sampled_cohorts, sampled_overall = analyse(dict(sampled_weights))
        except (KeyError, ValueError):
            continue
        sampled_values = {}
        for row in sampled_overall:
            if row['relative_week'] in boot_dids and row['did'] is not None:
                sampled_values[row['relative_week']] = row['did']
                boot_dids[row['relative_week']].append(row['did'])
        sampled_cohort_values = {}
        for cohort in sampled_cohorts:
            for comparison in cohort['comparisons']:
                key = (cohort['cohort_week'], comparison['relative_week'])
                if key in boot_cohort_dids and comparison['did'] is not None:
                    boot_cohort_dids[key].append(comparison['did'])
                    sampled_cohort_values[key] = comparison['did']
        boot_cohort_rows.append(sampled_cohort_values)
        if band_relatives and all(rel in sampled_values for rel in band_relatives):
            valid_replications += 1
            boot_rows.append(sampled_values)
    inference['valid_replications'] = valid_replications
    inference['minimum_valid_replications'] = options['min_valid_replications']

    sd = {}
    for relative in band_relatives:
        values = boot_dids[relative]
        if len(values) > 1:
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
            sd[relative] = variance ** 0.5
        else:
            sd[relative] = None
    inference['standard_errors'] = {str(rel): _clean_float(value) for rel, value in sd.items()}

    # Studentize each relative-week deviation by its bootstrap standard error,
    # then take the maximum across relative weeks per replication (sup-t).
    usable_relatives = [rel for rel in band_relatives if sd.get(rel)]
    sup_stats = []
    for sampled_values in boot_rows:
        statistics = [
            abs(sampled_values[rel] - point_overall[rel]) / sd[rel] for rel in usable_relatives
        ]
        if statistics:
            sup_stats.append(max(statistics))
    inference['sup_t_relative_weeks'] = usable_relatives
    inference['sup_t_observations'] = len(sup_stats)

    restrictions = []
    if len(roster) < options['min_inference_issuers']:
        restrictions.append({
            'code': 'too_few_issuers',
            'issuers': len(roster),
            'minimum': options['min_inference_issuers'],
        })
    if valid_replications < options['min_valid_replications']:
        restrictions.append({
            'code': 'too_few_valid_replications',
            'valid': valid_replications,
            'minimum': options['min_valid_replications'],
        })
    if not usable_relatives:
        restrictions.append({'code': 'no_estimable_relative_weeks'})
    inference['restrictions'] = restrictions
    inference['restricted'] = bool(restrictions)
    critical = None
    if not restrictions and sup_stats:
        ordered = sorted(sup_stats)
        position = min(len(ordered) - 1, max(0, int(round(options['confidence'] * (len(ordered) - 1)))))
        critical = ordered[position]
    inference['sup_t_critical'] = _clean_float(critical)

    lower_tail = (1 - options['confidence']) / 2
    for row in overall:
        relative = row['relative_week']
        row['interval'] = None
        row['simultaneous_interval'] = None
        if relative in boot_dids and boot_dids[relative]:
            values = sorted(boot_dids[relative])
            lo = values[min(len(values) - 1, int(lower_tail * (len(values) - 1)))]
            hi = values[min(len(values) - 1, int((1 - lower_tail) * (len(values) - 1)))]
            row['interval'] = {'lower': _clean_float(lo), 'upper': _clean_float(hi),
                               'kind': 'pointwise_percentile'}
            if critical is not None and sd.get(relative):
                row['simultaneous_interval'] = {
                    'lower': _clean_float(row['did'] - critical * sd[relative]),
                    'upper': _clean_float(row['did'] + critical * sd[relative]),
                    'kind': 'simultaneous_sup_t',
                }
    inference['restricted_relative_weeks'] = [
        row['relative_week'] for row in overall if row['simultaneous_interval'] is None
    ]

    pre_rows = [row for row in overall if row['relative_week'] < base_period]
    pre_breaches = [
        row['relative_week'] for row in pre_rows
        if row['simultaneous_interval'] and (
            row['simultaneous_interval']['lower'] > 0 or row['simultaneous_interval']['upper'] < 0)
    ]
    # A joint pre-treatment test: the largest studentized pre-period deviation
    # observed against the bootstrap distribution of that same maximum.
    pre_usable = [row['relative_week'] for row in pre_rows if sd.get(row['relative_week'])]
    pre_sup_stats = []
    pre_observed = None
    pre_critical = None
    pre_pvalue = None
    if pre_usable:
        pre_observed = max(abs(point_overall[rel]) / sd[rel] for rel in pre_usable)
        for sampled_values in boot_rows:
            if all(rel in sampled_values for rel in pre_usable):
                pre_sup_stats.append(max(
                    abs(sampled_values[rel] - point_overall[rel]) / sd[rel] for rel in pre_usable
                ))
        pre_pvalue = (sum(statistic >= pre_observed for statistic in pre_sup_stats) + 1) / (
            len(pre_sup_stats) + 1)
        if not restrictions and pre_sup_stats:
            ordered = sorted(pre_sup_stats)
            position = min(len(ordered) - 1,
                           max(0, int(round(options['confidence'] * (len(ordered) - 1)))))
            pre_critical = ordered[position]
    pretrend = {
        'relative_weeks': [row['relative_week'] for row in pre_rows],
        'comparisons': pre_rows,
        'any_simultaneous_interval_excludes_zero': bool(pre_breaches),
        'breaching_relative_weeks': pre_breaches,
        'max_absolute_did': _clean_float(max((abs(row['did']) for row in pre_rows), default=None)),
        'joint_test': {
            'statistic': _clean_float(pre_observed),
            'critical_value': _clean_float(pre_critical),
            'p_value': _clean_float(pre_pvalue),
            'observations': len(pre_sup_stats),
            'construction': 'sup_t_joint_bootstrap_max_abs_studentized_pre_period_did',
        },
        'supported': all(row['supported'] for row in pre_rows) if pre_rows else False,
        'interpretation': (
            'Absence of a significant pre-treatment difference does not prove parallel trends; '
            'the causal reading also requires no anticipatory response before launch.'
        ),
    }

    # Per-cohort pre-period diagnostics keep a single early-improving cohort
    # visible even when its contribution looks small in the aggregate.
    cohort_pretrend = []
    lower_tail = (1 - options['confidence']) / 2
    for cohort_week in cohorts:
        comparisons = next(
            item for item in cohort_rows if item['cohort_week'] == cohort_week)['comparisons']
        usable = [row['relative_week'] for row in comparisons
                  if not row['is_base_period'] and row['supported']
                  and len(boot_cohort_dids.get((cohort_week, row['relative_week']), [])) > 1]
        local_sd = {}
        for relative in usable:
            values = boot_cohort_dids[(cohort_week, relative)]
            mean = sum(values) / len(values)
            local_sd[relative] = (sum((value - mean) ** 2 for value in values)
                                  / (len(values) - 1)) ** 0.5
        cohort_sup = []
        for sampled in boot_cohort_rows:
            if all((cohort_week, relative) in sampled for relative in usable):
                statistics = [
                    abs(sampled[(cohort_week, relative)]
                        - next(row['did'] for row in comparisons if row['relative_week'] == relative))
                    / local_sd[relative]
                    for relative in usable if local_sd.get(relative)
                ]
                if statistics:
                    cohort_sup.append(max(statistics))
        cohort_critical = None
        if cohort_sup and not restrictions:
            ordered = sorted(cohort_sup)
            position = min(len(ordered) - 1,
                           max(0, int(round(options['confidence'] * (len(ordered) - 1)))))
            cohort_critical = ordered[position]
        for row in comparisons:
            relative = row['relative_week']
            row['interval'] = None
            row['simultaneous_interval'] = None
            values = boot_cohort_dids.get((cohort_week, relative), [])
            if values:
                ordered = sorted(values)
                row['interval'] = {
                    'lower': _clean_float(ordered[min(len(ordered) - 1, int(lower_tail * (len(ordered) - 1)))]),
                    'upper': _clean_float(ordered[min(len(ordered) - 1,
                                                       int((1 - lower_tail) * (len(ordered) - 1)))]),
                    'kind': 'pointwise_percentile',
                }
            if cohort_critical is not None and row['did'] is not None and local_sd.get(relative):
                row['simultaneous_interval'] = {
                    'lower': _clean_float(row['did'] - cohort_critical * local_sd[relative]),
                    'upper': _clean_float(row['did'] + cohort_critical * local_sd[relative]),
                    'kind': 'simultaneous_sup_t',
                    'critical_value': _clean_float(cohort_critical),
                }
        local = [(row, local_sd[row['relative_week']]) for row in comparisons
                 if row['relative_week'] < base_period and local_sd.get(row['relative_week'])]
        pre_observed = None
        pre_statistics = []
        pre_critical = None
        if local:
            pre_observed = max(abs(row['did']) / error for row, error in local)
            pre_relative = [row['relative_week'] for row, _ in local]
            for sampled in boot_cohort_rows:
                if not all((cohort_week, relative) in sampled for relative in pre_relative):
                    continue
                pre_statistics.append(max(
                    abs(sampled[(cohort_week, row['relative_week'])] - row['did']) / error
                    for row, error in local))
            if pre_statistics and not restrictions:
                ordered = sorted(pre_statistics)
                position = min(len(ordered) - 1,
                               max(0, int(round(options['confidence'] * (len(ordered) - 1)))))
                pre_critical = ordered[position]
        cohort_pretrend.append({
            'cohort_week': cohort_week,
            'relative_weeks': [row['relative_week'] for row, _ in local],
            'supported_pre_weeks': [row['relative_week'] for row in comparisons
                                    if row['relative_week'] < base_period and row['supported']],
            'max_absolute_did': _clean_float(max(abs(row['did']) for row, _ in local)) if local else None,
            'max_studentized_did': _clean_float(pre_observed),
            'breaching_relative_weeks': [
                row['relative_week'] for row, error in local
                if pre_critical is not None and abs(row['did']) / error > pre_critical
            ],
            'joint_test': None if pre_observed is None else {
                'statistic': _clean_float(pre_observed),
                'critical_value': _clean_float(pre_critical),
                'p_value': _clean_float((sum(value >= pre_observed for value in pre_statistics) + 1)
                                        / (len(pre_statistics) + 1)),
                'observations': len(pre_statistics),
                'construction': 'max_abs_studentized_pre_period_did',
            },
            'simultaneous_critical_value': _clean_float(cohort_critical),
            'interpretation': (
                'A flat pre-period without estimable variation cannot show a violation; it is '
                'consistent with, but does not establish, parallel trends or absence of anticipation.'
            ),
        })

    post_weeks = [row for row in overall if row['relative_week'] > 0 and row['supported']]
    if options['transition_week'] == 'exclude':
        post_weeks = [row for row in post_weeks if row['relative_week'] != 0]
    if post_weeks:
        weights_sum = sum(row['weight_total'] for row in post_weeks) or len(post_weeks)
        did_post = (sum(row['did'] * (row['weight_total'] or 1) for row in post_weeks) / weights_sum
                    if weights_sum else None)
    else:
        did_post = None
    naive_rows = [row for row in naive if row['change'] is not None]
    naive_weights = {
        row['cohort_week']: (len(row['treated_issuers'])
                             if options['weight'] == 'treated_issuers'
                             else row['post']['observed_days'])
        for row in naive_rows
    }
    total_naive_weight = sum(naive_weights.values())
    naive_post = (sum(row['change'] * naive_weights[row['cohort_week']] for row in naive_rows) / total_naive_weight
                  if total_naive_weight else None)
    comparison = {
        'naive_change': _clean_float(naive_post),
        'did_post_average': _clean_float(did_post),
        'difference': _clean_float(None if naive_post is None or did_post is None else naive_post - did_post),
        'naive_by_cohort': [
            {'cohort_week': row['cohort_week'], 'change': row['change'],
             'treated_issuers': len(row['treated_issuers'])}
            for row in naive
        ],
        'interpretation': (
            'The naive difference compares treated issuers before and after launch and absorbs any '
            'common time trend. The difference-in-differences estimate subtracts the contemporaneous '
            'change in the selected untreated comparison group. A large naive gain alongside a '
            'small DiD estimate points to pre-existing improvement rather than a rollout effect.'
        ),
    }

    warnings = []
    for restriction in restrictions:
        warnings.append({'code': restriction['code'], 'detail': restriction})
    for cohort in cohort_rows:
        missing = [row['relative_week'] for row in cohort['comparisons'] if not row['supported']]
        if missing:
            warnings.append({
                'code': 'cohort_support_gaps',
                'cohort_week': cohort['cohort_week'],
                'relative_weeks': missing,
            })
    if not pre_rows:
        warnings.append({'code': 'no_pre_treatment_comparison'})
    early = [issuer for issuer, state in launch_state.items()
             if state['status'] == 'treated' and state['launch_week'] == 0]
    if early:
        warnings.append({'code': 'cohort_without_base_week', 'issuers': early})

    issuer_layer = []
    for issuer in roster:
        issuer_layer.append({
            'issuer': issuer,
            'launch_week': launch_state[issuer]['launch_week'],
            'launch_status': launch_state[issuer]['status'],
            'weeks': [
                {
                    'week': week,
                    **record,
                    'ratio': _clean_float(_ratio(record['available_days'], record['observed_days'])),
                }
                for week, record in sorted(issuer_records.get(issuer, {}).items())
            ],
        })

    return {
        'roster': roster,
        'checks': definition_checks,
        'cohorts': cohort_rows,
        'overall': overall,
        'naive': naive,
        'comparison': comparison,
        'pretrend': pretrend,
        'cohort_pretrend': cohort_pretrend,
        'inference': inference,
        'issuers': issuer_layer,
        'warnings': warnings,
        'interpretation': (
            'Effect estimates are differences in registration availability share between the '
            'launch cohort and issuers not yet launched at that relative week, relative to the '
            'chosen base week. Cells lacking treated or control support are left as gaps, not '
            'imputed or extrapolated. Parallel trends and absence of anticipatory response remain '
            'assumptions; pre-treatment estimates can reveal violations but cannot confirm them.'
        ),
    }
