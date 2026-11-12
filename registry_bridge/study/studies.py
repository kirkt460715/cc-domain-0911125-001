import json

from registry_bridge.errors import Conflict, IntegrityFailure, InvalidMessage, NotFound
from registry_bridge.feeds.message import canonical, digest, identifier, timestamp
from registry_bridge.ledger import audit, issuers
from registry_bridge.study import design, metrics, rollout

RESULT_FIELDS = {'roster', 'checks', 'cohorts', 'overall', 'naive', 'comparison', 'pretrend',
                 'inference', 'issuers', 'warnings', 'interpretation'}
DEFINITION_FIELDS = {'version', 'start', 'end', 'knowledge_cutoff', 'options', 'issuers',
                     'event_ids', 'entries', 'launch_state', 'weeks'}


def _definition_sha(definition):
    return digest(definition)


def _scan_cutoff(store, cutoff):
    return store.all(
        "SELECT event_id,issuer,asset_id,effective_at,revision FROM events "
        "WHERE disposition='applied' AND applied_at<=? ORDER BY issuer,asset_id,effective_at,revision",
        (cutoff,),
    )


def _launch_table(entries):
    latest = {}
    for entry in entries:
        latest[entry['issuer']] = entry
    return latest


def create(store, name, start, end, *, knowledge_cutoff, options=None, actor='operator'):
    identifier(name, 'study')
    start = timestamp(start, 'start')
    end = timestamp(end, 'end')
    knowledge_cutoff = timestamp(knowledge_cutoff, 'knowledge_cutoff')
    if start >= end:
        raise InvalidMessage('Study window must be increasing')
    options = dict(design.DEFAULT_OPTIONS if options is None else options)
    design.validate_options(options)
    grid = design.week_grid(start, end)
    grid_end = grid['grid'][-1]['end']
    # Knowledge must be frozen before the end of business time so later
    # corrections cannot quietly change the study's outcome history.
    if knowledge_cutoff < grid_end:
        raise InvalidMessage('Knowledge cutoff precedes the study window end', context={
            'knowledge_cutoff': knowledge_cutoff, 'window_end': grid_end,
        })
    with store.transaction():
        roster = sorted(row['issuer'] for row in issuers.listing(store))
        cutoff_rows = _scan_cutoff(store, knowledge_cutoff)
        event_rows = store.scalar(
            "SELECT COUNT(*) FROM events WHERE disposition='applied' AND applied_at<=?",
            (knowledge_cutoff,),
        )
        if event_rows != len(cutoff_rows):
            raise IntegrityFailure('Frozen event scan is inconsistent')
        event_ids = sorted(row['event_id'] for row in cutoff_rows)
        entries = rollout.snapshot(store, roster, as_of=knowledge_cutoff)
        recorded_later = rollout.latest_after(store, knowledge_cutoff)
        launch_table = _launch_table(entries)
        launch_state = {}
        for issuer in roster:
            entry = launch_table.get(issuer)
            launch = entry['launched_at'] if entry else None
            launch_state[issuer] = {'issuer': issuer,
                                    **design.assign_launch(launch, grid['start_day'], grid['weeks'])}
        definition = {
            'version': 1,
            'start': start,
            'end': end,
            'knowledge_cutoff': knowledge_cutoff,
            'options': options,
            'issuers': roster,
            'event_ids': event_ids,
            'entries': entries,
            'entries_recorded_after_cutoff': recorded_later,
            'launch_state': launch_state,
            'weeks': {'start_day': grid['start_day'], 'weeks': grid['weeks'], 'grid': grid['grid']},
            'inputs': {'event_count': len(event_ids), 'entry_count': len(entries)},
        }
        previous = store.one('SELECT * FROM rollout_studies WHERE name=?', (name,))
        if previous:
            if previous['definition_sha256'] == _definition_sha(definition):
                return get(store, name)
            raise Conflict('Study name already describes a different frozen definition')
        records = metrics.weekly_records(store, {
            'start': start, 'end': grid_end, 'weeks': grid['weeks'], 'event_ids': event_ids,
        })
        result = design.compute_analysis(definition, records)
        result_sha = digest(result)
        store.execute(
            'INSERT INTO rollout_studies(name,definition,definition_sha256,result,result_sha256,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (name, canonical(definition), _definition_sha(definition), canonical(result), result_sha,
             store.clock()),
        )
        audit.append(store, 'study.created', actor, {
            'name': name, 'definition_sha256': _definition_sha(definition),
            'result_sha256': result_sha, 'knowledge_cutoff': knowledge_cutoff,
        })
    return get(store, name)


def decode(row):
    if row is None:
        return None
    result = dict(row)
    result['definition'] = json.loads(result['definition'])
    result['result'] = json.loads(result['result'])
    return result


def get(store, name):
    identifier(name, 'study')
    row = store.one('SELECT * FROM rollout_studies WHERE name=?', (name,))
    if row is None:
        raise NotFound('Study does not exist')
    return decode(row)


def stored_result(store, name):
    identifier(name, 'study')
    row = store.one('SELECT * FROM rollout_studies WHERE name=?', (name,))
    if row is None:
        raise NotFound('Study does not exist')
    return {
        'name': row['name'],
        'definition_sha256': row['definition_sha256'],
        'result_sha256': row['result_sha256'],
        'created_at': row['created_at'],
        'result': json.loads(row['result']),
    }


def verify(store, name):
    stored = stored_result(store, name)
    if digest(stored['result']) != stored['result_sha256']:
        raise IntegrityFailure('Stored study result does not match its digest', context={'name': name})
    return {'ok': True, 'name': name, 'definition_sha256': stored['definition_sha256'],
            'result_sha256': stored['result_sha256']}


def replay(store, name):
    """Recompute from the frozen event set, launch entries, options and seed."""
    stored = get(store, name)
    definition = stored['definition']
    metrics.validate_definition(definition)
    grid_end = definition['weeks']['grid'][-1]['end']
    records = metrics.weekly_records(store, {
        'start': definition['start'],
        'end': grid_end,
        'weeks': definition['weeks']['weeks'],
        'event_ids': definition['event_ids'],
    })
    result = design.compute_analysis(definition, records)
    if digest(result) != stored['result_sha256']:
        raise IntegrityFailure('Replay does not reproduce the stored study', context={
            'name': name, 'expected': stored['result_sha256'], 'actual': digest(result),
        })
    return {'ok': True, 'name': name, 'result_sha256': stored['result_sha256'],
            'event_count': len(definition['event_ids']),
            'launch_entries': len(definition['entries']),
            'seed': definition['options']['random_seed']}


def listing(store):
    rows = store.all(
        'SELECT name,definition_sha256,result_sha256,created_at,revision FROM rollout_studies ORDER BY name',
    )
    return rows


def record_entry(store, issuer, launched_at, source, *, note=None, actor='operator'):
    return rollout.record(store, issuer, launched_at, source, note=note, actor=actor)


def launch_entries(store, *, issuer=None):
    return rollout.entries(store, issuer=issuer)


def _cohort_lookup(studies, week):
    for study in studies:
        for cohort in study['result']['cohorts']:
            if cohort['cohort_week'] == week:
                return cohort
    return None


def cohort_layer(store, result, cohort_week):
    design.validate_options(result['definition']['options'])
    if isinstance(cohort_week, bool) or not isinstance(cohort_week, int):
        raise InvalidMessage('Cohort week must be an integer')
    cohort = next((row for row in result['result']['cohorts'] if row['cohort_week'] == cohort_week), None)
    if cohort is None:
        raise NotFound('Cohort does not exist in this study')
    return cohort


def week_layer(store, result, relative_week, *, cohort_week=None):
    if cohort_week is None:
        row = next((item for item in result['result']['overall']
                    if item['relative_week'] == relative_week), None)
        if row is None:
            raise NotFound('Relative week is not in the aggregated study')
        return row
    cohort = cohort_layer(store, result, cohort_week)
    row = next((item for item in cohort['comparisons'] if item['relative_week'] == relative_week), None)
    if row is None:
        raise NotFound('Relative week is outside this cohort sample')
    return row


def issuer_layer(store, result, issuer):
    identifier(issuer, 'issuer')
    row = next((item for item in result['result']['issuers'] if item['issuer'] == issuer), None)
    if row is None:
        raise NotFound('Issuer is not in this study roster')
    return row


def export_markdown(store, name):
    from registry_bridge.query import exporting
    return exporting.study_report(store, name)
