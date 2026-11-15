import os
"""Synthetic session transitions; no real agent, provider or user settings."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hivemind'))
import hive
import sessions


def project(ident="demo", sensitivity="internal"):
    return {"id": ident, "type": "project", "cluster": "projects/" + ident,
            "title": "Synthetic " + ident, "summary": "Synthetic project continuity fixture.",
            "body": "# Synthetic project\n\nProject purpose: bounded API retries.\n\nHistorical context stays intact.",
            "status": "stable", "evidence": "reported", "sensitivity": sensitivity,
            "aliases": [], "tags": [], "sources": [{"id": "seed", "resource": "test:seed", "title": "Synthetic seed"}],
            "verified": [], "stale_after": "2100-01-01T00:00:00Z", "links": []}


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        hive.apply(self.root, {"changes": [{"expected_revision": "new", "note": project()}]},
                   "test", "Synthetic project", "test:fixture")

    def tearDown(self):
        self.temp.cleanup()

    def open(self, agent="codex"):
        return sessions.open_session(self.root, "projects/demo", agent=agent, task="API retries", budget=1200)

    def payload(self, opened, number=1):
        value = sessions.draft_handoff(self.root, opened["session_id"])
        value.update(project_state="API retry count is " + str(number), evidence="reported",
                     decisions=[{"decision": "Use " + str(number) + " retries", "reason": "Synthetic workload evidence", "status": "agreed"}],
                     open_questions=["Does the provider honor Retry-After?"],
                     next_steps=[{"text": "Verify response headers", "status": "proposed"}])
        value["source"].update(resource="test:handoff/" + str(number), title="Synthetic task decision", observed_at="2026-10-29T12:00:00Z")
        return value

    def test_ten_alternating_agent_handoffs_retrieve_the_latest_decision(self):
        for number in range(1, 11):
            agent = "codex" if number % 2 else "claude"
            opened = self.open(agent)
            if number > 1:
                self.assertIn("API retry count is " + str(number - 1), opened["context"]["text"])
            payload = self.payload(opened, number)
            receipt = sessions.close_session(self.root, opened["session_id"], payload)
            self.assertEqual(receipt["status"], "saved")
            self.assertEqual(receipt["notes"][0]["revision"], hive.digest(hive.load_notes(self.root)["demo"][1]))
            self.assertEqual(sessions.close_session(self.root, opened["session_id"], payload), receipt)
        self.assertEqual(hive.check(self.root)["events"], 11)
        note = hive.load_notes(self.root)["demo"][0]
        self.assertEqual(note["body"].count(sessions.START), 1)
        self.assertIn("Historical context stays intact.", note["body"])
        self.assertEqual(note["handoff"]["decisions"][0]["decision"], "Use 10 retries")
        self.assertEqual(note["evidence"], "reported")
        self.assertEqual(note["verified"], [])

    def test_context_receipt_contains_metadata_not_conversation_or_handoff_text(self):
        opened = self.open()
        raw = (self.root / ".state/sessions" / (opened["session_id"] + ".json")).read_text()
        self.assertNotIn("API retries", raw)
        self.assertNotIn("Historical context", raw)
        self.assertEqual(json.loads(raw)["status"], "context_offered")
        self.assertTrue(json.loads(raw)["offered"])
        receipt = sessions.close_session(self.root, opened["session_id"], self.payload(opened))
        raw = (self.root / ".state/sessions" / (opened["session_id"] + ".json")).read_text()
        self.assertNotIn("API retry count", raw)
        self.assertNotIn("Synthetic workload evidence", raw)
        self.assertIn(receipt["event"], raw)

    def test_explicit_no_change_creates_no_journal_entry(self):
        opened = self.open()
        receipt = sessions.close_session(self.root, opened["session_id"], no_change=True)
        self.assertEqual(receipt["status"], "no_change")
        self.assertEqual(sessions.close_session(self.root, opened["session_id"], no_change=True), receipt)
        self.assertEqual(hive.check(self.root)["events"], 1)

    def test_repeated_unchanged_handoff_is_a_retryable_no_change(self):
        first = self.open()
        payload = self.payload(first)
        sessions.close_session(self.root, first["session_id"], payload)
        second = self.open("claude")
        repeated = sessions.draft_handoff(self.root, second["session_id"])
        for key in sessions.FIELDS - {"session_id", "note_id", "expected_revision"}:
            repeated[key] = copy.deepcopy(payload[key])
        receipt = sessions.close_session(self.root, second["session_id"], repeated)
        self.assertEqual(receipt["status"], "no_change")
        self.assertEqual(sessions.close_session(self.root, second["session_id"], repeated), receipt)
        self.assertEqual(hive.check(self.root)["events"], 2)

    def test_failed_receipt_write_recovers_from_exact_journal_event(self):
        opened = self.open()
        payload = self.payload(opened)
        save = sessions._save
        def fail_receipt(root, value):
            if value["status"] == "saved":
                raise OSError("Synthetic process death after canonical commit")
            return save(root, value)
        with patch.object(sessions, "_save", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                sessions.close_session(self.root, opened["session_id"], payload)
        self.assertEqual(hive.check(self.root)["events"], 2)
        receipt = sessions.close_session(self.root, opened["session_id"], payload)
        self.assertEqual(receipt["status"], "saved")
        self.assertEqual(hive.check(self.root)["events"], 2)
        changed = copy.deepcopy(payload)
        changed["project_state"] = "Different update"
        with self.assertRaisesRegex(ValueError, "different handoff"):
            sessions.close_session(self.root, opened["session_id"], changed)

    def test_failed_apply_can_be_retried_without_losing_the_intent(self):
        opened = self.open()
        payload = self.payload(opened)
        with patch.object(hive, "apply", side_effect=OSError("Synthetic pre-commit failure")):
            with self.assertRaises(OSError):
                sessions.close_session(self.root, opened["session_id"], payload)
        with self.assertRaisesRegex(ValueError, "retry"):
            sessions.close_session(self.root, opened["session_id"], no_change=True)
        receipt = sessions.close_session(self.root, opened["session_id"], payload)
        self.assertEqual(receipt["status"], "saved")
        self.assertEqual(hive.check(self.root)["events"], 2)

    def test_stale_revision_is_rejected_without_overwrite(self):
        first, second = self.open(), self.open("claude")
        stale, winner = self.payload(first, 1), self.payload(second, 2)
        sessions.close_session(self.root, second["session_id"], winner)
        with self.assertRaisesRegex(ValueError, "Revision conflict"):
            sessions.close_session(self.root, first["session_id"], stale)
        self.assertIn("API retry count is 2", hive.load_notes(self.root)["demo"][0]["body"])
        self.assertEqual(hive.check(self.root)["events"], 2)

    def test_wrong_project_personal_and_unattributable_updates_are_rejected(self):
        for value in (project("other"), project("private", "personal")):
            hive.apply(self.root, {"changes": [{"expected_revision": "new", "note": value}]}, "test", "Synthetic fixture", "test:fixture")
        opened = self.open()
        for ident in ("other", "private"):
            payload = self.payload(opened)
            payload["note_id"] = ident
            payload["expected_revision"] = hive.digest(hive.load_notes(self.root)[ident][1])
            with self.assertRaisesRegex(ValueError, "session's project"):
                sessions.close_session(self.root, opened["session_id"], payload)
        payload = self.payload(opened)
        payload["source"]["resource"] = ""
        with self.assertRaisesRegex(ValueError, "source.resource"):
            sessions.close_session(self.root, opened["session_id"], payload)
        self.assertEqual(hive.check(self.root)["events"], 3)

    def test_proposals_remain_proposals_and_do_not_verify_the_note(self):
        opened = self.open()
        payload = self.payload(opened)
        payload["decisions"][0]["status"] = "proposed"
        payload["evidence"] = "inferred"
        sessions.close_session(self.root, opened["session_id"], payload)
        note = hive.load_notes(self.root)["demo"][0]
        self.assertIn("Proposed, not agreed", note["body"])
        self.assertIn("Evidence: inferred", note["body"])
        self.assertEqual(note["evidence"], "reported")
        self.assertEqual(note["verified"], [])

    def test_empty_update_and_both_close_modes_fail(self):
        opened = self.open()
        payload = sessions.draft_handoff(self.root, opened["session_id"])
        payload["source"].update(resource="test:source", title="Synthetic", observed_at="2026-10-29T12:00:00Z")
        with self.assertRaisesRegex(ValueError, "Nothing durable"):
            sessions.close_session(self.root, opened["session_id"], payload)
        for kwargs in ({}, {"payload": self.payload(opened), "no_change": True}):
            with self.assertRaisesRegex(ValueError, "exclusively"):
                sessions.close_session(self.root, opened["session_id"], **kwargs)

    def test_context_baseline_is_missing_before_first_visit_and_records_previous(self):
        self.assertIsNone(sessions.context_baseline(self.root, "projects/demo"))
        with patch.object(sessions, "_now", return_value="2026-10-29T12:00:00.000001Z"):
            first = self.open()
        with patch.object(sessions, "_now", return_value="2026-10-29T12:00:00.000002Z"):
            second = self.open("claude")
        self.assertIsNone(first["previous_context_at"])
        self.assertEqual(second["previous_context_at"], first["opened_at"])
        self.assertEqual(sessions.context_baseline(self.root, "projects/demo", second["session_id"])["session_id"], first["session_id"])


if __name__ == "__main__":
    unittest.main()
