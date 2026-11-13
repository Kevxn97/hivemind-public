import os
"""Project routing and bounded context, using synthetic journalled fixtures."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hivemind'))
import hive
import project_context as context


def note(ident='demo', **fields):
    value = {'id': ident, 'type': 'project', 'cluster': 'projects/demo',
             'title': 'Demo project', 'summary': 'Current synthetic project overview.',
             'body': '# Demo\n\nGoal: preserve decisions with their evidence.',
             'status': 'stable', 'evidence': 'confirmed', 'sensitivity': 'internal',
             'aliases': [], 'tags': [], 'links': [],
             'sources': [{'id': 'fixture', 'resource': 'synthetic://project', 'title': 'Synthetic source'}],
             'verified': [], 'stale_after': '2100-01-01T00:00:00Z'}
    value.update(fields)
    return value


class ProjectContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'hive'
        self.root.mkdir()
        self.repo = self.base / 'code'
        self.repo.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, *items):
        old = hive.load_notes(self.root)
        payload = {'changes': [{'expected_revision': hive.digest(old[n['id']][1]) if n['id'] in old else 'new',
                                'note': n} for n in items]}
        hive.apply(self.root, payload, 'test/context', 'Synthetic context test', 'synthetic://context-test')

    def test_registration_routes_subfolders_and_preserves_canonical_revision(self):
        self.put(note())
        before = hive.load_notes(self.root)['demo'][1]
        self.assertTrue(context.register_project(self.root, self.repo, 'projects/demo')['changed'])
        self.assertFalse(context.register_project(self.root, self.repo, 'projects/demo')['changed'])
        (self.repo / 'src').mkdir()
        resolved = context.resolve_project(self.root, self.repo / 'src')
        self.assertEqual((resolved['cluster'], resolved['method']), ('projects/demo', 'registry'))
        self.assertEqual(hive.load_notes(self.root)['demo'][1], before)
        self.assertTrue(context.list_projects(self.root)[0]['exists'])
        self.assertTrue(context.unregister_project(self.root, self.repo)['unregistered'])
        self.assertIsNone(context.resolve_project(self.root, self.repo))

    def test_explicit_nested_mapping_wins_and_conflicting_same_path_is_rejected(self):
        nested = self.repo / 'nested'
        nested.mkdir()
        self.put(note(), note('nested', cluster='projects/nested'))
        context.register_project(self.root, self.repo, 'projects/demo')
        context.register_project(self.root, nested, 'projects/nested')
        self.assertEqual(context.resolve_project(self.root, nested / 'src')['cluster'], 'projects/nested')
        self.assertEqual(context.resolve_project(self.root, self.repo / 'other')['cluster'], 'projects/demo')
        with self.assertRaisesRegex(ValueError, 'already belongs'):
            context.register_project(self.root, self.repo, 'projects/nested')

    def test_registry_prefers_explicit_mapping_over_legacy_and_can_disable_fallback(self):
        self.put(note(body=f'Legacy project path: `{self.repo}`.'), note('other', cluster='projects/other'))
        self.assertEqual(context.resolve_project(self.root, self.repo)['method'], 'legacy')
        context.register_project(self.root, self.repo, 'projects/other')
        self.assertEqual(context.resolve_project(self.root, self.repo)['cluster'], 'projects/other')
        context.unregister_project(self.root, self.repo)
        (self.root / 'config').mkdir()
        (self.root / 'config/projects.json').write_text(json.dumps({'version': 1, 'legacy_fallback': False}))
        self.assertIsNone(context.resolve_project(self.root, self.repo))

    def test_unknown_inactive_and_global_routes_are_rejected(self):
        self.put(note(), note('old', cluster='projects/old', archived=True))
        for target, cluster in [(self.repo, 'projects/missing'), (self.repo, 'projects/old'),
                                (self.repo, 'knowledge'), (Path.home(), 'projects/demo'),
                                (self.base / 'missing', 'projects/demo')]:
            with self.subTest(target=target, cluster=cluster), self.assertRaises(ValueError):
                context.register_project(self.root, target, cluster)
        self.assertIsNone(context.resolve_project(self.root, self.repo))
        context.register_project(self.root, self.repo, 'projects/demo')
        old = copy.deepcopy(hive.load_notes(self.root)['demo'][0]); old['archived'] = True
        self.put(old)
        with self.assertRaisesRegex(ValueError, 'inactive'):
            context.resolve_project(self.root, self.repo)

    def test_task_selects_deep_passage_relevant_lesson_and_omits_unrelated_or_private(self):
        intro = 'General historical description without useful details. ' * 60
        self.put(note(body='# Demo\n\n' + intro + '\n\n## Wiederherstellung\n\nQuellenaufnahme bewahrt einen Checkpoint nach einem Worker-Abbruch.'),
                 note('restore', type='lesson', cluster='knowledge', title='Quellenaufnahme Wiederherstellung',
                      body='Eine Quellenaufnahme braucht Wiederherstellung aus Checkpoints.'),
                 note('design', type='decision', title='Farbgestaltung', body='Die Oberfläche bleibt blau.'),
                 note('private', type='lesson', cluster='personal', sensitivity='personal',
                      title='Quellenaufnahme Wiederherstellung', body='PERSONAL CONTENT MUST NOT LEAK'))
        result = context.task_brief(self.root, 'projects/demo', task='Quellenaufnahme Wiederherstellung', budget=500)
        ids = [n['id'] for n in result['selected']]
        self.assertIn('restore', ids)
        self.assertNotIn('design', ids)
        self.assertNotIn('private', ids)
        self.assertIn('Worker-Abbruch', result['text'])
        self.assertNotIn('PERSONAL CONTENT', result['text'])
        self.assertTrue(result['task_match'])
        for item in result['selected']:
            self.assertEqual(item['revision'], hive.digest(hive.load_notes(self.root)[item['id']][1]))
            self.assertEqual(item['sources'][0]['resource'], 'synthetic://project')

    def test_budget_includes_evidence_sources_and_headers(self):
        self.put(note(body='A useful project overview. ' * 200),
                 *(note('decision-' + str(i), type='decision', title='Decision ' + str(i), body='A choice with a reason. ' * 100) for i in range(6)))
        for budget in (120, 240, 500, 1200):
            with self.subTest(budget=budget):
                result = context.task_brief(self.root, 'projects/demo', budget=budget)
                self.assertLessEqual(len(result['text'].split()), budget)
                self.assertEqual(result['word_count'], len(result['text'].split()))
                self.assertTrue(result['selected'])
                self.assertIn('synthetic://project', result['text'])
                self.assertIn('kein Live-Stand', result['text'])

    def test_unknown_task_keeps_only_baseline_without_inventing_relevance(self):
        self.put(note(), note('decision', type='decision', title='Architecture', body='Use Markdown.'))
        result = context.task_brief(self.root, 'projects/demo', task='Was weißt du über Quantenwombat?', budget=400)
        self.assertEqual([n['id'] for n in result['selected']], ['demo'])
        self.assertFalse(result['task_match'])
        self.assertIn('Keine passenden Aufgabenbelege', result['text'])

    def test_stale_disputed_evidence_and_scope_remain_visible(self):
        self.put(note(evidence='disputed', stale_after='2000-01-01T00:00:00Z',
                      verified=[{'by': 'test', 'at': '2000-01-01T00:00:00Z', 'scope': 'Only the deployment snapshot'}]))
        result = context.task_brief(self.root, 'projects/demo', budget=250)
        self.assertIn('umstritten', result['text'])
        self.assertIn('Prüftermin überschritten', result['text'])
        self.assertIn('Only the deployment snapshot', result['text'])
        self.assertEqual(result['selected'][0]['evidence'], 'disputed')

    def handoff_note(self, **fields):
        data = {'evidence': 'reported', 'source_id': 'recent', 'observed_at': '2026-10-29T10:00:00Z',
                'project_state': 'Zeroflux ' + ('new unverified capabilities ' * 30),
                'decisions': [{'decision': 'Zebramuster-Prüfung vor Übernahme', 'reason': 'Verhindert doppelte Quittungen', 'status': 'agreed'}],
                'open_questions': ['Welche Quelle fehlt?'],
                'next_steps': [{'text': 'Checkpoint-Abnahme ausführen', 'status': 'proposed'}]}
        data.update(fields)
        return note(evidence='confirmed', handoff=data,
                    sources=[{'id': 'fixture', 'resource': 'synthetic://old', 'title': 'Old source'},
                             {'id': 'recent', 'resource': 'synthetic://recent-report', 'title': 'Recent report'}],
                    body='# Demo\n\n<!-- HIVEMIND:HANDOFF:START -->\n## Aktuelle Übergabe\n\n'
                         'Beobachtung: reported.\n\n### Projektstand\n\nZeroflux data.\n\n'
                         '<!-- HIVEMIND:HANDOFF:END -->\n\n## Historie\n\n' + 'Historical prose. ' * 10)

    def test_latest_handoff_keeps_decision_reason_and_next_step_before_history(self):
        self.put(self.handoff_note())
        result = context.task_brief(self.root, 'projects/demo', budget=1200)
        self.assertIn('Zebramuster-Prüfung', result['text'])
        self.assertIn('Verhindert doppelte Quittungen', result['text'])
        self.assertIn('Checkpoint-Abnahme ausführen', result['text'])
        self.assertIn('vorgeschlagen', result['text'])
        self.assertNotIn('<!--', result['text'])

    def test_small_budget_preserves_handoff_evidence_and_its_actual_source(self):
        self.put(self.handoff_note(decisions=[], open_questions=[], next_steps=[]))
        result = context.task_brief(self.root, 'projects/demo', task='Zeroflux', budget=120)
        self.assertLessEqual(result['word_count'], 120)
        self.assertIn('Aktuelle Übergabe: berichtet', result['text'])
        self.assertIn('Quelle der Übergabe: synthetic://recent-report', result['text'])
        self.assertIn('Zeroflux', result['text'])
        self.assertEqual(result['selected'][0]['handoff_evidence']['evidence'], 'reported')
        self.assertNotIn('synthetic://old', result['text'])

    def test_invalid_registry_and_budget_do_not_fall_through(self):
        self.put(note(body=f'`{self.repo}`'))
        (self.root / '.state').mkdir()
        (self.root / '.state/project-paths.json').write_text(json.dumps({'version': 1, 'mappings': [
            {'path': str(self.repo), 'cluster': 'projects/demo'}, {'path': str(self.repo), 'cluster': 'projects/demo'}]}))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            context.resolve_project(self.root, self.repo)
        for budget in (119, 2001, True, '1200'):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                context.task_brief(self.root, 'projects/demo', budget=budget)


if __name__ == '__main__':
    unittest.main()
