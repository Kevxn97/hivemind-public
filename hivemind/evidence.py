"""Read exact journaled revisions and bounded evidence, without network access."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

if __package__:
    from . import hive
else:
    import hive
if __package__:
    from . import insights
else:
    import insights
if __package__:
    from . import context_packet
else:
    import context_packet
if __package__:
    from .agent_contract import AgentError
else:
    from agent_contract import AgentError

REFERENCE = re.compile(r'([a-z0-9][a-z0-9-]*)@([a-f0-9]{64})\Z')


def revision(root, reference):
    match = REFERENCE.fullmatch(reference)
    if not match:
        raise AgentError('INVALID_ARGUMENT', 'Use an exact NOTE_ID@64-character-revision')
    ident, wanted = match.groups()
    records, history = insights._read(root)
    if ident not in records or records[ident][0]['sensitivity'] == 'personal':
        raise AgentError('INVALID_ARGUMENT', 'Note unavailable in the agent evidence interface')
    raw = records[ident][1]
    if hive.digest(raw) != wanted:
        raw = next((change[key] for event in reversed(history) for change in event['changes']
                    for key in ('after', 'before') if change['id'] == ident and change[key]
                    and hive.digest(change[key]) == wanted), None)
    if raw is None:
        raise AgentError('INVALID_ARGUMENT', 'Revision not found; no fallback to latest')
    note = hive.decode(raw)
    if note['sensitivity'] == 'personal':
        raise AgentError('INVALID_ARGUMENT', 'Historical personal content is unavailable')
    return note, hive.digest(records[ident][1])


def sections(body):
    headings = list(re.finditer(r'(?m)^(#{1,6}) (.+)$', body))
    return [{'title': match[2], 'offset': match.start(),
             'end': next((later.start() for later in headings[index + 1:]
                          if len(later[1]) <= len(match[1])), len(body))}
            for index, match in enumerate(headings)]


def _source(root, source):
    resource = source['resource']
    parsed = urlparse(resource)
    if parsed.scheme not in ('', 'file') or parsed.netloc or parsed.query or parsed.fragment:
        return 'reference_only', '', None
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    # Only explicitly attributed, local text evidence in these two stores.
    # Absolute paths into other repositories remain references, never reads.
    allowed = path.is_relative_to(root) and any(
        not (root / name).is_symlink() and (root / name).resolve().is_relative_to(root)
        and path.is_relative_to((root / name).resolve())
        for name in ('sources', 'INBOX'))
    if not allowed or path.suffix.lower() not in {'.md', '.txt', '.json', '.csv'}:
        return 'reference_only', '', None
    if not path.is_file():
        return 'missing', '', None
    with path.open('rb') as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return 'too_large', '', None
    actual = hashlib.sha256(raw).hexdigest()
    if source.get('sha256') and source['sha256'] != actual:
        return 'changed', '', actual
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return 'unsupported_encoding', '', actual
    return ('hash_matched' if source.get('sha256') else 'unhashed_current_file'), text, actual


def attributed_source(root, ident, resource, title, observed_at):
    """Describe explicit evidence; fingerprint bounded local source bytes.

    External resources remain references. A claimed local source must actually
    be readable text, not a missing file or a symlink silently treated as a URL.
    """
    source = {'id': ident, 'resource': resource, 'title': title, 'observed_at': observed_at}
    if not isinstance(resource, str) or not resource.strip():
        raise AgentError('INVALID_ARGUMENT', 'An attributable source is required')
    root = Path(root).resolve()
    parsed = urlparse(resource)
    if parsed.scheme in ('', 'file') and not parsed.netloc:
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        local = any(candidate.is_relative_to(root / name)
                    for candidate in (path, resolved) for name in ('sources', 'INBOX'))
        if local:
            # System/root aliases can name the vault, but links into or inside
            # an evidence store must not bypass its capture policy.
            if any(p.is_symlink() and not root.is_relative_to(p.resolve())
                   for p in (path, *path.parents)):
                raise AgentError('INVALID_ARGUMENT', 'Local evidence must not use symlinks')
            state, _, digest = _source(root, source)
            if state != 'unhashed_current_file':
                raise AgentError('INVALID_ARGUMENT', 'Local evidence is unavailable: ' + state)
            source['sha256'] = digest
    return source


def read(root, reference, section=None, source_id=None, offset=0, limit=4000, max_bytes=None):
    maximum = context_packet.byte_limit(max_bytes)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, limit)) or offset < 0 or not 1 <= limit <= 16000:
        raise AgentError('INVALID_ARGUMENT', 'offset must be nonnegative; limit must be 1..16000 characters')
    if section is not None and source_id is not None:
        raise AgentError('INVALID_ARGUMENT', 'Choose section or source, not both')
    root = Path(root).resolve()
    note, latest = revision(root, reference)
    result = {'contract_version': 1, 'ok': True, 'view': 'evidence', 'ref': reference,
              'revision': reference.rsplit('@', 1)[1], 'current_revision': latest,
              'historical': reference.rsplit('@', 1)[1] != latest,
              'evidence': note['evidence'], 'freshness': hive.freshness(note),
              'stale_after': note['stale_after'], 'verified': note.get('verified', []),
              'handoff_provenance': {key: note.get('handoff', {}).get(key)
                                     for key in ('evidence', 'source_id', 'observed_at', 'field_sources')},
              'notice': 'Read-only stored evidence. Hash equality verifies bytes, not truth or current runtime state.',
              'budget': {'max_bytes': maximum, 'bytes': 0, 'unit': 'UTF-8 bytes including JSON envelope and newline'}}
    section = section or 'outline'
    if source_id is not None:
        source = next((s for s in note['sources'] if s['id'] == source_id), None)
        if source is None:
            raise AgentError('INVALID_ARGUMENT', 'Source is not attached to this revision')
        state, text, actual = _source(root, source)
        result.update(source=source, source_state=state, observed_sha256=actual)
    elif section == 'outline':
        # Page even a very large source index; no unbounded metadata side channel.
        entries = ([{'kind': 'section', **s} for s in sections(note['body'])] +
                   [{'kind': 'source', 'id': s['id'], 'title': s['title']} for s in note['sources']])
        result.update(summary=note['summary'], sections=[], sources=[], offset=offset, next_offset=None)
        if offset > len(entries):
            raise AgentError('INVALID_ARGUMENT', 'Outline offset exceeds entry count')
        for index in range(offset, min(len(entries), offset + limit)):
            item = dict(entries[index]); kind = item.pop('kind'); key = 'sections' if kind == 'section' else 'sources'
            result[key].append(item); result['next_offset'] = index + 1 if index + 1 < len(entries) else None
            if context_packet.measure(result) > maximum:
                result[key].pop(); result['next_offset'] = index
                if index == offset:
                    raise AgentError('LIMIT_EXCEEDED', 'Outline entry exceeds budget; request larger max-bytes')
                break
        return context_packet.finish(result, maximum)
    elif section in ('claims', 'experience'):
        text = json.dumps(note.get(section, [] if section == 'claims' else None), ensure_ascii=False, indent=2)
    elif section in ('handoff', 'work'):
        if __package__:
            from . import work_items
        else:
            import work_items
        text = json.dumps(note.get('handoff', {}) if section == 'handoff' else work_items.read(note), ensure_ascii=False, indent=2)
    elif section == 'body':
        text = note['body']
    else:
        matches = [s for s in sections(note['body']) if s['title'] == section]
        if len(matches) != 1:
            raise AgentError('INVALID_ARGUMENT', 'Section must match exactly one heading; use outline or body')
        text = note['body'][matches[0]['offset']:matches[0]['end']]
    if offset > len(text):
        raise AgentError('INVALID_ARGUMENT', 'Offset exceeds selected content')
    result.update(section=section if source_id is None else None, offset=offset,
                  total_characters=len(text), next_offset=None, text='')
    # Binary search an exact character slice; never split UTF-8 bytes. A caller
    # can reconstruct the selected content using next_offset even on one long line.
    low, high = 0, min(limit, len(text) - offset)
    while low < high:
        count = (low + high + 1) // 2
        result.update(text=text[offset:offset + count], next_offset=offset + count if offset + count < len(text) else None)
        if context_packet.measure(result) <= maximum: low = count
        else: high = count - 1
    result.update(text=text[offset:offset + low], next_offset=offset + low if offset + low < len(text) else None)
    if low == 0 and offset < len(text):
        raise AgentError('LIMIT_EXCEEDED', 'Evidence metadata exceeds budget; request larger max-bytes')
    return context_packet.finish(result, maximum)
