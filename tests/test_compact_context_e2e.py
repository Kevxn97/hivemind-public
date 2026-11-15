import os
"""Real CLI journeys: bounded offers and revision-pinned evidence, with run evidence.

Failure cases fixed before implementation: Unicode/escaping exceeds a byte cap;
accumulated receipts bypass the cap; budget failures mutate a receipt; an old
revision silently returns latest content; an excerpt cannot be continued; changed
source bytes masquerade as historical evidence; private/outside/symlink sources
leak; untrusted headings or unknown refs select unintended content.
Natural-language glue crowds the topic out; generic and superseded notes beat
the decision; repeated provenance displaces usable facts at a startup budget.
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest

import test_agent_workflow_e2e as support
import agent_contract


class CompactContextE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.AgentWorkflowE2E.setUpClass.__func__(cls)
        cls.report['scope'] = 'Compact wire budgets and exact evidence through real CLI processes; synthetic local fixtures.'
        cls.report['source_sha256'][str(Path(__file__).relative_to(support.BASE.parent))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        cls.save_report()
    save_report = classmethod(support.AgentWorkflowE2E.save_report.__func__)
    setUp = support.AgentWorkflowE2E.setUp
    finish = support.AgentWorkflowE2E.finish
    run_cli = support.AgentWorkflowE2E.run_cli
    cli = support.AgentWorkflowE2E.cli
    apply = support.AgentWorkflowE2E.apply
    read = support.AgentWorkflowE2E.read

    def put(self, note):
        self.apply({'changes': [{'expected_revision': self.read()['revision'], 'note': note}]})

    def test_default_packet_bounds_entire_wire_on_both_adapters(self):
        note = self.read()['note']
        note['body'] += '\n\n## API retries\n\n' + ('Änderung 🐝 mit \\"Beleg\\". ' * 300)
        note['sources'] += [{'id': 's' + str(i), 'resource': 'test:' + 'x' * 500,
                             'title': 'Historischer Nachweis ' * 20} for i in range(35)]
        self.put(note)
        for adapter in ('hive.py', 'launcher.py'):
            for cap in (4096, 12000):
                args = ['session-open', 'projects/demo', '--task', 'API retries', '--max-bytes', str(cap)]
                if adapter == 'launcher.py': args += ['--json']
                packet = self.run_cli(adapter, args)
                wire = self.case['commands'][-1]['stdout'].encode('utf-8')
                self.assertLessEqual(len(wire), cap)
                self.assertEqual(packet['budget']['bytes'], len(wire))
                self.assertEqual(packet['view'], 'compact')
                agent_contract.validate('compact-session', packet)
                self.assertNotIn('workflow', packet)
                self.assertIn('demo@', packet['refs'][0]['ref'])
                self.assertTrue(packet['omitted'])
                full = self.cli('session-context', packet['session_id'], '--view', 'full')
                self.assertIn('workflow', full)
                self.assertGreater(len(self.case['commands'][-1]['stdout'].encode()), cap)
                self.assertEqual(len(full['workflow']['project']['sources']), 36)

    def test_refresh_keeps_old_feedback_and_rejects_invalid_budget_before_writes(self):
        opened = self.cli('session-open', 'projects/demo')
        old = opened['refs'][0]['ref']
        note = self.read()['note']; note['body'] += '\n\nNew revision.'; self.put(note)
        refreshed = self.cli('session-context', opened['session_id'], '--max-bytes', '4096')
        self.assertNotEqual(old, refreshed['refs'][0]['ref'])
        self.cli('session-feedback', opened['session_id'], '--outcome', 'helpful', '--used', old)
        receipt = self.root / '.state/sessions' / (opened['session_id'] + '.json')
        before = receipt.read_bytes()
        for flags in [('--max-bytes', '400'), ('--view', 'full', '--max-bytes', '4096')]:
            self.cli('session-context', opened['session_id'], *flags, ok=False)
            self.assertEqual(receipt.read_bytes(), before)

    def test_revision_sections_sources_and_continuation(self):
        source = self.root / 'sources' / 'observed.md'; source.parent.mkdir()
        source.write_text('Observed endpoint: Retry-After=7.\n' + 'ä🐝' * 10000)
        note = self.read()['note']
        note['body'] += '\n\n## Evidence\n\nOld claim: seven seconds.[^observed]\n\n## Other\n\nUnrelated.'
        note['sources'].append({'id': 'observed', 'resource': str(source), 'title': 'Endpoint observation',
                                'observed_at': '2026-11-12T12:00:00Z',
                                'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        self.put(note)
        old = self.read()['revision']; ref = 'demo@' + old
        note = self.read()['note']; note['body'] = note['body'].replace('seven seconds', 'nine seconds'); self.put(note)
        outline = self.cli('evidence', ref)
        agent_contract.validate('evidence', outline)
        self.assertEqual(outline['revision'], old)
        self.assertIn('Evidence', [s['title'] for s in outline['sections']])
        section = self.cli('evidence', ref, '--section', 'Evidence')
        self.assertIn('seven seconds', section['text']); self.assertNotIn('Unrelated', section['text'])
        first = self.cli('evidence', ref, '--source', 'observed', '--limit', '8000', '--max-bytes', '4096')
        self.assertEqual(first['source_state'], 'hash_matched')
        self.assertLessEqual(len(self.case['commands'][-1]['stdout'].encode()), 4096)
        self.assertTrue(first['next_offset'])
        second = self.cli('evidence', ref, '--source', 'observed', '--offset', str(first['next_offset']), '--limit', '10')
        self.assertEqual(second['text'], source.read_text()[first['next_offset']:first['next_offset'] + 10])
        source.write_text('CHANGED AND NOT HISTORICAL')
        changed = self.cli('evidence', ref, '--source', 'observed')
        self.assertEqual(changed['source_state'], 'changed')
        self.assertNotIn('CHANGED AND NOT HISTORICAL', json.dumps(changed))
        self.assertTrue(self.run_cli('hive.py', ['check'])['valid'])

    def test_evidence_boundaries_and_unknown_selectors(self):
        outside = self.root.parent / (self.root.name + '-outside.txt'); outside.write_text('PRIVATE_SENTINEL')
        self.addCleanup(outside.unlink)
        folder = self.root / 'sources'; folder.mkdir()
        link = folder / 'link.txt'; link.symlink_to(outside)
        note = self.read()['note']
        note['sources'] += [{'id': 'outside', 'resource': str(outside), 'title': 'Outside'},
                            {'id': 'link', 'resource': str(link), 'title': 'Symlink'},
                            {'id': 'web', 'resource': 'https://example.invalid/no-network', 'title': 'Web'}]
        note['body'] += '\n\n## Repeated\n\nA.\n\n## Repeated\n\nB.'
        self.put(note); ref = 'demo@' + self.read()['revision']
        events = self.run_cli('hive.py', ['check'])['events']
        for source in ('outside', 'link', 'web'):
            result = self.cli('evidence', ref, '--source', source)
            self.assertEqual(result['source_state'], 'reference_only')
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        for args in [(ref, '--section', 'Repeated'), (ref, '--source', 'missing'),
                     ('demo@' + '0' * 64,), ('../demo@' + '0' * 64,),
                     (ref, '--offset', '-1'), (ref, '--section', 'absent')]:
            self.cli('evidence', *args, ok=False)
        note = self.read()['note']; note['sensitivity'] = 'personal'; self.put(note)
        self.cli('evidence', ref, ok=False)
        self.assertEqual(self.run_cli('hive.py', ['check'])['events'], events + 1)

    def test_symlinked_source_directory_does_not_expand_source_access(self):
        outside = self.root.parent / (self.root.name + '-private.txt')
        outside.write_text('OUTSIDE_DIRECTORY_SENTINEL'); self.addCleanup(outside.unlink)
        (self.root / 'sources').symlink_to(self.root.parent, target_is_directory=True)
        note = self.read()['note']
        note['sources'].append({'id': 'escape', 'resource': str(self.root / 'sources' / outside.name), 'title': 'Escape'})
        self.put(note)
        result = self.cli('evidence', 'demo@' + self.read()['revision'], '--source', 'escape')
        self.assertEqual(result['source_state'], 'reference_only')
        self.assertNotIn('OUTSIDE_DIRECTORY_SENTINEL', json.dumps(result))

    def test_internal_source_alias_does_not_expose_operational_files(self):
        private = self.root / '.state' / 'private.json'; private.parent.mkdir(exist_ok=True)
        private.write_text('OPERATIONAL_SENTINEL')
        (self.root / 'sources').symlink_to(self.root, target_is_directory=True)
        note = self.read()['note']
        note['sources'].append({'id': 'escape', 'resource': 'sources/.state/private.json', 'title': 'Escape'})
        self.put(note)
        result = self.cli('evidence', 'demo@' + self.read()['revision'], '--source', 'escape')
        self.assertEqual(result['source_state'], 'reference_only')

    def test_compact_provenance_is_not_repeated_and_embedded_headings_survive(self):
        note = self.read()['note']
        note['body'] = '# Demo\n\nAPI retries: first fact.\n\nAPI retries: second fact.\n## Embedded\nEMBEDDED_FACT\n\nAPI retries: third fact.'
        self.put(note)
        packet = self.cli('session-open', 'projects/demo', '--task', 'API retries')
        self.assertEqual(packet['context']['text'].count('## Synthetic demo'), 1)
        self.assertIn('EMBEDDED_FACT', packet['context']['text'])
        plain = self.run_cli('hive.py', ['session-open', 'projects/demo'])
        agent_contract.validate('compact-session', plain)

    def add_notes(self, *notes):
        self.apply({'changes': [{'expected_revision': 'new', 'note': note} for note in notes]})

    def fixture_note(self, ident, title, summary, **fields):
        note = support.project(ident)
        note.update(type='decision', cluster='projects/demo', title=title, summary=summary, body=summary)
        note.update(fields)
        return note

    def test_bilingual_database_questions_find_the_current_decision_and_abstain(self):
        self.add_notes(
            self.fixture_note('storage-choice', 'Storage engine', 'Use SQLite. PostgreSQL was rejected for this pilot.',
                              links=[{'relation': 'supersedes', 'target': 'old-postgres', 'reason': 'Pilot architecture changed.'}]),
            self.fixture_note('old-postgres', 'PostgreSQL database decision', 'OLD_POSTGRES was approved.',
                              status='deprecated'),
            self.fixture_note('archived-postgres', 'PostgreSQL database decision', 'ARCHIVED_POSTGRES was approved.',
                              archived=True),
            self.fixture_note('database-glossary', 'Database glossary', 'PostgreSQL terms, useful for documentation.',
                              type='lesson'),
            self.fixture_note('planning', 'Decisions and questions', 'We decide how to carry out project planning.'))
        questions = ('What did we decide about Postgres?', 'which database do we use',
                     'Was haben wir zu Postgres entschieden?', 'Welche Datenbank verwenden wir?')
        for question in questions:
            rows = self.run_cli('hive.py', ['search', question, '--cluster', 'projects/demo'])
            with self.subTest(question=question):
                self.assertTrue(rows, question)
                self.assertEqual(rows[0]['id'], 'storage-choice')
                self.assertFalse({'old-postgres', 'archived-postgres', 'planning'} & {row['id'] for row in rows})
        for question in ('Which database do we use for Quantenwombat?', 'Was haben wir zu Quantenwombat entschieden?',
                         'What did we decide?', 'Welche Datenbank nutzen wir für Quantenwombat?'):
            with self.subTest(question=question):
                self.assertEqual(self.run_cli('hive.py', ['search', question, '--cluster', 'projects/demo']), [])
        packet = self.cli('session-open', 'projects/demo', '--task', questions[0], '--budget', '500')
        self.assertIn('Use SQLite. PostgreSQL was rejected', packet['context']['text'])
        self.assertNotIn('OLD_POSTGRES', packet['context']['text'])
        self.assertNotIn('ARCHIVED_POSTGRES', packet['context']['text'])
        unknown = self.cli('session-context', packet['session_id'], '--task', 'Quantenwombat', '--budget', '500')
        self.assertFalse(unknown['context']['task_match'])
        self.assertNotIn('Use SQLite', unknown['context']['text'])

    def test_startup_budget_favors_meaningful_note_coverage(self):
        facts = [
            'CHECKPOINT: Persist the checkpoint after each acknowledged batch so a crash resumes from the last verified cursor.',
            'THROTTLING: Respect the bounded retry window when the endpoint reports throttling and record each recovered retry separately.',
            'STORAGE: Open SQLite read-only for status requests and keep the ingestion writer inside its own transaction.',
            'EXPORT: Review the generated file locally before exporting and attach the immutable manifest for future comparison.',
            'HANDOFF: Keep the original source attribution when copying an older agreed decision into a fresh project handoff.',
        ]
        self.add_notes(*(self.fixture_note(
            f'feature-{i}', f'Pilot design {i + 1}', fact, aliases=['pilot design'], evidence=evidence,
            stale_after='2000-01-01T00:00:00Z' if i == 1 else '2100-01-01T00:00:00Z',
            verified=[{'by': 'test', 'at': '2026-10-29T10:00:00Z',
                       'scope': 'Only the dated synthetic observation of one development run on the original sample inputs.'}],
            sources=[{'id': f'fixture-{i}', 'resource': f'test:pilot/{i}/source/' + 'x' * 90,
                      'title': f'Original document for decision {i + 1}', 'observed_at': '2026-10-29T10:00:00Z'}],
        ) for i, (fact, evidence) in enumerate(zip(facts, ['reported', 'disputed', 'confirmed', 'reported', 'inferred']))))
        hook = self.run_cli('hive.py', ['brief', '--hook', '--agent', 'claude', '--budget', '500'],
                            stdin=json.dumps({'cwd': str(self.repo)}))['hookSpecificOutput']['additionalContext']
        compact = self.cli('session-open', 'projects/demo', '--budget', '500')
        contexts = {'hook': hook, 'compact': compact['context']['text']}
        self.case['startup_measurements'] = {
            name: {'bytes': len(value.encode('utf-8')), 'words': len(value.split()),
                   'meaningful_facts': [fact.split(':')[0] for fact in facts if fact in value]}
            for name, value in contexts.items()}
        self.case['startup_measurements']['compact']['wire_bytes'] = compact['budget']['bytes']
        self.save_report()
        for name, text in contexts.items():
            with self.subTest(view=name):
                self.assertEqual(self.case['startup_measurements'][name]['meaningful_facts'],
                                 [fact.split(':')[0] for fact in facts])
                self.assertIn('disputed', text)
                self.assertIn('Review overdue', text)
                self.assertNotIn('Selection:', text)
                self.assertNotIn('No separate verification documented.', text)
                self.assertNotIn('test:pilot/', text)
        ref = next(row['ref'] for row in compact['refs'] if row['ref'].startswith('feature-1@'))
        shown = self.run_cli('hive.py', ['show', 'feature-1'])
        self.assertEqual(ref, 'feature-1@' + shown['revision'])
        evidence = self.cli('evidence', ref)
        self.assertIn('Original document for decision 2', json.dumps(evidence))

    def test_plain_evidence_exposes_source_state_and_continuation(self):
        source = self.root / 'sources' / 'observed.txt'; source.parent.mkdir()
        source.write_text('Observed current bytes. ' * 50)
        note = self.read()['note']
        note['sources'].append({'id': 'current', 'resource': str(source), 'title': 'Unhashed'})
        self.put(note); ref = 'demo@' + self.read()['revision']
        import subprocess
        command = [sys.executable, str(support.BASE / 'launcher.py'), '--root', str(self.root),
                   'evidence', ref, '--source', 'current', '--limit', '10']
        p = subprocess.run(command, text=True, capture_output=True, timeout=20)
        self.case['commands'].append({'argv': command, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        self.save_report()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('unhashed_current_file', p.stdout)
        self.assertIn('next_offset', p.stdout)
        self.assertIn(ref, p.stdout)


if __name__ == '__main__':
    unittest.main()
