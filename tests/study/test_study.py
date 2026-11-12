import datetime as dt

from registry_bridge.errors import InvalidMessage, NotFound
from registry_bridge.feeds.receiver import receive
from registry_bridge.ledger import issuers
from registry_bridge.study import design, exercise, metrics, rollout, studies
from tests.support import LedgerCase, registration, transition

START = '2026-06-01T00:00:00Z'
CLOCK = '2026-11-11T00:00:00.000000Z'


def june_registration(*, asset='asset1', day=0, hour=0):
    value = registration(asset=asset)
    value['effective_at'] = (dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
                             + dt.timedelta(days=day, hours=hour)
                             ).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return value


def june_transition(sequence, revision, operation, *, day, hour=0, asset='asset1'):
    value = transition(sequence, revision, operation, asset=asset)
    value['effective_at'] = (dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
                             + dt.timedelta(days=day, hours=hour)
                             ).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return value


class RolloutRegistryTests(LedgerCase):
    def test_entries_are_append_only_and_duplicates_are_reused(self):
        first = rollout.record(self.store, 'north', '2026-06-20T00:00:00Z', 'operator-plan')
        second = rollout.record(self.store, 'north', '2026-06-20T00:00:00Z', 'operator-plan')
        self.assertEqual(first['entry_id'], second['entry_id'])
        self.assertTrue(second['reused'])
        corrected = rollout.record(self.store, 'north', '2026-06-27T00:00:00Z', 'operator-correction')
        latest = rollout.current(self.store, issuer='north')
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]['entry_id'], corrected['entry_id'])
        self.assertEqual(len(rollout.entries(self.store, issuer='north')), 2)

    def test_launch_lookups_do_not_change_registration_state(self):
        before = self.store.scalar('SELECT COUNT(*) FROM events')
        rollout.record(self.store, 'north', '2026-06-08T00:00:00Z', 'plan')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), before)
        with self.assertRaises(NotFound):
            rollout.record(self.store, 'unknown', '2026-06-08T00:00:00Z', 'plan')


class WeekGridTests(LedgerCase):
    def test_grid_requires_whole_weeks_from_start(self):
        self.assertEqual(design.week_grid(START, '2026-07-13T00:00:00Z')['weeks'], 6)
        with self.assertRaises(InvalidMessage):
            design.week_grid(START, '2026-07-12T00:00:00Z')
        with self.assertRaises(InvalidMessage):
            design.week_grid(START, '2026-06-08T00:00:00Z')

    def test_launch_week_is_business_time_not_receipt_time(self):
        grid = design.week_grid(START, '2026-07-13T00:00:00Z')
        self.assertEqual(
            design.assign_launch('2026-06-15T00:00:00Z', grid['start_day'], grid['weeks'])['treatment_week'], 2)
        # A mid-week launch still belongs to the week it started in.
        self.assertEqual(
            design.assign_launch('2026-06-18T00:00:00Z', grid['start_day'], grid['weeks'])['treatment_week'], 2)
        self.assertEqual(
            design.assign_launch('2026-05-31T00:00:00Z', grid['start_day'], grid['weeks'])['status'],
            'before_window')
        self.assertEqual(
            design.assign_launch('2026-08-01T00:00:00Z', grid['start_day'], grid['weeks'])['status'],
            'after_window')

    def test_options_are_frozen_and_bounded(self):
        bad = {**design.DEFAULT_OPTIONS, 'base_period': 0}
        with self.assertRaises(InvalidMessage):
            design.validate_options(bad)
        bad = {**design.DEFAULT_OPTIONS, 'control_rule': 'all_issuers'}
        with self.assertRaises(InvalidMessage):
            design.validate_options(bad)
        bad = {**design.DEFAULT_OPTIONS, 'bootstrap_replications': 20}
        with self.assertRaises(InvalidMessage):
            design.validate_options(bad)


class AvailabilityTests(LedgerCase):
    def _records(self, weeks=1, end='2026-06-08T00:00:00Z'):
        event_ids = [row['event_id'] for row in self.store.all(
            "SELECT event_id FROM events WHERE disposition='applied' ORDER BY event_id")]
        return metrics.weekly_records(self.store, {
            'start': START, 'end': end, 'weeks': weeks, 'event_ids': event_ids,
        })

    def test_whole_day_rule_excludes_partial_days(self):
        # Registration at 06:00 leaves that day partial, so it is not counted.
        receive(self.store, june_registration(hour=6))
        records = self._records()
        self.assertEqual(records['north'][0]['available_days'], 6)
        self.assertEqual(records['north'][0]['observed_days'], 6)

    def test_suspension_and_restore_do_not_count_partial_days(self):
        receive(self.store, june_registration())
        receive(self.store, june_transition(2, 2, 'suspend', day=2, hour=12))
        receive(self.store, june_transition(3, 3, 'restore', day=4, hour=12))
        records = self._records()
        self.assertEqual(records['north'][0]['observed_days'], 7)
        # Whole active days: Mon-Wed (3) plus Sat-Sun after Friday noon (2).
        self.assertEqual(records['north'][0]['available_days'], 5)

    def test_revocation_stops_observation_and_expiry_stops_availability(self):
        receive(self.store, june_registration())
        receive(self.store, june_transition(2, 2, 'revoke', day=3))
        short = june_registration(asset='asset2', day=1)
        short['sequence'] = 3
        short['event_id'] = 'north:event3'
        short['payload']['scope']['expires_at'] = '2026-06-04T00:00:00Z'
        receive(self.store, short)
        records = self._records()
        # asset1 is observed Jun 1-3 (3 days); asset2 is observed from Jun 2 to
        # the window end (6 days), but only Jun 2-3 are inside its authorization.
        self.assertEqual(records['north'][0]['observed_days'], 3 + 6)
        self.assertEqual(records['north'][0]['available_days'], 3 + 2)

    def test_knowledge_cutoff_excludes_later_corrections(self):
        self.clock.now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        receive(self.store, june_registration())
        self.clock.now = dt.datetime(2026, 6, 9, tzinfo=dt.timezone.utc)
        correction = june_transition(2, 2, 'amend', day=1, asset='asset1')
        receive(self.store, correction)
        rows = self.store.all(
            "SELECT event_id FROM events WHERE disposition='applied' AND applied_at<=?",
            ('2026-06-02T00:00:00Z',))
        self.assertEqual(len(rows), 1)
        records = metrics.weekly_records(self.store, {
            'start': START, 'end': '2026-06-08T00:00:00Z', 'weeks': 1,
            'event_ids': [row['event_id'] for row in rows],
        })
        self.assertEqual(records['north'][0]['assets'], 1)
        self.assertEqual(records['north'][0]['available_days'], 7)


class StudyTests(LedgerCase):
    def _seed(self, messages, entries):
        for message in messages:
            receive(self.store, message)
        for issuer, week in entries:
            launch = (dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
                      + dt.timedelta(weeks=week)).isoformat(timespec='microseconds')
            studies.record_entry(self.store, issuer, launch.replace('+00:00', 'Z'), 'plan')

    def test_study_freezes_event_set_launch_table_and_options(self):
        self._seed([june_registration()], [('north', 4)])
        study = studies.create(self.store, 'first', START, '2026-08-10T00:00:00Z',
                               knowledge_cutoff=CLOCK)
        self.assertEqual(study['definition']['options'], design.DEFAULT_OPTIONS)
        self.assertEqual(study['definition']['launch_state']['north']['treatment_week'], 4)
        frozen = study['definition']['event_ids']
        self.assertTrue(frozen)
        self.assertTrue(studies.verify(self.store, 'first')['ok'])
        self.assertTrue(studies.replay(self.store, 'first')['ok'])
        with self.assertRaises(InvalidMessage):
            studies.create(self.store, 'first', START, '2026-08-10T00:00:00Z',
                           knowledge_cutoff='2026-08-09T00:00:00Z')

    def test_launch_entry_after_cutoff_is_reported_not_silently_used(self):
        self._seed([june_registration()], [('north', 4)])
        study = studies.create(self.store, 'asof', START, '2026-08-10T00:00:00Z',
                               knowledge_cutoff='2026-08-10T00:00:00Z')
        self.assertEqual(study['definition']['launch_state']['north']['status'], 'never')
        self.assertEqual(len(study['definition']['entries_recorded_after_cutoff']), 1)

    def test_knowledge_cutoff_must_cover_the_business_window(self):
        with self.assertRaises(InvalidMessage):
            studies.create(self.store, 'early', START, '2026-08-10T00:00:00Z',
                           knowledge_cutoff='2026-07-01T00:00:00Z')

    def test_support_gaps_are_not_imputed(self):
        self._seed([june_registration()], [('north', 1)])
        study = studies.create(self.store, 'gap', START, '2026-08-10T00:00:00Z',
                               knowledge_cutoff=CLOCK,
                               options={**design.DEFAULT_OPTIONS, 'min_group_issuers': 2})
        overall = study['result']['overall']
        self.assertTrue(overall)
        self.assertTrue(all(row['did'] is None for row in overall))
        self.assertTrue(any(row['excluded_cohorts'] for row in overall))

    def test_too_few_issuers_restrict_inference(self):
        self._seed([june_registration()], [('north', 4)])
        study = studies.create(self.store, 'thin', START, '2026-08-10T00:00:00Z',
                               knowledge_cutoff=CLOCK,
                               options={**design.DEFAULT_OPTIONS, 'min_group_issuers': 1})
        restrictions = {row['code'] for row in study['result']['inference']['restrictions']}
        self.assertIn('too_few_issuers', restrictions)
        self.assertFalse(study['result']['overall'][-1]['simultaneous_interval'])


class ExerciseTests(LedgerCase):
    def test_drill_separates_early_improvement_from_launch_effect(self):
        from registry_bridge.study import exercise_runner
        report = exercise_runner.run(self.store, name='drill')
        study = studies.get(self.store, 'drill')
        result = study['result']
        self.assertEqual([row['cohort_week'] for row in result['cohorts']], [8, 12])
        early = next(row for row in result['cohort_pretrend'] if row['cohort_week'] == 8)
        late = next(row for row in result['cohort_pretrend'] if row['cohort_week'] == 12)
        # The early cohort improves before launch, so its pre-period check must
        # flag that instead of attributing the gain to the rollout.
        self.assertTrue(early['breaching_relative_weeks'])
        self.assertFalse(late['breaching_relative_weeks'])
        cohort = next(row for row in result['cohorts'] if row['cohort_week'] == 8)
        after = [row for row in cohort['comparisons'] if row['relative_week'] > 0]
        self.assertTrue(all(row['did'] == 0 for row in after))
        late_cohort = next(row for row in result['cohorts'] if row['cohort_week'] == 12)
        late_after = [row for row in late_cohort['comparisons'] if row['relative_week'] > 0]
        self.assertTrue(any(row['did'] and row['did'] > 0 for row in late_after))
        self.assertGreater(result['comparison']['naive_change'],
                           result['comparison']['did_post_average'])
        self.assertEqual(report['result_sha256'], study['result_sha256'])
