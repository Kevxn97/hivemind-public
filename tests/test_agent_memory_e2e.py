"""Agent memory through the CLI and the local host adapters."""
import hashlib
from pathlib import Path
import unittest

from hivemind import agent_contract
import test_session_runtime_e2e as runtime


class AgentMemoryE2E(unittest.TestCase):
    setUp = runtime.SessionRuntimeE2E.setUp
    finish = runtime.SessionRuntimeE2E.finish
    save_report = classmethod(runtime.SessionRuntimeE2E.save_report.__func__)
    run_cli = runtime.SessionRuntimeE2E.run_cli
    cli = runtime.SessionRuntimeE2E.cli
    apply = runtime.SessionRuntimeE2E.apply
    hook = runtime.SessionRuntimeE2E.hook

    @classmethod
    def setUpClass(cls):
        runtime.SessionRuntimeE2E.setUpClass.__func__(cls)
        cls.report['scope'] = 'Agent memory, sharing and explicit ranking feedback through synthetic CLI and hook inputs.'
        path = Path(__file__)
        cls.report['source_sha256']['tests/' + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        cls.save_report()

    def open(self, agent='codex', task='SQLite migration', project='projects/demo', *flags):
        return self.cli('session-open', project, '--agent', agent, '--task', task, *flags)

    def remember(self, session, result, topic='SQLite migration', when='SQLite migration'):
        return self.cli('agent-memory', 'remember', session, '--topic', topic,
                        '--attempt', 'Copied the fixture before running the migration.', '--result', result,
                        '--when', when)['memory_id']

    def session_from_hook(self, agent, runtime):
        text = self.hook(agent=agent, runtime=runtime)['hookSpecificOutput']['additionalContext']
        self.assertIn('agent-memory remember', text[:1000])
        return text.splitlines()[0].split()[-1]

    def context(self, session, *flags):
        return self.cli('session-context', session, '--task', 'SQLite migration', *flags)

    def test_fresh_host_sessions_recall_own_memory_with_project_context(self):
        creator = self.session_from_hook('codex', 'memory-creator')
        result = 'Copying with the journal present left two nodes.'
        memory_id = self.remember(creator, result)
        self.assertEqual(self.remember(creator, result), memory_id)
        self.cli('session-close', creator, '--no-change')
        receiver = self.session_from_hook('codex', 'memory-receiver')
        self.assertNotEqual(receiver, creator)

        packet = self.context(receiver)
        agent_contract.validate('compact-session', packet)
        self.assertEqual(packet['memory_refs'], [memory_id])
        self.assertIn(result, packet['context']['text'])
        self.assertTrue(packet['refs'][0]['ref'].startswith('demo@'))
        full = self.context(receiver, '--view', 'full')
        agent_contract.validate('session', full)
        self.assertEqual(full['memory_refs'], [memory_id])
        self.assertIn(result, full['context']['text'])
        self.assertIn(memory_id, self.cli('session-status', receiver)['memory_offered'])

        from test_sessions import project
        self.apply({'changes': [{'expected_revision': 'new', 'note': project('other')}]})
        for candidate in (self.open('claude'), self.open(project='projects/other'),
                          self.open(task=''), self.open(task='Typography and motion')):
            self.assertFalse(candidate.get('memory_refs'))
            self.assertNotIn(result, candidate['context']['text'])

    def test_share_requires_ownership_and_current_vault_source(self):
        codex = self.open()['session_id']
        memory_id = self.remember(codex, 'The old schema needs a journal checkpoint.')
        reader = self.session_from_hook('claude', 'memory-sharing')
        self.assertNotIn(memory_id, self.context(reader).get('memory_refs', []))
        share = ('agent-memory', 'share', codex, memory_id, '--source', 'sources/migration.txt')
        self.cli(*share, ok=False)

        source = self.root / 'sources' / 'migration.txt'
        source.parent.mkdir(exist_ok=True)
        source.write_text('Synthetic migration check: the old schema needed a checkpoint.\n', encoding='utf-8')
        self.case['commands'].append({'fixture_source': 'sources/migration.txt', 'contents': source.read_text()})
        self.cli('agent-memory', 'share', reader, memory_id, '--source', 'sources/migration.txt', ok=False)
        self.cli('agent-memory', 'share', codex, memory_id, '--source', 'https://example.invalid/report', ok=False)
        shared = self.cli(*share)
        self.assertEqual(self.cli(*share), shared)
        self.assertEqual(shared['status'], 'proposed')

        packet = self.context(reader)
        self.assertEqual(packet['memory_refs'], [memory_id])
        self.assertIn('sources/migration.txt', packet['context']['text'])
        self.assertIn('proposed', packet['context']['text'].lower())
        source.write_text('The original evidence changed.\n', encoding='utf-8')
        self.case['commands'].append({'fixture_source': 'sources/migration.txt', 'contents': source.read_text()})
        self.assertNotIn(memory_id, self.context(reader).get('memory_refs', []))
        self.assertIn(memory_id, self.context(codex)['memory_refs'])

    def test_feedback_changes_ranking_after_actual_offer_and_preserves_relevance(self):
        first = self.open(task='')['session_id']
        a = self.remember(first, 'Observation A.')
        b = self.remember(first, 'Observation B.')
        review = self.open()
        self.assertEqual(set(review['memory_refs']), {a, b})
        lower = review['memory_refs'][-1]
        self.cli('session-feedback', first, '--outcome', 'helpful', '--used-memory', lower, ok=False)
        self.cli('session-feedback', review['session_id'], '--outcome', 'not_used', '--used-memory', lower, ok=False)
        report = ('session-feedback', review['session_id'], '--outcome', 'helpful', '--used-memory', lower,
                  '--used', review['refs'][0]['ref'])
        feedback = self.cli(*report)
        agent_contract.validate('feedback-result', feedback)
        self.assertEqual(self.cli(*report), feedback)
        self.assertEqual(self.open()['memory_refs'][0], lower)
        self.assertFalse(self.open(task='Typography and motion').get('memory_refs'))
        other = review['memory_refs'][0]
        self.cli('session-feedback', review['session_id'], '--outcome', 'unhelpful', '--used-memory', other)
        self.assertEqual(self.cli(*report), feedback)

        self.cli('session-feedback', review['session_id'], '--outcome', 'unhelpful', '--used-memory', lower)
        self.assertEqual(self.open()['memory_refs'][-1], lower)
        restored = self.cli(*report)
        self.assertNotEqual(restored['feedback']['id'], feedback['feedback']['id'])
        self.assertEqual(self.open()['memory_refs'][0], lower)
        self.assertEqual(self.cli(*report), restored)

    def test_budget_omission_does_not_count_as_offer(self):
        creator = self.open(task='')['session_id']
        content = '🧪' * 600
        memory_id = self.remember(creator, content)
        for flags in (('--budget', '160'), ('--max-bytes', '4096')):
            tiny = self.open('codex', 'SQLite migration', 'projects/demo', *flags)
            self.assertNotIn(memory_id, tiny.get('memory_refs', []))
            self.assertNotIn(memory_id, self.cli('session-status', tiny['session_id']).get('memory_offered', []))
            self.cli('session-feedback', tiny['session_id'], '--outcome', 'helpful', '--used-memory', memory_id, ok=False)
        full_budget = self.open()
        self.assertEqual(full_budget['memory_refs'], [memory_id])
        self.assertIn(content, full_budget['context']['text'])
        self.assertLessEqual(len(self.case['commands'][-1]['stdout'].encode('utf-8')), 12000)

    def test_damaged_optional_store_still_offers_project_context(self):
        creator = self.open(task='')['session_id']
        self.remember(creator, 'Observation before local storage was damaged.')
        (self.root / '.state' / 'agent-memory.sqlite3').write_bytes(b'synthetic broken database')
        packet = self.open()
        self.assertTrue(packet['refs'][0]['ref'].startswith('demo@'))
        self.assertIn('Agent memories could not be loaded', packet['context']['text'])
        self.assertFalse(packet.get('memory_refs'))
