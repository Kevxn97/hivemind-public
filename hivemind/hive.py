#!/usr/bin/env python3
"""HiveMind: local Markdown knowledge, auditable writes, rebuildable FTS search.

Python standard library only. The wiki is authoritative; .cache is disposable.
"""
import argparse
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
if __package__:
    from .runtime_paths import default_vault
else:
    from runtime_paths import default_vault
import re
import shlex
import sqlite3
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
KINDS = {"person", "preference", "work", "project", "decision", "lesson", "system"}
STATUSES = {"draft", "stable", "deprecated"}
EVIDENCE = {"confirmed", "reported", "inferred", "disputed"}
RELATIONS = {"about", "related", "depends_on", "supersedes", "contradicts"}
ID = re.compile(r"^[a-z][a-z0-9-]{1,79}$")
CLUSTER = re.compile(r"^(personal|knowledge|system|(?:work|projects|people)/[a-z][a-z0-9-]{1,79})$")
AREAS = {
    "personal": "Personal · individual context and collaboration",
    "people": "People · relationships and context",
    "work": "Work · roles and professional context",
    "projects": "Projects · a separate context for each project",
    "knowledge": "Shared knowledge and lessons",
    "system": "Operations and tools",
}


def cluster_for(note):
    """Old notes remain readable; all new writes require an explicit home."""
    if "cluster" in note:
        return note["cluster"]
    kind = note["type"]
    if kind in {"person", "preference"}:
        return "personal"
    if kind in {"project", "work"}:
        return ("projects" if kind == "project" else "work") + "/" + note["id"]
    return "system" if kind == "system" else "knowledge"


def in_cluster(note, scope):
    home = cluster_for(note)
    return scope is None or home == scope or home.startswith(scope + "/")


def validate_scope(scope):
    if scope is not None and scope not in AREAS and not CLUSTER.fullmatch(scope):
        raise ValueError("Invalid cluster; browse the available areas first")


def cluster_title(scope, notes):
    if scope in AREAS:
        return AREAS[scope]
    members = [n for n in notes.values() if cluster_for(n) == scope]
    members.sort(key=lambda n: (n["id"] != scope.split("/")[-1], n["type"] not in {"project", "work"}, n["id"]))
    return members[0]["title"] if members else scope


def catalog_view(notes, scope=None, limit=10, include_archived=False):
    validate_scope(scope)
    visible = {i: n for i, n in notes.items() if include_archived or not n.get("archived", False)}
    matches = {i: n for i, n in visible.items() if in_cluster(n, scope)}
    if scope is not None and not matches:
        raise ValueError("Unknown or empty cluster: " + scope)
    if scope is None or scope in {"work", "projects", "people"}:
        homes = {cluster_for(n).split("/")[0] if scope is None else cluster_for(n) for n in matches.values()}
        ordered_homes = sorted(homes, key=lambda c: (list(AREAS).index(c) if c in AREAS else len(AREAS), c))
        routes = [{"cluster": c, "title": cluster_title(c, visible), "notes": sum(in_cluster(n, c) for n in visible.values())} for c in ordered_homes]
        return {"scope": scope, "clusters": routes[:limit], "total_clusters": len(routes)}
    ordered = sorted(matches.values(), key=lambda n: (n["status"] == "deprecated", n["id"] != scope.split("/")[-1], n["type"], n["id"]))
    summaries = [{"id": n["id"], "type": n["type"], "title": n["title"], "summary": n["summary"], "status": n["status"], "evidence": n["evidence"], "freshness": freshness(n)} for n in ordered[:limit]]
    cross = []
    seen = set()
    for origin, n in sorted(notes.items(), key=lambda item: (item[0] not in matches, item[0])):
        for edge in n["links"]:
            target = edge["target"]
            if origin not in visible or target not in visible:
                continue
            if (origin in matches) == (target in matches):
                continue
            endpoints = tuple(sorted((origin, target))) if edge["relation"] == "related" else (origin, target)
            key = (edge["relation"], endpoints)
            if key in seen:
                continue
            seen.add(key)
            other = target if origin in matches else origin
            cross.append({"origin": origin, "relation": edge["relation"], "target": target, "other_cluster": cluster_for(notes[other]), "reason": edge.get("reason", ""), "direction": "outgoing" if origin in matches else "incoming"})
    cross.sort(key=lambda e: (e["relation"] not in {"contradicts", "supersedes", "depends_on"}, not bool(e["reason"]), e["origin"], e["target"]))
    return {"scope": scope, "title": cluster_title(scope, notes), "notes": summaries, "total_notes": len(matches), "cross_connections": cross[:limit], "total_cross_connections": len(cross)}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(root, shared=False):
    if not shared:
        root.mkdir(parents=True, exist_ok=True)
    with (root / ".hive.lock").open("r" if shared else "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def encode(note):
    meta = {k: v for k, v in note.items() if k != "body"}
    # JSON values are a deliberately small, valid YAML subset; no YAML dependency.
    return "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in meta.items()) + "\n---\n\n" + note["body"].strip() + "\n"


def decode(text):
    # Parse editor line endings without normalizing the stored revision bytes.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise ValueError("Missing frontmatter")
    header, body = text[4:].split("\n---\n", 1)
    note = {}
    for line in header.splitlines():
        key, value = line.split(":", 1)
        if key in note or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError("Duplicate or invalid metadata key")
        note[key] = json.loads(value)
    note["body"] = body.strip()
    return note


def path_for(root, note):
    if note.get("type") not in KINDS or not ID.fullmatch(note.get("id", "")):
        raise ValueError("Invalid type or ID")
    return root / "wiki" / note["type"] / (note["id"] + ".md")


class IntegrityError(ValueError):
    """An on-disk problem with a message safe to include in a startup hook."""


class ExternalEdits(IntegrityError):
    def __init__(self, changed, added, missing):
        self.changed = changed
        self.added = added
        self.missing = missing
        details = []
        if changed:
            names = ", ".join(changed[:3]) + (", …" if len(changed) > 3 else "")
            details.append("Unjournaled Markdown edits in " + names + ". "
                           + "Run hive adopt " + changed[0] + " --help to review an edit, or restore the journaled version.")
        if added or missing:
            details.append("Untracked or missing note files (" + str(len(added) + len(missing))
                           + "). Keep a copy of local changes and restore the canonical paths.")
        super().__init__(" ".join(details))


def reject_symlinks(root, path):
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            relative = path.relative_to(root).as_posix()
            location = " at " + relative if re.fullmatch(r"[a-zA-Z0-9./_-]+", relative) else ""
            raise IntegrityError("Symlink" + location + ". Restore local paths and run hive check.")


def read_note(path):
    return path.read_bytes().decode("utf-8")


def load_notes(root):
    result = {}
    wiki = root / "wiki"
    for folder in (wiki, *(wiki / kind for kind in KINDS)):
        reject_symlinks(root, folder)
    for path in sorted(wiki.rglob("*.md")):
        if path.name in {"index.md", "log.md"}:
            continue
        reject_symlinks(root, path)
        label = path.stem if ID.fullmatch(path.stem) else "unknown ID"
        try:
            if not path.is_file():
                raise ValueError("Expected a regular note file")
            text = read_note(path)
            note = decode(text)
            if path != path_for(root, note) or note["id"] in result:
                raise ValueError("Duplicate ID or noncanonical path")
        except (ValueError, TypeError, OSError, KeyError) as exc:
            message = "Invalid Markdown note (" + label + "). Repair its format or restore the canonical file, then run hive check."
            raise IntegrityError(message) from exc
        result[note["id"]] = (note, text, path)
    return result


def validate(note, types, require_cluster=False):
    if "archived" in note and not isinstance(note["archived"], bool):
        raise ValueError("archived must be a boolean")
    path_for(Path("."), note)
    if require_cluster and "cluster" not in note:
        raise ValueError("Choose one primary cluster for this note")
    if not isinstance(cluster_for(note), str) or not CLUSTER.fullmatch(cluster_for(note)):
        raise ValueError("Invalid primary cluster")
    for key in ("title", "summary", "body"):
        if not isinstance(note.get(key), str) or not note[key].strip():
            raise ValueError("Required text: " + key)
    if len(note["summary"]) > 280 or len(note["body"]) > 14000:
        raise ValueError("Split the note: summary >280 or body >14000 characters")
    if note.get("status") not in STATUSES or note.get("evidence") not in EVIDENCE:
        raise ValueError("Invalid status or evidence")
    if note.get("sensitivity") not in {"personal", "internal"}:
        raise ValueError("Set sensitivity to personal or internal; do not store secrets")
    for key in ("aliases", "tags"):
        if not isinstance(note.get(key), list) or not all(isinstance(v, str) for v in note[key]):
            raise ValueError("Required string list: " + key)
    if not isinstance(note.get("sources"), list) or not note["sources"]:
        raise ValueError("At least one source is required")
    source_ids = set()
    for source in note["sources"]:
        for key in ("id", "resource", "title"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError("Invalid source " + key)
        if source["id"] in source_ids:
            raise ValueError("Duplicate source ID")
        source_ids.add(source["id"])
        if "observed_at" in source:
            timestamp(source["observed_at"])
    for key in ("created", "stale_after"):
        timestamp(note[key])
    timestamp(note["generated"]["at"])
    if not note["generated"].get("by"):
        raise ValueError("Missing producer")
    for entry in note.get("verified", []):
        timestamp(entry["at"])
        if not entry.get("by"):
            raise ValueError("Missing verifier")
    if __package__:
        from . import work_items
    else:
        import work_items
    work_items.validate(note)
    if __package__:
        from . import knowledge
    else:
        import knowledge
    knowledge.validate(note)
    if not isinstance(note.get("links"), list):
        raise ValueError("links must be a list")
    for edge in note["links"]:
        if edge.get("relation") not in RELATIONS or edge.get("target") not in types:
            raise ValueError("Unknown relation or dangling target: " + str(edge))
        if edge["target"] == note["id"]:
            raise ValueError("Self links are not useful")
        if "reason" in edge and (not isinstance(edge["reason"], str) or not edge["reason"].strip() or len(edge["reason"]) > 280):
            raise ValueError("Link reasons must be nonempty text, at most 280 characters")
    for target in re.findall(r"\]\(([^)]+\.md)(?:#[^)]*)?\)", note["body"]):
        if "://" not in target:
            origin = Path("wiki") / note["type"]
            resolved = Path(os.path.normpath(str(origin / target)))
            allowed = {Path("wiki") / kind / (ident + ".md") for ident, kind in types.items()}
            allowed |= {Path("wiki/index.md"), Path("wiki/log.md")}
            allowed |= {Path("wiki") / kind / "index.md" for kind in types.values()}
            if resolved not in allowed:
                raise ValueError("Dangling Markdown link: " + target)


def events(root):
    directory = root / ".history"
    reject_symlinks(root, directory)
    result = []
    for path in sorted(directory.glob("*.json")):
        reject_symlinks(root, path)
        if not path.is_file():
            raise IntegrityError("Invalid journal file. Inspect .history with hive check.")
        result.append(json.loads(path.read_text(encoding="utf-8")))
    return result


def has_pending(root):
    path = root / ".pending.json"
    return path.is_symlink() or path.exists()


def pending(root):
    path = root / ".pending.json"
    reject_symlinks(root, path)
    if path.exists():
        if not path.is_file():
            raise IntegrityError("Invalid pending transaction file. Inspect .pending.json before recovery.")
        event = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(event, dict) or not event.get("id") or not event.get("changes"):
            raise IntegrityError("Invalid pending transaction record. Preserve .pending.json and inspect the interrupted write.")
        return event
    return None


def require_clean(root):
    if has_pending(root):
        if __package__:
            from .agent_contract import AgentError
        else:
            from agent_contract import AgentError
        raise AgentError('RECOVERY_REQUIRED', "Interrupted write: run hive recover before using the wiki")


def journal_heads(root):
    latest = {}
    try:
        for event in events(root):
            for change in event["changes"]:
                ident = change["id"]
                if (not ID.fullmatch(ident) or change["before"] != latest.get(ident)
                        or not isinstance(change["after"], str)):
                    raise ValueError("Broken history chain")
                latest[ident] = change["after"]
    except IntegrityError:
        raise
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise IntegrityError("The history chain could not be read consistently. Inspect .history and run hive check.") from exc
    return latest


def audit(root, notes):
    latest = journal_heads(root)
    changed = sorted(ident for ident in latest.keys() & notes.keys() if latest[ident] != notes[ident][1])
    added = sorted(notes.keys() - latest.keys())
    missing = sorted(latest.keys() - notes.keys())
    if changed or added or missing:
        raise ExternalEdits(changed, added, missing)


def freshness(note):
    return "stale" if timestamp(note["stale_after"]) <= dt.datetime.now(dt.timezone.utc) else "within_review_window"


def render_indexes(root, notes):
    metadata = {i: n for i, (n, _, _) in notes.items()}
    routes = catalog_view(metadata)["clusters"]
    top = ['---', 'okf_version: "0.2"', '---', '', '# HiveMind · Topics', '', 'Open the relevant area first, then read a few notes and relevant cross-connections.', 'Historical reports still need review; their classification does not establish the current state.', '']
    for route in routes:
        top.append(f'- [{route["title"]}](./clusters/{route["cluster"]}/index.md) — {route["notes"]} notes')
    top.extend(['', '[Historical projects and notes](./archive/index.md) · Outside the current entry point, still available for targeted retrieval.'])
    top.extend(['', '## Alternative: by note type', '', 'These catalogs provide another view of the same notes.', ''])
    for kind in sorted(KINDS):
        entries = [n for n, _, _ in notes.values() if n["type"] == kind and not n.get("archived", False)]
        if not entries:
            atomic(root / "wiki" / kind / "index.md", '# ' + kind + '\n\n[Archive](../archive/index.md)\n')
            continue
        top.append(f'- [{kind}](./{kind}/index.md) — {len(entries)} notes')
        lines = ["# " + kind, "", "[All routes](../index.md)", ""]
        for n in sorted(entries, key=lambda n: n["id"]):
            lines.append(f'- [{n["title"]}](./{n["id"]}.md) — {n["summary"]} [{n["status"]}; {n["evidence"]}]')
        atomic(root / "wiki" / kind / "index.md", "\n".join(lines) + "\n")
    atomic(root / "wiki" / "index.md", "\n".join(top) + "\n")
    archived = {i: n for i, n in metadata.items() if n.get("archived", False)}
    archive_lines = ['# Historical archive', '', '[Current entry point](../index.md)', '', 'This classification reflects the current focus; it does not claim a project has ended. Retrieve with `show ID`, search and browse with `--history`.', '']
    for home in sorted({cluster_for(n) for n in archived.values()}):
        archive_lines.append(f'- [{cluster_title(home, metadata)}](../clusters/{home}/index.md)')
    atomic(root / 'wiki/archive/index.md', '\n'.join(archive_lines) + '\n')
    homes = {cluster_for(n) for n in metadata.values()}
    scopes = homes | {home.split("/")[0] for home in homes}
    expected = set()
    for scope in sorted(scopes):
        path = root / "wiki" / "clusters" / scope / "index.md"
        expected.add(path)
        archived_scope = not any(in_cluster(n, scope) and not n.get("archived", False) for n in metadata.values())
        view = catalog_view(metadata, scope, limit=50, include_archived=archived_scope)
        lines = ["# " + cluster_title(scope, metadata), "", f'[All areas]({os.path.relpath(root / "wiki/index.md", path.parent)})', '']
        if archived_scope:
            lines.extend(['Historical context outside the current entry point.', ''])
        if "clusters" in view:
            for route in view["clusters"]:
                link = os.path.relpath(root / "wiki/clusters" / route["cluster"] / "index.md", path.parent)
                lines.append(f'- [{route["title"]}]({link}) — {route["notes"]} notes')
            if view["total_clusters"] > 50:
                lines.extend(['', 'More areas are available. Search specifically with `search --cluster ' + scope + '`.'])
        else:
            intro = 'Context, decisions, and lessons for this project.' if scope.startswith('projects/') else 'Notes in this area. Follow relevant cross-connections to load other areas.'
            lines.extend([intro, ''])
            for n in view["notes"]:
                link = os.path.relpath(path_for(root, metadata[n["id"]]), path.parent)
                lines.append(f'- [{n["title"]}]({link}) — {n["summary"]} [{n["type"]}; {n["status"]}; {n["evidence"]}; {n["freshness"]}]')
            if view["total_notes"] > 50:
                lines.extend(['', 'More notes are available. Search specifically with `search --cluster ' + scope + '`.'])
            lines.extend(['', '## Cross-connections', ''])
            for edge in view["cross_connections"]:
                origin, target = metadata[edge["origin"]], metadata[edge["target"]]
                a = os.path.relpath(path_for(root, origin), path.parent)
                b = os.path.relpath(path_for(root, target), path.parent)
                explanation = ' — ' + edge["reason"] if edge["reason"] else ''
                lines.append(f'- [{origin["title"]}]({a}) → {edge["relation"]} → [{target["title"]}]({b}){explanation}')
            if not view["cross_connections"]:
                lines.append('No specific connections to other areas yet.')
            if view["total_cross_connections"] > 50:
                lines.append('More connections are available; load them through the relevant note.')
        atomic(path, "\n".join(lines) + "\n")
    # Only generated cluster routes are removed when the last member changes home.
    for obsolete in (root / "wiki/clusters").rglob("index.md"):
        if obsolete not in expected:
            obsolete.unlink()
    log = ["# Change log", ""]
    day = None
    for event in reversed(events(root)):
        if event["at"][:10] != day:
            day = event["at"][:10]
            log.extend(["## " + day, ""])
        log.append(f'- {event["at"]} · {event["actor"]} · {event["reason"]} · IDs: ' + ", ".join(c["id"] for c in event["changes"]) + f' · event `{event["id"]}`')
    atomic(root / "wiki" / "log.md", "\n".join(log) + "\n")


def signature(root):
    return [(str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted((root / "wiki").rglob("*.md")) if p.name not in {"index.md", "log.md"}]


def rebuild(root, notes=None):
    require_clean(root)
    notes = load_notes(root) if notes is None else notes
    audit(root, notes)
    for note, _, _ in notes.values():
        validate(note, {i: n[0]["type"] for i, n in notes.items()})
    render_indexes(root, notes)
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=cache, suffix=".sqlite")
    os.close(fd)
    try:
        with contextlib.closing(sqlite3.connect(temp)) as db, db:
            db.execute("CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED, title, summary, terms, body, tokenize='unicode61 remove_diacritics 2')")
            db.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, metadata TEXT, revision TEXT, path TEXT)")
            db.execute("CREATE TABLE edges (origin TEXT, relation TEXT, target TEXT)")
            for ident, (n, text, path) in notes.items():
                if __package__:
                    from . import knowledge
                else:
                    import knowledge
                db.execute("INSERT INTO search VALUES (?, ?, ?, ?, ?)", (ident, n["title"], n["summary"], " ".join(n["aliases"] + n["tags"]), n["body"] + '\n\n' + knowledge.render(n)))
                db.execute("INSERT INTO notes VALUES (?, ?, ?, ?)", (ident, json.dumps({k:v for k,v in n.items() if k != "body"}), digest(text), str(path.relative_to(root))))
                db.executemany("INSERT INTO edges VALUES (?, ?, ?)", [(ident, e["relation"], e["target"]) for e in n["links"]])
            db.execute("CREATE TABLE state (signature TEXT)")
            db.execute("INSERT INTO state VALUES (?)", (json.dumps(signature(root)),))
        os.replace(temp, cache / "search.sqlite")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return len(notes)


@contextlib.contextmanager
def database(root):
    path = root / ".cache" / "search.sqlite"
    valid = False
    if path.exists():
        try:
            with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                valid = db.execute("SELECT signature FROM state").fetchone()[0] == json.dumps(signature(root))
        except sqlite3.DatabaseError:
            pass
    if not valid:
        with locked(root):
            rebuild(root)
    with locked(root, shared=True):
        require_clean(root)
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT signature FROM state").fetchone()[0] != json.dumps(signature(root)):
                raise ValueError("Concurrent index change: retry or run reindex")
            yield db


def _local_index_paths(root):
    wiki = root / "wiki"
    routes = wiki / "clusters"
    for path in (root / ".cache", wiki / "archive", routes, *(wiki / kind for kind in KINDS)):
        reject_symlinks(root, path)
    # Existing cluster folders can redirect the reconstruction of nested indexes.
    for path in routes.rglob("*"):
        if path.name != "index.md" and path.is_symlink():
            reject_symlinks(root, path)


def finish(root, event):
    adopted = event.get("operation") == "adopt"
    if adopted:
        _local_index_paths(root)
    # Adoption records observed bytes without ever overwriting the editor's file.
    # Ordinary recovery accepts either the old or already-written new state.
    for change in event["changes"]:
        n = decode(change["after"])
        path = path_for(root, n)
        reject_symlinks(root, path)
        if path.exists() and not path.is_file():
            raise IntegrityError("Recovery conflict; preserve pending transaction: " + n["id"])
        actual = read_note(path) if path.exists() else None
        allowed = (change["after"],) if adopted else (change["before"], change["after"])
        if actual not in allowed:
            raise IntegrityError("Recovery conflict; preserve pending transaction: " + n["id"])
    journal = root / ".history" / (event["id"] + ".json")
    if journal.parent != root / ".history":
        raise IntegrityError("Invalid pending event path. Preserve .pending.json.")
    reject_symlinks(root, journal)
    if not adopted:
        for change in event["changes"]:
            atomic(path_for(root, decode(change["after"])), change["after"])
    serialized = json.dumps(event, ensure_ascii=False, indent=2) + "\n"
    if journal.exists() and journal.read_text(encoding="utf-8") != serialized:
        raise IntegrityError("History event collision")
    if not journal.exists():
        atomic(journal, serialized)
    if adopted:
        # Invalidate the cache before completing the transaction even if an
        # editor preserved the original file size and mtime.
        (root / ".cache/search.sqlite").unlink(missing_ok=True)
    (root / ".pending.json").unlink()
    if adopted:
        try:
            rebuild(root)
        except (IntegrityError, OSError, sqlite3.Error):
            return False
    else:
        rebuild(root)
    return True


def commit(root, entries, actor, reason, context, at, *, operation=None):
    event = {"id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8],
             "at": at, "actor": actor, "reason": reason, "context": context, "changes": entries}
    if operation:
        event["operation"] = operation
    atomic(root / ".pending.json", json.dumps(event, ensure_ascii=False, indent=2) + "\n")
    return event["id"], finish(root, event)


def _diff_lines(raw):
    """Show nonprinting bytes and line endings without putting controls on a terminal."""
    for line in raw.splitlines(keepends=True):
        line = line.removesuffix("\n") if line.endswith("\n") else line + " [no LF]"
        yield "".join(ch if ch.isprintable() or ch == "\t" else ascii(ch)[1:-1] for ch in line)


def adopt(root, ident, actor, reason, context, *, apply_edit=False, preview=None):
    """Record the exact external bytes of one existing note after review."""
    if __package__:
        from .agent_contract import AgentError
        from .runtime_paths import cli_command
    else:
        from agent_contract import AgentError
        from runtime_paths import cli_command
    if not ID.fullmatch(ident):
        raise ValueError("Choose an existing journaled note ID")
    if not actor.strip() or not reason.strip() or not context.strip():
        raise ValueError("Actor, reason and context/source reference are required")
    if apply_edit and not preview:
        raise AgentError("REVISION_CONFLICT", "A matching prior --preview token is required. Preview the edit first.")
    lock = root / ".hive.lock"
    reject_symlinks(root, lock)
    if not lock.is_file():
        raise ValueError("Choose an existing vault with its write lock")
    with locked(root, shared=not apply_edit):
        require_clean(root)
        _local_index_paths(root)
        latest = journal_heads(root)
        if ident not in latest:
            raise ValueError("Choose an existing journaled note ID")
        before = latest[ident]
        notes = {key: decode(text) for key, text in latest.items()}
        old = notes[ident]
        path = path_for(root, old)
        reject_symlinks(root, path)
        if not path.is_file():
            raise ValueError("Canonical note file is missing or has moved. Restore " + str(path.relative_to(root)))
        after = read_note(path)
        if before == after:
            raise ValueError("No external edit for " + ident + ". Run hive check for other changes.")
        relative = str(path.relative_to(root))
        binding = {"version": 1, "root": str(root), "id": ident, "path": relative,
                   "before_revision": digest(before), "after_revision": digest(after),
                   "actor": actor, "reason": reason, "context": context}
        token = digest(json.dumps(binding, sort_keys=True, ensure_ascii=False))
        if apply_edit and token != preview:
            raise AgentError("REVISION_CONFLICT", "The preview no longer matches the note or provenance. Preview again.")
        updated = decode(after)
        if updated.get("id") != ident or updated.get("type") != old["type"] or updated.get("created") != old["created"]:
            raise ValueError("A journaled note's ID, type, location and creation time must remain unchanged.")
        notes[ident] = updated
        types = {key: note["type"] for key, note in notes.items()}
        try:
            for key, note in notes.items():
                validate(note, types, require_cluster=(key == ident))
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("Invalid note metadata. Repair the frontmatter before previewing again.") from exc
        result = {key: binding[key] for key in ("id", "path", "before_revision", "after_revision", "actor", "reason", "context")}
        if apply_edit:
            reject_symlinks(root, path)
            if read_note(path) != after:
                raise AgentError("REVISION_CONFLICT", "The file changed during adoption. Preview again.")
            event, indexed = commit(root, [{"id": ident, "before": before, "after": after}],
                                    actor, reason, context, now(), operation="adopt")
            detail = "Saved external edit to journal event " + event + "."
            if not indexed:
                detail += " Run hive check, then hive reindex to refresh the index."
            return {**result, "status": "adopted", "event": event, "indexed": indexed, "text": detail}
        difference = "\n".join(difflib.unified_diff(list(_diff_lines(before)), list(_diff_lines(after)),
                                                     "journal/" + relative, "working/" + relative, lineterm=""))
        command = shlex.join(cli_command(root, "adopt", ident, "--actor", actor, "--reason", reason,
                                         "--context", context, "--apply", "--preview", token))
        text = "Review external edit to " + relative + "\n\n" + difference + "\n\nApply with:\n" + command
        return {**result, "status": "preview", "preview": token, "diff": difference, "text": text}


def apply(root, payload, actor, reason, context, respect_pause=False):
    if not actor.strip() or not reason.strip() or not context.strip():
        raise ValueError("Actor, reason and context/source reference are required")
    with locked(root):
        require_clean(root)
        if respect_pause:
            if __package__:
                from . import automation
            else:
                import automation
            automation.require_running(root)
        current = load_notes(root)
        audit(root, current)
        expected_revisions = payload.get("expected_revisions", {})
        if not isinstance(expected_revisions, dict):
            raise ValueError("expected_revisions must map existing note IDs to revisions")
        for ident, expected_revision in expected_revisions.items():
            if ident not in current or not isinstance(expected_revision, str):
                raise ValueError("Read precondition requires an existing note and revision")
            if digest(current[ident][1]) != expected_revision:
                raise ValueError("Revision conflict: reread and preview " + ident)
        changes = payload.get("changes", [])
        if not changes:
            raise ValueError("No changes supplied")
        ids = [c["note"]["id"] for c in changes]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate IDs in batch")
        at = now()
        entries = []
        prospective = dict(current)
        for change in changes:
            n = dict(change["note"])
            ident = n["id"]
            old = current.get(ident)
            expected = digest(old[1]) if old else "new"
            if change.get("expected_revision") != expected:
                raise ValueError("Revision conflict: reread and merge " + ident)
            if old and n["type"] != old[0]["type"]:
                raise ValueError("Type is immutable; create a replacement and deprecate old note")
            n["created"] = old[0]["created"] if old else at
            n["generated"] = {"by": actor, "at": at}
            types = {i: record[0]["type"] for i, record in current.items()}
            types.update({c["note"]["id"]: c["note"]["type"] for c in changes})
            validate(n, types, require_cluster=True)
            after = encode(n)
            entries.append({"id": ident, "before": old[1] if old else None, "after": after})
            prospective[ident] = (n, after, path_for(root, n))
        for n, _, _ in prospective.values():
            validate(n, {i: record[0]["type"] for i, record in prospective.items()})
        event, _ = commit(root, entries, actor, reason, context, at)
        return {"event": event, "updated": ids}


SUFFIXES = ("ern", "en", "er", "es", "et", "e", "s", "n", "t")
SEARCH_STOPWORDS = set("""aber alle allem allen aller alles als also am an auch auf aus bei bin bis bist da dabei
dadurch daher darum das dass dem den denn der des die dies diese diesem diesen dieser dieses doch dort du durch
ein eine einem einen einer eines er es etwas euch fur gegen gibt habe haben hat hatte hier ich ihr ihre im in ins
ist ja jeder jedes kann kannst kein keine konnen konnte man mein meine meinen meiner mir mit muss nach nicht
noch nun nur ob oder ohne sein seine selbst sich sie sind so soll sollen statt uber um und uns unser unsere unter
vom von vor war waren was welche welchem welchen welcher welches wenn wer werde werden wie wieder wir wird
wo woher woran worauf warum wurde zu zum zur zwischen weiss weisst wissen bitte zeig zeigen sag sagen mochte
mochten hat haben wird werden lauft laufen kommen kommt liegt liegen gemacht getroffen ansehen dazu daruber
the a an and are as at be by can did do does for from how i in is it me my of on or our that this to was we what when
where which who why will with you your about tell show know only weiß weißt beim liebsten bekommt bekommen
gehen geht mehrere mehr etwas dazu""".split())
SEARCH_DECISION_VERBS = {"decide", "decided", "entscheiden", "entschieden", "beschlossen", "festgelegt", "gewahlt"}
SEARCH_USE_VERBS = {"use", "uses", "used", "using", "nutzen", "nutzt", "verwenden", "verwendet", "benutzen", "benutzt"}
DATABASE_TERMS = {"database", "databases", "datenbank", "datenbanken", "datenbanksystem", "datenbanksysteme"}
POSTGRES_TERMS = {"postgres", "postgresql"}
DATABASE_PRODUCTS = POSTGRES_TERMS | {"sqlite"}
SEARCH_GENERIC_ANCHORS = {"entscheidung", "projekt", "erfahrung", "kontext", "stand", "daten", "information",
                          "frage", "thema", "system", "arbeit", "notiz", "wissen", "status", "quelle",
                          "decision", "project", "question", "topic", "information", "data"} | DATABASE_TERMS
SEARCH_LOG_LIMIT = 500


def stem(word):
    """Light German suffix stripping; never shorter than four letters."""
    word = word.lower()
    return next((word[:-len(s)] for s in SUFFIXES if word.endswith(s) and len(word) - len(s) >= 4), word)


def search_words(text):
    """Match FTS unicode61's case/diacritic folding without changing stored text."""
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.findall(r"\w+", plain, flags=re.UNICODE)


def query_terms(query):
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("Search query must be text with at most 2,000 characters")
    tokens = search_words(query)
    stop = SEARCH_STOPWORDS
    if tokens and (tokens[0] in SEARCH_STOPWORDS or query.rstrip().endswith("?")):
        stop = stop | SEARCH_DECISION_VERBS | SEARCH_USE_VERBS
    return list(dict.fromkeys(w for w in tokens if w not in stop))[:32]


def _search_forms(word):
    if word in DATABASE_TERMS:
        # A category can match a named product. A product query stays specific.
        return sorted(DATABASE_TERMS | DATABASE_PRODUCTS), None
    if word in POSTGRES_TERMS:
        return sorted(POSTGRES_TERMS), None
    if word in DATABASE_PRODUCTS:
        return [word], None
    prefix = stem(word)
    return [word], prefix if len(prefix) >= 4 else None


def matched_terms(terms, text):
    tokens = set(search_words(text))
    matched = []
    for word in terms:
        exact, prefix = _search_forms(word)
        if not tokens.isdisjoint(exact) or (prefix is not None and any(t.startswith(prefix) for t in tokens)):
            matched.append(word)
    return matched


def search(root, query, limit=5, include_deprecated=False, cluster=None, include_archived=False):
    validate_scope(cluster)
    words = query_terms(query)
    if not words:
        return []
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Search limit must be between 1 and 100")
    # Literal terms only: no raw FTS syntax or SQL interpolation from input. A stem of at least
    # four letters also matches as prefix, so inflections and compounds ("Kalenders", "Prüfungspräferenz") count.
    forms = {}
    for word in words:
        exact, prefix = _search_forms(word)
        forms.update(dict.fromkeys('"' + term + '"' for term in exact))
        if prefix is not None:
            forms['"' + prefix + '"*'] = None
    expression = " OR ".join(forms)
    with database(root) as db:
        rows = db.execute("SELECT search.id, metadata, revision, path, bm25(search,0,8,5,4,1), search.title, search.summary, search.terms, search.body FROM search JOIN notes ON search.id=notes.id WHERE search MATCH ? ORDER BY 5", (expression,)).fetchall()
        corpus = [json.loads(row[0]) for row in db.execute("SELECT metadata FROM notes")]
    corpus = [n for n in corpus if in_cluster(n, cluster) and (include_archived or not n.get("archived", False)) and (include_deprecated or n["status"] != "deprecated")]
    salient = {n["id"]: matched_terms(words, n["title"] + " " + " ".join(n["aliases"])) for n in corpus}
    frequencies = {w: sum(w in terms for terms in salient.values()) for w in words}
    ranked = []
    query_tokens = set(search_words(query))
    decision_lookup = bool(query_tokens & SEARCH_DECISION_VERBS or
                           (query_tokens & DATABASE_TERMS and query_tokens & SEARCH_USE_VERBS))
    generic_stems = {stem(term) for term in SEARCH_GENERIC_ANCHORS}
    # Require a majority of meaningful terms, or a specific title/alias anchor.
    # An anchor lets a named subject survive unfamiliar descriptive wording.
    # Common question words cannot turn an unknown topic into plausible hits.
    # Coverage is lexical, not proof that a note answers or verifies the query.
    minimum = max(1, (len(words) * 3 + 4) // 5)
    for ident, meta, revision, path, score, title, summary, terms, body in rows:
        n = json.loads(meta)
        if not in_cluster(n, cluster):
            continue
        if n.get("archived", False) and not include_archived:
            continue
        if n["status"] == "deprecated" and not include_deprecated:
            continue
        matched = matched_terms(words, " ".join((title, summary, terms, body)))
        anchors = [w for w in salient.get(ident, []) if frequencies[w] <= max(1, len(corpus) // 8)
                   and stem(w) not in generic_stems]
        if len(matched) < minimum and not anchors:
            continue
        result = {"id": ident, "cluster": cluster_for(n), "title": n["title"], "summary": n["summary"], "status": n["status"], "evidence": n["evidence"], "freshness": freshness(n), "stale_after": n["stale_after"], "revision": revision, "path": str(root / path), "query_terms": words, "matched_terms": matched, "coverage": round(len(matched) / len(words), 3)}
        result["anchor_terms"] = anchors
        ranked.append((-len(matched), decision_lookup and n["type"] != "decision", score, ident, result))
    return [r[-1] for r in sorted(ranked)[:limit]]


def _search_log(root, filename, entry):
    """Caller holds the writer lock; retain bounded local feedback, never note bodies."""
    path = root / ".state" / filename
    lines = path.read_text().splitlines() if path.exists() else []
    atomic(path, "\n".join(lines[-(SEARCH_LOG_LIMIT - 1):] + [json.dumps(entry, ensure_ascii=False)]) + "\n")


def record_miss(root, query, cluster=None):
    """Agent searches without hits become candidates for retrieval cases; private local state only."""
    query_terms(query)
    validate_scope(cluster)
    with locked(root):
        _search_log(root, "search-misses.jsonl", {"at": now(), "query": query, "cluster": cluster})


def feedback(root, query, helpful, result_ids=None, cluster=None, actor="user"):
    """Explicit relevance feedback. A verdict does not verify or modify knowledge."""
    query_terms(query)
    validate_scope(cluster)
    if not query.strip() or not isinstance(helpful, bool):
        raise ValueError("Feedback needs a query and a boolean helpful verdict")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 120:
        raise ValueError("Feedback needs an actor of at most 120 characters")
    ids = [] if result_ids is None else result_ids
    if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Feedback accepts at most 20 unique note IDs")
    with locked(root):
        require_clean(root)
        records = load_notes(root)
        audit(root, records)
        if any(i not in records or not in_cluster(records[i][0], cluster) for i in ids):
            raise ValueError("Feedback references an unknown or out-of-scope note")
        entry = {"id": uuid.uuid4().hex, "at": now(), "actor": actor.strip(), "query": query,
                 "cluster": cluster, "helpful": helpful,
                 "results": [{"id": i, "revision": digest(records[i][1])} for i in ids]}
        _search_log(root, "search-feedback.jsonl", entry)
    return entry


def evaluate(root, cases):
    """Positive retrieval and abstention are measured separately; no answer truth claim."""
    results = []
    for case in cases:
        expected = case.get("expect")
        if not isinstance(expected, list) or not all(isinstance(i, str) for i in expected):
            raise ValueError("Each retrieval case requires expected IDs; [] means unanswerable")
        found = [r["id"] for r in search(root, case["query"], cluster=case.get("cluster"))]
        rank = next((i + 1 for i, ident in enumerate(found) if ident in expected), None)
        results.append({"query": case["query"], "expect": expected, "rank": rank, "found": found})
    misses = root / ".state" / "search-misses.jsonl"
    recent = [json.loads(line)["query"] for line in misses.read_text().splitlines()[-10:]] if misses.exists() else []
    positives = [r for r in results if r["expect"]]
    negatives = [r for r in results if not r["expect"]]
    count = len(positives) or 1
    path = root / ".state" / "search-feedback.jsonl"
    feedback_entries = [json.loads(line) for line in path.read_text().splitlines()[-SEARCH_LOG_LIMIT:]] if path.exists() else []
    return {"cases": len(results), "positive_cases": len(positives), "negative_cases": len(negatives),
            "hit_at_1": round(sum(r["rank"] == 1 for r in positives) / count, 3),
            "hit_at_5": round(sum(r["rank"] is not None for r in positives) / count, 3),
            "rejection_rate": round(sum(not r["found"] for r in negatives) / len(negatives), 3) if negatives else None,
            "false_positive_rate": round(sum(bool(r["found"]) for r in negatives) / len(negatives), 3) if negatives else None,
            "misses": [r for r in positives if r["rank"] is None],
            "false_positives": [r for r in negatives if r["found"]],
            "recent_unanswered_searches": recent,
            "feedback": {"count": len(feedback_entries), "helpful": sum(e.get("helpful") is True for e in feedback_entries),
                         "unhelpful": sum(e.get("helpful") is False for e in feedback_entries)}}


def browse(root, cluster=None, limit=10, include_archived=False):
    with database(root) as db:
        metadata = {i: json.loads(n) for i, n in db.execute("SELECT id, metadata FROM notes")}
    return catalog_view(metadata, cluster, limit, include_archived)


# Project brief: bounded session context derived from canonical notes, without the search cache.
ENTRY = re.compile(r"`(/[^`\s]+)`")
FOOTNOTE = re.compile(r"^\[\^[^\]]+\]:.*$|\[\^[^\]]+\]", re.M)
LABELS = {"preference": "Preference", "person": "Person", "lesson": "Lesson", "decision": "Decision", "project": "Project", "work": "Work context", "system": "System"}
EVIDENCE_LABELS = {"confirmed": "confirmed", "reported": "reported", "inferred": "inferred", "disputed": "disputed"}
# Retain the previous constant for integrations that imported it directly.
GERMAN_EVIDENCE = {"confirmed": "bestätigt", "reported": "berichtet", "inferred": "abgeleitet", "disputed": "umstritten"}
STRONG = {"contradicts": " (contradicts)", "supersedes": " (supersedes)", "depends_on": " (dependency)"}


def active(note):
    return not note.get("archived", False) and note["status"] != "deprecated"


def entry_points(notes):
    """Folders written as `/absolute/path` in active project notes. A folder named by several
    projects, containing another project's folder or enclosing the home folder identifies none."""
    home = Path.home().resolve()
    claims = {}
    for n in notes.values():
        scope = cluster_for(n)
        if not active(n) or not scope.startswith("projects/"):
            continue
        for raw in ENTRY.findall(n["body"]):
            folder = Path(raw).resolve()
            if folder.is_dir() and folder != home and folder not in home.parents:
                claims.setdefault(folder, set()).add(scope)
    unique = {folder: scopes.pop() for folder, scopes in claims.items() if len(scopes) == 1}
    return {folder: scope for folder, scope in unique.items()
            if not any(folder in other.parents and unique[other] != scope for other in unique)}


def german_day(value):
    return timestamp(value).astimezone().strftime("%d.%m.%Y")


def note_label(note):
    kind = LABELS.get(note["type"], note["type"])
    return note["title"] if note["title"].startswith(kind) else kind + " · " + note["title"]


def excerpt(body, words):
    """Leading paragraphs within a word budget, without footnotes; also returns omitted section titles."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", FOOTNOTE.sub("", body))
    kept, omitted, waiting, used = [], [], [], 0
    for block in (b.strip() for b in text.split("\n\n")):
        if block.startswith("#"):
            head, _, block = block.partition("\n")
            if not head.startswith("# "):
                waiting.append(head.lstrip("#").strip())
            block = block.strip()
        if not block:
            continue
        size = len(block.split())
        if used + size > words:
            if used < words * .6:
                # Headings appear only together with text below them.
                kept += ["**" + title + "**" for title in waiting] + [" ".join(block.split()[:words - used]) + " …"]
                waiting = []
            omitted += waiting
            waiting, used = [], words
            continue
        kept += ["**" + title + "**" for title in waiting] + [block]
        waiting, used = [], used + size
    return "\n\n".join(kept), omitted + waiting


def brief(root, target=".", words=170, *, task=None, budget=None, corrections=None):
    """Markdown context for a project folder or projects/<id>; None when no unique project applies."""
    if __package__:
        from . import project_context
    else:
        import project_context
    if task is not None or budget is not None or corrections is not None:
        return project_context.task_brief(root, target, task=task or "", budget=1200 if budget is None else budget,
                                          corrections=corrections)
    resolved = project_context.resolve_project(root, target)
    if resolved is None:
        return None
    with locked(root, shared=(root / ".hive.lock").exists()):
        require_clean(root)
        records = load_notes(root)
        audit(root, records)
    notes = {i: n for i, (n, _, _) in records.items()}
    scope = resolved["cluster"]
    folder = Path(resolved["folder"]) if resolved["folder"] is not None else None
    members = sorted((n for n in notes.values() if active(n) and cluster_for(n) == scope and n["sensitivity"] != "personal"),
                     key=lambda n: (n["type"] != "project", n["id"] != scope.split("/")[-1], n["id"]))
    if not members:
        if folder is None:
            raise ValueError("Unknown or empty cluster: " + scope)
        return None
    lead, ids = members[0], {n["id"] for n in members}
    assurance = [EVIDENCE_LABELS[lead["evidence"]]]
    accepted = lead.get("acceptance")
    if isinstance(accepted, dict) and accepted.get("at"):
        original = EVIDENCE_LABELS.get(accepted.get("original_evidence"))
        assurance.append("accepted on " + german_day(accepted["at"]) + (", originally " + original if original else ""))
    if lead.get("verified"):
        last = max(lead["verified"], key=lambda entry: timestamp(entry["at"]))
        assurance.append("last checked " + german_day(last["at"]) + (" (" + last["scope"].rstrip(".") + ")" if last.get("scope") else ""))
    where = f"`{folder}` → `{scope}`" if folder else f"`{scope}`"
    lines = ["# Project context · " + lead["title"], "",
             f"{where} · edited {german_day(lead['generated']['at'])} · review due {german_day(lead['stale_after'])}",
             "Evidence: " + "; ".join(assurance) + "."]
    if freshness(lead) == "stale":
        lines.append("Review overdue: verify current-state claims before use.")
    text, omitted = excerpt(lead["body"], words)
    lines += ["", "## Status", "", lead["summary"], "", text]
    if omitted:
        lines += ["", f"More sections (`hive note {lead['id']}`): " + " · ".join(omitted) + "."]
    if members[1:]:
        lines += ["", "## In the same project", ""] + [f"- {note_label(n)} — {n['summary']}" for n in members[1:]]
    edges, hidden = [], set()
    for n in notes.values():
        for edge in n["links"]:
            if (n["id"] in ids) == (edge["target"] in ids):
                continue
            other = notes.get(edge["target"] if n["id"] in ids else n["id"])
            if other is None or not active(other):
                continue
            if other["sensitivity"] == "personal":
                hidden.add(other["id"])
                continue
            edges.append((edge["relation"], other, edge.get("reason", "")))
    order = ("preference", "lesson", "decision", "project", "work", "system", "person")
    edges.sort(key=lambda e: (e[0] not in STRONG, not e[2], order.index(e[1]["type"]), e[1]["title"]))
    rows, seen = [], set()
    for relation, other, reason in edges:
        if other["id"] not in seen:
            seen.add(other["id"])
            rows.append(f"- {note_label(other)}{STRONG.get(relation, '')} — {reason or other['summary']}")
    if rows or hidden:
        lines += ["", "## For this work", ""] + rows[:8]
        if len(rows) > 8:
            lines.append(f"- {len(rows) - 8} more via `hive.py browse {scope}`")
        if hidden:
            lines.append(f"- {len(hidden)} personal connection(s) omitted.")
    lines += ["", "## How to use this context", "",
              "“Confirmed” applies only within the stated scope; dated statements are observations, not live status.",
              "More: `" + shlex.join([sys.executable, str(ROOT / 'hive.py'), '--root', str(root), 'show', 'ID']) +
              "` · `" + shlex.join([sys.executable, str(ROOT / 'hive.py'), '--root', str(root), 'search', 'topic', '--cluster', scope]) + "`."]
    return {"cluster": scope, "folder": str(folder) if folder else None, "text": "\n".join(lines) + "\n"}


def session_hook(root, agent='claude', budget=1200):
    """Offer project context and a handoff receipt; unknown folders remain silent."""
    try:
        event = json.loads(sys.stdin.read())
        if __package__:
            from . import project_context
        else:
            import project_context
        if __package__:
            from . import sessions
        else:
            import sessions
        if not isinstance(event, dict):
            return 0
        kind = event.get('hook_event_name', 'SessionStart')
        if kind == 'SessionEnd':
            result = sessions.end_runtime(root, agent, event.get('session_id'), event.get('reason', 'other'))
            print(json.dumps(result))
            return 0
        if kind != 'SessionStart':
            return 0
        target = event.get("cwd")
        if not isinstance(target, str) or not Path(target).is_absolute():
            return 0
        if project_context.resolve_project(root, target) is None:
            return 0
        prefix = ("HiveMind session: {session_id}\n"
                  "Project write reference: {reference}\nSession opened: {opened_at}\n"
                  "Use supplied context. When the task is known, use session-context SESSION_ID --task TASK to recall matching memories. "
                  "Use agent-memory remember --help to save a useful observation. "
                  "Simple handoff: use session-close --record with the project write reference, source and observation time; no session-draft. Read existing decisions/questions with evidence ID@REVISION --section handoff. Only named step_updates change. For requested close without change use session-close --no-change.\n\n")
        offered = sessions.open_session(root, target, agent=agent, budget=budget - len(prefix.split()),
                                        runtime_id=event.get('session_id'),
                                        runtime_source=event.get('source') if event.get('session_id') is not None else None)
        project = offered['workflow']['project']
        reference = project['id'] + '@' + project['revision']
        text = prefix.format(session_id=offered["session_id"], reference=reference,
                             opened_at=offered['opened_at']) + offered["context"]["text"]
    except Exception as error:
        # Only known vault issues become context. Other errors can contain file
        # or event contents and remain host-side diagnostics.
        if isinstance(error, IntegrityError) or getattr(error, "code", None) == "RECOVERY_REQUIRED":
            text = "HiveMind vault needs attention. Project context was withheld. " + str(error)
        else:
            print("HiveMind brief: " + str(error), file=sys.stderr)
            return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))
    return 0


def check(root):
    with locked(root, shared=(root / ".hive.lock").exists()):
        require_clean(root)
        notes = load_notes(root)
        audit(root, notes)
        for n, _, _ in notes.values():
            validate(n, {i: record[0]["type"] for i, record in notes.items()})
    due = []
    for ident, (n, _, _) in notes.items():
        reasons = []
        if "cluster" not in n:
            reasons.append("needs_explicit_cluster")
        if n["status"] != "deprecated" and not n.get("archived", False):
            if freshness(n) == "stale":
                reasons.append("review_due")
            if n["evidence"] in {"reported", "inferred", "disputed"}:
                reasons.append(n["evidence"])
        if reasons:
            due.append({"id": ident, "reasons": reasons})
    return {"valid": True, "notes": len(notes), "clusters": len({cluster_for(n) for n, _, _ in notes.values()}), "events": len(events(root)), "review_queue": due}


def main(argv=None):
    if 'doctor' in sys.argv[1:]:
        sys.dont_write_bytecode = True
    if __package__:
        from . import workflow_cli
    else:
        import workflow_cli
    if __package__:
        from . import agent_contract
    else:
        import agent_contract
    parser = workflow_cli.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_vault())
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, choices=range(1, 21), default=5); p.add_argument("--history", action="store_true"); p.add_argument("--cluster")
    p = sub.add_parser("browse"); p.add_argument("cluster", nargs="?"); p.add_argument("--limit", type=int, choices=range(1, 21), default=10); p.add_argument("--history", action="store_true")
    p = sub.add_parser("brief"); p.add_argument("target", nargs="?", default="."); p.add_argument("--hook", action="store_true"); p.add_argument("--task"); p.add_argument("--budget", type=int); p.add_argument('--agent', choices=('codex', 'claude'), default='claude')
    workflow_cli.dreaming.context_flags(p)
    p = sub.add_parser("eval"); p.add_argument("--cases", type=Path, required=True)
    for cmd in ("show", "neighbors", "history"):
        sub.add_parser(cmd).add_argument("id")
    p = sub.add_parser("draft"); p.add_argument("id"); p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("apply"); p.add_argument("file", type=Path); p.add_argument("--actor", required=True); p.add_argument("--reason", required=True); p.add_argument("--context", required=True)
    for cmd in ("check", "reindex", "recover"):
        sub.add_parser(cmd)
    if __package__:
        from . import workflow_cli
    else:
        import workflow_cli
    workflow_cli.configure(sub)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command in workflow_cli.COMMANDS:
            result = workflow_cli.execute(args, root)
        elif args.command == "search":
            result = search(root, args.query, args.limit, args.history, args.cluster, args.history)
            if not result:
                record_miss(root, args.query, args.cluster)
        elif args.command == "eval":
            result = evaluate(root, json.loads((root / args.cases).read_text())["cases"])
        elif args.command == "browse":
            result = browse(root, args.cluster, args.limit, args.history)
        elif args.command == "brief":
            corrections = workflow_cli.dreaming.request(args)
            if args.hook:
                if corrections is not None:
                    raise ValueError('Dreaming flags require an explicit project brief or session')
                return session_hook(root, agent=args.agent, budget=args.budget if args.budget is not None else 1200)
            result = brief(root, args.target, task=args.task, budget=args.budget, corrections=corrections)
            if result is None:
                raise ValueError("No unique HiveMind project for this folder: " + args.target)
        elif args.command == "apply":
            result = apply(root, json.loads(args.file.read_text()), args.actor, args.reason, args.context)
        elif args.command == "check":
            result = check(root)
        elif args.command in {"reindex", "recover"}:
            with locked(root):
                event = pending(root) if args.command == "recover" else None
                if event is not None:
                    finish(root, event)
                    result = {"recovered": True}
                else:
                    result = {"indexed": rebuild(root)}
        elif args.command == "history":
            result = [{k: v for k, v in e.items() if k != "changes"} | {"revision": digest(c["after"]), "previous_revision": digest(c["before"]) if c["before"] else None} for e in events(root) for c in e["changes"] if c["id"] == args.id]
        else:
            with database(root) as db:
                row = db.execute("SELECT metadata, revision, path FROM notes WHERE id=?", (args.id,)).fetchone()
                if not row:
                    raise ValueError("Unknown ID: " + args.id)
                if args.command == "neighbors":
                    result = [{"origin": o, "relation": r, "target": t} for o, r, t in db.execute("SELECT * FROM edges WHERE origin=? OR target=?", (args.id, args.id))]
                else:
                    text = read_note(root / row[2])
                    if digest(text) != row[1]:
                        raise ValueError("Concurrent change: retry read")
                    note = decode(text)
                    if args.command == "draft":
                        with args.out.open("x") as out:
                            json.dump({"changes": [{"expected_revision": row[1], "note": note}]}, out, ensure_ascii=False, indent=2)
                        result = {"draft": str(args.out)}
                    else:
                        result = {"revision": row[1], "freshness": freshness(note), "note": note}
        if __package__:
            from .context_packet import wire
        else:
            from context_packet import wire
        print(wire(result), end='')
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(json.dumps(agent_contract.failure(error), ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.modules["hive"] = sys.modules[__name__]
    sys.exit(main())
