"""Read-only provider input and strictly data-only, revision-bound proposals."""
import copy
import json
import os
from pathlib import Path
import stat

from . import hive
from .integrations import ingest
from .runtime_paths import require_vault

MAX_PACKET = 2_000_000
MAX_CHANGES = 64


def safe_tree(root):
    root = require_vault(Path(root).resolve())
    for name in ('.state', 'config', '.history', 'wiki'):
        parent = root / name
        if parent.is_symlink():
            raise ValueError('Sync state and knowledge directories must not be symlinks.')
        if parent.exists():
            for path in parent.rglob('*'):
                if path.is_symlink():
                    raise ValueError('Sync state and knowledge paths must not be symlinks.')
    return root


def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PACKET:
                raise ValueError('Provider proposal is not a bounded regular file.')
            raw = handle.read(MAX_PACKET + 1)
            if len(raw) > MAX_PACKET:
                raise ValueError('Provider proposal exceeds the size limit.')
            return json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError('Provider did not return a valid bounded JSON proposal.') from exc


def prepare(root, run_id, stage):
    root = safe_tree(root)
    if not ingest.RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid source run ID.')
    manifest = read_json(root / '.state/runs' / run_id / 'manifest.json')
    if manifest.get('id') != run_id:
        raise ValueError('Source manifest does not match its run.')
    with hive.locked(root):
        hive.require_clean(root)
        records = hive.load_notes(root)
        hive.audit(root, records)
    projects = {hive.cluster_for(n) for n, _, _ in records.values() if n['type'] == 'project'}
    source_clusters = {}
    source_errors = {}
    for ident, source in manifest['source_config'].items():
        cluster = source.get('project')
        if cluster is None and len(projects) == 1:
            cluster = next(iter(projects))
        if cluster is not None:
            if cluster not in projects:
                source_errors[ident] = 'Selected source project is not registered in this vault.'
            else:
                source_clusters[ident] = cluster
    clusters = set(source_clusters.values())
    notes = [{'note': note, 'revision': hive.digest(raw)} for note, raw, _ in records.values()
             if hive.cluster_for(note) in clusters and note.get('sensitivity') != 'personal']
    documents = []
    for row in manifest['local']:
        if row['status'] != 'ready':
            continue
        try:
            document = ingest._read(root, manifest['source_config'][row['id']])
            if document['sha256'] != row['sha256'] or document['path'] != row['path']:
                raise ValueError('Selected local source changed after preparation.')
            documents.append(document)
        except (OSError, ValueError):
            source_errors[row['id']] = 'Selected local source changed or became unavailable after preparation.'
    checkpoints = ingest._state(root)['checkpoints']
    packet = {'schema_version': 1, 'manifest': manifest, 'source_clusters': source_clusters,
              'source_errors': source_errors, 'documents': documents, 'notes': notes,
              'checkpoints': {ident: {key: value for key, value in checkpoints[ident].items()
                                     if key in {'sha256', 'item_versions', 'completed_at'}}
                              for ident in manifest['source_config'] if ident in checkpoints}}
    raw = json.dumps(packet, ensure_ascii=False)
    if len(raw.encode()) > MAX_PACKET:
        raise ValueError('Prepared source context exceeds 2 MB; select fewer sources or narrower projects.')
    (Path(stage) / 'input.json').write_text(raw)
    (Path(stage) / 'input.json').chmod(0o600)
    return packet


def validate_receipt_shape(row, kind):
    common = {'id', 'kind', 'status', 'reason'}
    if row.get('status') != 'complete':
        allowed = common
    elif kind == 'local':
        allowed = common | {'sha256', 'disposition', 'note_ids'}
    else:
        allowed = common | {'window', 'coverage', 'items', 'counts'}
    if set(row) - allowed:
        raise ValueError('Source receipts may contain only coverage and citation fields, not raw content.')
    if kind == 'remote' and row.get('status') == 'complete':
        coverage = row.get('coverage')
        counts = row.get('counts')
        if not isinstance(counts, dict) or set(counts) != {'examined'}:
            raise ValueError('Invalid remote count fields.')
        fields = {'complete', 'pages_complete', 'permission', 'continuation', 'page_count'}
        if row['id'] == 'wispr':
            fields |= {'modified_feed_complete', 'streams'}
        if not isinstance(coverage, dict) or set(coverage) - fields:
            raise ValueError('Invalid remote coverage fields.')
        for stream in coverage.get('streams', {}).values():
            if not isinstance(stream, dict) or set(stream) != {'complete', 'pages_complete', 'continuation', 'page_count'}:
                raise ValueError('Invalid stream coverage fields.')


def curated_ids(row, kind):
    if row.get('status') != 'complete':
        return []
    if kind == 'local':
        return row.get('note_ids', []) if row.get('disposition') == 'curated' else []
    return [ident for item in row.get('items', []) if isinstance(item, dict)
            and item.get('disposition') == 'curated' for ident in item.get('note_ids', [])]


def apply_proposal(root, packet, proposal):
    root = safe_tree(root)
    manifest = packet['manifest']
    run_id = manifest['id']
    if (not isinstance(proposal, dict) or set(proposal) != {'schema_version', 'run_id', 'changes', 'receipts'}
            or proposal['schema_version'] != 1 or proposal['run_id'] != run_id
            or not isinstance(proposal['changes'], list) or len(proposal['changes']) > MAX_CHANGES):
        raise ValueError('Malformed source proposal envelope.')
    receipts = proposal['receipts']
    if (not isinstance(receipts, dict) or set(receipts) != {'schema_version', 'run_id', 'sources'}
            or receipts['schema_version'] != 1 or receipts['run_id'] != run_id
            or not isinstance(receipts['sources'], list)):
        raise ValueError('Malformed source receipt envelope.')
    expected = {row['id']: ('local', row) for row in manifest['local']}
    expected.update({ident: ('remote', window) for ident, window in manifest['windows'].items()})
    rows = {}
    for row in receipts['sources']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in rows or row['id'] not in expected:
            raise ValueError('Unknown or duplicate source receipt.')
        rows[row['id']] = row
    if set(rows) != set(expected):
        raise ValueError('Every prepared source requires a receipt.')
    for ident, (kind, _) in expected.items():
        validate_receipt_shape(rows[ident], kind)
    with hive.locked(root):
        hive.require_clean(root)
        current = hive.load_notes(root)
        hive.audit(root, current)
    offered = {row['note']['id']: row for row in packet['notes']}
    prospective = dict(current)
    seen = set()
    editable = {'title', 'summary', 'body', 'aliases', 'tags', 'links', 'sources', 'evidence'}
    changes = copy.deepcopy(proposal['changes'])
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'expected_revision', 'note'} or not isinstance(change['note'], dict):
            raise ValueError('Only revision-bound canonical note changes are accepted.')
        note = change['note']
        ident = note.get('id')
        if not isinstance(ident, str) or ident in seen or ident in {'index', 'log'}:
            raise ValueError('Duplicate or invalid proposed note ID.')
        seen.add(ident)
        old = current.get(ident)
        if old:
            if ident not in offered or change['expected_revision'] != offered[ident]['revision'] or hive.digest(old[1]) != change['expected_revision']:
                raise ValueError('Source proposal revision conflict; rerun against current knowledge.')
            if {k:v for k,v in note.items() if k not in editable} != {k:v for k,v in old[0].items() if k not in editable}:
                raise ValueError('Source proposals cannot change protected note metadata or project workflow.')
            for source in old[0]['sources']:
                if source not in note.get('sources', []):
                    raise ValueError('Source proposals must preserve existing source evidence.')
        elif change['expected_revision'] != 'new':
            raise ValueError('New source notes require a new revision precondition.')
        if not old:
            allowed = editable | {'id', 'type', 'cluster', 'status', 'sensitivity', 'stale_after', 'created', 'generated'}
            if set(note) - allowed:
                raise ValueError('New source notes may contain only canonical content fields.')
            note['created'] = hive.now()
            note['generated'] = {'at': hive.now(), 'by': 'hivemind-sync'}
        if note.get('evidence') != 'reported':
            raise ValueError('Imported source conclusions must retain reported evidence.')
        if not old and (note.get('verified') or note.get('handoff') or note.get('claims') or note.get('experience')):
            raise ValueError('Source proposals cannot invent verification or workflow records.')
        related = [source_id for source_id, row in rows.items()
                   if ident in curated_ids(row, expected[source_id][0])]
        if not related or any(packet['source_clusters'].get(source_id) != hive.cluster_for(note) for source_id in related):
            raise ValueError('Proposed note must belong to the prepared source project and be cited in its receipt.')
        encoded = hive.encode(note)
        hive.decode(encoded)  # Nothing that would strand a pending write reaches apply.
        prospective[ident] = (note, encoded, hive.path_for(root, note))
    types = {ident: row[0]['type'] for ident, row in prospective.items()}
    for change in changes:
        hive.validate(change['note'], types, require_cluster=True)
    state = ingest._state(root)
    if state.get('active_run') != run_id:
        raise ValueError('Source run is no longer active.')
    if read_json(root / '.state/runs' / run_id / 'manifest.json') != manifest:
        raise ValueError('Prepared source manifest changed during the provider run.')
    for ident, (kind, prepared) in expected.items():
        row = rows[ident]
        if row.get('kind') != kind or row.get('status') not in ('complete', 'incomplete', 'blocked', 'failed'):
            raise ValueError('Invalid source receipt kind or status.')
        ingest.text(row.get('reason'), 'source reason')
        if row['status'] == 'complete':
            ingest._validate_receipt(row, prepared, kind, prospective, root, state, check_current=False)
    # Malformed data above rejects the proposal as a whole. Real input drift
    # below affects only that source and changes depending on it.
    failed = dict(packet.get('source_errors', {}))
    for ident, (kind, prepared) in expected.items():
        if kind == 'local' and rows[ident].get('status') == 'complete':
            try:
                ingest._validate_receipt(rows[ident], prepared, kind, prospective, root, state,
                                         source=manifest['source_config'][ident])
            except (OSError, ValueError):
                failed[ident] = 'Selected local source changed or became unavailable after preparation.'
    excluded = set()
    while True:
        blocked_notes = {note for ident in failed for note in curated_ids(rows[ident], expected[ident][0])}
        dependent = {ident for ident, row in rows.items() if blocked_notes.intersection(curated_ids(row, expected[ident][0]))}
        if blocked_notes == excluded and dependent.issubset(failed):
            break
        excluded = blocked_notes
        for ident in dependent:
            failed.setdefault(ident, 'A shared proposed note depends on another source that could not be verified.')
    changes = [change for change in changes if change['note']['id'] not in excluded]
    for ident, reason in failed.items():
        rows[ident] = {'id': ident, 'kind': expected[ident][0], 'status': 'failed', 'reason': reason}
    receipts = {**receipts, 'sources': list(rows.values())}
    if changes:
        hive.apply(root, {'changes': changes}, actor='hivemind-sync',
                   reason='Validated read-only provider proposal', context='source-run:' + run_id)
    return receipts
