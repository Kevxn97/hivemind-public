import os
"""Contract boundaries: stable versions, attributable IDs, and metadata-only feedback.

Failure cases established before implementation: envelopes must preserve legacy
keys without mutating callers; errors must use typed codes, not prose matching;
drafts may be incomplete but submitted handoffs cannot; invalid identifiers,
revisions, unknown fields, fake agreement and oversized arrays must fail; runtime
and feedback must reject transcript fields. The schema validator must refuse
unsupported keywords so a future schema cannot silently weaken validation.
"""
import copy
import importlib
import json
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1] / 'hivemind'
sys.path.insert(0, str(BASE))


def receipt():
    return {'schema_version': 1, 'session_id': 'a' * 32, 'agent': 'codex',
            'cluster': 'projects/demo', 'status': 'context_offered',
            'opened_at': '2026-10-31T12:00:00Z', 'previous_context_at': None,
            'offered': [{'id': 'demo', 'revision': 'b' * 64}]}


def handoff(blank=False):
    return {'session_id': 'a' * 32, 'note_id': 'demo', 'expected_revision': 'b' * 64,
            'source': {'id': 'handoff-' + 'a' * 32, 'resource': '' if blank else 'test:source',
                       'title': '' if blank else 'Synthetic source',
                       'observed_at': '' if blank else '2026-10-31T12:00:00Z'},
            'evidence': 'reported', 'project_state': '' if blank else 'A bounded state',
            'decisions': [], 'open_questions': [], 'next_steps': [], 'step_updates': []}


class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = importlib.import_module('agent_contract')

    def test_additive_versioned_envelopes_and_typed_errors(self):
        value = receipt()
        original = copy.deepcopy(value)
        result = self.contract.success('session-open', value)
        self.assertEqual(result, {**original, 'contract_version': 1, 'ok': True})
        self.assertEqual(value, original)
        self.assertEqual(self.contract.success('search', value), value)
        failure = self.contract.failure(self.contract.AgentError('REVISION_CONFLICT', 'reread'))
        self.assertEqual(failure, {'contract_version': 1, 'ok': False,
                                  'error_code': 'REVISION_CONFLICT', 'error': 'reread'})
        self.assertEqual(self.contract.failure(ValueError('Revision conflict'))['error_code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.contract.failure(OSError('Unavailable'))['error_code'], 'IO_ERROR')
        self.contract.validate('error', failure)

    def test_receipts_accept_runtime_lifecycle_and_reject_false_metadata(self):
        value = receipt()
        value['runtime'] = {'id': 'thr_123', 'source': 'startup', 'last_source': 'resume',
                            'ended_at': '2026-10-31T13:00:00Z', 'end_reason': 'other'}
        value['previous_session_id'] = 'c' * 32
        value['runtime']['end_reason'] = 'resume'
        self.contract.validate('receipt', value)
        for status in ('context_offered', 'saved', 'no_change'):
            value['status'] = status
            if status != 'context_offered': value['closed_at'] = '2026-10-31T13:00:00Z'
            if status == 'saved':
                value.update(event='test-event', committed_at='2026-10-31T13:00:00Z',
                             payload_sha256='d' * 64, notes=[{'id': 'demo', 'revision': 'b' * 64}], work_items=[])
            self.contract.validate('receipt', value)
        for key, invalid in [('session_id', '../outside'), ('status', 'read'), ('schema_version', True),
                             ('offered', [{'id': 'demo', 'revision': 'short'}])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.contract.validate('receipt', {**receipt(), key: invalid})
        invalid = copy.deepcopy(value)
        invalid['runtime']['transcript'] = 'should not be stored'
        with self.assertRaises(ValueError): self.contract.validate('receipt', invalid)
        with self.assertRaises(ValueError): self.contract.validate('receipt', {**receipt(), 'offered': receipt()['offered'] * 513})

    def test_editable_draft_and_submitted_handoff_are_distinct(self):
        self.contract.validate('draft', handoff(blank=True))
        with self.assertRaises(ValueError): self.contract.validate('handoff', handoff(blank=True))
        self.contract.validate('handoff', handoff())
        for patch in ({'evidence': 'verified'}, {'expected_revision': 'new'},
                      {'decisions': ['not a structured decision']}, {'open_questions': ['q'] * 9},
                      {'transcript': 'private text'}, {'source': {'id': 'missing-fields'}},
                      {'step_updates': [{'text': 'Finish', 'status': 'agreed', 'state': 'done'}]}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.contract.validate('handoff', {**handoff(), **patch})
        value = handoff()
        value['step_updates'] = [{'text': 'Inspect header', 'status': 'proposed'},
                                 {'id': 'step-' + '0' * 16, 'state': 'waiting', 'reason': 'Fixture absent'}]
        self.contract.validate('handoff', value)

    def test_feedback_is_metadata_only_and_bounded(self):
        value = {'actor': 'agent', 'outcome': 'helpful', 'used': receipt()['offered'],
                 'cited': [], 'missing': 'none'}
        self.contract.validate('feedback', value)
        for patch in ({'outcome': 'read'}, {'actor': 'unknown'}, {'missing': 'Free prose'},
                      {'comment': 'transcript'}, {'used': [{'id': 'demo'}]}, {'used': receipt()['offered'] * 65}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.contract.validate('feedback', {**value, **patch})

    def test_manifest_and_schema_fail_closed(self):
        manifest = self.contract.describe()
        self.assertEqual(manifest['contract_version'], 1)
        self.assertIn('receipt', manifest['schemas'])
        self.assertIn('REVISION_CONFLICT', manifest['error_codes'])
        self.assertEqual(self.contract.describe('handoff')['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        with self.assertRaises(ValueError): self.contract.describe('../../private')
        with self.assertRaises(ValueError):
            self.contract.validate_schema({'type': 'string', 'unknownAssertion': True}, 'accepted?')
        with self.assertRaises(ValueError):
            self.contract.validate_schema({'type': 'object', 'properties': {'absent': {'unknownAssertion': True}}}, {})
        with self.assertRaises(ValueError):
            self.contract.validate_schema({'$ref': 'https://example.invalid/schema'}, {})
        with self.assertRaises(ValueError):
            self.contract.validate_schema({'type': 'integer'}, True)


if __name__ == '__main__': unittest.main(verbosity=2)
