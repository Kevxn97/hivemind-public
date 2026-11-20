"""Small, revision-addressed agent responses; budget the actual UTF-8 wire bytes."""
import json

if __package__:
    from .agent_contract import AgentError
else:
    from agent_contract import AgentError

DEFAULT_BYTES = 12000


def wire(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def byte_limit(value):
    value = DEFAULT_BYTES if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 4096 <= value <= 65536:
        raise AgentError('INVALID_ARGUMENT', 'max-bytes must be 4096..65536 UTF-8 bytes')
    return value


def measure(value):
    # Count the final envelope and LF too; stabilize the decimal byte counter.
    for _ in range(8):
        size = len(wire(value).encode('utf-8'))
        if value['budget']['bytes'] == size:
            return size
        value['budget']['bytes'] = size
    raise AgentError('INTERNAL_ERROR', 'Could not measure response')


def finish(value, maximum):
    if measure(value) > maximum:
        raise AgentError('LIMIT_EXCEEDED', 'Response metadata exceeds max-bytes; request a larger budget')
    return value


def compact(receipt, context, workflow, maximum=None, *, rendered_refs=None):
    maximum = byte_limit(maximum)
    selected = context['selected']
    candidates = {r['id'] + '@' + r['revision']: r for r in selected}
    for row in workflow['changes']['items'] + workflow['lessons']['items']:
        candidates.setdefault(row['id'] + '@' + row['revision'], row)
    refs = [{'ref': row['id'] + '@' + row['revision'], 'title': row['title'][:120],
             'evidence': row['evidence'], 'freshness': row['freshness']} for row in candidates.values()]
    result = {'contract_version': 1, 'ok': True, 'packet_version': 1, 'view': 'compact',
              **{k: receipt[k] for k in ('session_id', 'cluster', 'status', 'previous_context_at')},
              'context': {'text': '', 'budget': context['budget'], 'word_count': 0,
                          'task_match': False if context.get('task_match') is not None else None},
              'refs': refs, 'offered': [],
              'omitted': {'references': 0, 'text_blocks': 0, 'workflow_details': True},
              'next_commands': ['session-close ' + receipt['session_id'] + ' --record --expected ' + workflow['project']['id'] + '@' + workflow['project']['revision'] + ' --source sources/FILE.md --source-title TITLE --observed-at TIME',
                                'evidence ID@REVISION --section handoff', 'evidence ID@REVISION', 'evidence ID@REVISION --section SECTION',
                                'evidence ID@REVISION --source SOURCE_ID',
                                'verify-plan ID@REVISION', 'review-plan ' + receipt['cluster'],
                                'session-context ' + receipt['session_id'] + ' --view full',
                                'agent-memory remember --help'],
              'notice': 'Stored evidence, not execution permission or a live verification. Missing context is not proof of absence.',
              'budget': {'max_bytes': maximum, 'bytes': 0, 'unit': 'UTF-8 bytes including JSON envelope and newline'}}
    for key in ('runtime', 'previous_session_id'):
        if key in receipt:
            result[key] = receipt[key]
    # Leave room for useful prose, not a growing receipt or duplicated sources.
    while True:
        result['offered'] = [{'id': r['ref'].split('@')[0], 'revision': r['ref'].split('@')[1]} for r in refs]
        if measure(result) <= maximum * .45 or len(refs) == 1:
            break
        refs.pop()
        result['omitted']['references'] += 1
    finish(result, maximum)
    included = {r['ref'] for r in refs}
    groups = {b['ref']: b for b in context['_reading_blocks'] if b['ref'] in included}
    blocks = []
    # Start with project facts and decisions; then give each note a first excerpt.
    for ref, group in groups.items():
        if group['paragraphs']:
            blocks.append((ref, group['paragraphs'][0]))
    rule = context.get('_dreaming_rule')
    rule_block = (None, rule['text']) if rule and blocks else None
    if rule_block is not None:
        blocks.insert(1, rule_block)
    memory_blocks = {memory['id']: (None, memory['text']) for memory in context.get('_agent_memories', [])}
    memory_index = 2 if rule_block is not None else 1
    blocks[memory_index:memory_index] = memory_blocks.values()
    if context.get('task_match') is False:
        blocks.append((None, 'No matching task evidence found.'))
    blocks.extend((None, paragraph) for paragraph in context['_supplement'])
    for index in range(1, max((len(g['paragraphs']) for g in groups.values()), default=0)):
        for ref, group in groups.items():
            if index < len(group['paragraphs']):
                blocks.append((ref, group['paragraphs'][index]))

    def render(entries):
        paragraphs, note_parts = [], {}
        for ref, paragraph in entries:
            if ref is None:
                paragraphs.append(paragraph)
            else:
                note_parts.setdefault(ref, []).append(paragraph)
        # Metadata appears once per note, with its exact generated attribution.
        return '\n\n'.join([groups[ref]['metadata'] + '\n\n' + '\n\n'.join(parts)
                              for ref, parts in note_parts.items()] + paragraphs)

    def report_memories(entries):
        memories = [ident for ident, block in memory_blocks.items() if block in entries]
        if memories:
            result['memory_refs'] = memories
        else:
            result.pop('memory_refs', None)

    accepted = []
    for block in blocks:
        if not accepted and block in memory_blocks.values():
            result['omitted']['text_blocks'] += 1
            continue
        if block is rule_block:
            if not accepted:
                result['omitted']['text_blocks'] += 1
                continue
            result['dreaming'] = {'offered_rule_ids': [rule['id']]}
        entries = [*accepted, block]
        candidate = render(entries)
        report_memories(entries)
        result['context'].update(text=candidate, word_count=len(candidate.split()))
        if len(candidate.split()) <= context['budget'] and measure(result) <= maximum:
            accepted.append(block)
        else:
            if block is rule_block:
                result.pop('dreaming')
            report_memories(accepted)
            result['omitted']['text_blocks'] += 1
    result['context'].update(text=render(accepted), word_count=len(render(accepted).split()))
    # Counter growth can cost a byte at a decimal boundary; drop whole blocks.
    while measure(result) > maximum and accepted:
        if accepted.pop() is rule_block:
            result.pop('dreaming')
        report_memories(accepted)
        result['omitted']['text_blocks'] += 1
        result['context'].update(text=render(accepted), word_count=len(render(accepted).split()))
    if context.get('task_match') is not None:
        shown = {ref for ref, _ in accepted}
        result['context']['task_match'] = bool(result.get('memory_refs')) or any(
            row['matched_terms'] for row in selected if row['id'] + '@' + row['revision'] in shown)
    packet = finish(result, maximum)
    if rendered_refs is not None:
        rendered_refs.update(ref for ref, _ in accepted if ref is not None)
    return packet
