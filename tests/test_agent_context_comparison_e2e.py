"""Exercise the comparison CLI with a real, tiny journaled HiveMind store."""
import atexit
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest


BASE = Path(os.environ.get('HIVE_COMPARISON_CODE_ROOT', Path(__file__).resolve().parents[1] / 'hivemind')).resolve()
RUNNER = Path(__file__).resolve().parents[1] / 'scripts/compare_agent_context.py'
OUT = Path(os.environ.get('HIVE_COMPARISON_E2E_OUTPUT',
                         Path(os.environ.get('HIVEMIND_EVIDENCE_DIR', str(RUNNER.parents[1] / 'output'))) / 'agent-context-comparison-e2e'))


def now():
    return datetime.now(timezone.utc).isoformat()


class AgentContextComparisonE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.out = OUT / stamp
        cls.out.mkdir(parents=True, mode=0o700)
        files = [*BASE.glob('*.py'), *BASE.glob('schemas/*.json'), RUNNER,
                 RUNNER.with_name('agent_context_cases.json'), Path(__file__)]
        cls.evidence = {
            'started_at': now(), 'outcome': 'running', 'command': [sys.executable, *sys.orig_argv[1:]],
            'cwd': str(Path.cwd()), 'code_root': str(BASE), 'runner': str(RUNNER),
            'environment': {'python': sys.version, 'platform': platform.platform()},
            'fixture': 'Each case creates a new synthetic journal with hive.py apply and a small local source.',
            'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()},
            'cases': [],
        }
        cls.save_evidence()
        atexit.register(cls.finalize, interrupted=True)

    @classmethod
    def save_evidence(cls):
        temp = cls.out / '.evidence.json.tmp'
        temp.write_text(json.dumps(cls.evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(cls.out / 'evidence.json')

    @classmethod
    def tearDownClass(cls):
        cls.finalize()
        atexit.unregister(cls.finalize)

    @classmethod
    def finalize(cls, interrupted=False):
        cases = cls.evidence['cases']
        for case in cases:
            if case['outcome'] == 'running':
                case['outcome'] = 'aborted'
        outcomes = {case['outcome'] for case in cases}
        cls.evidence.update(finished_at=now(), outcome='aborted' if interrupted or 'aborted' in outcomes
                            else 'failed' if 'failed' in outcomes else 'passed')
        cls.save_evidence()

    def invoke(self, argv, timeout):
        record = {'argv': argv, 'started_at': now(), 'outcome': 'running'}
        self.record['commands'].append(record)
        self.save_evidence()
        try:
            result = subprocess.run(argv, capture_output=True, timeout=timeout)
            record.update(outcome='ok' if result.returncode == 0 else 'failed', returncode=result.returncode,
                          stdout=result.stdout.decode(errors='replace'), stderr=result.stderr.decode(errors='replace'))
            return result
        except BaseException as error:
            record.update(outcome='aborted', error=type(error).__name__)
            raise
        finally:
            record['finished_at'] = now()
            self.save_evidence()

    def finish(self):
        result = self._outcome.result
        failures = [str(error) for test, error in result.failures + result.errors
                    if test is self or getattr(test, 'test_case', None) is self]
        outcome = 'aborted' if any(c['outcome'] == 'aborted' for c in self.record['commands']) else (
            'failed' if failures else 'passed')
        self.record.update(outcome=outcome, failures=failures, finished_at=now())
        self.save_evidence()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='hive-context-comparison-e2e-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.record = {'test': self.id(), 'outcome': 'running', 'started_at': now(),
                       'fixtures': {}, 'commands': []}
        self.evidence['cases'].append(self.record)
        self.addCleanup(self.finish)
        self.root, self.run = self.base / 'original', self.base / 'run'
        self.root.mkdir()
        self.source = self.root / 'sources/fixture.md'
        self.source.parent.mkdir()
        local_source = 'Ein absichtlich kleiner, lokaler Nachweis.\n'
        self.source.write_text(local_source, encoding='utf-8')
        self.record['fixtures']['sources/fixture.md'] = local_source
        (self.root / '.state').mkdir()
        (self.root / '.state/skip-me.json').write_text('"synthetic operational state"')
        self.record['fixtures']['.state/skip-me.json'] = 'synthetic operational state'
        self.note = {
            'id': 'hivemind', 'type': 'project', 'title': 'Synthetic continuity',
            'summary': 'Fixture for a local experiment.', 'body': '# Synthetic continuity\n\n## Local\n\nDer Beleg ist lokal.',
            'status': 'stable', 'evidence': 'confirmed', 'sensitivity': 'internal',
            'aliases': [], 'tags': [], 'links': [], 'cluster': 'projects/hivemind',
            'sources': [{'id': 'fixture', 'resource': 'sources/fixture.md', 'title': 'Local fixture'}],
            'created': '2026-01-01T00:00:00Z', 'stale_after': '2099-01-01T00:00:00Z',
            'generated': {'by': 'comparison-e2e', 'at': '2026-01-01T00:00:00Z'},
        }
        self.apply()
        self.spec = self.write('spec.json', {'version': 1, 'cluster': 'projects/hivemind',
            'sources': ['sources/fixture.md'], 'tasks': [{'id': 'fixture', 'question': 'Was ist lokal belegt?',
                'criteria': {'scope': 'Erkennt den Nachweis als lokal.'}}]})

    def write(self, name, value):
        path = self.base / name
        self.record['fixtures'].setdefault(name, []).append(json.loads(json.dumps(value)))
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def apply(self, expected='new'):
        patch = self.write('change.json', {'changes': [{'expected_revision': expected, 'note': self.note}]})
        p = self.invoke([sys.executable, str(BASE / 'hive.py'), '--root', str(self.root), 'apply',
                         str(patch), '--actor', 'test', '--reason', 'Comparison E2E fixture',
                         '--context', 'test:comparison'], timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode(errors='replace'))

    def compare(self, *args, ok=True):
        p = self.invoke([sys.executable, str(RUNNER), *map(str, args)], timeout=90)
        self.assertEqual(p.returncode == 0, ok, p.stderr.decode(errors='replace'))
        return p

    def prepare(self, ok=True):
        return self.compare('prepare', '--root', self.root, '--out', self.run, '--spec', self.spec,
                            '--code-root', BASE, ok=ok)

    def case(self, arm):
        return self.run / 'tasks/fixture' / arm

    def test_comparison_uses_same_snapshot_and_counts_real_followups_before_review(self):
        self.prepare()
        packet = {}
        for arm in ('full', 'compact'):
            p = self.case(arm) / 'initial.stdout'
            packet[arm] = json.loads(p.read_bytes())
            self.assertTrue(packet[arm]['context']['text'])
            self.assertIsNone(packet[arm].get('previous_context_at'))
            self.assertNotIn('Erkennt den Nachweis', (p.parent.parent / 'question.txt').read_text())
        revision = packet['full']['workflow']['project']['revision']
        ref = f'hivemind@{revision}'
        self.assertIn('Synthetic continuity', packet['full']['context']['text'])
        self.assertEqual({r['ref']: r['title'] for r in packet['compact']['refs']}[ref], 'Synthetic continuity')
        self.assertFalse((self.run / 'snapshot/.state/skip-me.json').exists())
        self.assertEqual((self.run / 'snapshot/sources/fixture.md').stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.run.stat().st_mode & 0o077, 0)

        self.note['body'] = '# Changed outside the run'
        self.apply(expected=revision)
        self.source.write_text('Changed outside the run')
        followup = self.compare('read', self.run, 'fixture', 'compact', ref, '--source', 'fixture')
        self.assertIn('absichtlich kleiner', followup.stdout.decode())
        invalid = self.compare('read', self.run, 'fixture', 'full', 'hivemind@' + '0' * 64,
                               '--legacy-show', ok=False)
        self.assertTrue(invalid.stderr)
        self.compare('report', self.run)
        report = json.loads((self.run / 'report.json').read_bytes())
        self.assertEqual(report['assessment']['status'], 'unscored')
        row = report['tasks']['fixture']
        for arm, followup_bytes in (('full', len(invalid.stderr)), ('compact', len(followup.stdout))):
            result = row[arm]
            initial_bytes = (self.case(arm) / 'initial.stdout').stat().st_size
            self.assertEqual(result['initial_bytes'], initial_bytes)
            self.assertEqual(result['followup_bytes'], followup_bytes)
            self.assertEqual(result['total_bytes'], initial_bytes + followup_bytes)
            self.assertEqual(result['followup_calls'], 1)
            self.assertGreater(result['cli_wall_ms'], 0)
        self.assertEqual(row['full']['followup_failures'], 1)
        self.assertEqual(row['compact']['followup_failures'], 0)
        self.assertTrue(report['technical']['paired_initials_complete'])

        for arm in ('full', 'compact'):
            answer = self.write(f'{arm}.json', {'answer': 'Nur lokal beobachtet.', 'refs': [ref]})
            self.compare('submit', self.run, 'fixture', arm, answer)
        grades = self.write('grades.json', {'reviewer': 'fixture-reviewer', 'method': 'human', 'entries': [
            {'task': 'fixture', 'arm': arm, 'criteria': {'scope': True}, 'comment': 'Local observation.'}
            for arm in ('full', 'compact')]})
        self.compare('grade', self.run, grades)
        self.compare('report', self.run)
        report = json.loads((self.run / 'report.json').read_bytes())
        self.assertEqual(report['assessment']['status'], 'reviewed')
        self.assertEqual(report['assessment']['reviewer'], 'fixture-reviewer')
        self.assertEqual(report['assessment']['totals']['compact']['criteria_met'], 1)
        self.assertEqual(report['assessment']['totals']['full']['criteria_met'], 1)

    def test_modified_frozen_rubric_cannot_receive_answers_grades_or_a_review_report(self):
        self.prepare()
        answer = self.write('answer.json', {'answer': 'Nur lokal beobachtet.', 'refs': []})
        grades = self.write('grades.json', {'reviewer': 'fixture-reviewer', 'method': 'agent', 'entries': [
            {'task': 'fixture', 'arm': 'full', 'criteria': {'scope': True}, 'comment': 'Original rubric.'}]})
        self.compare('submit', self.run, 'fixture', 'full', answer)

        frozen = self.run / 'spec.json'
        original = frozen.read_bytes()
        mutation = original.replace('Erkennt den Nachweis'.encode(), 'Belohnt eine andere Aussage'.encode())
        self.assertNotEqual(original, mutation)
        frozen.chmod(0o600)
        frozen.write_bytes(mutation)
        self.record['fixtures']['rubric_mutation'] = 'Criterion text changed while IDs stayed fixed.'
        for command in [('submit', self.run, 'fixture', 'compact', answer), ('grade', self.run, grades)]:
            with self.subTest(command=command[0]):
                p = self.compare(*command, ok=False)
                self.assertIn(b'Frozen task specification changed', p.stderr)
        self.assertFalse((self.case('compact') / 'answer.json').exists())
        self.assertFalse((self.run / 'grades.json').exists())

        frozen.write_bytes(original)
        self.compare('grade', self.run, grades)
        frozen.write_bytes(mutation)
        p = self.compare('report', self.run, ok=False)
        self.assertIn(b'Frozen task specification changed', p.stderr)
        self.assertFalse((self.run / 'report.json').exists())

    def test_interrupted_snapshot_leaves_failure_evidence_and_no_packets(self):
        (self.root / '.pending.json').write_text('{}')
        self.record['fixtures']['.pending.json'] = {}
        self.prepare(ok=False)
        receipt = json.loads((self.run / 'manifest.json').read_bytes())
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('pending', receipt['error'].lower())
        self.assertEqual(receipt['spec_sha256'], hashlib.sha256(self.spec.read_bytes()).hexdigest())
        self.assertFalse((self.case('full') / 'initial.stdout').exists())
        self.compare('report', self.run)
        report = json.loads((self.run / 'report.json').read_bytes())
        self.assertEqual(report['run_status'], 'failed')
        self.assertFalse(report['technical']['paired_initials_complete'])


if __name__ == '__main__':
    unittest.main()
