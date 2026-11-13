import os
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hive
from test_hive import note


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, *notes):
        return hive.apply(self.root, {"changes": [{"expected_revision": "new", "note": n} for n in notes]},
                          "retrieval-test", "Synthetic regression", "test:retrieval")

    def test_unknown_subject_does_not_match_question_words_or_generic_decisions(self):
        self.put(note("decision-note", title="Entscheidungen", summary="Was wir über Entscheidungen wissen.",
                      aliases=[], body="Wir haben Entscheidungen getroffen und Quellen geprüft."))
        for query in ("Quantenwombat", "Was weißt du über Quantenwombat?",
                      "Welche Entscheidungen haben wir zum Quantenwombat getroffen?", "wie was und"):
            with self.subTest(query=query):
                self.assertEqual(hive.search(self.root, query), [])

    def test_named_subject_survives_unknown_description_and_exposes_partial_coverage(self):
        self.put(note("calendar-note", title="Kalender-Anbindung", aliases=["Gmail"],
                      summary="Ausgewählte Nachrichten als Quelle.", body="Begrenzter lesender Zugriff."))
        result = hive.search(self.root, "Wie wird Gmail eingebunden?")
        self.assertEqual(result[0]["id"], "calendar-note")
        self.assertEqual(result[0]["matched_terms"], ["gmail"])
        self.assertLess(result[0]["coverage"], 1)
        self.assertEqual(result[0]["anchor_terms"], ["gmail"])

    def test_diacritics_inflections_and_scope_are_preserved(self):
        self.put(note("review-note", title="Prüfungen am Kalender", aliases=[], cluster="knowledge"),
                 note("other-note", title="Prüfungen am Kalender", aliases=[], cluster="system"),
                 note("archived-note", title="Prüfungen am Kalender", aliases=[], cluster="knowledge", archived=True))
        for query in ("Prüfungen Kalenders", "Prufungen Kalender"):
            self.assertEqual([r["id"] for r in hive.search(self.root, query, cluster="knowledge")], ["review-note"])

    def test_eval_separates_positive_retrieval_from_negative_abstention(self):
        self.put(note("calendar-note", title="Kalender", aliases=[]))
        report = hive.evaluate(self.root, [
            {"query": "Kalender", "expect": ["calendar-note"]},
            {"query": "Quantenwombat", "expect": []},
            # An intentionally bad negative label exercises false-positive accounting.
            {"query": "Kalender", "expect": []},
        ])
        self.assertEqual((report["cases"], report["positive_cases"], report["negative_cases"]), (3, 1, 2))
        self.assertEqual((report["hit_at_1"], report["hit_at_5"]), (1, 1))
        self.assertEqual((report["rejection_rate"], report["false_positive_rate"]), (.5, .5))
        self.assertEqual(report["misses"], [])
        self.assertEqual(len(report["false_positives"]), 1)

    def test_feedback_is_bounded_attributable_and_does_not_change_knowledge(self):
        self.put(note("calendar-note", title="Kalender", aliases=[]))
        before = {str(p.relative_to(self.root)): p.read_bytes() for parent in ("wiki", ".history")
                  for p in (self.root / parent).rglob("*") if p.is_file()}
        revision = hive.digest(hive.load_notes(self.root)["calendar-note"][1])
        with patch.object(hive, "SEARCH_LOG_LIMIT", 3):
            for i in range(5):
                event = hive.feedback(self.root, "Kalender", i % 2 == 0, ["calendar-note"], actor="user/test")
        rows = [json.loads(line) for line in (self.root / ".state/search-feedback.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1], event)
        self.assertEqual(event["actor"], "user/test")
        self.assertEqual(event["results"], [{"id": "calendar-note", "revision": revision}])
        self.assertNotIn("body", json.dumps(rows))
        after = {str(p.relative_to(self.root)): p.read_bytes() for parent in ("wiki", ".history")
                 for p in (self.root / parent).rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(hive.check(self.root)["valid"])
        report = hive.evaluate(self.root, [])
        self.assertEqual(report["feedback"], {"count": 3, "helpful": 2, "unhelpful": 1})

    def test_feedback_rejects_unknown_out_of_scope_and_invalid_verdicts(self):
        self.put(note("calendar-note", cluster="system"))
        invalid = [dict(helpful="no"), dict(helpful=False, result_ids=["missing-note"]),
                   dict(helpful=False, result_ids=["calendar-note"], cluster="knowledge"),
                   dict(helpful=False, actor=""), dict(helpful=True, result_ids=["calendar-note"] * 2)]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                hive.feedback(self.root, "Kalender", **arguments)
        self.assertFalse((self.root / ".state/search-feedback.jsonl").exists())

    def test_concurrent_feedback_and_misses_keep_all_events(self):
        self.put(note("calendar-note", title="Kalender", aliases=[]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: hive.feedback(self.root, "Kalender", bool(i % 2), ["calendar-note"], actor=f"test/{i}"), range(8)))
            list(pool.map(lambda i: hive.record_miss(self.root, f"Quantenwombat{i}"), range(8)))
        feedback = (self.root / ".state/search-feedback.jsonl").read_text().splitlines()
        misses = (self.root / ".state/search-misses.jsonl").read_text().splitlines()
        self.assertEqual((len(feedback), len(misses)), (8, 8))
        self.assertEqual(len({json.loads(line)["id"] for line in feedback}), 8)


if __name__ == "__main__":
    unittest.main()
