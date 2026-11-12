import datetime as dt
import json
from collections import defaultdict

from registry_bridge.errors import IntegrityFailure, InvalidMessage
from registry_bridge.feeds.message import identifier, integer, strict_object, timestamp

DAY = 86_400
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _microseconds(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    delta = parsed - EPOCH
    return (delta.days * DAY + delta.seconds) * 1_000_000 + delta.microseconds


def _day_ceiling(value):
    """Index of the first whole UTC day that starts at or after the instant."""
    micros = _microseconds(value)
    return -(-micros // (DAY * 1_000_000))


def _day_floor(value):
    micros = _microseconds(value)
    return micros // (DAY * 1_000_000)


def _merge(intervals):
    ordered = sorted(intervals)
    merged = []
    for start, end in ordered:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _full_days(intervals, week_start_day, week_end_day):
    """Whole days in [week_start_day, week_end_day) covered by any interval."""
    count = 0
    for start, end in intervals:
        first = max(week_start_day, _day_ceiling(start))
        last = min(week_end_day - 1, _day_ceiling(end) - 1)
        if last >= first:
            count += last - first + 1
    return count


def _asset_intervals(snapshots, window_end):
    """Observed and available whole-day intervals for one asset.

    Observed starts at registration and stops at revocation. Available is the
    union of active spans, capped by authorization expiry. Suspensions and
    mid-day changes therefore leave a partial day out of both counts.
    """
    ordered = sorted(snapshots, key=lambda item: (item['effective_at'], item['revision']))
    if not ordered:
        return [], []
    observed = []
    available = []
    observation_start = ordered[0]['effective_at']
    observation_end = window_end
    for index, snapshot in enumerate(ordered):
        start = snapshot['effective_at']
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        end = following['effective_at'] if following else window_end
        if snapshot['state'] == 'revoked':
            # Revocation is terminal: it ends observation at the revocation
            # instant, so later partial days cannot enter either count.
            observation_end = start
            break
        if snapshot['state'] == 'active':
            expiry = snapshot['scope']['expires_at']
            available.append((start, min(end, expiry)))
    observed.append((observation_start, observation_end))
    return _merge(observed), _merge(available)


def weekly_records(store, definition):
    """Rebuild issuer-week registration availability from applied events."""
    start = definition['start']
    weeks = definition['weeks']
    window_end = timestamp(definition['end'], 'end')
    event_ids = sorted(definition['event_ids'])
    rows = store.all(
        "SELECT event_id,issuer,asset_id,effective_at,revision FROM events "
        "WHERE disposition='applied' AND event_id IN ({})".format(
            ','.join('?' * len(event_ids)) or 'NULL',
        ),
        event_ids,
    ) if event_ids else []
    # A frozen event must still be applied; skipped or rejected rows are not part
    # of the observed registration history.
    present = {row['event_id'] for row in rows}
    missing = sorted(set(event_ids) - present)
    if missing:
        raise IntegrityFailure('Frozen event set is incomplete', context={'missing': missing[:20]})
    snapshots = {}
    for row in rows:
        stored = store.one('SELECT snapshot FROM history WHERE event_id=?', (row['event_id'],))
        if stored is None:
            raise IntegrityFailure('Applied event lacks a projected snapshot', context={'event_id': row['event_id']})
        snapshot = json.loads(stored['snapshot'])
        if snapshot['issuer'] != row['issuer'] or snapshot['asset_id'] != row['asset_id']:
            raise IntegrityFailure('Snapshot key disagrees with the event', context={'event_id': row['event_id']})
        snapshots.setdefault((row['issuer'], row['asset_id']), []).append(snapshot)

    records = defaultdict(dict)
    for (issuer, asset_id), asset_snapshots in sorted(snapshots.items()):
        observed, available = _asset_intervals(asset_snapshots, window_end)
        if not observed:
            continue
        for index in range(weeks):
            week_start_day = _day_ceiling(start) + 7 * index
            observed_days = _full_days(observed, week_start_day, week_start_day + 7)
            if observed_days == 0:
                continue
            available_days = _full_days(available, week_start_day, week_start_day + 7)
            item = records[issuer].setdefault(index, {'available_days': 0, 'observed_days': 0, 'assets': 0})
            item['available_days'] += available_days
            item['observed_days'] += observed_days
            item['assets'] += 1
    return {issuer: dict(sorted(by_week.items())) for issuer, by_week in records.items()}


def validate_definition(definition):
    from registry_bridge.study.design import validate_options
    if not isinstance(definition, dict) or definition.get('version') != 1:
        raise InvalidMessage('Unsupported study definition')
    for field in ('start', 'end', 'knowledge_cutoff'):
        timestamp(definition.get(field), field)
    fields = {'version', 'start', 'end', 'knowledge_cutoff', 'options', 'issuers', 'event_ids',
              'entries', 'entries_recorded_after_cutoff', 'launch_state', 'weeks', 'inputs'}
    strict_object(definition, fields, fields, 'definition')
    if not isinstance(definition['event_ids'], list) or len(definition['event_ids']) > 2_000_000:
        raise InvalidMessage('Study event set is invalid')
    if not isinstance(definition['issuers'], list) or not isinstance(definition['entries'], list):
        raise InvalidMessage('Study roster is invalid')
    for issuer in definition['issuers']:
        identifier(issuer, 'issuer')
    validate_options(definition['options'])
    grid = definition['weeks']
    integer(grid['weeks'], 'weeks', 1, 520)
    if not isinstance(grid['grid'], list) or len(grid['grid']) != grid['weeks']:
        raise InvalidMessage('Study week grid is inconsistent')
    return definition
