import os
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hivemind'))
import hive


def note(ident="sample-person", **fields):
    n = {"id": ident, "type": "person", "cluster": "personal", "title": "Sample Person", "summary": "Local test knowledge", "status": "stable", "evidence": "confirmed", "sensitivity": "personal", "aliases": ["Testbegriff"], "tags": [], "sources": [{"id": "test-source", "resource": "test:fixture", "title": "Synthetic test evidence"}], "verified": [], "stale_after": "2000-01-01T00:00:00Z", "links": [], "body": "# Sample Person\n\nSynthetic evidence, not personal data."}
    n.update(fields)
    return n


class HiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "brain"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, *notes):
        current = hive.load_notes(self.root)
        return hive.apply(self.root, {"changes": [{"expected_revision": hive.digest(current[n["id"]][1]) if n["id"] in current else "new", "note": n} for n in notes]}, "test-agent", "test reason", "test:fixture")

    def test_retrieval_staleness_and_deprecated_history(self):
        self.put(note(), note("old-note", status="deprecated"))
        result = hive.search(self.root, "Testbegriff")
        self.assertEqual([r["id"] for r in result], ["sample-person"])
        self.assertEqual(result[0]["freshness"], "stale")
        self.assertEqual(len(hive.search(self.root, "Testbegriff", include_deprecated=True)), 2)
        self.assertEqual(hive.load_notes(self.root)["sample-person"][0]["verified"], [])
        self.assertTrue(hive.check(self.root)["valid"])

    def test_archive_hides_old_context_without_losing_history_or_links(self):
        active = note('active-project', type='project', cluster='projects/active-project',
                      links=[{'target': 'old-project', 'relation': 'related'}])
        old = note('old-project', type='project', cluster='projects/old-project', archived=True)
        self.put(active, old)
        self.assertEqual([r['id'] for r in hive.search(self.root, 'Testbegriff')], ['active-project'])
        self.assertEqual(len(hive.search(self.root, 'Testbegriff', include_archived=True)), 2)
        self.assertEqual(hive.browse(self.root, 'projects')['total_clusters'], 1)
        self.assertEqual(hive.browse(self.root, 'projects', include_archived=True)['total_clusters'], 2)
        self.assertEqual(hive.browse(self.root, 'projects/active-project')['total_cross_connections'], 0)
        self.assertEqual(hive.load_notes(self.root)['old-project'][0]['status'], 'stable')
        self.assertNotIn('old-project', (self.root / 'wiki/clusters/projects/index.md').read_text())
        self.assertIn('old-project', (self.root / 'wiki/archive/index.md').read_text())
        self.assertEqual([r['id'] for r in hive.check(self.root)['review_queue']], ['active-project'])
        # Restore from the canonical record: no import or history replacement required.
        restored = copy.deepcopy(hive.load_notes(self.root)['old-project'][0])
        restored['archived'] = False
        self.put(restored)
        self.assertEqual(hive.browse(self.root, 'projects')['total_clusters'], 2)

    def test_people_have_separate_routes_and_project_connections(self):
        person = note('person-test', cluster='people/person-test',
                      links=[{'target': 'project-test', 'relation': 'related'}])
        project = note('project-test', type='project', cluster='projects/project-test')
        self.put(person, project)
        self.assertEqual(hive.browse(self.root, 'people')['total_clusters'], 1)
        self.assertEqual(hive.browse(self.root, 'people/person-test')['total_cross_connections'], 1)
        self.assertEqual(hive.search(self.root, 'Testbegriff', cluster='personal'), [])
        self.assertEqual(hive.search(self.root, 'Testbegriff', cluster='people')[0]['id'], 'person-test')

    def test_concurrent_stale_writers_do_not_overwrite(self):
        self.put(note())
        before = hive.load_notes(self.root)["sample-person"][1]
        barrier = threading.Barrier(2)
        def writer(title):
            barrier.wait()
            try:
                hive.apply(self.root, {"changes": [{"expected_revision": hive.digest(before), "note": note(title=title)}]}, title, "concurrency", "test:fixture")
                return "ok"
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ["First writer", "Second writer"]))
        self.assertEqual(results.count("ok"), 1)
        self.assertTrue(any("Revision conflict" in r for r in results))
        self.assertEqual(hive.check(self.root)["events"], 2)

    def test_batch_validation_rejects_wrong_path_without_writes(self):
        a = note(body="# Evidence\n\n[Other](../project/other-note.md)")
        b = note("other-note")
        with self.assertRaisesRegex(ValueError, "Dangling Markdown"):
            self.put(a, b)
        self.assertEqual(hive.load_notes(self.root), {})
        self.assertIsNone(hive.pending(self.root))
        a["body"] = "[Other](../person/other-note.md)"
        a["links"] = [{"relation": "related", "target": "other-note"}]
        self.put(a, b)
        self.assertTrue(hive.check(self.root)["valid"])

    def test_interrupted_batch_is_detected_and_recovered(self):
        self.put(note(), note("second-note"))
        real_atomic = hive.atomic
        def interrupted(path, text):
            if path.name == "second-note.md":
                raise OSError("simulated interruption")
            return real_atomic(path, text)
        with patch.object(hive, "atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.put(note(title="Updated first"), note("second-note", title="Updated second"))
        with self.assertRaisesRegex(ValueError, "Interrupted write"):
            hive.check(self.root)
        with hive.locked(self.root):
            hive.finish(self.root, hive.pending(self.root))
        self.assertEqual(hive.check(self.root)["events"], 2)
        self.assertEqual(hive.load_notes(self.root)["second-note"][0]["title"], "Updated second")

    def test_recovery_refuses_conflicting_manual_change(self):
        self.put(note())
        old = hive.load_notes(self.root)["sample-person"][1]
        new = hive.decode(old); new["title"] = "Intended change"
        event = {"id": "pending-test", "at": hive.now(), "actor": "test", "reason": "test", "context": "test:fixture", "changes": [{"id": "sample-person", "before": old, "after": hive.encode(new)}]}
        hive.atomic(self.root / ".pending.json", json.dumps(event))
        path = hive.path_for(self.root, new)
        path.write_text(old + "\nAn unrelated manual change.\n")
        with self.assertRaisesRegex(ValueError, "Recovery conflict"):
            hive.finish(self.root, event)
        self.assertIsNotNone(hive.pending(self.root))
        self.assertIn("unrelated manual", path.read_text())

    def test_cache_rebuild_and_folder_move(self):
        self.put(note())
        shutil.rmtree(self.root / ".cache")
        self.assertEqual(len(hive.search(self.root, "Testbegriff")), 1)
        moved = self.root.with_name("moved")
        shutil.move(str(self.root), moved)
        result = hive.search(moved, "Testbegriff")
        self.assertTrue(Path(result[0]["path"]).is_file())
        self.assertTrue(result[0]["path"].startswith(str(moved)))

    def test_direct_edit_does_not_get_silently_indexed(self):
        self.put(note())
        path = hive.path_for(self.root, note())
        path.write_text(path.read_text() + "\nUnjournaled text\n")
        with self.assertRaisesRegex(ValueError, "Unjournaled"):
            hive.search(self.root, "Unjournaled")

    def test_clusters_scope_queries_and_preserve_cross_connections(self):
        project = note("sample", type="project", cluster="projects/sample")
        decision = note("print-decision", type="decision", cluster="projects/sample", links=[{"relation":"related", "target":"sample-person", "reason":"Apply a relevant collaboration preference"}])
        self.put(note(links=[{"relation":"related","target":"print-decision"}]), project, decision, note("sample-extra", type="project", cluster="projects/sample-extra"))
        self.assertEqual({r["id"] for r in hive.search(self.root, "Testbegriff", cluster="projects/sample")}, {"sample", "print-decision"})
        self.assertEqual(len(hive.search(self.root, "Testbegriff", cluster="projects")), 3)
        view = hive.browse(self.root, "projects/sample", limit=1)
        self.assertEqual(view["total_notes"], 2)
        self.assertEqual(view["notes"][0]["id"], "sample")
        self.assertEqual(view["cross_connections"][0]["other_cluster"], "personal")
        self.assertEqual(view["total_cross_connections"], 1)
        self.assertEqual(len(hive.browse(self.root, "projects")["clusters"]), 2)
        # Reclassification changes the view without duplicating notes or rewriting IDs.
        project["cluster"] = "projects/merged"
        decision["cluster"] = "projects/merged"
        self.put(project, decision)
        self.assertFalse((self.root / "wiki/clusters/projects/sample/index.md").exists())
        self.assertTrue((self.root / "wiki/clusters/projects/merged/index.md").exists())
        self.assertEqual(len(hive.load_notes(self.root)), 4)
        self.assertTrue(hive.check(self.root)["valid"])

    def test_legacy_upgrade_and_new_write_require_explicit_home(self):
        legacy = note()
        del legacy["cluster"]
        with self.assertRaisesRegex(ValueError, "primary cluster"):
            self.put(legacy)
        # Build the previous on-disk format, including its original event.
        legacy["created"] = "2026-01-01T00:00:00Z"
        legacy["generated"] = {"by":"legacy-test", "at":legacy["created"]}
        text = hive.encode(legacy)
        hive.atomic(hive.path_for(self.root, legacy), text)
        old_event = {"id":"20260101-legacy", "at":legacy["created"], "actor":"legacy-test", "reason":"previous-format fixture", "context":"test:fixture", "changes":[{"id":legacy["id"],"before":None,"after":text}]}
        hive.atomic(self.root / ".history/20260101-legacy.json", json.dumps(old_event))
        with hive.locked(self.root):
            hive.rebuild(self.root)
        self.assertEqual(hive.search(self.root, "Testbegriff")[0]["cluster"], "personal")
        self.assertIn("needs_explicit_cluster", hive.check(self.root)["review_queue"][0]["reasons"])
        legacy["cluster"] = "personal"
        self.put(legacy)
        self.assertEqual(hive.check(self.root)["events"], 2)
        self.assertNotIn("needs_explicit_cluster", hive.check(self.root)["review_queue"][0]["reasons"])

    def project(self, ident, body, **fields):
        return note(ident, type="project", cluster="projects/" + ident, sensitivity="internal", body=body, **fields)

    def test_brief_finds_project_context_from_a_subfolder(self):
        repo = Path(self.temp.name) / "repo"
        (repo / "server").mkdir(parents=True)
        self.put(self.project("demo", f"# Demo\n\nEinstieg: `{repo}`.\n\nStand: Positionen werden geprüft.", title="Demo-Projekt"))
        result = hive.brief(self.root, str(repo / "server"))
        self.assertEqual((result["cluster"], result["folder"]), ("projects/demo", str(repo.resolve())))
        self.assertIn("Demo-Projekt", result["text"])
        self.assertIn("Positionen werden geprüft", result["text"])
        self.assertIn("overdue", result["text"])  # the fixture's review date has passed

    def test_brief_stays_silent_without_a_unique_project_folder(self):
        base = Path(self.temp.name)
        shared, parent = base / "shared", base / "parent"
        for folder in (shared, parent / "child" / "src", parent / "other"):
            folder.mkdir(parents=True)
        self.put(self.project("one", f"`{shared}` und `{parent}`"), self.project("two", f"`{shared}`"),
                 self.project("three", f"`{parent / 'child'}`"), self.project("home", f"`{Path.home()}`"),
                 self.project("old", f"`{parent / 'other'}`", archived=True))
        self.assertEqual(hive.brief(self.root, str(parent / "child" / "src"))["cluster"], "projects/three")
        self.assertIsNone(hive.brief(self.root, str(shared)))  # named by two projects
        self.assertIsNone(hive.brief(self.root, str(parent / "other")))  # container of another project; archived claim ignored
        self.assertIsNone(hive.brief(self.root, str(Path.home())))
        self.assertIsNone(hive.brief(self.root, str(base)))

    def test_brief_leaves_out_personal_connections(self):
        lesson = note("evidence-lesson", type="lesson", cluster="knowledge", sensitivity="internal", title="Evidenz vor Status")
        private = note("private-person", cluster="people/private-person", title="Private Person",
                       links=[{"relation": "related", "target": "demo", "reason": "Kennt das Projekt"}])
        project = self.project("demo", "Stand.", title="Demo-Projekt",
                               links=[{"relation": "related", "target": "evidence-lesson", "reason": "Replay ist keine Freigabe."}])
        self.put(lesson, project, private)
        text = hive.brief(self.root, "projects/demo")["text"]
        self.assertIn("Replay ist keine Freigabe.", text)
        self.assertNotIn("Private Person", text)
        self.assertNotIn("Kennt das Projekt", text)

    def test_search_finds_inflected_german_words(self):
        self.put(note("word-forms", title="Kalender und Zusammenarbeit", summary="Entscheidungen zu Ausschreibungen und Tests", aliases=[]))
        for query in ("Kalenders", "zusammenarbeiten", "Entscheidung", "Ausschreibung", "testet"):
            self.assertEqual([r["id"] for r in hive.search(self.root, query)], ["word-forms"], query)

    def test_eval_counts_first_hits_and_misses(self):
        self.put(note("garden-note", title="Gartenplanung", aliases=[]), note("calendar-note", title="Kalenderablauf", aliases=[]))
        report = hive.evaluate(self.root, [{"query": "Gartenplanung", "expect": ["garden-note"]},
                                           {"query": "Gartenplanung", "expect": ["calendar-note"]}])
        self.assertEqual((report["cases"], report["hit_at_1"], report["hit_at_5"]), (2, 0.5, 0.5))
        self.assertEqual([m["expect"] for m in report["misses"]], [["calendar-note"]])

    def test_cli_records_searches_without_hits(self):
        self.put(note())
        for query in ("Testbegriff", "Zauberwort"):
            subprocess.run([sys.executable, hive.__file__, "--root", str(self.root), "search", query], capture_output=True, timeout=10)
        log = (self.root / ".state/search-misses.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["query"] for line in log], ["Zauberwort"])

    def test_session_hook_injects_context_only_inside_projects(self):
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        self.put(self.project("demo", f"Einstieg: `{repo}`", title="Demo-Projekt"))
        def hook(payload):
            return subprocess.run([sys.executable, hive.__file__, "--root", str(self.root), "brief", "--hook"],
                                  input=payload, capture_output=True, text=True, timeout=10)
        inside = hook(json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(repo)}))
        self.assertEqual(inside.returncode, 0, inside.stderr)
        output = json.loads(inside.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("Demo-Projekt", output["additionalContext"])
        for payload in (json.dumps({"cwd": self.temp.name}), "kein JSON"):
            quiet = hook(payload)
            self.assertEqual((quiet.returncode, quiet.stdout), (0, ""), quiet.stderr)


if __name__ == "__main__":
    unittest.main()
