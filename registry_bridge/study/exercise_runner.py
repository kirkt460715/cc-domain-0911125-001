import datetime as dt

from registry_bridge.feeds.receiver import receive
from registry_bridge.ledger import issuers
from registry_bridge.study import design, exercise, studies


def run(store, *, name='rollout', options=None, seed=17):
    """Load the synthetic drill, then run the study through the normal path.

    The store clock is pinned just after the exercise window so the operator
    launch entries are visible at the study's knowledge cutoff; the study itself
    still freezes business time and knowledge time separately.
    """
    data = exercise.build(seed=seed)
    start_point = dt.datetime.fromisoformat(data['start'].replace('Z', '+00:00'))
    end_point = start_point + dt.timedelta(days=7 * data['weeks'])
    end = end_point.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    previous_clock = store.clock
    store.clock = lambda: end_point.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    try:
        for item in data['issuers']:
            issuers.add(store, item['issuer'], item['display_name'])
        for message in data['messages']:
            receive(store, message)
        for entry in data['entries']:
            studies.record_entry(store, entry['issuer'], entry['launched_at'],
                                 entry['source'], note=entry['note'])
        result = studies.create(
            store, name, data['start'], end, knowledge_cutoff=end,
            options={**design.DEFAULT_OPTIONS, 'min_inference_issuers': 5, **(options or {})},
        )
    finally:
        store.clock = previous_clock
    return {
        'study': name,
        'issuers': len(data['issuers']),
        'events': len(data['messages']),
        'comparison': result['result']['comparison'],
        'cohort_pretrend': result['result']['cohort_pretrend'],
        'warnings': result['result']['warnings'],
        'result_sha256': result['result_sha256'],
    }
