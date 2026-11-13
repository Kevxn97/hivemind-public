import os
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hivemind'))
import hive
import insights


def note(ident, **extra):
    value = {'id': ident, 'type': 'lesson', 'cluster': 'knowledge', 'title': ident,
             'summary': 'Synthetic knowledge for decision views.', 'body': '# Knowledge\n\nStored observation.',
             'status': 'stable', 'evidence': 'confirmed', 'sensitivity': 'internal',
             'aliases': [], 'tags': [], 'sources': [{'id': 'fixture', 'resource': 'synthetic://evidence', 'title': 'Synthetic evidence'}],
             'verified': [], 'stale_after': '2100-01-01T00:00:00Z', 'links': []}
    value.update(extra)
    return value


def link(target, relation='depends_on', reason='Recorded dependency'):
    return {'target': target, 'relation': relation, 'reason': reason}


class InsightsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, *notes, at='2026-01-01T10:00:00Z'):
        current = hive.load_notes(self.root)
        payload = {'changes': [{'expected_revision': hive.digest(current[n['id']][1]) if n['id'] in current else 'new', 'note': n} for n in notes]}
        with patch.object(hive, 'now', return_value=at):
            return hive.apply(self.root, payload, 'test-agent', 'Evidence changed', 'synthetic://task')

    def canonical(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for parent in ('wiki', '.history')
                for p in (self.root / parent).rglob('*') if p.is_file()}

    def test_explain_cites_authored_rationale_and_excludes_personal(self):
        decision = note('chosen-decision', type='decision', cluster='projects/demo',
                        body='# Decision\n\nOld context.\n\n## Begründung\n\nWe measured the latency.',
                        links=[link('safe-lesson'), link('secret-person', 'related', 'PRIVATE RELATION')])
        self.put(decision, note('safe-lesson'), note('secret-person', sensitivity='personal', title='PRIVATE TITLE'))
        before = self.canonical()
        result = insights.explain(self.root, 'chosen-decision')
        self.assertIn('We measured the latency.', result['items'][0]['excerpt'])
        self.assertNotIn('Old context', result['items'][0]['excerpt'])
        self.assertEqual(result['items'][1]['path'][0]['origin'], 'chosen-decision')
        self.assertEqual(result['items'][0]['revision'], hive.digest(hive.load_notes(self.root)['chosen-decision'][1]))
        self.assertIn('synthetic://evidence', result['text'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(before, self.canonical())
        with self.assertRaisesRegex(ValueError, 'persönlich'):
            insights.explain(self.root, 'secret-person')

    def test_impact_uses_incoming_dependencies_and_stops_cycles(self):
        self.put(note('assumption', links=[link('foundation'), link('project-two')]),
                 note('foundation'),
                 note('project-one', type='project', cluster='projects/one', links=[link('assumption')]),
                 note('project-two', type='project', cluster='projects/two', links=[link('project-one')]),
                 note('just-related', links=[link('assumption', 'related')]),
                 note('private-dependent', sensitivity='personal', links=[link('assumption')]))
        result = insights.impact(self.root, 'assumption')
        self.assertEqual([i['id'] for i in result['items']], ['project-one', 'project-two'])
        self.assertEqual([(e['origin'], e['target']) for e in result['items'][1]['path']],
                         [('project-one', 'assumption'), ('project-two', 'project-one')])
        self.assertEqual(len(insights.impact(self.root, 'assumption', depth=1)['items']), 1)
        self.assertNotIn('private-dependent', json.dumps(result))

    def test_reported_handoff_is_not_presented_as_confirmed_note_evidence(self):
        import sessions
        self.put(note('demo-project', type='project', cluster='projects/demo', evidence='confirmed'))
        opened = sessions.open_session(self.root, 'projects/demo')
        payload = sessions.draft_handoff(self.root, opened['session_id'])
        payload.update(project_state='New state reported by an earlier session.', evidence='reported')
        payload['source'].update(resource='synthetic://new-handoff', title='New scoped evidence', observed_at='2026-10-29T12:00:00Z')
        sessions.close_session(self.root, opened['session_id'], payload)
        result = insights.explain(self.root, 'demo-project')
        self.assertEqual(result['items'][0]['evidence'], 'confirmed')
        self.assertEqual(result['items'][0]['handoff_evidence'], 'reported')
        self.assertIn('Aktuelle Übergabe: berichtet', result['text'])
        self.assertIn('synthetic://new-handoff', result['text'])
        self.assertLess(result['text'].index('Aktuelle Übergabe: berichtet'), result['text'].index('New state reported'))

    def test_impact_reports_replacements_and_conflicts_without_transitive_guess(self):
        self.put(note('old-choice', status='deprecated'),
                 note('new-choice', links=[link('old-choice', 'supersedes')]),
                 note('objection', links=[link('old-choice', 'contradicts')]),
                 note('unrelated-effect', links=[link('objection')]))
        result = insights.impact(self.root, 'old-choice')
        self.assertEqual({i['id'] for i in result['items']}, {'new-choice', 'objection'})
        self.assertIn('ersetzt', result['text'])
        self.assertIn('Widerspruch', result['text'])

    def test_lessons_explain_paths_and_search_candidates_are_not_new_edges(self):
        self.put(note('demo-project', type='project', cluster='projects/demo', links=[link('other-project', 'related')]),
                 note('other-project', type='project', cluster='projects/other', links=[link('useful-lesson', 'related')]),
                 note('useful-lesson', cluster='projects/other'),
                 note('search-lesson'), note('private-lesson', sensitivity='personal'))
        before = self.canonical()
        with patch.object(hive, 'search', return_value=[{'id': 'search-lesson'}, {'id': 'private-lesson'}]):
            result = insights.lessons(self.root, 'projects/demo', task='latency')
        found = {i['id']: i for i in result['items']}
        self.assertEqual(len(found['useful-lesson']['path']), 2)
        self.assertEqual(found['search-lesson']['path'], [])
        self.assertIn('keine gespeicherte', found['search-lesson']['reasons'][0])
        self.assertNotIn('private-lesson', found)
        self.assertEqual(before, self.canonical())

    def test_changes_since_timestamp_is_scoped_bounded_and_keeps_review_dates(self):
        values = [note('demo-project', type='project', cluster='projects/demo')]
        values += [note('decision-' + str(i), type='decision', cluster='projects/demo', evidence='disputed') for i in range(7)]
        values += [note('private-note', sensitivity='personal', cluster='projects/demo'), note('other-project', cluster='projects/other')]
        self.put(*values)
        changed = copy.deepcopy(values[0]); changed['summary'] = 'A new stored fact.'
        self.put(changed, at='2026-01-03T12:00:00Z')
        before = self.canonical()
        result = insights.changes(self.root, 'projects/demo', since='2026-01-02T00:00:00Z')
        self.assertEqual(len(result['items']), 5)
        self.assertEqual(result['total_candidates'], 8)
        self.assertTrue(all(i['evidence'] == 'disputed' for i in result['items']))
        self.assertNotIn('private-note', json.dumps(result))
        self.assertNotIn('other-project', json.dumps(result))
        self.assertEqual(before, self.canonical())
        with self.assertRaises(ValueError):
            insights.changes(self.root, 'projects/demo', limit=6)

    def test_lessons_task_uses_real_search_contract(self):
        self.put(note('demo-project', type='project', cluster='projects/demo'),
                 note('latency-lesson', title='Latenzmessung', summary='Latenzmessung verhindert Verzögerungen.'))
        result = insights.lessons(self.root, 'projects/demo', task='Latenzmessung')
        self.assertEqual([i['id'] for i in result['items']], ['latency-lesson'])
        self.assertEqual(result['items'][0]['path'], [])

    def test_changes_defaults_to_actual_context_offered_not_edit_time(self):
        value = note('demo-project', type='project', cluster='projects/demo')
        self.put(value)
        value['summary'] = 'Changed after context offered.'
        self.put(value, at='2026-01-03T12:00:00Z')
        fake = types.SimpleNamespace(context_baseline=lambda root, cluster: {'at': '2026-01-02T00:00:00Z', 'session_id': 'test'},
                                     status=lambda root, ident: {'status':'saved','cluster':'projects/demo','opened_at':'2026-01-02T00:00:00Z'})
        with patch.dict(sys.modules, {'sessions': fake}):
            result = insights.changes(self.root, 'projects/demo')
        self.assertEqual(result['baseline'], 'last_context_offered')
        self.assertEqual(result['items'][0]['event']['at'], '2026-01-03T12:00:00Z')
        self.assertEqual(result['items'][0]['event']['reason'], 'Evidence changed')

    def test_changes_reports_newly_retired_decisions_but_not_old_archive(self):
        decision = note('old-decision', type='decision', cluster='projects/demo')
        self.put(decision, note('old-archive', cluster='projects/demo', archived=True))
        decision['status'] = 'deprecated'
        self.put(decision, at='2026-01-03T12:00:00Z')
        result = insights.changes(self.root, 'projects/demo', since='2026-01-02T00:00:00Z')
        self.assertEqual([i['id'] for i in result['items']], ['old-decision'])
        self.assertEqual(result['items'][0]['status'], 'deprecated')
        self.assertIn('überholt', result['text'])

    def test_fresh_session_does_not_hide_changes_since_previous_visit(self):
        import sessions
        self.put(note('demo-project', type='project', cluster='projects/demo'))
        with patch.object(sessions, '_now', return_value='2026-01-02T00:00:00Z'):
            previous = sessions.open_session(self.root, 'projects/demo', agent='codex')
        changed = note('demo-project', type='project', cluster='projects/demo', summary='Work saved between visits.')
        self.put(changed, at='2026-01-03T12:00:00Z')
        with patch.object(sessions, '_now', return_value='2026-01-04T00:00:00Z'):
            current = sessions.open_session(self.root, 'projects/demo', agent='claude')
        result = insights.changes(self.root, 'projects/demo')
        self.assertEqual(result['since'], previous['opened_at'])
        self.assertEqual(result['baseline'], 'previous_context_offered')
        self.assertEqual(result['items'][0]['id'], 'demo-project')
        explicit = insights.changes(self.root, 'projects/demo', session_id=current['session_id'])
        self.assertEqual(explicit['since'], previous['opened_at'])
        with self.assertRaises(ValueError):
            insights.changes(self.root, 'projects/demo', since=previous['opened_at'], session_id=current['session_id'])

    def test_views_fail_closed_on_unjournaled_changes(self):
        self.put(note('safe-note'))
        path = hive.load_notes(self.root)['safe-note'][2]
        path.write_text(path.read_text() + '\nAn unjournaled edit.\n')
        with self.assertRaisesRegex(ValueError, 'Unjournaled'):
            insights.explain(self.root, 'safe-note')


if __name__ == '__main__':
    unittest.main()
