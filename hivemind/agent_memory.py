"""Local agent memories and explicitly proposed project lessons."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

if __package__:
    from . import automation, evidence, hive, sessions
    from .agent_contract import AgentError
else:
    import automation
    import evidence
    import hive
    import sessions
    from agent_contract import AgentError


MEMORY_ID = re.compile(r'memory-[0-9a-f]{32}')
MEMORIES_PER_OFFER = 3


def configure(sub):
    command = sub.add_parser('agent-memory', help='Remember observations and propose project lessons')
    actions = command.add_subparsers(dest='action', required=True)
    remember = actions.add_parser('remember', help='Save a task observation for the current agent and project')
    remember.add_argument('session_id')
    remember.add_argument('--topic', required=True)
    remember.add_argument('--attempt', required=True)
    remember.add_argument('--result', required=True)
    remember.add_argument('--when', required=True, help='Describe when the observation applies')
    share = actions.add_parser('share', help='Propose an owned memory as a project lesson with local evidence')
    share.add_argument('session_id')
    share.add_argument('memory_id')
    share.add_argument('--source', required=True, help='Vault-relative text file under sources/ or INBOX/')


def _one_line(value, field, maximum):
    value = value.strip()
    if not value or len(value) > maximum or not value.isprintable():
        raise AgentError('INVALID_ARGUMENT', f'{field} must be one printable line of at most {maximum} characters')
    return value


def _memory_path(root):
    path = root / '.state' / 'agent-memory.sqlite3'
    if path.parent.is_symlink() or path.is_symlink():
        raise AgentError('INVALID_STATE', 'The agent memory store must be local to this vault')
    return path


@contextmanager
def _database(path, write=False):
    if write:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    mode = 'rwc' if write else 'ro'
    with closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        if write:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, project TEXT NOT NULL, agent TEXT NOT NULL,
                topic TEXT NOT NULL, attempt TEXT NOT NULL, result TEXT NOT NULL,
                when_text TEXT NOT NULL, created_at TEXT NOT NULL, session_id TEXT NOT NULL,
                source TEXT, sha256 TEXT)""")
            db.execute('CREATE INDEX IF NOT EXISTS memories_project ON memories(project)')
        yield db


def _source_path(root, resource):
    path = Path(resource)
    if path.drive or path.is_absolute() or '..' in path.parts or path.parts[:1] not in (('sources',), ('INBOX',)):
        raise AgentError('INVALID_ARGUMENT', 'Use a vault-relative source file under sources/ or INBOX/')
    if any((root / part).is_symlink() for part in (path, *path.parents)):
        raise AgentError('INVALID_ARGUMENT', 'The memory evidence path must not use symlinks')
    return path


def _remember(path, session, args):
    fields = (
        _one_line(args.topic, 'Topic', 200),
        _one_line(args.attempt, 'Attempt', 400),
        _one_line(args.result, 'Result', 600),
        _one_line(args.when, 'When', 240),
    )
    scope = (session['cluster'], session['agent'])
    ident = 'memory-' + hive.digest(json.dumps((*scope, *fields), ensure_ascii=False))[:32]
    with _database(path, write=True) as db:
        db.execute("""INSERT INTO memories
            (memory_id, project, agent, topic, attempt, result, when_text, created_at, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(memory_id) DO NOTHING""",
            (ident, *scope, *fields, hive.now(), session['session_id']))
    return {'memory_id': ident, 'status': 'remembered'}


def _share(root, db, session, args):
    ident = args.memory_id
    if not MEMORY_ID.fullmatch(ident):
        raise AgentError('INVALID_ARGUMENT', 'Use a valid agent memory ID')
    row = db.execute('SELECT source, sha256 FROM memories WHERE memory_id=? AND project=? AND agent=?',
                     (ident, session['cluster'], session['agent'])).fetchone()
    if row is None:
        raise AgentError('INVALID_ARGUMENT', 'Choose a memory owned by this agent in this project')
    path = _source_path(root, _one_line(args.source, 'Source', 1000))
    source = path.as_posix()
    state, _, sha256 = evidence._source(root, {'resource': quote(source)})
    if state != 'unhashed_current_file':
        raise AgentError('INVALID_ARGUMENT', 'Sharing requires readable local text evidence: ' + state)
    if row['source'] is None:
        db.execute('UPDATE memories SET source=?, sha256=? WHERE memory_id=?', (source, sha256, ident))
    elif (row['source'], row['sha256']) != (source, sha256):
        raise AgentError('INVALID_ARGUMENT', 'This memory is already shared with different evidence')
    return {'memory_id': ident, 'status': 'proposed', 'source': source, 'source_sha256': sha256}


def execute(args, root):
    root = Path(root).resolve()
    path = _memory_path(root)
    with sessions._locked(root), hive.locked(root):
        hive.require_clean(root)
        automation.require_running(root)
        session = sessions._read(root, args.session_id)
        if session['status'] != 'context_offered':
            raise AgentError('SESSION_CLOSED', 'Open a new session before writing agent memory')
        if args.action == 'remember':
            return _remember(path, session, args)
        if not path.is_file() or not path.stat().st_size:
            raise AgentError('INVALID_ARGUMENT', 'Choose a memory owned by this agent in this project')
        with _database(path, write=True) as db:
            return _share(root, db, session, args)


def _current_evidence(root, row):
    try:
        path = _source_path(root, row['source'])
        state, _, _ = evidence._source(root, {'resource': quote(path.as_posix()), 'sha256': row['sha256']})
        return state == 'hash_matched'
    except (OSError, ValueError, RuntimeError):
        return False


def select(root, project, agent, task, budget):
    """Select complete memory blocks for an agent's current project task."""
    if not agent or not task or budget <= 0:
        return []
    terms = [term for term in hive.query_terms(task) if term not in hive.SEARCH_GENERIC_ANCHORS]
    if not terms:
        return []
    root = Path(root).resolve()
    path = _memory_path(root)
    if not path.is_file() or not path.stat().st_size:
        return []
    with _database(path) as db:
        rows = db.execute('SELECT * FROM memories WHERE project=? AND (agent=? OR source IS NOT NULL)',
                          (project, agent)).fetchall()
    candidates = []
    for row in rows:
        coverage = len(hive.matched_terms(terms, row['topic'] + ' ' + row['when_text']))
        if coverage:
            candidates.append((coverage, row))
    if not candidates:
        return []
    scores = sessions.memory_scores(root, project, agent, {row['memory_id'] for _, row in candidates})
    candidates.sort(key=lambda item: (-item[0], -scores.get(item[1]['memory_id'], 0), item[1]['memory_id']))

    offered = []
    for _, row in candidates:
        ident = row['memory_id']
        own = row['agent'] == agent
        label = 'Own agent memory' if own else 'Proposed project lesson'
        lines = [f"{label} {ident} · {row['topic']}",
                 f"Agent observation by {row['agent']} ({row['created_at']})",
                 'When: ' + row['when_text'], 'Attempt: ' + row['attempt'], 'Result: ' + row['result']]
        if not own:
            lines.append(f"Source: {row['source']} (SHA-256 {row['sha256']})")
        text = '\n'.join(lines)
        words = len(text.split())
        if words > budget or (not own and not _current_evidence(root, row)):
            continue
        offered.append({'id': ident, 'text': text})
        budget -= words
        if len(offered) == MEMORIES_PER_OFFER:
            break
    return offered
