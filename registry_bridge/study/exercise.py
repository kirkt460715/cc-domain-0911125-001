"""Synthetic rollout study datasets for local exercises.

The generator only creates ledger events and operator launch entries; it never
writes study results. Two groups are planted:

* ``early`` issuers restore unavailable assets before launch and are flat
  afterwards. Their registration history therefore improves early, while the
  rollout adds nothing. A naive before/after comparison books that improvement
  as a rollout gain; the event study's pre-treatment points expose it.
* ``late`` issuers keep two assets unavailable through the pre-period and
  restore them just after launch, so the difference-in-differences estimate
  should recover a positive launch effect.

Never-treated issuers stay at full availability for the whole window, including
one revocation and one mid-week registration that should not move the ratio.
"""

import datetime as dt
import hashlib

DAY = dt.timedelta(days=1)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class IssuerBuilder:
    def __init__(self, issuer, display_name):
        self.issuer = issuer
        self.display_name = display_name
        self.sequence = 0
        self.messages = []

    def _message(self, asset, revision, operation, effective_at, payload):
        self.sequence += 1
        message = {
            'version': 1,
            'event_id': f'{self.issuer}:{self.sequence:05d}',
            'issuer': self.issuer,
            'sequence': self.sequence,
            'asset_id': asset,
            'revision': revision,
            'operation': operation,
            'effective_at': effective_at,
            'payload': payload,
        }
        self.messages.append(message)
        return message

    def register(self, asset, effective_at, *, expires_at='2032-01-01T00:00:00Z'):
        payload = {
            'title': 'Synthetic rollout dataset',
            'holder': 'holder1',
            'dataset_fingerprint': _digest(self.issuer + ':' + asset),
            'scope': {
                'purposes': ['research', 'repair'],
                'regions': ['CN'],
                'rights': ['hold', 'process'],
                'expires_at': expires_at,
            },
            'evidence': [{'kind': 'source_agreement', 'sha256': _digest('evidence')}],
        }
        return self._message(asset, 1, 'register', effective_at, payload)

    def suspend(self, asset, revision, effective_at):
        return self._message(asset, revision, 'suspend', effective_at,
                             {'reason': 'Synthetic suspension for exercise'})

    def restore(self, asset, revision, effective_at):
        return self._message(asset, revision, 'restore', effective_at,
                             {'reason': 'Synthetic restoration for exercise'})

    def revoke(self, asset, revision, effective_at):
        return self._message(asset, revision, 'revoke', effective_at,
                             {'reason': 'Synthetic revocation for exercise'})


def build(*, start='2026-06-01T00:00:00Z', weeks=20, early=2, late=2, controls=3, seed=17):
    """Return a fixture-shaped exercise dataset.

    ``start`` must be a Monday; weeks run from that day.
    """
    start_point = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
    if start_point.weekday() != 0:
        raise ValueError('Exercise start must be a Monday')
    if weeks < 16:
        raise ValueError('Exercise needs at least sixteen weeks')
    early_launch = 8
    late_launch = 12
    if late_launch + 4 > weeks:
        raise ValueError('Exercise window is too short for the planted launch weeks')

    def stamp(week, offset=0):
        point = start_point + DAY * (7 * week + offset)
        return point.astimezone(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

    issuers = []
    messages = []
    entries = []

    def finish(builder, entry=None):
        issuers.append({'issuer': builder.issuer, 'display_name': builder.display_name})
        messages.extend(builder.messages)
        if entry is not None:
            entry['sample_event_id'] = builder.messages[0]['event_id']
            entries.append(entry)

    for number in range(1, early + 1):
        builder = IssuerBuilder(f'early{number}', f'Early improver {number}')
        for index in range(6):
            builder.register(f'asset{index + 1}', stamp(0))
        # Two assets are unavailable from week one and return in week four.
        # Availability therefore improves before launch and stays flat after it.
        for asset in ('asset1', 'asset2'):
            builder.suspend(asset, 2, stamp(1))
            builder.restore(asset, 3, stamp(4))
        finish(builder, {
            'issuer': builder.issuer,
            'launched_at': stamp(early_launch, 2),
            'source': 'operator-rollout-plan',
            'note': 'Planted pre-launch improvement with no incremental rollout effect',
        })

    for number in range(1, late + 1):
        builder = IssuerBuilder(f'late{number}', f'Late improver {number}')
        for index in range(6):
            builder.register(f'asset{index + 1}', stamp(0))
        # Availability is flat at four of six through the pre-period, then
        # returns to full just after the launch week.
        for asset in ('asset1', 'asset2'):
            builder.suspend(asset, 2, stamp(1))
            builder.restore(asset, 3, stamp(13))
        finish(builder, {
            'issuer': builder.issuer,
            'launched_at': stamp(late_launch, 2),
            'source': 'operator-rollout-plan',
            'note': 'Planted improvement that starts after launch',
        })

    for number in range(1, controls + 1):
        builder = IssuerBuilder(f'control{number}', f'Comparison {number}')
        for index in range(6):
            builder.register(f'asset{index + 1}', stamp(0))
        if number == 2:
            # Revocation ends observation and availability together: ratio stays 1.
            builder.revoke('asset6', 2, stamp(5))
        if number == 3:
            # Mid-week registration starts observation on a partial day only.
            builder.register('asset7', stamp(3, 3))
        finish(builder)

    return {
        'start': stamp(0),
        'weeks': weeks,
        'issuers': issuers,
        'messages': messages,
        'entries': entries,
    }
