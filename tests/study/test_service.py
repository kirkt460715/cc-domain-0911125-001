import datetime as dt

from registry_bridge.errors import Conflict, InvalidMessage, NotFound
from registry_bridge.query import exporting
from registry_bridge.service import RegistryService
from registry_bridge.study import design, exercise, studies
from tests.support import LedgerCase, registration


class ServiceIntegrationTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.service = RegistryService(self.store)

    def _drill(self):
        from registry_bridge.study import exercise_runner
        exercise_runner.run(self.store, name='drill')
        return self.service

    def test_operations_expose_layers_and_raw_counts(self):
        service = self._drill()
        study = service.call('study.get', {'name': 'drill'})
        cohort = service.call('study.cohort', {'name': 'drill', 'cohort_week': 8})
        self.assertEqual(cohort['cohort_week'], 8)
        comparison = service.call('study.week', {
            'name': 'drill', 'relative_week': 1, 'cohort_week': 8})
        self.assertEqual(comparison['calendar_week'], 9)
        self.assertIn('contributors', comparison['treated'])
        aggregate = service.call('study.week', {'name': 'drill', 'relative_week': 1})
        self.assertIn('excluded_cohorts', aggregate)
        issuer = service.call('study.issuer', {'name': 'drill', 'issuer': 'early1'})
        self.assertEqual(issuer['launch_week'], 8)
        self.assertTrue(all('available_days' in week for week in issuer['weeks']))
        self.assertIn('definition', study)
        with self.assertRaises(NotFound):
            service.call('study.issuer', {'name': 'drill', 'issuer': 'absent'})
        with self.assertRaises(NotFound):
            service.call('study.week', {'name': 'drill', 'relative_week': 999})

    def test_rollout_entries_and_study_are_idempotent(self):
        first = self.service.call('rollout.record', {
            'issuer': 'north', 'launched_at': '2026-06-08T00:00:00Z', 'source': 'plan'})
        repeated = self.service.call('rollout.record', {
            'issuer': 'north', 'launched_at': '2026-06-08T00:00:00Z', 'source': 'plan'})
        self.assertEqual(first['entry_id'], repeated['entry_id'])
        self.assertTrue(repeated['reused'])
        entries = self.service.call('rollout.list', {})
        self.assertEqual(len(entries), 1)
        study = self.service.call('study.create', {
            'name': 'one', 'start': '2026-06-01T00:00:00Z', 'end': '2026-08-10T00:00:00Z',
            'knowledge_cutoff': '2026-08-10T00:00:00Z'})
        again = self.service.call('study.create', {
            'name': 'one', 'start': '2026-06-01T00:00:00Z', 'end': '2026-08-10T00:00:00Z',
            'knowledge_cutoff': '2026-08-10T00:00:00Z'})
        self.assertEqual(study['result_sha256'], again['result_sha256'])
        with self.assertRaises(Conflict):
            self.service.call('study.create', {
                'name': 'one', 'start': '2026-06-01T00:00:00Z', 'end': '2026-08-10T00:00:00Z',
                'knowledge_cutoff': '2026-08-10T00:00:00Z',
                'options': {**design.DEFAULT_OPTIONS, 'base_period': -2}})

    def test_new_study_after_correction_does_not_rewrite_old_one(self):
        self.service.call('rollout.record', {
            'issuer': 'north', 'launched_at': '2026-06-15T00:00:00Z', 'source': 'plan'})
        self._receive_registration()
        old = self.service.call('study.create', {
            'name': 'old', 'start': '2026-06-01T00:00:00Z', 'end': '2026-08-10T00:00:00Z',
            'knowledge_cutoff': '2026-11-11T00:00:00Z'})
        self.assertEqual(old['definition']['launch_state']['north']['treatment_week'], 2)
        self.service.call('rollout.record', {
            'issuer': 'north', 'launched_at': '2026-07-13T00:00:00Z', 'source': 'correction'})
        new = self.service.call('study.create', {
            'name': 'new', 'start': '2026-06-01T00:00:00Z', 'end': '2026-08-10T00:00:00Z',
            'knowledge_cutoff': '2026-11-11T00:00:00Z'})
        self.assertEqual(new['definition']['launch_state']['north']['treatment_week'], 6)
        self.assertEqual(
            self.service.call('study.replay', {'name': 'old'})['result_sha256'],
            old['result_sha256'])
        self.assertEqual(studies.get(self.store, 'old')['definition']['entries'][0]['launched_at'],
                         '2026-06-15T00:00:00.000000Z')

    def test_study_export_uses_existing_export_path(self):
        self._drill()
        path = self.path.parent / 'study.md'
        result = exporting.write_study(self.store, path, 'drill')
        self.assertEqual(result['study'], 'drill')
        text = path.read_text(encoding='utf-8')
        self.assertIn('naive change', text)
        self.assertIn('Pre-treatment checks', text)
        with self.assertRaises(Conflict):
            exporting.write_study(self.store, path, 'drill')

    def _receive_registration(self):
        from registry_bridge.feeds.receiver import receive
        receive(self.store, registration())
