from registry_bridge.errors import InvalidMessage
from registry_bridge.feeds.message import digest, identifier, text, timestamp
from registry_bridge.ledger import audit, issuers


def decode(row):
    if row is None:
        return None
    result = dict(row)
    if 'note' in result:
        result['note'] = result['note'] or None
    return result


def record(store, issuer, launched_at, source, *, note=None, actor='operator'):
    identifier(issuer, 'issuer')
    launched_at = timestamp(launched_at, 'launched_at')
    source = text(source, 'source', 512)
    note = text(note, 'note', 2000) if note is not None else None
    entry_id = digest({
        'issuer': issuer, 'launched_at': launched_at, 'source': source, 'note': note,
    })
    with store.transaction():
        issuers.get(store, issuer)
        existing = store.one('SELECT * FROM rollout_entries WHERE entry_id=?', (entry_id,))
        if existing:
            return {**decode(existing), 'reused': True}
        cursor = store.execute(
            'INSERT INTO rollout_entries(entry_id,issuer,launched_at,source,note,recorded_at) '
            'VALUES(?,?,?,?,?,?)',
            (entry_id, issuer, launched_at, source, note, store.clock()),
        )
        audit.append(store, 'rollout.recorded', actor, {
            'entry_id': entry_id, 'issuer': issuer, 'launched_at': launched_at,
        })
        row = store.one('SELECT * FROM rollout_entries WHERE entry_seq=?', (cursor.lastrowid,))
        return {**decode(row), 'reused': False}


def entries(store, *, issuer=None):
    if issuer is not None:
        identifier(issuer, 'issuer')
        rows = store.all(
            'SELECT * FROM rollout_entries WHERE issuer=? ORDER BY entry_seq', (issuer,),
        )
    else:
        rows = store.all('SELECT * FROM rollout_entries ORDER BY entry_seq')
    return [decode(row) for row in rows]


def current(store, *, issuer=None, as_of=None):
    """Latest operator entry per issuer, in stable entry order."""
    params = []
    clauses = []
    if as_of is not None:
        clauses.append('recorded_at<=?')
        params.append(timestamp(as_of, 'as_of'))
    if issuer is not None:
        identifier(issuer, 'issuer')
        clauses.append('issuer=?')
        params.append(issuer)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = store.all(
        'SELECT r.* FROM rollout_entries r JOIN ('
        f'SELECT issuer,MAX(entry_seq) AS entry_seq FROM rollout_entries{where} GROUP BY issuer'
        ') latest ON latest.issuer=r.issuer AND latest.entry_seq=r.entry_seq ORDER BY r.issuer',
        params,
    )
    return [decode(row) for row in rows]


def latest_after(store, cutoff):
    """Count latest entries per issuer that were recorded after the cutoff.

    The study freezes the launch table as it was known at the knowledge cutoff.
    Entries recorded later stay in the ledger but are reported as excluded so a
    reader can see why an issuer looks untreated in an older study.
    """
    cutoff = timestamp(cutoff, 'cutoff')
    rows = store.all(
        'SELECT r.issuer,r.entry_id,r.launched_at,r.recorded_at FROM rollout_entries r JOIN ('
        'SELECT issuer,MAX(entry_seq) AS entry_seq FROM rollout_entries GROUP BY issuer'
        ') latest ON latest.issuer=r.issuer AND latest.entry_seq=r.entry_seq '
        'WHERE r.recorded_at>? ORDER BY r.issuer',
        (cutoff,),
    )
    return [decode(row) for row in rows]


def snapshot(store, issuers_list, *, as_of=None):
    """Freeze the latest entry per registered issuer as canonical records.

    ``as_of`` restricts the frozen table to entries recorded by that knowledge
    cutoff, so a later backfill or correction cannot leak into an older study.
    """
    latest = {row['issuer']: row for row in current(store, as_of=as_of)}
    frozen = []
    for issuer in sorted(issuers_list):
        row = latest.get(issuer)
        if row is None:
            continue
        frozen.append({
            'entry_id': row['entry_id'],
            'issuer': row['issuer'],
            'launched_at': row['launched_at'],
            'source': row['source'],
            'note': row['note'],
            'recorded_at': row['recorded_at'],
        })
    return frozen
