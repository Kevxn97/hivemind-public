"""Public CLI and real terminal coverage for selected-source first use.

The cases exercise consent/cancellation/EOF, ambiguous projects, selected-vault
routing, provenance, pause, replay and exact-file guards without host calls.
All fixtures and subprocess environments are synthetic and credential-free.
"""
import copy
import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import time

import test_source_connections_e2e as source_suite
from test_source_connections_e2e import CLI
import unittest


class EasySourcesE2E(unittest.TestCase):
    setUpClass = classmethod(source_suite.SourceConnectionsE2E.setUpClass.__func__)
    tearDownClass = classmethod(source_suite.SourceConnectionsE2E.tearDownClass.__func__)
    save = classmethod(source_suite.SourceConnectionsE2E.save.__func__)
    setUp = source_suite.SourceConnectionsE2E.setUp
    finish = source_suite.SourceConnectionsE2E.finish
    call = source_suite.SourceConnectionsE2E.call
    snapshot = source_suite.SourceConnectionsE2E.snapshot

    def terminal(self, args, answers):
        master, slave = pty.openpty()
        command = [*CLI, '--root', str(self.root), *args]
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                   cwd=self.fixture, env=self.env)
        os.close(slave)
        transcript = ''
        pending = list(answers)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    transcript += chunk.decode()
                    if pending and pending[0][0] in transcript:
                        _, answer = pending.pop(0)
                        os.write(master, answer.encode())
                if process.poll() is not None:
                    break
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        finally:
            os.close(master)
        self.case['commands'].append({'argv': command, 'returncode': process.returncode,
                                      'terminal': transcript, 'unanswered_prompts': pending})
        self.save()
        self.assertEqual(process.returncode, 0, transcript)
        self.assertFalse(pending, transcript)
        return transcript

    def capture(self, *extra, **kwargs):
        return self.call('sources', 'capture', '--kind', 'gmail', '--title', 'Pilot agreed',
            '--summary', 'The fictional pilot covers one team.', '--source', 'mail:fictional',
            '--observed-at', '2026-11-14T09:00:00Z', *extra, **kwargs)

    def test_flags_preview_explicit_yes_provenance_replay_and_pause(self):
        before = self.snapshot()
        plan = self.capture()
        self.assertEqual(plan['status'], 'preview')
        self.assertEqual(before, self.snapshot())
        saved = self.capture('--yes')
        self.assertEqual(saved['status'], 'captured')
        self.assertTrue(saved['event'])
        self.assertEqual(saved['note']['cluster'], 'projects/sample-project')
        self.assertEqual(saved['note']['sources'][0]['resource'], 'mail:fictional')
        self.assertEqual(self.capture('--yes')['status'], 'already_captured')
        pause = self.call('memory', 'pause', '--reason', 'Synthetic pause')
        self.call('memory', 'pause', '--reason', 'Synthetic pause', '--apply', '--preview', pause['preview'])
        before = self.snapshot()
        self.capture('--yes', succeeds=False)
        self.assertEqual(before, self.snapshot())

    def test_remember_name_selection_and_legacy_exact_file_guard(self):
        before = self.snapshot()
        plan = self.call('remember', 'Use one fictional pilot team.', '--project', 'Sample Project')
        self.assertEqual(plan['note']['sources'][0]['resource'], 'user:memory')
        self.assertEqual(plan['note']['tags'], ['selected-source', 'manual'])
        self.assertEqual(before, self.snapshot())
        saved = self.call('remember', 'Use one fictional pilot team.', '--yes')
        self.assertTrue(saved['applied'])
        old = self.call('sources', 'capture', '--file', str(self.selected))
        self.selected.write_text(json.dumps(self.selection) + '\n')
        before = self.snapshot()
        self.call('sources', 'capture', '--file', str(self.selected), '--apply', '--preview', old['preview'], succeeds=False)
        self.assertEqual(before, self.snapshot())

    def test_terminal_menu_capture_one_confirmation(self):
        output = self.terminal(['sources'], [('Choose a source', '4\n'),
            ('Summary', 'A fictional one-team pilot is approved.\n'), ('Title', '\n'),
            ('Source reference', '\n'), ('Save this selection', 'y\n')])
        self.assertIn('Sample Project', output)
        self.assertIn('Saved to the journal', output)
        self.assertEqual(output.count('Save this selection'), 1)
        self.assertNotIn('Preview token', output)
        self.assertIn('fictional one-team', self.call('brief', str(self.project), structured=False).stdout)

    def test_terminal_cancellation_and_eof_preserve_all_files(self):
        for answers in ([('Save this selection', 'n\n')], [('Save this selection', '\x04')]):
            with self.subTest(answers=answers):
                before = self.snapshot()
                output = self.terminal(['remember', 'Synthetic cancellation.'], answers)
                self.assertIn('Not saved', output)
                self.assertEqual(before, self.snapshot())
        before = self.snapshot()
        self.terminal(['sources'], [('Choose a source', '\x04')])
        self.assertEqual(before, self.snapshot())

    def test_ambiguous_project_requires_choice_and_terminal_accepts_name(self):
        # Add a second synthetic project through the public revision-checked writer.
        draft = self.fixture / 'second.json'
        self.call('draft', 'sample-project', '--out', str(draft), structured=False)
        payload = json.loads(draft.read_text())
        note = copy.deepcopy(payload['changes'][0]['note'])
        note.update(id='second-project', cluster='projects/second-project', title='Second Project')
        draft.write_text(json.dumps({'changes': [{'expected_revision': 'new', 'note': note}]}))
        self.call('apply', str(draft), '--actor', 'synthetic', '--reason', 'Second fixture project', '--context', 'synthetic:test', structured=False)
        before = self.snapshot()
        self.call('remember', 'Ambiguous synthetic selection.', '--yes', succeeds=False)
        self.assertEqual(before, self.snapshot())
        output = self.terminal(['remember', 'Chosen synthetic selection.'],
            [('Choose project', 'Second Project\n'), ('Save this selection', 'y\n')])
        self.assertIn('projects/second-project', output)
        self.assertIn('Saved to the journal', output)

    def test_guidance_infers_agent_and_project_and_prompt_targets_selected_vault(self):
        self.call('connect', 'codex', '--apply')
        before = self.snapshot()
        for kind in ('gmail', 'calendar'):
            guide = self.call('sources', 'setup', kind)
            self.assertEqual(guide['agent'], 'codex')
            self.assertEqual(guide['capture_template']['project'], 'projects/sample-project')
            self.assertIn(str(self.root), guide['agent_prompt'])
            self.assertIn('--kind ' + kind, guide['agent_prompt'])
            self.assertIn('--project projects/sample-project', guide['agent_prompt'])
            self.assertIn('--yes', guide['agent_prompt'])
            command = shlex.split(guide['capture_command'])
            self.assertIn(str(self.root), command)
            self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(before, self.snapshot())

    def test_mcp_inference_terminal_fields_and_owned_removal(self):
        self.call('connect', 'claude', '--apply')
        output = self.terminal(['sources', 'setup', 'mcp'], [('Server name', 'fictional\n'),
            ('HTTPS endpoint', 'https://mcp.example.test/service\n'), ('Apply these project settings', 'y\n')])
        self.assertIn('Applied locally', output)
        target = self.project / '.mcp.json'
        self.assertEqual(json.loads(target.read_text())['mcpServers']['fictional']['url'], 'https://mcp.example.test/service')
        self.call('sources', 'remove', '--name', 'fictional', '--apply')
        self.assertFalse(target.exists())

    def test_new_flags_reject_invalid_selection_without_mutation(self):
        for args in (['--summary', 'x' * 281], ['--observed-at', 'tomorrow'],
                     ['--project', 'Unknown Project'], ['--file', str(self.selected)]):
            before = self.snapshot()
            self.capture(*args, '--yes', succeeds=False)
            self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
