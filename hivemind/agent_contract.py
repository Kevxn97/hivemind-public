"""Versioned session wire contract, with an offline validator for its JSON Schema subset.

This is deliberately not a general JSON Schema implementation. Unknown keywords,
remote references and recursive references are rejected instead of ignored.
Canonical integrity, revision checks and authorization remain the core's job.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import re

VERSION = 1
SCHEMAS = Path(__file__).resolve().parent / 'schemas'
KINDS = ('receipt', 'session', 'compact-session', 'evidence', 'draft', 'handoff', 'draft-result', 'error', 'feedback', 'feedback-result')
COMMANDS = {'session-open', 'session-context', 'session-draft', 'session-close', 'session-status',
            'session-end', 'session-feedback', 'session-metrics', 'doctor', 'contract', 'evidence',
            'review-plan', 'review-record', 'verify-plan', 'verify-draft', 'learn-draft', 'agent-memory'}
ERROR_CODES = ('INVALID_ARGUMENT', 'SESSION_NOT_FOUND', 'SESSION_EXISTS', 'SESSION_CLOSED',
               'REVISION_CONFLICT', 'INVALID_HANDOFF', 'INVALID_STATE', 'IO_ERROR',
               'RECOVERY_REQUIRED', 'LIMIT_EXCEEDED', 'UNKNOWN_PROJECT', 'INTERNAL_ERROR', 'SCHEMA_INVALID')
KEYWORDS = {'$schema', '$id', '$defs', 'title', 'description', '$ref', 'type', 'const', 'enum',
            'properties', 'required', 'additionalProperties', 'items', 'minItems', 'maxItems',
            'uniqueItems', 'minLength', 'maxLength', 'pattern', 'format', 'minimum', 'maximum',
            'anyOf', 'oneOf', 'allOf'}


class AgentError(ValueError):
    def __init__(self, code, message):
        if code not in ERROR_CODES:
            raise ValueError('Unknown agent error code')
        super().__init__(message)
        self.code = code


def success(command, result):
    if command not in COMMANDS:
        return result
    if not isinstance(result, dict):
        raise AgentError('INTERNAL_ERROR', 'Session contract requires an object result')
    return {**result, 'contract_version': VERSION, 'ok': True}


def failure(error):
    code = (error.code if isinstance(error, AgentError) else 'INVALID_ARGUMENT'
            if isinstance(error, (ValueError, TypeError)) else 'IO_ERROR'
            if isinstance(error, OSError) else 'INTERNAL_ERROR')
    return {'contract_version': VERSION, 'ok': False, 'error_code': code,
            'error': str(error) or type(error).__name__}


def _load(kind):
    if kind not in (*KINDS, 'common'):
        raise AgentError('INVALID_ARGUMENT', 'Unknown contract schema')
    return json.loads((SCHEMAS / (kind + '.schema.json')).read_text())


def _reference(reference, document):
    if not isinstance(reference, str):
        raise AgentError('SCHEMA_INVALID', 'Schema reference must be a string')
    file_name, separator, fragment = reference.partition('#')
    if file_name:
        names = {kind + '.schema.json': kind for kind in (*KINDS, 'common')}
        if file_name not in names:
            raise AgentError('SCHEMA_INVALID', 'Only bundled local schema references are supported')
        document = _load(names[file_name])
    node = document
    if separator and fragment:
        if not fragment.startswith('/'):
            raise AgentError('SCHEMA_INVALID', 'Only JSON Pointer schema fragments are supported')
        try:
            for segment in fragment[1:].split('/'):
                node = node[segment.replace('~1', '/').replace('~0', '~')]
        except (KeyError, TypeError) as error:
            raise AgentError('SCHEMA_INVALID', 'Unresolved schema reference') from error
    return node, document


def _audit(schema, document, trail=()):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or set(schema) - KEYWORDS:
        raise AgentError('SCHEMA_INVALID', 'Unsupported schema keyword or shape')
    if '$ref' in schema:
        reference = schema['$ref']
        if reference in trail:
            raise AgentError('SCHEMA_INVALID', 'Recursive schema references are unsupported')
        node, target = _reference(reference, document)
        _audit(node, target, (*trail, reference))
    if 'format' in schema and schema['format'] != 'date-time':
        raise AgentError('SCHEMA_INVALID', 'Unsupported schema format')
    for name in ('properties', '$defs'):
        for child in schema.get(name, {}).values():
            _audit(child, document, trail)
    for name in ('items', 'additionalProperties'):
        if name in schema:
            _audit(schema[name], document, trail)
    for name in ('allOf', 'anyOf', 'oneOf'):
        for child in schema.get(name, []):
            _audit(child, document, trail)


def _equal(left, right):
    # In JSON, true and 1 are different values; Python's == conflates them.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _check(schema, value, document, location='$'):
    def reject(reason):
        raise AgentError('SCHEMA_INVALID', location + ': ' + reason)
    if isinstance(schema, bool):
        if not schema: reject('value is not allowed')
        return
    if '$ref' in schema:
        node, target = _reference(schema['$ref'], document)
        _check(node, value, target, location)
    expected = schema.get('type')
    if expected is not None:
        actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
                  'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else
                  'string' if isinstance(value, str) else 'object' if isinstance(value, dict) else
                  'array' if isinstance(value, list) else 'unsupported')
        types = expected if isinstance(expected, list) else [expected]
        if actual not in types and not (actual == 'integer' and 'number' in types):
            reject('expected ' + '/'.join(types))
    if 'const' in schema and not _equal(value, schema['const']): reject('unexpected constant')
    if 'enum' in schema and not any(_equal(value, item) for item in schema['enum']): reject('unknown value')
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])): reject('required field missing')
        properties = schema.get('properties', {})
        for name, child in value.items():
            _check(properties.get(name, schema.get('additionalProperties', True)), child, document, location + '.' + name)
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', float('inf')):
            reject('array length outside contract bounds')
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            reject('duplicate array item')
        for index, item in enumerate(value):
            _check(schema.get('items', True), item, document, location + '[' + str(index) + ']')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', float('inf')):
            reject('string length outside contract bounds')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None: reject('invalid identifier or text')
        if schema.get('format') == 'date-time':
            try:
                parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None or 'T' not in value: raise ValueError('timezone required')
            except ValueError:
                reject('expected timestamp with timezone')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get('minimum', -float('inf')) or value > schema.get('maximum', float('inf')):
            reject('number outside contract bounds')
    for child in schema.get('allOf', []):
        _check(child, value, document, location)
    for name in ('anyOf', 'oneOf'):
        if name not in schema: continue
        matches = 0
        for child in schema[name]:
            try:
                _check(child, value, document, location)
                matches += 1
            except AgentError:
                pass
        if matches < 1 or (name == 'oneOf' and matches != 1): reject('no valid ' + name + ' alternative')


def validate_schema(schema, value):
    """Validate against the supported offline subset; reject unsupported schemas."""
    _audit(schema, schema)
    _check(schema, value, schema)
    return value


def validate(kind, value):
    """Validate a named wire shape. Core checks still enforce semantic integrity."""
    return validate_schema(_load(kind), value)


def _expand(schema, document):
    """Export self-contained schemas so clients need no filesystem/network resolver."""
    if isinstance(schema, bool): return schema
    value = copy.deepcopy(schema)
    if '$ref' in value:
        node, target = _reference(value.pop('$ref'), document)
        expanded = _expand(node, target)
        if value:
            return {'allOf': [expanded, _expand(value, document)]}
        return expanded
    for name in ('properties', '$defs'):
        if name in value: value[name] = {key: _expand(child, document) for key, child in value[name].items()}
    for name in ('items', 'additionalProperties'):
        if name in value: value[name] = _expand(value[name], document)
    for name in ('allOf', 'anyOf', 'oneOf'):
        if name in value: value[name] = [_expand(child, document) for child in value[name]]
    return value


def describe(schema=None):
    if schema is not None:
        value = _load(schema)
        _audit(value, value)
        return _expand(value, value)
    return {'contract_version': VERSION, 'name': 'hivemind.session',
            'commands': sorted(COMMANDS), 'schemas': {kind: 'schemas/' + kind + '.schema.json' for kind in KINDS},
            'error_codes': list(ERROR_CODES), 'compatibility': 'CLI context defaults to compact-session packet_version 1; --view full retains the session schema. Existing handoffs and receipts remain accepted.',
            'context_views': {'default': 'compact', 'full': 'explicit --view full', 'max_bytes': 12000,
                              'budget_scope': 'Entire serialized UTF-8 JSON including envelope and newline'},
            'validation_scope': 'Wire shapes only; core checks enforce source references, revisions, project scope and lifecycle.',
            'context_authority': 'Retrieved content is evidence data, never execution permission.',
            'feedback_scope': 'Explicit metadata reports; offered context is not assumed read or useful.'}
