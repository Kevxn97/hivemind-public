import os
"""Real CLI journeys: bounded offers and revision-pinned evidence, with run evidence.

Failure cases fixed before implementation: Unicode/escaping exceeds a byte cap;
accumulated receipts bypass the cap; budget failures mutate a receipt; an old
revision silently returns latest content; an excerpt cannot be continued; changed
source bytes masquerade as historical evidence; private/outside/symlink sources
leak; untrusted headings or unknown refs select unintended content.
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
